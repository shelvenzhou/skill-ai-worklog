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
        "event_count": len(ordered),
        "returned_events": min(len(ordered), bounded_limit),
        "snapshots": classify_snapshots(snapshot_records),
        "code_metrics": {
            "generated_code": code_metrics["generated_code"],
            "adopted_code": code_metrics["adopted_code"],
        },
    }
