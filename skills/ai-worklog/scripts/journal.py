#!/usr/bin/env python3
"""Hook event journal for Codex and Cursor."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
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

import skill_release

VERSION = skill_release.VERSION
DEFAULT_HOME = Path.home() / ".ai-worklog"
DEFAULT_CONFIG_PATH = DEFAULT_HOME / "config.json"
DEFAULT_MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 2.0
DEFAULT_ASYNC_UPLOAD_INTERVAL_SECONDS = 60
DEFAULT_ASYNC_UPLOAD_LOCK_STALE_SECONDS = 10 * 60
DEFAULT_ASYNC_UPLOAD_MAX_RUNTIME_SECONDS = 2 * 60
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
EVENT_SCHEMA_VERSION = skill_release.EVENT_SCHEMA_VERSION


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
        "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        "upload_mode": "async",
        "upload_preflight": True,
        "async_upload": {
            "enabled": True,
            "batch_size": 100,
            "trigger_interval_seconds": DEFAULT_ASYNC_UPLOAD_INTERVAL_SECONDS,
            "lock_stale_seconds": DEFAULT_ASYNC_UPLOAD_LOCK_STALE_SECONDS,
            "max_runtime_seconds": DEFAULT_ASYNC_UPLOAD_MAX_RUNTIME_SECONDS,
        },
        "skill_update": {
            "enabled": True,
            "name": "ai-worklog",
            "current_version": VERSION,
            "trigger_interval_seconds": 24 * 60 * 60,
            "notify_interval_seconds": 24 * 60 * 60,
        },
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


def request_timeout_seconds(cfg: dict[str, Any]) -> float:
    try:
        value = float(cfg.get("request_timeout_seconds") or DEFAULT_REQUEST_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    return value


def upload_mode(cfg: dict[str, Any]) -> str:
    value = str(cfg.get("upload_mode") or "async").lower()
    return value if value in {"async", "sync", "local"} else "async"


def read_stdin_text() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        data = buffer.read()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
    return sys.stdin.read()


def read_stdin_json() -> dict[str, Any]:
    raw = read_stdin_text()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_unparsed_stdin": raw}
    return value if isinstance(value, dict) else {"_stdin": value}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def json_safe(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, dict):
        return {json_safe(str(k)): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


def json_dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(json_safe(value), **kwargs)


def stable_hash(value: Any) -> str:
    return sha256_text(json_dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def record_pk(record: dict[str, Any]) -> str:
    event_id = record.get("event_id")
    if event_id:
        return f"event:{event_id}"
    snapshot_id = record.get("snapshot_id")
    if snapshot_id:
        return f"snapshot:{snapshot_id}"
    return f"hash:{stable_hash(record)}"


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


def string_value(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def float_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def sequence_value(payload: dict[str, Any]) -> int | None:
    return int_value(value_at(payload, "sequence_no", "sequenceNo", "event_index", "eventIndex", "step_index", "stepIndex"))


def timing_metadata(payload: dict[str, Any], received_at: str) -> dict[str, Any]:
    started_at = string_value(
        value_at(payload, "started_at", "startedAt", "start_time", "startTime")
        or first_nested(payload, [["timing", "started_at"], ["timing", "start_time"]])
    )
    ended_at = string_value(
        value_at(payload, "ended_at", "endedAt", "end_time", "endTime", "completed_at", "completedAt")
        or first_nested(payload, [["timing", "ended_at"], ["timing", "end_time"]])
    )
    duration_ms = float_value(
        value_at(payload, "duration_ms", "durationMs", "elapsed_ms", "elapsedMs")
        or first_nested(payload, [["timing", "duration_ms"], ["timing", "elapsed_ms"]])
    )
    timing: dict[str, Any] = {
        "started_at": started_at or received_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
    }
    return {key: value for key, value in timing.items() if value is not None}


def operation_for_hook(hook_event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    hook = hook_event_name.lower()
    category = "unknown"
    phase = "event"

    if hook in {"sessionstart", "workspaceopen"}:
        category, phase = "session", "start"
    elif hook in {"stop", "sessionend", "session_end"}:
        category, phase = "session", "stop"
    elif hook in {"userpromptsubmit", "beforesubmitprompt"}:
        category, phase = "prompt", "submit"
    elif hook == "afteragentresponse":
        category, phase = "response", "complete"
    elif hook == "afteragentthought":
        category, phase = "thought", "complete"
    elif hook in {"pretooluse", "posttooluse", "posttoolusefailure"}:
        category = "tool"
        phase = "failure" if hook.endswith("failure") else ("before" if hook.startswith("pre") else "after")
    elif hook in {"beforeshellexecution", "aftershellexecution"}:
        category, phase = "shell", "before" if hook.startswith("before") else "after"
    elif hook in {"beforemcpexecution", "aftermcpexecution"}:
        category, phase = "mcp", "before" if hook.startswith("before") else "after"
    elif hook in {"afterfileedit", "aftertabfileedit"}:
        category, phase = "file_edit", "after"
    elif hook in {"beforereadfile", "beforetabfileread"}:
        category, phase = "file_read", "before"
    elif hook in {"subagentstart", "subagentstop"}:
        category, phase = "subagent", "start" if hook.endswith("start") else "stop"
    elif hook in {"precompact", "postcompact"}:
        category, phase = "compaction", "before" if hook.startswith("pre") else "after"
    elif hook == "permissionrequest":
        category, phase = "approval", "request"

    error = value_at(payload, "error", "error_type", "errorType", "exception")
    exit_code = int_value(value_at(payload, "exit_code", "exitCode", "status_code", "statusCode"))
    success: bool | None = None
    if phase == "failure" or error is not None:
        success = False
    elif exit_code is not None:
        success = exit_code == 0
    elif phase in {"after", "complete", "stop"}:
        success = True

    operation: dict[str, Any] = {
        "category": category,
        "phase": phase,
        "name": hook_event_name,
    }
    if success is not None:
        operation["success"] = success
    error_type = string_value(value_at(payload, "error_type", "errorType") or error)
    if error_type:
        operation["error_type"] = error_type
    return operation


def collect_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key in ("path", "file_path", "filepath", "filename", "file"):
            path = value.get(key)
            if isinstance(path, str) and path:
                paths.append(path)
        for child in value.values():
            paths.extend(collect_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(collect_paths(child))
    return paths


def tool_metadata(payload: dict[str, Any], hook_event_name: str, operation: dict[str, Any]) -> dict[str, Any] | None:
    if operation.get("category") not in {"tool", "shell", "mcp", "file_edit", "file_read"}:
        return None

    tool_value = value_at(payload, "tool_name", "toolName", "tool")
    tool_name = string_value(tool_value) or string_value(first_nested(payload, [["tool", "name"], ["metadata", "tool_name"]]))
    if not tool_name:
        tool_name = str(operation.get("category") or "unknown")

    tool_input = value_at(payload, "tool_input", "input", "args", "arguments")
    tool_response = value_at(payload, "tool_response", "output", "result")
    command = None
    if isinstance(tool_input, dict):
        command = string_value(value_at(tool_input, "cmd", "command"))
    command = command or string_value(value_at(payload, "cmd", "command"))
    exit_code = int_value(value_at(payload, "exit_code", "exitCode", "status_code", "statusCode"))

    files_written: list[str] = []
    files_read: list[str] = []
    if operation.get("category") == "file_read":
        files_read = collect_paths(tool_input if tool_input is not None else payload)
    elif operation.get("category") == "file_edit":
        files_written = collect_paths(tool_input if tool_input is not None else payload)
    else:
        for path in collect_paths(tool_input):
            files_written.append(path)

    metadata: dict[str, Any] = {
        "name": tool_name,
        "type": string_value(value_at(payload, "tool_type", "toolType")) or str(operation.get("category") or "tool"),
        "cwd": string_value(value_at(payload, "cwd", "workspace_path", "workspacePath")),
        "command": command,
        "exit_code": exit_code,
        "success": operation.get("success"),
        "duration_ms": float_value(value_at(payload, "duration_ms", "durationMs", "elapsed_ms", "elapsedMs")),
        "files_read": sorted(set(files_read)),
        "files_written": sorted(set(files_written)),
    }
    return {key: value for key, value in metadata.items() if value not in (None, [], {})}


def skill_metadata(payload: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any] | None:
    skill_name = string_value(
        value_at(payload, "skill_name", "skillName", "skill")
        or first_nested(payload, [["skill", "name"], ["metadata", "skill_name"]])
    )
    if not skill_name:
        return None
    metadata: dict[str, Any] = {
        "name": skill_name,
        "path": string_value(value_at(payload, "skill_path", "skillPath") or first_nested(payload, [["skill", "path"]])),
        "version": string_value(value_at(payload, "skill_version", "skillVersion") or first_nested(payload, [["skill", "version"]])),
        "phase": string_value(value_at(payload, "skill_phase", "skillPhase")) or operation.get("phase"),
        "success": operation.get("success"),
    }
    return {key: value for key, value in metadata.items() if value is not None}


def run_metadata_command(args: list[str], cwd: str | None = None, timeout: float = 1.5) -> str | None:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def git_config_value(key: str, cwd: str | None = None) -> str | None:
    if cwd and Path(cwd).expanduser().exists():
        value = run_metadata_command(["git", "config", "--get", key], cwd=str(Path(cwd).expanduser()))
        if value:
            return value
    return run_metadata_command(["git", "config", "--global", "--get", key], timeout=1.0)


def windows_upn() -> str | None:
    if platform.system().lower() != "windows":
        return None
    value = run_metadata_command(["whoami", "/upn"], timeout=1.0)
    if value and "@" in value:
        return value
    return None


def identity_metadata(cwd: str | None, git: dict[str, Any] | None = None) -> dict[str, Any]:
    os_user = os.environ.get("USER") or os.environ.get("USERNAME") or os.environ.get("LOGNAME")
    hostname = socket.gethostname()
    repo_git_email = string_value((git or {}).get("user_email")) or git_config_value("user.email", cwd)
    repo_git_name = string_value((git or {}).get("user_name")) or git_config_value("user.name", cwd)
    global_git_email = git_config_value("user.email")
    global_git_name = git_config_value("user.name")
    explicit_user_email = os.environ.get("AI_WORKLOG_USER_EMAIL") or os.environ.get("AI_USAGE_COLLECTOR_USER_EMAIL")
    candidates = {
        "user_email": explicit_user_email,
        "git_user_email": repo_git_email,
        "git_user_name": repo_git_name,
        "global_git_user_email": global_git_email,
        "global_git_user_name": global_git_name,
        "windows_upn": windows_upn(),
        "os_user": os_user,
        "user_domain": os.environ.get("USERDOMAIN"),
        "hostname": hostname,
    }
    return {key: value for key, value in candidates.items() if value}


def best_user_email(identity: dict[str, Any]) -> str | None:
    for key in ("user_email", "git_user_email", "global_git_user_email", "windows_upn"):
        value = string_value(identity.get(key))
        if value and "@" in value:
            return value
    return None


def git_metadata(cwd: str | None) -> dict[str, Any] | None:
    if not cwd:
        return None
    path = Path(cwd).expanduser()
    if not path.exists():
        return None

    def run_git(args: list[str]) -> str | None:
        return run_metadata_command(["git", *args], cwd=str(path))

    root = run_git(["rev-parse", "--show-toplevel"])
    if not root:
        return None
    status = run_git(["status", "--porcelain"]) or ""
    return {
        "root": root,
        "branch": run_git(["branch", "--show-current"]),
        "commit": run_git(["rev-parse", "HEAD"]),
        "dirty": bool(status),
        "user_email": run_git(["config", "--get", "user.email"]),
        "user_name": run_git(["config", "--get", "user.name"]),
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
    os_user = os.environ.get("USER") or os.environ.get("USERNAME") or os.environ.get("LOGNAME")
    git = git_metadata(cwd)
    identity = identity_metadata(cwd, git)
    return {
        "os": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "hostname": socket.gethostname(),
        "user": os_user,
        "user_domain": os.environ.get("USERDOMAIN"),
        "shell": os.environ.get("SHELL"),
        "term_program": os.environ.get("TERM_PROGRAM"),
        "cwd": cwd,
        "git": git,
        "identity": identity,
    }


def compact_session_metadata(payload: dict[str, Any], surface: str, cwd: str | None, transcript_path: str | None) -> dict[str, Any]:
    identity = identity_metadata(cwd)
    return {
        "surface": surface,
        "session_id": value_at(payload, "session_id", "sessionId", "conversation_id", "conversationId"),
        "agent_id": value_at(payload, "agent_id", "agentId"),
        "agent_type": value_at(payload, "agent_type", "agentType"),
        "model": value_at(payload, "model", "model_name", "modelName"),
        "permission_mode": value_at(payload, "permission_mode", "permissionMode"),
        "user_email": value_at(payload, "user_email", "userEmail") or best_user_email(identity),
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


def extract_transcript_context(transcript_path: str | None, max_bytes: int) -> dict[str, Any]:
    if not transcript_path:
        return {"usage": None, "model": None}
    path = Path(transcript_path).expanduser()
    if not path.exists() or not path.is_file():
        return {"usage": None, "model": None}
    try:
        lines = tail_text(path, max_bytes).splitlines()
    except Exception:
        return {"usage": None, "model": None}

    latest: dict[str, Any] | None = None
    latest_model: str | None = None
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
        if item.get("type") in {"session_meta", "turn_context"}:
            model = string_value(value_at(payload, "model", "model_name", "modelName"))
            if model:
                latest_model = model
    return {"usage": latest, "model": latest_model}


def extract_transcript_usage(transcript_path: str | None, max_bytes: int) -> dict[str, Any] | None:
    context = extract_transcript_context(transcript_path, max_bytes)
    usage = context.get("usage")
    return usage if isinstance(usage, dict) else None


def is_session_stop_hook(hook_event_name: str) -> bool:
    return hook_event_name in {"Stop", "stop", "sessionEnd", "SessionEnd", "session_end"}


def is_session_start_hook(hook_event_name: str) -> bool:
    return hook_event_name in {"SessionStart", "sessionStart", "workspaceOpen", "session_start"}


def extract_content(payload: dict[str, Any], level: str, hook_event_name: str = "") -> dict[str, Any]:
    content: dict[str, Any] = {}
    prompt = value_at(payload, "prompt", "user_prompt", "message")
    response = None
    if not is_session_stop_hook(hook_event_name):
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
    max_transcript_bytes = int(cfg.get("max_transcript_bytes") or DEFAULT_MAX_TRANSCRIPT_BYTES)
    payload_model = string_value(value_at(payload, "model", "model_name", "modelName"))
    should_read_transcript = bool(
        transcript_path
        and (
            not payload_model
            or (level != "basic" and cfg.get("capture", {}).get("token_usage_from_transcript", True))
        )
    )
    transcript_context = (
        extract_transcript_context(str(transcript_path), max_transcript_bytes)
        if should_read_transcript
        else {"usage": None, "model": None}
    )
    env = environment_metadata(str(cwd) if cwd else None)
    env_ref = stable_hash(env)
    session = compact_session_metadata(payload, surface, str(cwd) if cwd else None, str(transcript_path) if transcript_path else None)
    if not session.get("model"):
        session["model"] = string_value(transcript_context.get("model"))
    session_ref = stable_hash(session)
    received_at = utc_now()
    event_id = str(uuid.uuid4())
    trace_id = string_value(value_at(payload, "trace_id", "traceId")) or str(session["session_id"] or session_ref)
    parent_span_id = string_value(value_at(payload, "parent_span_id", "parentSpanId", "parent_event_id", "parentEventId"))
    operation = operation_for_hook(hook_event_name, payload)
    timeline = {
        "trace_id": trace_id,
        "span_id": event_id,
        "parent_span_id": parent_span_id,
        "sequence_no": sequence_value(payload),
        **timing_metadata(payload, received_at),
    }

    event: dict[str, Any] = {
        "record_type": "event",
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "received_at": received_at,
        "collector_version": VERSION,
        "source_id": source_id,
        "surface": surface,
        "collection_level": level,
        "hook_event_name": hook_event_name,
        "session_id": session["session_id"],
        "turn_id": value_at(payload, "turn_id", "turnId"),
        "agent_id": session["agent_id"],
        "agent_type": session["agent_type"],
        "model": session["model"],
        "environment_ref": env_ref,
        "session_ref": session_ref,
        "timeline": {key: value for key, value in timeline.items() if value is not None},
        "operation": operation,
    }
    tool = tool_metadata(payload, hook_event_name, operation)
    if tool:
        event["tool"] = scrub_sensitive(tool)
    skill = skill_metadata(payload, operation)
    if skill:
        event["skill"] = scrub_sensitive(skill)

    if level != "basic":
        event["content"] = extract_content(payload, level, hook_event_name)
        if cfg.get("capture", {}).get("token_usage_from_transcript", True):
            event["usage"] = transcript_context.get("usage")
        hook_usage = value_at(payload, "usage", "token_usage", "tokenUsage")
        if hook_usage is not None:
            event["hook_usage"] = hook_usage if level == "full" else summarize_value(hook_usage)

    if level == "full" and cfg.get("capture", {}).get("raw_hook_input", True):
        event["raw_hook_input"] = scrub_sensitive(event_specific_payload(payload))

    if is_session_stop_hook(hook_event_name):
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
        fh.write(json_dumps(event, ensure_ascii=False, sort_keys=True, default=str))
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
    if not isinstance(state.get("remote_snapshot_ids"), list):
        state["remote_snapshot_ids"] = []
    if not isinstance(state.get("session_sequences"), dict):
        state["session_sequences"] = {}
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_new_snapshots(snapshots: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    path = state_path(cfg)
    state = load_state(path)
    local_known = set(str(item) for item in state.get("snapshot_ids", []))
    remote_known = set(str(item) for item in state.get("remote_snapshot_ids", []))
    upload_candidates: list[dict[str, Any]] = []
    snapshot_dir = Path(str(cfg.get("snapshot_log_dir") or DEFAULT_HOME / "snapshots")).expanduser()
    for snapshot in snapshots:
        snapshot_id = str(snapshot.get("snapshot_id"))
        if snapshot_id not in local_known:
            append_jsonl(snapshot_dir, snapshot)
            local_known.add(snapshot_id)
        if snapshot_id not in remote_known:
            upload_candidates.append(snapshot)
    state["snapshot_ids"] = sorted(local_known)
    state["remote_snapshot_ids"] = sorted(remote_known)
    save_state(path, state)
    return upload_candidates


def mark_remote_snapshot_known(snapshot: dict[str, Any], cfg: dict[str, Any]) -> None:
    snapshot_id = snapshot.get("snapshot_id")
    if not snapshot_id:
        return
    path = state_path(cfg)
    state = load_state(path)
    remote_known = set(str(item) for item in state.get("remote_snapshot_ids", []))
    remote_known.add(str(snapshot_id))
    state["remote_snapshot_ids"] = sorted(remote_known)
    save_state(path, state)


def assign_event_sequence(event: dict[str, Any], cfg: dict[str, Any]) -> None:
    timeline = event.setdefault("timeline", {})
    if not isinstance(timeline, dict) or timeline.get("sequence_no") is not None:
        return
    path = state_path(cfg)
    state = load_state(path)
    sequences = state.setdefault("session_sequences", {})
    if not isinstance(sequences, dict):
        sequences = {}
        state["session_sequences"] = sequences
    session_id = str(event.get("session_id") or timeline.get("trace_id") or "unknown")
    next_value = int_value(sequences.get(session_id)) or 0
    next_value += 1
    timeline["sequence_no"] = next_value
    sequences[session_id] = next_value
    save_state(path, state)


def upload_headers(cfg: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-AI-Worklog-Version": VERSION,
    }
    api_key_env = cfg.get("api_key_env")
    if api_key_env and os.environ.get(str(api_key_env)):
        headers["Authorization"] = f"Bearer {os.environ[str(api_key_env)]}"
    return headers


def preflight_url(server_url: str) -> str:
    base = server_url.rstrip("/")
    if base.endswith("/events"):
        return f"{base}/exists"
    return f"{base}/events/exists"


def server_has_record(record: dict[str, Any], cfg: dict[str, Any]) -> bool:
    if not cfg.get("upload_preflight", True):
        return False
    server_url = cfg.get("server_url")
    if not server_url:
        return False
    payload = {"record_pks": [record_pk(record)]}
    data = json_dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        preflight_url(str(server_url)),
        data=data,
        headers=upload_headers(cfg),
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=request_timeout_seconds(cfg),
        ) as response:
            if not (200 <= response.status < 300):
                return False
            result = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    existing = result.get("existing") if isinstance(result, dict) else None
    return isinstance(existing, list) and record_pk(record) in existing


def upload_event(event: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str | None]:
    server_url = cfg.get("server_url")
    if not server_url:
        return True, None
    if server_has_record(event, cfg):
        return True, None
    data = json_dumps(event, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(str(server_url), data=data, headers=upload_headers(cfg), method="POST")
    try:
        with urllib.request.urlopen(
            request,
            timeout=request_timeout_seconds(cfg),
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


def auto_codex_backfill_enabled(cfg: dict[str, Any]) -> bool:
    section = cfg.get("codex_history_backfill")
    if isinstance(section, dict) and section.get("enabled") is False:
        return False
    return bool(cfg.get("server_url")) and upload_mode(cfg) != "local"


def async_upload_enabled(cfg: dict[str, Any]) -> bool:
    section = cfg.get("async_upload")
    if isinstance(section, dict) and section.get("enabled") is False:
        return False
    return bool(cfg.get("server_url")) and upload_mode(cfg) == "async"


def maybe_spawn_async_upload(cfg: dict[str, Any], config_path: Path) -> None:
    if not async_upload_enabled(cfg):
        return
    script = Path(__file__).resolve().with_name("async_upload_trigger.py")
    if not script.exists():
        return
    try:
        subprocess.Popen(
            [sys.executable or "python3", str(script), "--config", str(config_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return


def maybe_spawn_codex_backfill(payload: dict[str, Any], cfg: dict[str, Any], surface: str, config_path: Path) -> None:
    hook_event_name = str(
        value_at(payload, "hook_event_name", "event", "event_name", "hookName")
        or first_nested(payload, [["hook", "event"], ["metadata", "hook_event_name"]])
        or ""
    )
    transcript_path = string_value(value_at(payload, "transcript_path", "transcriptPath"))
    if surface != "codex" or not auto_codex_backfill_enabled(cfg):
        return
    if hook_event_name != "SessionStart" and not transcript_path:
        return
    script = Path(__file__).resolve().with_name("codex_backfill_trigger.py")
    if not script.exists():
        return
    command = [sys.executable or "python3", str(script), "--config", str(config_path)]
    if transcript_path:
        command.extend(["--sessions-root", transcript_path, "--ignore-interval"])
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return


def skill_update_config(cfg: dict[str, Any]) -> dict[str, Any]:
    section = cfg.get("skill_update")
    return section if isinstance(section, dict) else {}


def skill_update_enabled(cfg: dict[str, Any]) -> bool:
    section = skill_update_config(cfg)
    if section.get("enabled") is False:
        return False
    return bool(section.get("manifest_url") or os.environ.get("AI_WORKLOG_UPDATE_MANIFEST_URL"))


def skill_update_state_path(cfg: dict[str, Any]) -> Path:
    explicit = skill_update_config(cfg).get("state_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return DEFAULT_HOME / "skill_update_state.json"


def skill_update_notice_path(cfg: dict[str, Any]) -> Path:
    explicit = skill_update_config(cfg).get("notice_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return DEFAULT_HOME / "skill_update_notice.txt"


def skill_update_notify_interval_seconds(cfg: dict[str, Any]) -> int:
    value = skill_update_config(cfg).get("notify_interval_seconds")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 24 * 60 * 60


def maybe_emit_skill_update_notice(payload: dict[str, Any], cfg: dict[str, Any]) -> None:
    hook_event_name = str(
        value_at(payload, "hook_event_name", "event", "event_name", "hookName")
        or first_nested(payload, [["hook", "event"], ["metadata", "hook_event_name"]])
        or ""
    )
    if not is_session_start_hook(hook_event_name) or not skill_update_enabled(cfg):
        return

    notice = skill_update_notice_path(cfg)
    if not notice.exists():
        return
    state_file = skill_update_state_path(cfg)
    state = load_json(state_file)
    if state.get("update_available") is not True:
        return
    now = time.time()
    try:
        last_notified = float(state.get("last_notified_epoch") or 0)
    except (TypeError, ValueError):
        last_notified = 0
    if now - last_notified < skill_update_notify_interval_seconds(cfg):
        return

    try:
        text = notice.read_text(encoding="utf-8").strip()
    except Exception:
        return
    if not text:
        return
    print(text[:1000], file=sys.stderr)
    state["last_notified_at"] = utc_now()
    state["last_notified_epoch"] = now
    try:
        save_state(state_file, state)
    except Exception:
        return


def maybe_spawn_skill_update_check(payload: dict[str, Any], cfg: dict[str, Any], config_path: Path) -> None:
    hook_event_name = str(
        value_at(payload, "hook_event_name", "event", "event_name", "hookName")
        or first_nested(payload, [["hook", "event"], ["metadata", "hook_event_name"]])
        or ""
    )
    if not is_session_start_hook(hook_event_name) or not skill_update_enabled(cfg):
        return
    script = Path(__file__).resolve().with_name("check_update.py")
    if not script.exists():
        return
    try:
        subprocess.Popen(
            [sys.executable or "python3", str(script), "--config", str(config_path), "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Record one Codex/Cursor hook event.")
    parser.add_argument("--surface", default="unknown", help="codex, cursor, or another source label")
    parser.add_argument("--config", default=os.environ.get("AI_WORKLOG_CONFIG") or os.environ.get("AI_USAGE_COLLECTOR_CONFIG") or str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--source-id", default="ai-worklog")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    cfg = merged_config(config_path)
    payload = read_stdin_json()
    maybe_emit_skill_update_notice(payload, cfg)
    event, snapshots = build_records(payload, cfg, args.surface, args.source_id)
    if event is None:
        return 0
    assign_event_sequence(event, cfg)

    snapshot_upload_candidates = write_new_snapshots(snapshots, cfg)

    local_log_dir = Path(str(cfg.get("local_log_dir") or DEFAULT_HOME / "events")).expanduser()
    append_jsonl(local_log_dir, event)

    if upload_mode(cfg) == "sync":
        for snapshot in snapshot_upload_candidates:
            ok, error = upload_event(snapshot, cfg)
            if not ok:
                spool_failed(snapshot, cfg, error)
            else:
                mark_remote_snapshot_known(snapshot, cfg)

        ok, error = upload_event(event, cfg)
        if not ok:
            spool_failed(event, cfg, error)
    else:
        maybe_spawn_async_upload(cfg, config_path)

    maybe_spawn_codex_backfill(payload, cfg, args.surface, config_path)
    maybe_spawn_skill_update_check(payload, cfg, config_path)
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
