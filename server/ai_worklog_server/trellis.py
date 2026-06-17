from __future__ import annotations

from collections import Counter, defaultdict
import json
import re
from typing import Any


TRELLIS_PATTERNS = (
    ".trellis",
    "trellis-",
    "task.py",
    "get_context.py",
    "finish-work.md",
    "Requirement exploration",
)

PROBLEM_PATTERNS = (
    "error",
    "failed",
    "failure",
    "exception",
    "not found",
    "timeout",
    "traceback",
    "permission denied",
    "missing",
    "cannot",
    "can't",
    "unable",
    "测试失败",
    "缺少",
    "无法",
    "失败",
)

TASK_PATH_RE = re.compile(r"(?:^|[\s\"'`])(?P<path>\.trellis[/\\]tasks[/\\](?P<task>[^\\/\s\"'`]+))")


def record_time(record: dict[str, Any]) -> str:
    return str(record.get("received_at") or record.get("client_received_at") or record.get("_server_ingested_at") or "")


def nested(record: dict[str, Any], key: str) -> dict[str, Any]:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def display_text(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("hook_event_name", "event_id", "session_id"):
        value = record.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("tool", "content", "raw_hook_input", "display"):
        value = record.get(key)
        if value not in (None, {}, []):
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
    return "\n".join(parts)


def tool_command(record: dict[str, Any]) -> str | None:
    tool = nested(record, "tool")
    command = tool.get("command")
    if isinstance(command, str) and command:
        return command
    content = nested(record, "content")
    tool_input = content.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command") or tool_input.get("cmd")
        if isinstance(command, str) and command:
            return command
    if isinstance(tool_input, str) and tool_input:
        return tool_input
    return None


def is_trellis_event(record: dict[str, Any]) -> bool:
    text = display_text(record).lower()
    return any(pattern.lower() in text for pattern in TRELLIS_PATTERNS)


def task_refs(text: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in TASK_PATH_RE.finditer(text):
        path = match.group("path").replace("\\", "/")
        task = match.group("task")
        key = (path, task)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"task_path": path, "task_id": task})
    return refs


def phase_guess(record: dict[str, Any]) -> str:
    text = display_text(record).lower()
    command = (tool_command(record) or "").lower()
    if "finish-work.md" in text or "task.py finish" in command:
        return "finish"
    if "check.jsonl" in text or "trellis-check" in text or " check" in command:
        return "check"
    if "implement.md" in text or "implement.jsonl" in text or "trellis-implement" in text:
        return "implementation"
    if "design.md" in text or "trellis-design" in text:
        return "design"
    if "prd.md" in text or "trellis-brainstorm" in text or "requirement exploration" in text or "--step 1." in command:
        return "requirements"
    if "get_context.py" in command and "--mode phase" in command:
        return "workflow_context"
    if "get_context.py" in command or ".trellis/spec" in text:
        return "context"
    return "unknown"


def artifact_refs(text: str) -> list[str]:
    artifacts = []
    for name in ("prd.md", "design.md", "implement.md", "check.jsonl", "implement.jsonl", "task.json"):
        if name in text:
            artifacts.append(name)
    return artifacts


def problem_signals(record: dict[str, Any]) -> list[dict[str, str]]:
    signals: list[dict[str, str]] = []
    operation = nested(record, "operation")
    tool = nested(record, "tool")
    if operation.get("success") is False or tool.get("success") is False:
        signals.append({"type": "structured_failure", "evidence": str(operation.get("error_type") or tool.get("exit_code") or "success=false")})
    text = display_text(record)
    text_lower = text.lower()
    for pattern in PROBLEM_PATTERNS:
        if pattern.lower() in text_lower:
            signals.append({"type": "text_match", "evidence": pattern})
            break
    return signals


def event_signal(record: dict[str, Any]) -> dict[str, Any] | None:
    if not is_trellis_event(record):
        return None
    text = display_text(record)
    refs = task_refs(text)
    command = tool_command(record)
    tool = nested(record, "tool")
    return {
        "event_id": record.get("event_id"),
        "session_id": record.get("session_id"),
        "received_at": record_time(record),
        "hook_event_name": record.get("hook_event_name"),
        "tool_name": tool.get("name"),
        "command": command,
        "phase": phase_guess(record),
        "task_refs": refs,
        "artifacts": artifact_refs(text),
        "problem_signals": problem_signals(record),
    }


def infer_trellis_session(records: list[dict[str, Any]]) -> dict[str, Any]:
    signals = [signal for record in records for signal in [event_signal(record)] if signal]
    phase_counts = Counter(str(signal.get("phase") or "unknown") for signal in signals)
    tool_counts = Counter(str(signal.get("tool_name") or "unknown") for signal in signals)
    task_ids: set[str] = set()
    task_paths: set[str] = set()
    artifacts: set[str] = set()
    commands: Counter[str] = Counter()
    problems: list[dict[str, Any]] = []
    for signal in signals:
        command = signal.get("command")
        if isinstance(command, str) and command:
            commands[command] += 1
        for ref in signal.get("task_refs") or []:
            if isinstance(ref, dict):
                if ref.get("task_id"):
                    task_ids.add(str(ref["task_id"]))
                if ref.get("task_path"):
                    task_paths.add(str(ref["task_path"]))
        for artifact in signal.get("artifacts") or []:
            artifacts.add(str(artifact))
        for problem in signal.get("problem_signals") or []:
            problems.append(
                {
                    "event_id": signal.get("event_id"),
                    "received_at": signal.get("received_at"),
                    "phase": signal.get("phase"),
                    **problem,
                }
            )
    repeated_commands = [
        {"command": command, "count": count}
        for command, count in commands.most_common()
        if count >= 3
    ]
    return {
        "uses_trellis": bool(signals),
        "event_count": len(signals),
        "first_seen": min((str(signal.get("received_at") or "") for signal in signals), default=None),
        "last_seen": max((str(signal.get("received_at") or "") for signal in signals), default=None),
        "phase_counts": dict(sorted(phase_counts.items())),
        "tool_counts": dict(sorted(tool_counts.items())),
        "task_ids": sorted(task_ids),
        "task_paths": sorted(task_paths),
        "artifacts": sorted(artifacts),
        "problem_signal_count": len(problems),
        "problem_signals": problems[:50],
        "repeated_commands": repeated_commands[:20],
        "events": signals[:200],
    }


def infer_trellis_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        session_id = str(record.get("session_id") or "unknown")
        by_session[session_id].append(record)
    session_summaries = {
        session_id: infer_trellis_session(session_records)
        for session_id, session_records in by_session.items()
    }
    trellis_sessions = {
        session_id: summary
        for session_id, summary in session_summaries.items()
        if summary["uses_trellis"]
    }
    phase_counts: Counter[str] = Counter()
    task_counts: Counter[str] = Counter()
    problem_signal_count = 0
    event_count = 0
    for summary in trellis_sessions.values():
        phase_counts.update(summary["phase_counts"])
        for task_id in summary["task_ids"]:
            task_counts[task_id] += 1
        problem_signal_count += int(summary["problem_signal_count"])
        event_count += int(summary["event_count"])
    return {
        "total_sessions": len(session_summaries),
        "trellis_sessions": len(trellis_sessions),
        "non_trellis_sessions": len(session_summaries) - len(trellis_sessions),
        "trellis_event_count": event_count,
        "phase_counts": dict(sorted(phase_counts.items())),
        "task_counts": dict(task_counts.most_common(50)),
        "problem_signal_count": problem_signal_count,
        "sessions": dict(sorted(trellis_sessions.items())),
    }
