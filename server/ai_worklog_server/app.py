from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import build_session_detail, build_sessions_index, transcript_apply_patch_events
from .metrics import compute_code_metrics
from .storage import WorklogStore


def parse_records(body: bytes, content_type: str) -> list[dict[str, Any]]:
    text = body.decode("utf-8")
    if not text.strip():
        return []
    if "application/x-ndjson" in content_type or "\n" in text.strip():
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("NDJSON lines must be JSON objects")
            records.append(item)
        return records

    item = json.loads(text)
    if isinstance(item, list):
        if not all(isinstance(record, dict) for record in item):
            raise ValueError("JSON array items must be objects")
        return item
    if isinstance(item, dict):
        return [item]
    raise ValueError("request body must be a JSON object, array, or NDJSON")


def parse_record_pks(body: bytes) -> list[str]:
    text = body.decode("utf-8")
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
      grid-template-columns: repeat(5, minmax(110px, 1fr));
      gap: 8px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
    }
    .mini { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 10px; min-width: 0; }
    .mini strong { display: block; margin-top: 6px; font-size: 16px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .config {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    .config-item {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      background: var(--panel-2);
    }
    .config-item div:last-child {
      margin-top: 5px;
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
    pre {
      margin: 8px 0 0;
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
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
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
        const tools = Object.entries(process.tool_counts || {}).map(([name, count]) => `${name}:${count}`).join(" ");
        btn.innerHTML = `<div class="session-title"><span class="sid mono"></span><span class="pill"></span></div><div class="row"></div><div class="label muted"></div>`;
        btn.querySelector(".sid").textContent = shortId(session.session_id);
        btn.querySelector(".pill").textContent = `${session.event_count || 0} events`;
        const row = btn.querySelector(".row");
        row.append(pill((session.surfaces || ["unknown"]).join(",")));
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
      $("summary").replaceChildren(
        mini("events", session.event_count || 0),
        mini("tools", count(proc.operation_category_counts, "tool")),
        mini("generated", `${generated.additions || 0}+ / ${generated.files || 0} files`),
        mini("adopted", `${adopted.additions || 0}+ / ${adopted.files || 0} files`),
        mini("latest commit", `${latestCommit.additions || 0}+ / ${latestCommit.files || 0} files`),
        mini("uncommitted", `${uncommitted.additions || 0}+ / ${uncommitted.files || 0} files`),
      );
      renderConfig(detail.snapshots || {}, session.code_metrics || {});
      const timelineRecords = [...(detail.events || []), ...(detail.transcript_tool_events || []), ...(detail.assistant_messages || [])]
        .sort((a, b) => String(a.received_at || "").localeCompare(String(b.received_at || "")));
      renderTimeline(timelineRecords);
    }
    function mini(label, value) {
      const el = document.createElement("div");
      el.className = "mini";
      const l = document.createElement("div");
      l.className = "label";
      l.textContent = label;
      const v = document.createElement("strong");
      v.textContent = compact(value);
      el.append(l, v);
      return el;
    }
    function configItem(label, value) {
      const el = document.createElement("div");
      el.className = "config-item";
      const l = document.createElement("div");
      l.className = "label";
      l.textContent = label;
      const v = document.createElement("div");
      v.className = "mono";
      v.textContent = compact(value);
      el.append(l, v);
      return el;
    }
    function renderConfig(snapshots, codeMetrics) {
      const sessionSnapshot = (snapshots.session || [])[0]?.session || {};
      const envSnapshot = (snapshots.environment || [])[0]?.environment || {};
      const git = envSnapshot.git || {};
      $("config").replaceChildren(
        configItem("model", sessionSnapshot.model),
        configItem("permission", sessionSnapshot.permission_mode),
        configItem("cwd", sessionSnapshot.cwd || envSnapshot.cwd),
        configItem("surface", sessionSnapshot.surface),
        configItem("transcript", sessionSnapshot.transcript_path),
        configItem("os", [envSnapshot.system, envSnapshot.release, envSnapshot.machine].filter(Boolean).join(" ")),
        configItem("shell", envSnapshot.shell),
        configItem("git", git.root ? `${git.branch || "-"} @ ${git.commit || "-"} ${git.dirty ? "dirty" : "clean"}` : "none"),
        configItem("adoption source", codeMetrics.adoption_source),
        configItem("latest commit event", codeMetrics.latest_git_commit_event_id),
        configItem("latest commit code", `${codeMetrics.latest_git_commit_code?.additions || 0}+ / -${codeMetrics.latest_git_commit_code?.deletions || 0} / ${codeMetrics.latest_git_commit_code?.files || 0} files`),
        configItem("uncommitted", `${codeMetrics.uncommitted_code?.additions || 0}+ / ${codeMetrics.uncommitted_code?.files || 0} files`),
      );
    }
    function eventText(record) {
      const content = record.content || {};
      const tool = record.tool || {};
      const raw = record.raw_hook_input || {};
      return tool.command || content.prompt || content.response || content.tool_input?.command || raw.tool_name || record.hook_event_name || "";
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
        const primary = eventText(record);
        el.append(title, row);
        if (primary) el.append(jsonBlock(primary));
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
    bearer_token: str | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/ui"}:
            write_html(self, HTTPStatus.OK, DASHBOARD_HTML)
            return

        if parsed.path == "/healthz":
            write_json(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "records": self.store.count_records(),
                },
            )
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
                snapshot_ids: list[str] = []
                for event in records:
                    for key in ("environment_ref", "session_ref"):
                        value = event.get(key)
                        if value:
                            snapshot_ids.append(str(value))
                snapshots = self.store.query_snapshots_by_ids(snapshot_ids)
                records = [*records, *transcript_apply_patch_events(session_id, records, snapshots)]
            write_json(self, HTTPStatus.OK, compute_code_metrics(records))
            return

        if parsed.path == "/sessions":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["50"])[0])
            records = self.store.query_events_for_analysis(surface=(query.get("surface") or [None])[0])
            snapshot_ids: list[str] = []
            for event in records:
                for key in ("environment_ref", "session_ref"):
                    value = event.get(key)
                    if value:
                        snapshot_ids.append(str(value))
            snapshots = self.store.query_snapshots_by_ids(snapshot_ids)
            write_json(self, HTTPStatus.OK, build_sessions_index(records, limit=limit, snapshot_records=snapshots))
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
            snapshot_ids: list[str] = []
            for event in events:
                for key in ("environment_ref", "session_ref"):
                    value = event.get(key)
                    if value:
                        snapshot_ids.append(str(value))
            snapshots = self.store.query_snapshots_by_ids(snapshot_ids)
            write_json(self, HTTPStatus.OK, build_session_detail(session_id, events, snapshots, limit=limit))
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
        if parsed.path not in {"/events", "/events/exists"}:
            write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self.authorized():
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

    def authorized(self) -> bool:
        if not self.bearer_token:
            return True
        expected = f"Bearer {self.bearer_token}"
        return self.headers.get("Authorization") == expected

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def build_server(host: str, port: int, data_dir: Path, token: str | None) -> ThreadingHTTPServer:
    WorklogHandler.store = WorklogStore(data_dir)
    WorklogHandler.bearer_token = token
    return ThreadingHTTPServer((host, port), WorklogHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AI Worklog collector server.")
    parser.add_argument("--host", default=os.environ.get("AI_WORKLOG_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_WORKLOG_SERVER_PORT", "8765")))
    parser.add_argument("--data-dir", default=os.environ.get("AI_WORKLOG_SERVER_DATA_DIR", "./data"))
    parser.add_argument("--token-env", default="AI_WORKLOG_SERVER_TOKEN")
    args = parser.parse_args()

    token = os.environ.get(args.token_env) if args.token_env else None
    server = build_server(args.host, args.port, Path(args.data_dir), token)
    print(f"AI Worklog collector listening on http://{args.host}:{args.port}")
    print(f"POST endpoint: http://{args.host}:{args.port}/events")
    print(f"Data directory: {Path(args.data_dir).expanduser().resolve()}")
    if token:
        print(f"Authorization: bearer token from ${args.token_env}")
    server.serve_forever()
    return 0
