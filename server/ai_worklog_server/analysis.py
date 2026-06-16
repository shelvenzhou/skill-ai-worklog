from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .metrics import compute_code_metrics
from .storage import token_usage


def record_time(record: dict[str, Any]) -> str:
    return str(record.get("received_at") or record.get("client_received_at") or record.get("_server_ingested_at") or "")


def session_key(record: dict[str, Any]) -> str:
    value = record.get("session_id")
    return str(value) if value not in (None, "") else "unknown"


def token_totals(records: list[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }
    for record in records:
        usage = token_usage(record)
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
    return totals


def nested_dict(record: dict[str, Any], key: str) -> dict[str, Any]:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


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


def summarize_session_records(
    session_id: str,
    records: list[dict[str, Any]],
    code_metrics_by_session: dict[str, Any] | None = None,
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
        "process": process_summary(ordered),
        "code_metrics": {
            "generated_code": code_metrics.get("generated", {}),
            "adopted_code": code_metrics.get("adopted", {}),
            "latest_workspace_diff_event_id": code_metrics.get("latest_workspace_diff_event_id"),
            "latest_workspace_diff_received_at": code_metrics.get("latest_workspace_diff_received_at"),
        },
        "environment_refs": environment_refs,
        "session_refs": session_refs,
    }


def build_sessions_index(records: list[dict[str, Any]], limit: int = 50) -> dict[str, Any]:
    event_records = [record for record in records if record.get("record_type") == "event"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in event_records:
        grouped[session_key(record)].append(record)

    code_metrics = compute_code_metrics(event_records)
    summaries = [
        summarize_session_records(session_id, session_records, code_metrics.get("by_session", {}))
        for session_id, session_records in grouped.items()
    ]
    summaries.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
    bounded_limit = max(1, min(int(limit), 500))
    return {
        "sessions": summaries[:bounded_limit],
        "total_sessions": len(summaries),
        "returned_sessions": min(len(summaries), bounded_limit),
        "code_metrics": {
            "generated_code": code_metrics["generated_code"],
            "adopted_code": code_metrics["adopted_code"],
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
    code_metrics = compute_code_metrics(ordered)
    summary = summarize_session_records(session_id, ordered, code_metrics.get("by_session", {}))
    bounded_limit = max(1, min(int(limit), 1000))
    return {
        "session": summary,
        "events": ordered[:bounded_limit],
        "timeline": [timeline_event(record) for record in ordered[:bounded_limit]],
        "event_count": len(ordered),
        "returned_events": min(len(ordered), bounded_limit),
        "snapshots": classify_snapshots(snapshot_records),
        "code_metrics": {
            "generated_code": code_metrics["generated_code"],
            "adopted_code": code_metrics["adopted_code"],
        },
        "process": process_summary(ordered),
    }
