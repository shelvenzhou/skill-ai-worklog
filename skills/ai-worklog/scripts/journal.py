#!/usr/bin/env python3
"""Hook event journal for Codex and Cursor."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


VERSION = "0.2.0"
DEFAULT_HOME = Path.home() / ".ai-worklog"
DEFAULT_CONFIG_PATH = DEFAULT_HOME / "config.json"
DEFAULT_MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
ENVELOPE_KEYS = {
    "agent_id",
    "agent_type",
    "agentId",
    "agentType",
    "conversation_id",
    "conversationId",
    "cwd",
    "event",
    "event_name",
    "hook_event_name",
    "hookName",
    "model",
    "model_name",
    "modelName",
    "permission_mode",
    "permissionMode",
    "session_id",
    "sessionId",
    "transcript_path",
    "transcriptPath",
    "turn_id",
    "turnId",
    "user_email",
    "userEmail",
    "workspace_path",
    "workspacePath",
}
CODE_EXTENSIONS = {
    ".c",
    ".cc",
    ".clj",
    ".cljs",
    ".cpp",
    ".cs",
    ".css",
    ".dart",
    ".ex",
    ".exs",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".m",
    ".mm",
    ".php",
    ".pl",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
CODE_FILENAMES = {
    "Dockerfile",
    "Makefile",
    "Rakefile",
    "Gemfile",
    "go.mod",
    "go.sum",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "tsconfig.json",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def default_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "collection_level": "full",
        "local_log_dir": str(DEFAULT_HOME / "events"),
        "snapshot_log_dir": str(DEFAULT_HOME / "snapshots"),
        "failed_log_dir": str(DEFAULT_HOME / "failed"),
        "server_url": None,
        "api_key_env": "AI_WORKLOG_API_KEY",
        "request_timeout_seconds": 2.0,
        "max_transcript_bytes": DEFAULT_MAX_TRANSCRIPT_BYTES,
        "capture": {
            "raw_hook_input": True,
            "prompt": True,
            "response": True,
            "tool_payloads": True,
            "environment": True,
            "token_usage_from_transcript": True,
            "reasoning_summary": True,
            "raw_reasoning": False,
        },
    }


def merged_config(config_path: Path) -> dict[str, Any]:
    cfg = default_config()
    file_cfg = load_json(config_path)
    for key, value in file_cfg.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    server_url = os.environ.get("AI_WORKLOG_SERVER_URL") or os.environ.get("AI_USAGE_COLLECTOR_SERVER_URL")
    if server_url:
        cfg["server_url"] = server_url
    level = os.environ.get("AI_WORKLOG_LEVEL") or os.environ.get("AI_USAGE_COLLECTOR_LEVEL")
    if level:
        cfg["collection_level"] = level
    return cfg


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_unparsed_stdin": raw}
    return value if isinstance(value, dict) else {"_stdin": value}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def stable_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def summarize_value(value: Any) -> Any:
    if isinstance(value, str):
        return {
            "type": "string",
            "length": len(value),
            "sha256": sha256_text(value),
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "length": len(value),
            "sha256": stable_hash(value),
        }
    if isinstance(value, dict):
        return {
            "type": "object",
            "keys": sorted(str(k) for k in value.keys()),
            "sha256": stable_hash(value),
        }
    return value


def scrub_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if is_sensitive_key(str(k)) else scrub_sensitive(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_sensitive(item) for item in value]
    return value


def value_at(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def first_nested(payload: dict[str, Any], paths: list[list[str]]) -> Any:
    for path in paths:
        cur: Any = payload
        for segment in path:
            if not isinstance(cur, dict) or segment not in cur:
                cur = None
                break
            cur = cur[segment]
        if cur not in (None, ""):
            return cur
    return None


def git_metadata(cwd: str | None) -> dict[str, Any] | None:
    if not cwd:
        return None
    path = Path(cwd).expanduser()
    if not path.exists():
        return None

    def run_git(args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(path),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.5,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    root = run_git(["rev-parse", "--show-toplevel"])
    if not root:
        return None
    status = run_git(["status", "--porcelain"]) or ""
    return {
        "root": root,
        "branch": run_git(["branch", "--show-current"]),
        "commit": run_git(["rev-parse", "HEAD"]),
        "dirty": bool(status),
    }


def is_code_path(path: str | None) -> bool:
    if not path:
        return False
    name = Path(path).name
    if name in CODE_FILENAMES:
        return True
    return Path(path).suffix.lower() in CODE_EXTENSIONS


def count_text_lines(path: Path, max_bytes: int = 2 * 1024 * 1024) -> int | None:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except Exception:
        return None
    if b"\0" in data:
        return None
    text = data.decode("utf-8", errors="replace")
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def git_workspace_diff(cwd: str | None) -> dict[str, Any] | None:
    if not cwd:
        return None
    path = Path(cwd).expanduser()
    if not path.exists():
        return None

    def run_git(args: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(path),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3.0,
            )
        except Exception:
            return None
        return result if result.returncode == 0 else None

    root_result = run_git(["rev-parse", "--show-toplevel"])
    if not root_result or not root_result.stdout.strip():
        return None
    root = Path(root_result.stdout.strip())

    files: list[dict[str, Any]] = []
    diff_result = run_git(["diff", "--numstat", "HEAD", "--"])
    if diff_result:
        for line in diff_result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            additions_raw, deletions_raw, file_path = parts[0], parts[1], parts[2]
            binary = additions_raw == "-" or deletions_raw == "-"
            additions = 0 if binary else int(additions_raw)
            deletions = 0 if binary else int(deletions_raw)
            files.append(
                {
                    "path": file_path,
                    "additions": additions,
                    "deletions": deletions,
                    "binary": binary,
                    "untracked": False,
                    "is_code": is_code_path(file_path),
                }
            )

    untracked_result = run_git(["ls-files", "--others", "--exclude-standard", "-z"])
    if untracked_result and untracked_result.stdout:
        for file_path in [item for item in untracked_result.stdout.split("\0") if item]:
            full_path = root / file_path
            line_count = count_text_lines(full_path)
            if line_count is None:
                continue
            files.append(
                {
                    "path": file_path,
                    "additions": line_count,
                    "deletions": 0,
                    "binary": False,
                    "untracked": True,
                    "is_code": is_code_path(file_path),
                }
            )

    def totals(items: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "files": len(items),
            "additions": sum(int(item.get("additions") or 0) for item in items),
            "deletions": sum(int(item.get("deletions") or 0) for item in items),
        }

    code_files = [item for item in files if item.get("is_code")]
    return {
        "source": "git_diff_head_numstat",
        "captured_at": utc_now(),
        "git_root": str(root),
        "includes_staged": True,
        "includes_unstaged": True,
        "includes_untracked": True,
        "files": files,
        "totals": totals(files),
        "code_totals": totals(code_files),
    }


def environment_metadata(cwd: str | None) -> dict[str, Any]:
    return {
        "os": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "hostname": socket.gethostname(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or os.environ.get("LOGNAME"),
        "shell": os.environ.get("SHELL"),
        "term_program": os.environ.get("TERM_PROGRAM"),
        "cwd": cwd,
        "git": git_metadata(cwd),
    }


def compact_session_metadata(payload: dict[str, Any], surface: str, cwd: str | None, transcript_path: str | None) -> dict[str, Any]:
    return {
        "surface": surface,
        "session_id": value_at(payload, "session_id", "sessionId", "conversation_id", "conversationId"),
        "agent_id": value_at(payload, "agent_id", "agentId"),
        "agent_type": value_at(payload, "agent_type", "agentType"),
        "model": value_at(payload, "model", "model_name", "modelName"),
        "permission_mode": value_at(payload, "permission_mode", "permissionMode"),
        "user_email": value_at(payload, "user_email", "userEmail"),
        "cwd": cwd,
        "transcript_path": transcript_path,
    }


def tail_text(path: Path, max_bytes: int) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def extract_transcript_usage(transcript_path: str | None, max_bytes: int) -> dict[str, Any] | None:
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    if not path.exists() or not path.is_file():
        return None
    try:
        lines = tail_text(path, max_bytes).splitlines()
    except Exception:
        return None

    latest: dict[str, Any] | None = None
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:
            continue
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict):
            continue
        if item.get("type") == "event_msg" and payload.get("type") == "token_count":
            latest = {
                "source": "transcript_token_count",
                "timestamp": item.get("timestamp"),
                "info": payload.get("info"),
                "rate_limits": payload.get("rate_limits"),
            }
    return latest


def extract_content(payload: dict[str, Any], level: str) -> dict[str, Any]:
    content: dict[str, Any] = {}
    prompt = value_at(payload, "prompt", "user_prompt", "message")
    response = value_at(payload, "last_assistant_message", "agent_response", "response")
    thought = value_at(payload, "thought", "agent_thought")
    tool_input = value_at(payload, "tool_input", "input")
    tool_response = value_at(payload, "tool_response", "output", "result")

    values = {
        "prompt": prompt,
        "response": response,
        "thought": thought,
        "tool_input": tool_input,
        "tool_response": tool_response,
    }
    for key, value in values.items():
        if value is None:
            continue
        content[key] = scrub_sensitive(value) if level == "full" else summarize_value(value)
    return content


def event_specific_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in payload.items() if str(k) not in ENVELOPE_KEYS}


def build_records(payload: dict[str, Any], cfg: dict[str, Any], surface: str, source_id: str | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not cfg.get("enabled", True):
        return None, []
    level = str(cfg.get("collection_level") or "full").lower()
    if level == "off":
        return None, []
    if level not in {"full", "diagnostic", "basic"}:
        level = "diagnostic"

    hook_event_name = str(
        value_at(payload, "hook_event_name", "event", "event_name", "hookName")
        or first_nested(payload, [["hook", "event"], ["metadata", "hook_event_name"]])
        or "unknown"
    )
    transcript_path = value_at(payload, "transcript_path", "transcriptPath")
    cwd = value_at(payload, "cwd", "workspace_path", "workspacePath")
    env = environment_metadata(str(cwd) if cwd else None)
    env_ref = stable_hash(env)
    session = compact_session_metadata(payload, surface, str(cwd) if cwd else None, str(transcript_path) if transcript_path else None)
    session_ref = stable_hash(session)

    event: dict[str, Any] = {
        "record_type": "event",
        "event_id": str(uuid.uuid4()),
        "received_at": utc_now(),
        "collector_version": VERSION,
        "source_id": source_id,
        "surface": surface,
        "collection_level": level,
        "hook_event_name": hook_event_name,
        "session_id": session["session_id"],
        "turn_id": value_at(payload, "turn_id", "turnId"),
        "agent_id": session["agent_id"],
        "agent_type": session["agent_type"],
        "environment_ref": env_ref,
        "session_ref": session_ref,
    }

    if level != "basic":
        event["content"] = extract_content(payload, level)
        if cfg.get("capture", {}).get("token_usage_from_transcript", True):
            event["usage"] = extract_transcript_usage(
                transcript_path,
                int(cfg.get("max_transcript_bytes") or DEFAULT_MAX_TRANSCRIPT_BYTES),
            )
        hook_usage = value_at(payload, "usage", "token_usage", "tokenUsage")
        if hook_usage is not None:
            event["hook_usage"] = hook_usage if level == "full" else summarize_value(hook_usage)

    if level == "full" and cfg.get("capture", {}).get("raw_hook_input", True):
        event["raw_hook_input"] = scrub_sensitive(event_specific_payload(payload))

    if hook_event_name in {"Stop", "stop", "sessionEnd", "SessionEnd", "session_end"}:
        workspace_diff = git_workspace_diff(str(cwd) if cwd else None)
        if workspace_diff is not None:
            event["workspace_diff"] = workspace_diff

    snapshots = [
        {
            "record_type": "snapshot",
            "snapshot_type": "environment",
            "snapshot_id": env_ref,
            "received_at": utc_now(),
            "collector_version": VERSION,
            "source_id": source_id,
            "surface": surface,
            "environment": env,
        },
        {
            "record_type": "snapshot",
            "snapshot_type": "session",
            "snapshot_id": session_ref,
            "environment_ref": env_ref,
            "received_at": utc_now(),
            "collector_version": VERSION,
            "source_id": source_id,
            "surface": surface,
            "session": session,
        },
    ]
    return event, snapshots


def append_jsonl(directory: Path, event: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    day = dt.datetime.now().strftime("%Y-%m-%d")
    path = directory / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str))
        fh.write("\n")
    return path


def state_path(cfg: dict[str, Any]) -> Path:
    explicit = cfg.get("state_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return DEFAULT_HOME / "state.json"


def load_state(path: Path) -> dict[str, Any]:
    state = load_json(path)
    if not isinstance(state.get("snapshot_ids"), list):
        state["snapshot_ids"] = []
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_new_snapshots(snapshots: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    path = state_path(cfg)
    state = load_state(path)
    known = set(str(item) for item in state.get("snapshot_ids", []))
    new_snapshots: list[dict[str, Any]] = []
    snapshot_dir = Path(str(cfg.get("snapshot_log_dir") or DEFAULT_HOME / "snapshots")).expanduser()
    for snapshot in snapshots:
        snapshot_id = str(snapshot.get("snapshot_id"))
        if snapshot_id in known:
            continue
        append_jsonl(snapshot_dir, snapshot)
        known.add(snapshot_id)
        new_snapshots.append(snapshot)
    state["snapshot_ids"] = sorted(known)
    save_state(path, state)
    return new_snapshots


def upload_event(event: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str | None]:
    server_url = cfg.get("server_url")
    if not server_url:
        return True, None
    headers = {
        "Content-Type": "application/json",
        "X-AI-Worklog-Version": VERSION,
    }
    api_key_env = cfg.get("api_key_env")
    if api_key_env and os.environ.get(str(api_key_env)):
        headers["Authorization"] = f"Bearer {os.environ[str(api_key_env)]}"
    data = json.dumps(event, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(str(server_url), data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(
            request,
            timeout=float(cfg.get("request_timeout_seconds") or 2.0),
        ) as response:
            if 200 <= response.status < 300:
                return True, None
            return False, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def spool_failed(event: dict[str, Any], cfg: dict[str, Any], error: str | None) -> None:
    failed = dict(event)
    failed["upload_error"] = error
    failed["upload_failed_at"] = utc_now()
    failed_log_dir = Path(str(cfg.get("failed_log_dir") or DEFAULT_HOME / "failed")).expanduser()
    append_jsonl(failed_log_dir, failed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record one Codex/Cursor hook event.")
    parser.add_argument("--surface", default="unknown", help="codex, cursor, or another source label")
    parser.add_argument("--config", default=os.environ.get("AI_WORKLOG_CONFIG") or os.environ.get("AI_USAGE_COLLECTOR_CONFIG") or str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--source-id", default="ai-worklog")
    args = parser.parse_args()

    cfg = merged_config(Path(args.config).expanduser())
    payload = read_stdin_json()
    event, snapshots = build_records(payload, cfg, args.surface, args.source_id)
    if event is None:
        return 0

    for snapshot in write_new_snapshots(snapshots, cfg):
        ok, error = upload_event(snapshot, cfg)
        if not ok:
            spool_failed(snapshot, cfg, error)

    local_log_dir = Path(str(cfg.get("local_log_dir") or DEFAULT_HOME / "events")).expanduser()
    append_jsonl(local_log_dir, event)

    ok, error = upload_event(event, cfg)
    if not ok:
        spool_failed(event, cfg, error)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        fallback_dir = DEFAULT_HOME / "errors"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        error_event = {
            "record_type": "error",
            "event_id": str(uuid.uuid4()),
            "received_at": utc_now(),
            "collector_version": VERSION,
            "error": str(exc),
        }
        append_jsonl(fallback_dir, error_event)
        time.sleep(0.01)
        raise SystemExit(0)
