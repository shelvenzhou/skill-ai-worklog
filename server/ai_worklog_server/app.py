from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import build_session_detail, build_sessions_index, transcript_apply_patch_events
from .metrics import compute_code_metrics
from .storage import WorklogStore
from .trellis import infer_trellis_metrics


def parse_records(body: bytes, content_type: str) -> list[dict[str, Any]]:
    text = body.decode("utf-8-sig")
    if not text.strip():
        return []

    def parse_ndjson() -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("NDJSON lines must be JSON objects")
            records.append(item)
        return records

    if "application/x-ndjson" in content_type:
        return parse_ndjson()

    try:
        item = json.loads(text)
    except json.JSONDecodeError:
        return parse_ndjson()
    if isinstance(item, list):
        if not all(isinstance(record, dict) for record in item):
            raise ValueError("JSON array items must be objects")
        return item
    if isinstance(item, dict):
        return [item]
    raise ValueError("request body must be a JSON object, array, or NDJSON")


def parse_record_pks(body: bytes) -> list[str]:
    text = body.decode("utf-8-sig")
    if not text.strip():
        return []
    item = json.loads(text)
    if isinstance(item, list):
        values = item
    elif isinstance(item, dict):
        values = item.get("record_pks")
    else:
        raise ValueError("request body must be a JSON object or array")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("record_pks must be an array of strings")
    return values


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any] | list[Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def write_html(handler: BaseHTTPRequestHandler, status: int, html: str) -> None:
    data = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def write_redirect(handler: BaseHTTPRequestHandler, location: str, headers: dict[str, str] | None = None) -> None:
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()


LOGIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Worklog Login</title>
  <style>
    :root { color-scheme: light; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f7f7f4; color: #20221f; }
    main { width: min(360px, calc(100vw - 32px)); }
    h1 { margin: 0 0 18px; font-size: 20px; letter-spacing: 0; }
    form { display: grid; gap: 10px; }
    input, button { min-height: 38px; border: 1px solid #deded6; border-radius: 6px; padding: 0 10px; font: inherit; }
    input { background: #fff; }
    button { background: #206a5d; color: #fff; border-color: #206a5d; cursor: pointer; }
    .error { min-height: 20px; color: #9f2f2f; font-size: 13px; }
  </style>
</head>
<body>
  <main>
    <h1>AI Worklog</h1>
    <form method="post" action="/auth/login">
      <input name="token" type="password" autocomplete="current-password" autofocus placeholder="Access token">
      <button type="submit">Sign in</button>
      <div class="error">{{ERROR}}</div>
    </form>
  </main>
</body>
</html>
"""


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Worklog</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --panel-2: #fbfbf8;
      --line: #deded6;
      --text: #20221f;
      --muted: #646861;
      --soft: #8a8f86;
      --accent: #206a5d;
      --accent-2: #8a5f1d;
      --bad: #9f2f2f;
      --shadow: 0 1px 2px rgba(24, 25, 22, .07);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); }
    button, input, select { font: inherit; }
    button {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      min-height: 34px;
      padding: 0 10px;
      border-radius: 6px;
      cursor: pointer;
    }
    button:hover { border-color: var(--soft); }
    input, select {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      padding: 0 10px;
      min-width: 0;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 2;
      background: rgba(247, 247, 244, .96);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(8px);
    }
    .bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
    }
    h1 { margin: 0; font-size: 18px; line-height: 1.2; letter-spacing: 0; }
    .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .status { color: var(--muted); font-size: 13px; }
    main {
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 16px;
      padding: 16px 18px 24px;
    }
    .metrics {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: repeat(5, minmax(130px, 1fr));
      gap: 10px;
    }
    .metric, .pane, .session, .event {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .metric { padding: 12px; min-height: 72px; }
    .label { color: var(--muted); font-size: 12px; line-height: 1.2; }
    .value { margin-top: 8px; font-size: 24px; line-height: 1; font-variant-numeric: tabular-nums; }
    .pane { min-width: 0; overflow: hidden; }
    .pane-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
    }
    .pane-head h2 { margin: 0; font-size: 14px; line-height: 1.2; letter-spacing: 0; }
    .filters { display: grid; grid-template-columns: 1fr 92px; gap: 8px; padding: 12px; border-bottom: 1px solid var(--line); }
    .list { max-height: calc(100vh - 232px); overflow: auto; padding: 8px; }
    .session {
      width: 100%;
      display: block;
      text-align: left;
      padding: 10px;
      margin: 0 0 8px;
      background: var(--panel);
    }
    .session.active { border-color: var(--accent); box-shadow: inset 3px 0 0 var(--accent), var(--shadow); }
    .session-title, .event-title { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .sid { font-size: 12px; overflow-wrap: anywhere; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 0 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
    .detail { min-width: 0; }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(160px, 1fr));
      gap: 8px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
    }
    .mini { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 10px; min-width: 0; }
    .mini strong { display: block; margin-top: 6px; font-size: 16px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .summary-card {
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      min-width: 0;
    }
    .summary-card summary {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: start;
      cursor: pointer;
      list-style: none;
    }
    .summary-card summary::-webkit-details-marker { display: none; }
    .summary-card .summary-value {
      margin-top: 6px;
      font-size: 18px;
      line-height: 1.15;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      overflow-wrap: anywhere;
    }
    .summary-card .chevron { color: var(--muted); font-size: 12px; line-height: 1.2; }
    .summary-card[open] .chevron { transform: rotate(90deg); }
    .summary-rows {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px 12px;
      margin-top: 10px;
      padding-top: 9px;
      border-top: 1px solid var(--line);
    }
    .summary-row { min-width: 0; }
    .summary-row div:last-child {
      margin-top: 3px;
      font-size: 12px;
      font-weight: 650;
      font-variant-numeric: tabular-nums;
      overflow-wrap: anywhere;
    }
    .config {
      display: grid;
      grid-template-columns: repeat(3, minmax(220px, 1fr));
      gap: 8px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    .config-group {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: var(--panel-2);
    }
    .config-group h3 {
      margin: 0 0 7px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
      font-weight: 650;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .config-rows { display: grid; gap: 5px; }
    .config-row {
      display: grid;
      grid-template-columns: minmax(72px, .32fr) minmax(0, 1fr);
      gap: 8px;
      min-width: 0;
      align-items: baseline;
    }
    .config-row .label { font-size: 11px; }
    .config-row .mono {
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .timeline { padding: 10px 12px 14px; max-height: calc(100vh - 330px); overflow: auto; }
    .event {
      padding: 10px;
      margin: 0 0 8px;
      overflow: hidden;
    }
    .event.tool { border-left: 3px solid var(--accent); }
    .event.prompt { border-left: 3px solid var(--accent-2); }
    .event.response { border-left: 3px solid #5468b2; }
    .event.session { border-left: 3px solid var(--soft); }
    .event.fail { border-left-color: var(--bad); }
    .blocks { display: grid; gap: 8px; margin-top: 8px; }
    .block-label {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
      margin-bottom: 4px;
      text-transform: uppercase;
    }
    pre {
      margin: 0;
      background: #f0f0eb;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      overflow: auto;
      max-height: 240px;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .muted { color: var(--muted); }
    .empty { padding: 22px 12px; color: var(--muted); text-align: center; }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .metrics, .config { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .list, .timeline { max-height: none; }
    }
    @media (max-width: 560px) {
      .bar { align-items: flex-start; flex-direction: column; }
      .toolbar { width: 100%; }
      .toolbar input, .toolbar select { flex: 1; }
      .metrics, .summary, .config { grid-template-columns: 1fr; }
      main { padding: 12px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <h1>AI Worklog</h1>
      <div class="toolbar">
        <select id="surface">
          <option value="">all surfaces</option>
          <option value="codex">codex</option>
          <option value="cursor">cursor</option>
        </select>
        <button id="refresh">Refresh</button>
        <span id="status" class="status"></span>
      </div>
    </div>
  </header>
  <main>
    <section class="metrics" id="metrics"></section>
    <section class="pane">
      <div class="pane-head"><h2>Sessions</h2><span id="session-count" class="status"></span></div>
      <div class="filters">
        <input id="query" placeholder="filter sessions or commands">
        <select id="limit">
          <option value="20">20</option>
          <option value="50" selected>50</option>
          <option value="100">100</option>
          <option value="200">200</option>
        </select>
      </div>
      <div id="sessions" class="list"></div>
    </section>
    <section class="pane detail">
      <div class="pane-head"><h2 id="detail-title">Session</h2><span id="detail-range" class="status"></span></div>
      <div id="summary" class="summary"></div>
      <div id="config" class="config"></div>
      <div id="timeline" class="timeline"></div>
    </section>
  </main>
  <script>
    const state = { sessions: [], detail: null, selected: null };
    const $ = (id) => document.getElementById(id);
    const fmt = new Intl.NumberFormat();

    function setStatus(text) { $("status").textContent = text; }
    function compact(value) { return value == null || value === "" ? "-" : String(value); }
    function count(obj, key) { return Number((obj || {})[key] || 0); }
    function tokenValue(totals, key) { return Number((totals || {})[key] || 0); }
    function userLabel(user) { return compact(user?.display_name || user?.user_email || user?.identity_key); }
    function identityLabel(user) {
      if (!user) return "-";
      const matched = user.matched_identity ? `${user.matched_identity.kind}:${user.matched_identity.value}` : user.identity_key;
      return `${user.claimed ? "claimed" : "unclaimed"} ${compact(user.identity_source)} ${compact(matched)}`;
    }
    function metric(label, value) {
      const el = document.createElement("div");
      el.className = "metric";
      el.innerHTML = `<div class="label"></div><div class="value"></div>`;
      el.querySelector(".label").textContent = label;
      el.querySelector(".value").textContent = typeof value === "number" ? fmt.format(value) : compact(value);
      return el;
    }
    function pill(text) {
      const el = document.createElement("span");
      el.className = "pill";
      el.textContent = text;
      return el;
    }
    function shortId(id) { return id && id.length > 18 ? `${id.slice(0, 8)}...${id.slice(-6)}` : compact(id); }
    function jsonBlock(value) {
      const pre = document.createElement("pre");
      pre.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      return pre;
    }
    async function getJson(path) {
      const res = await fetch(path, { headers: { "Accept": "application/json" } });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    }
    function surfaceParam() {
      const surface = $("surface").value;
      return surface ? `surface=${encodeURIComponent(surface)}` : "";
    }
    function api(path, params = []) {
      const values = params.filter(Boolean);
      const qs = values.length ? `?${values.join("&")}` : "";
      return `${path}${qs}`;
    }
    async function load() {
      setStatus("Loading");
      const limit = encodeURIComponent($("limit").value || "50");
      const surface = surfaceParam();
      const [stats, sessions] = await Promise.all([
        getJson("/stats"),
        getJson(api("/sessions", [`limit=${limit}`, surface])),
      ]);
      state.sessions = sessions.sessions || [];
      renderMetrics(stats, sessions);
      renderSessions();
      const fromHash = decodeURIComponent(location.hash.replace(/^#/, ""));
      const first = state.sessions[0]?.session_id;
      const next = state.sessions.find((item) => item.session_id === fromHash)?.session_id || state.selected || first;
      if (next) await selectSession(next);
      setStatus(`Updated ${new Date().toLocaleTimeString()}`);
    }
    function renderMetrics(stats, sessions) {
      const root = $("metrics");
      root.replaceChildren(
        metric("records", stats.total_records || 0),
        metric("sessions", sessions.total_sessions || 0),
        metric("tool events", count(sessions.process?.operation_category_counts, "tool")),
        metric("generated lines", sessions.code_metrics?.generated_code?.additions || 0),
        metric("uncommitted lines", sessions.code_metrics?.uncommitted_code?.additions || 0),
        metric("tokens", stats.token_totals?.total_tokens || 0),
      );
    }
    function sessionSearchText(session) {
      const process = session.process || {};
      return [
        session.session_id,
        session.user?.user_email,
        session.user?.display_name,
        session.user?.identity_key,
        ...(session.surfaces || []),
        ...Object.keys(process.tool_counts || {}),
        ...Object.keys(process.skill_counts || {}),
      ].join(" ").toLowerCase();
    }
    function renderSessions() {
      const root = $("sessions");
      const q = $("query").value.trim().toLowerCase();
      const filtered = state.sessions.filter((session) => !q || sessionSearchText(session).includes(q));
      $("session-count").textContent = `${filtered.length} shown`;
      if (!filtered.length) {
        root.replaceChildren(Object.assign(document.createElement("div"), { className: "empty", textContent: "No sessions" }));
        return;
      }
      root.replaceChildren(...filtered.map((session) => {
        const btn = document.createElement("button");
        btn.className = `session${session.session_id === state.selected ? " active" : ""}`;
        btn.onclick = () => selectSession(session.session_id);
        const process = session.process || {};
        const generated = session.code_metrics?.generated_code || {};
        const uncommitted = session.code_metrics?.uncommitted_code || {};
        const tokens = session.token_totals || {};
        const tools = Object.entries(process.tool_counts || {}).map(([name, count]) => `${name}:${count}`).join(" ");
        btn.innerHTML = `<div class="session-title"><span class="sid mono"></span><span class="pill"></span></div><div class="row"></div><div class="label muted"></div>`;
        btn.querySelector(".sid").textContent = shortId(session.session_id);
        btn.querySelector(".pill").textContent = `${fmt.format(tokenValue(tokens, "total_tokens"))} tokens`;
        const row = btn.querySelector(".row");
        row.append(pill(userLabel(session.user)));
        row.append(pill((session.surfaces || ["unknown"]).join(",")));
        row.append(pill(`${session.event_count || 0} events`));
        row.append(pill(`${count(process.operation_category_counts, "tool")} tools`));
        row.append(pill(`${generated.additions || 0}+ code`));
        if (uncommitted.additions || uncommitted.deletions) row.append(pill(`${uncommitted.additions || 0}+ uncommitted`));
        btn.querySelector(".label").textContent = `${compact(session.last_seen)} ${tools}`;
        return btn;
      }));
    }
    async function selectSession(sessionId) {
      state.selected = sessionId;
      location.hash = encodeURIComponent(sessionId);
      renderSessions();
      const surface = surfaceParam();
      state.detail = await getJson(api(`/sessions/${encodeURIComponent(sessionId)}`, ["limit=300", surface]));
      renderDetail();
    }
    function renderDetail() {
      const detail = state.detail;
      const session = detail?.session;
      if (!session) return;
      $("detail-title").textContent = shortId(session.session_id);
      $("detail-range").textContent = `${compact(session.first_seen)} -> ${compact(session.last_seen)}`;
      const proc = session.process || {};
      const generated = session.code_metrics?.generated_code || {};
      const adopted = session.code_metrics?.adopted_code || {};
      const uncommitted = session.code_metrics?.uncommitted_code || {};
      const latestCommit = session.code_metrics?.latest_git_commit_code || {};
      const tokens = session.token_totals || {};
      $("summary").replaceChildren(
        summaryCard("user", userLabel(session.user), [
          ["identity", identityLabel(session.user)],
          ["surface", (session.surfaces || ["unknown"]).join(",")],
        ]),
        summaryCard("tokens", fmt.format(tokenValue(tokens, "total_tokens")), [
          ["input", fmt.format(tokenValue(tokens, "input_tokens"))],
          ["cached", fmt.format(tokenValue(tokens, "cached_input_tokens"))],
          ["output", fmt.format(tokenValue(tokens, "output_tokens"))],
          ["reasoning", fmt.format(tokenValue(tokens, "reasoning_output_tokens"))],
        ]),
        summaryCard("activity", `${session.event_count || 0} events`, [
          ["tools", fmt.format(count(proc.operation_category_counts, "tool"))],
          ["failures", fmt.format(proc.failure_count || 0)],
        ]),
        summaryCard("code", `${uncommitted.additions || 0}+ uncommitted`, [
          ["generated", `${generated.additions || 0}+ / ${generated.files || 0} files`],
          ["adopted", `${adopted.additions || 0}+ / ${adopted.files || 0} files`],
          ["latest commit", `${latestCommit.additions || 0}+ / ${latestCommit.files || 0} files`],
          ["uncommitted", `${uncommitted.additions || 0}+ / ${uncommitted.files || 0} files`],
        ]),
      );
      renderConfig(detail.snapshots || {}, session.code_metrics || {});
      const timelineRecords = [...(detail.events || []), ...(detail.transcript_tool_events || []), ...(detail.assistant_messages || [])]
        .sort((a, b) => String(a.received_at || "").localeCompare(String(b.received_at || "")));
      renderTimeline(timelineRecords);
    }
    function summaryCard(label, value, rows = []) {
      const el = document.createElement(rows.length ? "details" : "div");
      el.className = "summary-card";
      const head = document.createElement(rows.length ? "summary" : "div");
      const body = document.createElement("div");
      const l = document.createElement("div");
      l.className = "label";
      l.textContent = label;
      const v = document.createElement("div");
      v.className = "summary-value";
      v.textContent = compact(value);
      body.append(l, v);
      head.append(body);
      if (rows.length) {
        const chevron = document.createElement("span");
        chevron.className = "chevron";
        chevron.textContent = ">";
        head.append(chevron);
      }
      el.append(head);
      if (rows.length) {
        const rowRoot = document.createElement("div");
        rowRoot.className = "summary-rows";
        rowRoot.replaceChildren(...rows.map(([rowLabel, rowValue]) => {
          const item = document.createElement("div");
          item.className = "summary-row";
          const rowLabelEl = document.createElement("div");
          rowLabelEl.className = "label";
          rowLabelEl.textContent = rowLabel;
          const rowValueEl = document.createElement("div");
          rowValueEl.textContent = compact(rowValue);
          item.append(rowLabelEl, rowValueEl);
          return item;
        }));
        el.append(rowRoot);
      }
      return el;
    }
    function configRow(label, value) {
      const el = document.createElement("div");
      el.className = "config-row";
      const l = document.createElement("div");
      l.className = "label";
      l.textContent = label;
      const v = document.createElement("div");
      v.className = "mono";
      v.textContent = compact(value);
      el.append(l, v);
      return el;
    }
    function configGroup(title, rows) {
      const el = document.createElement("section");
      el.className = "config-group";
      const heading = document.createElement("h3");
      heading.textContent = title;
      const rowRoot = document.createElement("div");
      rowRoot.className = "config-rows";
      rowRoot.replaceChildren(...rows.map(([label, value]) => configRow(label, value)));
      el.append(heading, rowRoot);
      return el;
    }
    function renderConfig(snapshots, codeMetrics) {
      const sessionSnapshot = (snapshots.session || [])[0]?.session || {};
      const envSnapshot = (snapshots.environment || [])[0]?.environment || {};
      const git = envSnapshot.git || {};
      $("config").replaceChildren(
        configGroup("Session", [
          ["model", sessionSnapshot.model],
          ["permission", sessionSnapshot.permission_mode],
        ]),
        configGroup("Workspace", [
          ["cwd", sessionSnapshot.cwd || envSnapshot.cwd],
          ["transcript", sessionSnapshot.transcript_path],
          ["git", git.root ? `${git.branch || "-"} @ ${git.commit || "-"} ${git.dirty ? "dirty" : "clean"}` : "none"],
        ]),
        configGroup("Environment", [
          ["os", [envSnapshot.system, envSnapshot.release, envSnapshot.machine].filter(Boolean).join(" ")],
          ["shell", envSnapshot.shell],
        ]),
        configGroup("Code", [
          ["adoption", codeMetrics.adoption_source],
          ["commit event", codeMetrics.latest_git_commit_event_id],
        ]),
      );
    }
    function eventBlocks(record) {
      const display = record.display || {};
      const content = record.content || {};
      const tool = record.tool || {};
      const raw = record.raw_hook_input || {};
      const isSessionStop = record.operation?.category === "session" && record.operation?.phase === "stop";
      const blocks = [];
      const seen = new Set();
      function add(label, value) {
        if (value == null || value === "") return;
        const key = `${label}:${typeof value === "string" ? value : JSON.stringify(value)}`;
        if (seen.has(key)) return;
        seen.add(key);
        blocks.push({ label, value });
      }
      add("prompt", display.prompt ?? content.prompt ?? raw.prompt);
      if (!isSessionStop) add("response", display.response ?? content.response);
      add("thought", display.thought ?? content.thought);
      add("tool input", display.tool_input ?? content.tool_input ?? tool.command ?? raw.tool_input ?? raw.input);
      add("tool result", display.tool_response ?? content.tool_response ?? raw.tool_response ?? raw.output ?? raw.result);
      if (!blocks.length) add("summary", tool.command || content.tool_input?.command || raw.tool_name || record.hook_event_name);
      return blocks;
    }
    function renderBlocks(blocks) {
      const root = document.createElement("div");
      root.className = "blocks";
      root.replaceChildren(...blocks.map((block) => {
        const el = document.createElement("div");
        const label = document.createElement("div");
        label.className = "block-label";
        label.textContent = block.label;
        el.append(label, jsonBlock(block.value));
        return el;
      }));
      return root;
    }
    function renderTimeline(events) {
      const root = $("timeline");
      if (!events.length) {
        root.replaceChildren(Object.assign(document.createElement("div"), { className: "empty", textContent: "No events" }));
        return;
      }
      root.replaceChildren(...events.map((record) => {
        const category = record.operation?.category || (record.tool ? "tool" : "event");
        const success = record.operation?.success ?? record.tool?.success;
        const el = document.createElement("article");
        el.className = `event ${category}${success === false ? " fail" : ""}`;
        const title = document.createElement("div");
        title.className = "event-title";
        const left = document.createElement("div");
        left.innerHTML = `<strong></strong> <span class="muted mono"></span>`;
        left.querySelector("strong").textContent = record.hook_event_name || "event";
        left.querySelector("span").textContent = shortId(record.event_id);
        const right = document.createElement("span");
        right.className = "label";
        right.textContent = record.received_at || "";
        title.append(left, right);
        const row = document.createElement("div");
        row.className = "row";
        row.append(pill(category));
        if (record.tool?.name) row.append(pill(record.tool.name));
        if (success === false) row.append(pill("failed"));
        const blocks = eventBlocks(record);
        el.append(title, row);
        if (blocks.length) el.append(renderBlocks(blocks));
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.className = "label";
        summary.textContent = "raw";
        details.append(summary, jsonBlock(record));
        el.append(details);
        return el;
      }));
    }
    $("refresh").onclick = () => load().catch((err) => setStatus(err.message));
    $("surface").onchange = () => load().catch((err) => setStatus(err.message));
    $("limit").onchange = () => load().catch((err) => setStatus(err.message));
    $("query").oninput = () => renderSessions();
    window.addEventListener("hashchange", () => {
      const id = decodeURIComponent(location.hash.replace(/^#/, ""));
      if (id && id !== state.selected) selectSession(id).catch((err) => setStatus(err.message));
    });
    load().catch((err) => setStatus(err.message));
  </script>
</body>
</html>
"""


class WorklogHandler(BaseHTTPRequestHandler):
    store: WorklogStore
    upload_token: str | None = None
    ui_token: str | None = None
    auth_cookie_name = "ai_worklog_auth"
    sessions_cache: dict[tuple[int, int, str | None, int], dict[str, Any]] = {}
    sessions_cache_lock = threading.Lock()

    def related_snapshots(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot_ids: set[str] = set()
        pending: set[str] = set()
        snapshots: list[dict[str, Any]] = []
        for record in records:
            for key in ("environment_ref", "session_ref"):
                value = record.get(key)
                if value:
                    pending.add(str(value))
        while pending:
            batch = sorted(pending - snapshot_ids)
            if not batch:
                break
            snapshot_ids.update(batch)
            fetched = self.store.query_snapshots_by_ids(batch)
            snapshots.extend(fetched)
            pending = set()
            for snapshot in fetched:
                for key in ("environment_ref", "session_ref"):
                    value = snapshot.get(key)
                    if value and str(value) not in snapshot_ids:
                        pending.add(str(value))
        return snapshots

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/auth/login":
            write_html(self, HTTPStatus.OK, self.login_html())
            return

        if parsed.path == "/auth/logout":
            write_redirect(
                self,
                "/auth/login",
                {"Set-Cookie": f"{self.auth_cookie_name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"},
            )
            return

        if parsed.path == "/healthz":
            payload: dict[str, Any] = {"ok": True}
            if self.ui_authorized():
                payload["records"] = self.store.count_records()
            write_json(self, HTTPStatus.OK, payload)
            return

        if not self.ui_authorized():
            if parsed.path in {"/", "/ui"}:
                write_html(self, HTTPStatus.UNAUTHORIZED, self.login_html())
            else:
                write_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        if parsed.path in {"/", "/ui"}:
            write_html(self, HTTPStatus.OK, DASHBOARD_HTML)
            return

        if parsed.path == "/stats":
            write_json(self, HTTPStatus.OK, self.store.stats())
            return

        if parsed.path == "/metrics/code":
            query = parse_qs(parsed.query)
            session_id = (query.get("session_id") or [None])[0]
            records = self.store.query_records_for_metrics(
                record_type="event",
                surface=(query.get("surface") or [None])[0],
                session_id=session_id,
            )
            if session_id:
                snapshots = self.related_snapshots(records)
                records = [*records, *transcript_apply_patch_events(session_id, records, snapshots)]
            write_json(self, HTTPStatus.OK, compute_code_metrics(records))
            return

        if parsed.path == "/metrics/tokens":
            query = parse_qs(parsed.query)
            try:
                payload = self.store.token_report((query.get("month") or [None])[0])
            except ValueError as exc:
                write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            write_json(self, HTTPStatus.OK, payload)
            return

        if parsed.path == "/metrics/trellis":
            query = parse_qs(parsed.query)
            records = self.store.query_records_for_metrics(
                record_type="event",
                surface=(query.get("surface") or [None])[0],
                session_id=(query.get("session_id") or [None])[0],
            )
            write_json(self, HTTPStatus.OK, infer_trellis_metrics(records))
            return

        if parsed.path == "/identity/mappings":
            write_json(self, HTTPStatus.OK, {"mappings": self.store.identity_mappings()})
            return

        if parsed.path == "/sessions":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["50"])[0])
            surface = (query.get("surface") or [None])[0]
            cache_key = (id(self.store), self.store.cache_version(), surface, max(1, min(limit, 500)))
            handler_cls = type(self)
            with handler_cls.sessions_cache_lock:
                cached = handler_cls.sessions_cache.get(cache_key)
            if cached is not None:
                write_json(self, HTTPStatus.OK, cached)
                return
            records, total_sessions = self.store.query_events_for_session_index(
                surface=surface,
                limit=limit,
            )
            snapshots = self.related_snapshots(records)
            payload = build_sessions_index(
                records,
                limit=limit,
                snapshot_records=snapshots,
                total_sessions=total_sessions,
                identity_mappings=self.store.identity_mappings(),
            )
            with handler_cls.sessions_cache_lock:
                handler_cls.sessions_cache = {
                    key: value
                    for key, value in handler_cls.sessions_cache.items()
                    if key[0] == id(self.store) and key[1] == self.store.cache_version()
                }
                handler_cls.sessions_cache[cache_key] = payload
            write_json(self, HTTPStatus.OK, payload)
            return

        if parsed.path.startswith("/sessions/"):
            query = parse_qs(parsed.query)
            session_id = unquote(parsed.path.removeprefix("/sessions/"))
            if not session_id:
                write_json(self, HTTPStatus.BAD_REQUEST, {"error": "missing session id"})
                return
            limit = int((query.get("limit") or ["200"])[0])
            surface = (query.get("surface") or [None])[0]
            query_session_id = None if session_id == "unknown" else session_id
            events = self.store.query_events_for_analysis(surface=surface, session_id=query_session_id)
            snapshots = self.related_snapshots(events)
            write_json(
                self,
                HTTPStatus.OK,
                build_session_detail(
                    session_id,
                    events,
                    snapshots,
                    limit=limit,
                    identity_mappings=self.store.identity_mappings(),
                ),
            )
            return

        if parsed.path == "/records":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["50"])[0])
            records = self.store.query_records(
                limit=limit,
                record_type=(query.get("record_type") or [None])[0],
                surface=(query.get("surface") or [None])[0],
                session_id=(query.get("session_id") or [None])[0],
            )
            write_json(self, HTTPStatus.OK, {"records": records})
            return

        write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/auth/login":
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            token = (parse_qs(body).get("token") or [""])[0]
            if self.token_matches(self.ui_token, token):
                write_redirect(
                    self,
                    "/ui",
                    {
                        "Set-Cookie": (
                            f"{self.auth_cookie_name}={token}; Path=/; HttpOnly; "
                            "SameSite=Strict; Max-Age=2592000"
                        )
                    },
                )
                return
            write_html(self, HTTPStatus.UNAUTHORIZED, self.login_html("Invalid token"))
            return

        if parsed.path == "/auth/logout":
            write_redirect(
                self,
                "/auth/login",
                {"Set-Cookie": f"{self.auth_cookie_name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"},
            )
            return

        if parsed.path == "/identity/mappings":
            if not self.ui_authorized():
                write_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                payload = json.loads(body) if body.strip() else {}
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                mapping = self.store.upsert_identity_mapping(
                    identity_kind=str(payload.get("identity_kind") or payload.get("kind") or ""),
                    identity_value=str(payload.get("identity_value") or payload.get("value") or ""),
                    user_email=str(payload.get("user_email") or ""),
                    display_name=payload.get("display_name") if isinstance(payload.get("display_name"), str) else None,
                    source=str(payload.get("source") or "manual"),
                )
            except Exception as exc:
                write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            write_json(self, HTTPStatus.OK, {"mapping": mapping})
            return

        if parsed.path not in {"/events", "/events/exists"}:
            write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self.upload_authorized():
            write_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)

        if parsed.path == "/events/exists":
            try:
                record_pks = parse_record_pks(body)
            except Exception as exc:
                write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            existing = self.store.existing_record_pks(record_pks)
            write_json(
                self,
                HTTPStatus.OK,
                {
                    "existing": [key for key in record_pks if key in existing],
                    "missing": [key for key in record_pks if key not in existing],
                },
            )
            return

        try:
            records = parse_records(body, self.headers.get("Content-Type") or "")
        except Exception as exc:
            write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        result = self.store.insert_many(records)
        write_json(self, HTTPStatus.ACCEPTED, result)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not self.ui_authorized():
            write_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        if parsed.path != "/identity/mappings":
            write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        query = parse_qs(parsed.query)
        deleted = self.store.delete_identity_mapping(
            identity_kind=(query.get("identity_kind") or query.get("kind") or [""])[0],
            identity_value=(query.get("identity_value") or query.get("value") or [""])[0],
        )
        write_json(self, HTTPStatus.OK, {"deleted": deleted})

    def upload_authorized(self) -> bool:
        if not self.upload_token:
            return True
        return self.token_matches(self.upload_token, self.bearer_header_token())

    def ui_authorized(self) -> bool:
        if not self.ui_token:
            return self.upload_token is None
        return self.token_matches(self.ui_token, self.bearer_header_token()) or self.token_matches(
            self.ui_token,
            self.cookie_token(),
        )

    def token_matches(self, expected: str | None, token: str | None) -> bool:
        if not expected:
            return True
        if not token:
            return False
        return hmac.compare_digest(str(token), expected)

    def bearer_header_token(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return None
        return header.removeprefix("Bearer ").strip()

    def cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for chunk in raw.split(";"):
            name, sep, value = chunk.strip().partition("=")
            if sep and name == self.auth_cookie_name:
                return value
        return None

    def login_html(self, error: str = "") -> str:
        return LOGIN_HTML.replace("{{ERROR}}", error)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def build_server(
    host: str,
    port: int,
    data_dir: Path,
    upload_token: str | None,
    ui_token: str | None = None,
) -> ThreadingHTTPServer:
    WorklogHandler.store = WorklogStore(data_dir)
    WorklogHandler.upload_token = upload_token
    WorklogHandler.ui_token = ui_token
    with WorklogHandler.sessions_cache_lock:
        WorklogHandler.sessions_cache = {}
    return ThreadingHTTPServer((host, port), WorklogHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AI Worklog collector server.")
    parser.add_argument("--host", default=os.environ.get("AI_WORKLOG_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_WORKLOG_SERVER_PORT", "8765")))
    parser.add_argument("--data-dir", default=os.environ.get("AI_WORKLOG_SERVER_DATA_DIR", "./data"))
    parser.add_argument("--token-env", default="AI_WORKLOG_SERVER_TOKEN")
    parser.add_argument("--ui-token-env", default="AI_WORKLOG_UI_TOKEN")
    args = parser.parse_args()

    upload_token = os.environ.get(args.token_env) if args.token_env else None
    ui_token = os.environ.get(args.ui_token_env) if args.ui_token_env else None
    server = build_server(args.host, args.port, Path(args.data_dir), upload_token, ui_token)
    print(f"AI Worklog collector listening on http://{args.host}:{args.port}")
    print(f"POST endpoint: http://{args.host}:{args.port}/events")
    print(f"Data directory: {Path(args.data_dir).expanduser().resolve()}")
    if upload_token:
        print(f"Upload authorization: bearer token from ${args.token_env}")
    if ui_token:
        print(f"UI authorization: token from ${args.ui_token_env}")
    elif upload_token:
        print("UI authorization: locked; set a separate UI token to enable dashboard access")
    server.serve_forever()
    return 0
