from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any

from .metrics import compute_code_metrics
from .storage import session_models, token_totals, token_totals_by_model


MOJIBAKE_MARKERS = (
    "锟斤拷",
    "濂",
    "紝",
    "鎸",
    "変",
    "綘",
    "鐨",
    "勬",
    "帹",
    "鑽",
    "愬",
    "疄",
    "鐜",
    "鑾",
    "彇",
    "鍗",
    "曚",
    "釜",
    "瀹",
    "壒",
    "炰",
    "緥",
    "璇",
    "儏",
    "涓",
    "轰",
    "鍙",
    "傛",
    "佺",
)


def record_time(record: dict[str, Any]) -> str:
    return str(record.get("received_at") or record.get("client_received_at") or record.get("_server_ingested_at") or "")


def session_key(record: dict[str, Any]) -> str:
    value = record.get("session_id")
    return str(value) if value not in (None, "") else "unknown"


def nested_dict(record: dict[str, Any], key: str) -> dict[str, Any]:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def mojibake_score(value: str) -> int:
    return value.count("\ufffd") * 3 + sum(value.count(marker) for marker in MOJIBAKE_MARKERS)


def repair_mojibake_text(value: str) -> str:
    if not value or value.isascii():
        return value
    score = mojibake_score(value)
    if score < 2:
        return value
    try:
        repaired = value.encode("gb18030").decode("utf-8")
    except UnicodeError:
        return value
    if repaired == value:
        return value
    if mojibake_score(repaired) < score:
        return repaired
    return value


def repair_mojibake_tree(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake_text(value)
    if isinstance(value, list):
        return [repair_mojibake_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_mojibake_tree(item) for key, item in value.items()}
    return value


def first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def display_fields(record: dict[str, Any]) -> dict[str, Any]:
    content = nested_dict(record, "content")
    raw = nested_dict(record, "raw_hook_input")
    fields = {
        "prompt": first_value(content.get("prompt"), raw.get("prompt"), raw.get("user_prompt"), raw.get("message")),
        "response": first_value(content.get("response"), raw.get("last_assistant_message"), raw.get("agent_response")),
        "thought": first_value(content.get("thought"), raw.get("thought"), raw.get("agent_thought")),
        "tool_input": first_value(content.get("tool_input"), raw.get("tool_input"), raw.get("input"), raw.get("arguments")),
        "tool_response": first_value(content.get("tool_response"), raw.get("tool_response"), raw.get("output"), raw.get("result")),
    }
    return {key: repair_mojibake_tree(value) for key, value in fields.items() if value is not None}


def record_with_display(record: dict[str, Any]) -> dict[str, Any]:
    fields = display_fields(record)
    if not fields:
        return record
    return {**record, "display": fields}


def operation_category(record: dict[str, Any]) -> str:
    operation = nested_dict(record, "operation")
    category = operation.get("category")
    if isinstance(category, str) and category:
        return category
    hook = str(record.get("hook_event_name") or "").lower()
    if "tool" in hook:
        return "tool"
    if "prompt" in hook:
        return "prompt"
    if "response" in hook:
        return "response"
    if "subagent" in hook:
        return "subagent"
    if hook in {"stop", "sessionend", "sessionstart"}:
        return "session"
    return "unknown"


def operation_phase(record: dict[str, Any]) -> str:
    operation = nested_dict(record, "operation")
    phase = operation.get("phase")
    return str(phase) if phase not in (None, "") else "event"


def operation_success(record: dict[str, Any]) -> bool | None:
    operation = nested_dict(record, "operation")
    success = operation.get("success")
    if isinstance(success, bool):
        return success
    tool = nested_dict(record, "tool")
    tool_success = tool.get("success")
    if isinstance(tool_success, bool):
        return tool_success
    return None


def record_duration_ms(record: dict[str, Any]) -> float | None:
    timeline = nested_dict(record, "timeline")
    value = timeline.get("duration_ms")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def process_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    category_counts = Counter(operation_category(record) for record in records)
    phase_counts = Counter(operation_phase(record) for record in records)
    tool_counts: Counter[str] = Counter()
    skill_counts: Counter[str] = Counter()
    failures = 0
    durations_by_category: dict[str, float] = defaultdict(float)
    duration_event_counts: Counter[str] = Counter()

    for record in records:
        success = operation_success(record)
        if success is False:
            failures += 1

        duration_ms = record_duration_ms(record)
        if duration_ms is not None:
            category = operation_category(record)
            durations_by_category[category] += duration_ms
            duration_event_counts[category] += 1

        tool = nested_dict(record, "tool")
        tool_name = tool.get("name")
        if isinstance(tool_name, str) and tool_name:
            tool_counts[tool_name] += 1

        skill = nested_dict(record, "skill")
        skill_name = skill.get("name")
        if isinstance(skill_name, str) and skill_name:
            skill_counts[skill_name] += 1

    return {
        "operation_category_counts": dict(sorted(category_counts.items())),
        "operation_phase_counts": dict(sorted(phase_counts.items())),
        "tool_counts": dict(sorted(tool_counts.items())),
        "skill_counts": dict(sorted(skill_counts.items())),
        "failure_count": failures,
        "duration_ms_by_category": {
            category: {
                "total": round(total, 3),
                "events": int(duration_event_counts[category]),
                "avg": round(total / duration_event_counts[category], 3) if duration_event_counts[category] else 0,
            }
            for category, total in sorted(durations_by_category.items())
        },
    }


def timeline_event(record: dict[str, Any]) -> dict[str, Any]:
    timeline = nested_dict(record, "timeline")
    operation = nested_dict(record, "operation")
    tool = nested_dict(record, "tool")
    skill = nested_dict(record, "skill")
    item: dict[str, Any] = {
        "event_id": record.get("event_id"),
        "received_at": record_time(record),
        "sequence_no": timeline.get("sequence_no"),
        "hook_event_name": record.get("hook_event_name"),
        "category": operation_category(record),
        "phase": operation_phase(record),
        "success": operation_success(record),
        "duration_ms": record_duration_ms(record),
    }
    if tool:
        item["tool"] = {
            key: value
            for key, value in {
                "name": tool.get("name"),
                "type": tool.get("type"),
                "command": tool.get("command"),
                "exit_code": tool.get("exit_code"),
                "files_read": tool.get("files_read"),
                "files_written": tool.get("files_written"),
            }.items()
            if value not in (None, [], {})
        }
    if skill:
        item["skill"] = {
            key: value
            for key, value in {
                "name": skill.get("name"),
                "phase": skill.get("phase"),
                "version": skill.get("version"),
                "path": skill.get("path"),
            }.items()
            if value not in (None, "")
        }
    if operation.get("error_type"):
        item["error_type"] = operation.get("error_type")
    return {key: value for key, value in item.items() if value is not None}


def session_transcript_paths(snapshot_records: list[dict[str, Any]], session_id: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for record in snapshot_records:
        session = nested_dict(record, "session")
        if not session:
            continue
        snapshot_session_id = session.get("session_id")
        if snapshot_session_id not in (None, "", session_id):
            continue
        transcript_path = session.get("transcript_path")
        if not isinstance(transcript_path, str) or not transcript_path:
            continue
        path = Path(transcript_path).expanduser()
        key = str(path)
        if key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


def transcript_agent_messages(
    session_id: str,
    snapshot_records: list[dict[str, Any]],
    *,
    max_messages: int = 500,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for path in session_transcript_paths(snapshot_records, session_id):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") != "event_msg":
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "agent_message":
                continue
            message = payload.get("message")
            if not isinstance(message, str) or not message:
                continue
            timestamp = str(item.get("timestamp") or "")
            phase = str(payload.get("phase") or "message")
            digest = hashlib.sha256(f"{path}\0{timestamp}\0{phase}\0{message}".encode("utf-8")).hexdigest()[:20]
            messages.append(
                {
                    "record_type": "transcript_agent_message",
                    "event_id": f"transcript:{digest}",
                    "session_id": session_id,
                    "received_at": timestamp,
                    "hook_event_name": "AgentMessage",
                    "operation": {
                        "category": "response",
                        "phase": phase,
                        "name": "AgentMessage",
                        "success": True,
                    },
                    "content": {"response": message},
                    "transcript_path": str(path),
                    "transcript_only": True,
                }
            )
    messages.sort(key=record_time)
    return messages[-max_messages:]


def transcript_apply_patch_events(
    session_id: str,
    event_records: list[dict[str, Any]],
    snapshot_records: list[dict[str, Any]],
    *,
    max_events: int = 500,
) -> list[dict[str, Any]]:
    existing_tool_use_ids = {
        str(raw.get("tool_use_id"))
        for record in event_records
        for raw in [nested_dict(record, "raw_hook_input")]
        if raw.get("tool_use_id")
    }
    events: list[dict[str, Any]] = []
    for path in session_transcript_paths(snapshot_records, session_id):
        calls: dict[str, dict[str, Any]] = {}
        results: dict[str, dict[str, Any]] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = str(item.get("timestamp") or "")
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            if item.get("type") == "response_item" and payload.get("type") == "custom_tool_call":
                call_id = payload.get("call_id")
                if (
                    isinstance(call_id, str)
                    and payload.get("name") == "apply_patch"
                    and isinstance(payload.get("input"), str)
                ):
                    calls[call_id] = {
                        "timestamp": timestamp,
                        "input": payload.get("input"),
                        "status": payload.get("status"),
                    }
            elif item.get("type") == "event_msg" and payload.get("type") == "patch_apply_end":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    results[call_id] = {
                        "timestamp": timestamp,
                        "success": payload.get("success"),
                        "changes": payload.get("changes"),
                    }

        for call_id, call in calls.items():
            if call_id in existing_tool_use_ids:
                continue
            result = results.get(call_id, {})
            if result.get("success") is False:
                continue
            changes = result.get("changes")
            files_written = sorted(changes) if isinstance(changes, dict) else []
            timestamp = str(result.get("timestamp") or call.get("timestamp") or "")
            digest = hashlib.sha256(f"{path}\0{call_id}\0{call.get('input')}".encode("utf-8")).hexdigest()[:20]
            events.append(
                {
                    "record_type": "event",
                    "event_id": f"transcript-tool:{digest}",
                    "session_id": session_id,
                    "received_at": timestamp,
                    "hook_event_name": "PostToolUse",
                    "operation": {
                        "category": "tool",
                        "phase": "after",
                        "name": "PostToolUse",
                        "success": True,
                    },
                    "tool": {
                        "name": "apply_patch",
                        "type": "tool",
                        "files_written": files_written,
                    },
                    "content": {"tool_input": call.get("input")},
                    "raw_hook_input": {
                        "tool_name": "apply_patch",
                        "tool_use_id": call_id,
                        "source": "transcript",
                    },
                    "transcript_path": str(path),
                    "transcript_only": True,
                }
            )
    events.sort(key=record_time)
    return events[-max_events:]


def summarize_session_records(
    session_id: str,
    records: list[dict[str, Any]],
    code_metrics_by_session: dict[str, Any] | None = None,
    model_by_session: dict[str, str] | None = None,
) -> dict[str, Any]:
    ordered = sorted(records, key=record_time)
    hook_counts = Counter(str(record.get("hook_event_name") or "unknown") for record in ordered)
    surfaces = sorted({str(record.get("surface")) for record in ordered if record.get("surface")})
    collection_levels = sorted({str(record.get("collection_level")) for record in ordered if record.get("collection_level")})
    environment_refs = sorted({str(record.get("environment_ref")) for record in ordered if record.get("environment_ref")})
    session_refs = sorted({str(record.get("session_ref")) for record in ordered if record.get("session_ref")})
    code_metrics = (code_metrics_by_session or {}).get(
        session_id,
        {
            "generated": {"additions": 0, "deletions": 0, "files": 0, "events": 0},
            "adopted": {"additions": 0, "deletions": 0, "files": 0, "events": 0},
            "uncommitted": {"additions": 0, "deletions": 0, "files": 0, "events": 0},
        },
    )

    return {
        "session_id": session_id,
        "surfaces": surfaces,
        "first_seen": record_time(ordered[0]) if ordered else None,
        "last_seen": record_time(ordered[-1]) if ordered else None,
        "event_count": len(ordered),
        "hook_event_counts": dict(sorted(hook_counts.items())),
        "collection_levels": collection_levels,
        "token_totals": token_totals(ordered),
        "token_totals_by_model": token_totals_by_model(ordered, model_by_session),
        "process": process_summary(ordered),
        "code_metrics": {
            "generated_code": code_metrics.get("generated", {}),
            "adopted_code": code_metrics.get("adopted", {}),
            "uncommitted_code": code_metrics.get("uncommitted", {}),
            "adoption_source": code_metrics.get("adoption_source"),
            "git_commit_events": code_metrics.get("git_commit_events", 0),
            "latest_git_commit_code": code_metrics.get("latest_git_commit_code"),
            "latest_git_commit_event_id": code_metrics.get("latest_git_commit_event_id"),
            "latest_git_commit_received_at": code_metrics.get("latest_git_commit_received_at"),
            "latest_workspace_diff_event_id": code_metrics.get("latest_workspace_diff_event_id"),
            "latest_workspace_diff_received_at": code_metrics.get("latest_workspace_diff_received_at"),
        },
        "environment_refs": environment_refs,
        "session_refs": session_refs,
    }


def build_sessions_index(
    records: list[dict[str, Any]],
    limit: int = 50,
    snapshot_records: list[dict[str, Any]] | None = None,
    total_sessions: int | None = None,
) -> dict[str, Any]:
    event_records = [record for record in records if record.get("record_type") == "event"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in event_records:
        grouped[session_key(record)].append(record)

    transcript_tool_events: list[dict[str, Any]] = []
    if snapshot_records:
        for grouped_session_id, session_records in grouped.items():
            transcript_tool_events.extend(transcript_apply_patch_events(grouped_session_id, session_records, snapshot_records))

    metric_records = [*event_records, *transcript_tool_events]
    code_metrics = compute_code_metrics(metric_records)
    model_by_session = session_models([*event_records, *(snapshot_records or [])])
    summaries = [
        summarize_session_records(session_id, session_records, code_metrics.get("by_session", {}), model_by_session)
        for session_id, session_records in grouped.items()
    ]
    summaries.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
    bounded_limit = max(1, min(int(limit), 500))
    session_count = len(summaries) if total_sessions is None else total_sessions
    return {
        "sessions": summaries[:bounded_limit],
        "total_sessions": session_count,
        "returned_sessions": min(len(summaries), bounded_limit),
        "code_metrics": {
            "generated_code": code_metrics["generated_code"],
            "adopted_code": code_metrics["adopted_code"],
            "uncommitted_code": code_metrics["uncommitted_code"],
        },
        "process": process_summary(event_records),
    }


def classify_snapshots(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    snapshots: dict[str, list[dict[str, Any]]] = {"environment": [], "session": [], "other": []}
    for record in records:
        snapshot_type = str(record.get("snapshot_type") or "other")
        key = snapshot_type if snapshot_type in snapshots else "other"
        snapshots[key].append(record)
    return snapshots


def build_session_detail(
    session_id: str,
    event_records: list[dict[str, Any]],
    snapshot_records: list[dict[str, Any]],
    *,
    limit: int = 200,
) -> dict[str, Any]:
    matching_events = [record for record in event_records if session_key(record) == session_id]
    ordered = sorted(matching_events, key=record_time)
    transcript_tool_events = transcript_apply_patch_events(session_id, ordered, snapshot_records)
    metric_records = sorted([*ordered, *transcript_tool_events], key=record_time)
    code_metrics = compute_code_metrics(metric_records)
    model_by_session = session_models([*ordered, *snapshot_records])
    summary = summarize_session_records(session_id, ordered, code_metrics.get("by_session", {}), model_by_session)
    bounded_limit = max(1, min(int(limit), 1000))
    assistant_messages = transcript_agent_messages(session_id, snapshot_records)
    visible_events = ordered[-bounded_limit:]
    display_events = [record_with_display(record) for record in visible_events]
    display_transcript_tool_events = [record_with_display(record) for record in transcript_tool_events]
    display_assistant_messages = [record_with_display(record) for record in assistant_messages]
    combined_timeline_records = sorted([*display_events, *display_transcript_tool_events, *display_assistant_messages], key=record_time)
    return {
        "session": summary,
        "events": display_events,
        "assistant_messages": display_assistant_messages,
        "transcript_tool_events": display_transcript_tool_events,
        "timeline": [timeline_event(record) for record in combined_timeline_records],
        "event_count": len(ordered),
        "returned_events": min(len(ordered), bounded_limit),
        "snapshots": classify_snapshots(snapshot_records),
        "code_metrics": {
            "generated_code": code_metrics["generated_code"],
            "adopted_code": code_metrics["adopted_code"],
            "uncommitted_code": code_metrics["uncommitted_code"],
        },
        "process": process_summary(ordered),
    }
