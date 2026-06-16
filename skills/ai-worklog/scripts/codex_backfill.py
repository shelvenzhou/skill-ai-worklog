#!/usr/bin/env python3
"""Backfill Codex transcript JSONL files into AI Worklog collector records."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
from pathlib import Path
from typing import Any, Iterable

import journal
import replay


_ORIGINAL_ENVIRONMENT_METADATA = journal.environment_metadata
_ENVIRONMENT_CACHE: dict[str | None, dict[str, Any]] = {}


def cached_environment_metadata(cwd: str | None) -> dict[str, Any]:
    key = cwd or None
    cached = _ENVIRONMENT_CACHE.get(key)
    if cached is not None:
        return cached
    value = _ORIGINAL_ENVIRONMENT_METADATA(cwd)
    _ENVIRONMENT_CACHE[key] = value
    return value


def iter_transcript_paths(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    if not root.exists():
        return
    yield from sorted(root.glob("**/rollout-*.jsonl"))


def upload_state_path(cfg: dict[str, Any]) -> Path:
    explicit = cfg.get("backfill_state_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "codex_backfill_state.sqlite3"


def file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


class BackfillLedger:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists uploaded_records (
                  collector_url text not null,
                  record_pk text not null,
                  uploaded_at text not null,
                  primary key (collector_url, record_pk)
                );

                create table if not exists transcript_progress (
                  collector_url text not null,
                  transcript_path text not null,
                  mtime_ns integer not null,
                  size_bytes integer not null,
                  status text not null,
                  records_count integer not null default 0,
                  skipped_local integer not null default 0,
                  skipped_existing integer not null default 0,
                  attempted integer not null default 0,
                  accepted integer not null default 0,
                  duplicates integer not null default 0,
                  error text,
                  updated_at text not null,
                  primary key (collector_url, transcript_path)
                );
                """
            )

    def uploaded_pks(self, collector_url: str, record_pks: list[str]) -> set[str]:
        keys = sorted({str(key) for key in record_pks if key})
        if not keys:
            return set()
        placeholders = ",".join("?" for _ in keys)
        sql = f"""
            select record_pk from uploaded_records
            where collector_url = ? and record_pk in ({placeholders})
        """
        with self._connect() as conn:
            rows = conn.execute(sql, [collector_url, *keys]).fetchall()
        return {str(row[0]) for row in rows}

    def mark_uploaded(self, collector_url: str, record_pks: Iterable[str]) -> None:
        keys = sorted({str(key) for key in record_pks if key})
        if not keys:
            return
        uploaded_at = journal.utc_now()
        with self._connect() as conn:
            conn.executemany(
                """
                insert or replace into uploaded_records (collector_url, record_pk, uploaded_at)
                values (?, ?, ?)
                """,
                [(collector_url, key, uploaded_at) for key in keys],
            )

    def transcript_complete(self, collector_url: str, path: Path) -> bool:
        mtime_ns, size_bytes = file_signature(path)
        with self._connect() as conn:
            row = conn.execute(
                """
                select status from transcript_progress
                where collector_url = ?
                  and transcript_path = ?
                  and mtime_ns = ?
                  and size_bytes = ?
                """,
                (collector_url, str(path), mtime_ns, size_bytes),
            ).fetchone()
        return bool(row and row[0] == "complete")

    def mark_transcript(
        self,
        collector_url: str,
        path: Path,
        status: str,
        counts: dict[str, int] | None = None,
        error: str | None = None,
    ) -> None:
        mtime_ns, size_bytes = file_signature(path)
        values = counts or {}
        with self._connect() as conn:
            conn.execute(
                """
                insert or replace into transcript_progress (
                  collector_url, transcript_path, mtime_ns, size_bytes, status,
                  records_count, skipped_local, skipped_existing, attempted,
                  accepted, duplicates, error, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    collector_url,
                    str(path),
                    mtime_ns,
                    size_bytes,
                    status,
                    int(values.get("scanned") or 0),
                    int(values.get("skipped_local") or 0),
                    int(values.get("skipped_existing") or 0),
                    int(values.get("attempted") or 0),
                    int(values.get("accepted") or 0),
                    int(values.get("duplicates") or 0),
                    error,
                    journal.utc_now(),
                ),
            )


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc
            if isinstance(item, dict):
                yield item


def text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def parse_json_object(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    return value


def command_from_tool_input(value: Any) -> str | None:
    if isinstance(value, dict):
        command = value.get("cmd") or value.get("command")
        if isinstance(command, str):
            return command
    return None


def cwd_from_tool_input(value: Any, fallback: str | None) -> str | None:
    if isinstance(value, dict) and isinstance(value.get("workdir"), str):
        return value["workdir"]
    return fallback


def output_exit_code(output: Any) -> int | None:
    if not isinstance(output, str):
        return None
    marker = "Process exited with code "
    index = output.find(marker)
    if index < 0:
        return None
    tail = output[index + len(marker) :]
    digits = []
    for char in tail:
        if char.isdigit() or (char == "-" and not digits):
            digits.append(char)
        else:
            break
    if not digits:
        return None
    try:
        return int("".join(digits))
    except ValueError:
        return None


def latest_usage_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    payload = item.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    return {
        "source": "transcript_token_count",
        "timestamp": item.get("timestamp"),
        "info": payload.get("info"),
        "rate_limits": payload.get("rate_limits"),
    }


def deterministic_id(path: Path, *parts: Any) -> str:
    key = "\0".join([str(path), *[str(part) for part in parts]])
    return f"codex-backfill-{journal.sha256_text(key)[:32]}"


def set_event_identity(event: dict[str, Any], event_id: str, received_at: str | None) -> None:
    event["event_id"] = event_id
    if received_at:
        event["received_at"] = received_at
        timeline = event.get("timeline")
        if isinstance(timeline, dict):
            timeline["started_at"] = timeline.get("started_at") or received_at
            timeline["ended_at"] = timeline.get("ended_at") or received_at
            timeline["span_id"] = event_id


def build_event(
    payload: dict[str, Any],
    cfg: dict[str, Any],
    *,
    path: Path,
    event_id: str,
    timestamp: str | None,
    latest_usage: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    event, snapshots = journal.build_records(payload, cfg, "codex", "codex-backfill")
    if event is None:
        return None, []
    set_event_identity(event, event_id, timestamp)
    event.pop("workspace_diff", None)
    if latest_usage is not None:
        event["usage"] = latest_usage
    event["backfill"] = {
        "source": "codex_transcript",
        "transcript_path": str(path),
    }
    for snapshot in snapshots:
        if timestamp:
            snapshot["received_at"] = timestamp
    return event, snapshots


def events_from_transcript(path: Path, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    session_meta: dict[str, Any] = {}
    current_turn_id: str | None = None
    current_cwd: str | None = None
    current_model: str | None = None
    latest_usage: dict[str, Any] | None = None
    tool_calls: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(payload: dict[str, Any], event_id: str, timestamp: str | None) -> None:
        nonlocal records
        event, snapshots = build_event(payload, cfg, path=path, event_id=event_id, timestamp=timestamp, latest_usage=latest_usage)
        for record in [*snapshots, event] if event is not None else snapshots:
            pk = journal.record_pk(record)
            if pk in seen:
                continue
            seen.add(pk)
            records.append(record)

    for item in iter_jsonl(path):
        timestamp = item.get("timestamp") if isinstance(item.get("timestamp"), str) else None
        item_type = item.get("type")
        payload = item.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        usage = latest_usage_payload(item)
        if usage:
            latest_usage = usage

        if item_type == "session_meta":
            session_meta = payload
            current_cwd = str(payload.get("cwd")) if payload.get("cwd") else current_cwd
            current_model = str(payload.get("model")) if payload.get("model") else current_model
            session_id = str(payload.get("id") or path.stem)
            add(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": session_id,
                    "cwd": current_cwd,
                    "model": current_model,
                    "transcript_path": str(path),
                    "started_at": timestamp or payload.get("timestamp"),
                },
                deterministic_id(path, "SessionStart", session_id),
                timestamp or payload.get("timestamp"),
            )
            continue

        session_id = str(session_meta.get("id") or path.stem)

        if item_type == "turn_context":
            current_turn_id = str(payload.get("turn_id") or current_turn_id) if payload.get("turn_id") else current_turn_id
            current_cwd = str(payload.get("cwd")) if payload.get("cwd") else current_cwd
            current_model = str(payload.get("model")) if payload.get("model") else current_model
            continue

        if item_type == "event_msg" and payload.get("type") == "user_message":
            message = payload.get("message")
            add(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "turn_id": current_turn_id,
                    "cwd": current_cwd,
                    "model": current_model,
                    "transcript_path": str(path),
                    "prompt": message,
                    "started_at": timestamp,
                },
                deterministic_id(path, "UserPromptSubmit", current_turn_id, timestamp, message),
                timestamp,
            )
            continue

        if item_type == "event_msg" and payload.get("type") == "agent_message":
            message = payload.get("message")
            add(
                {
                    "hook_event_name": "AfterAgentResponse",
                    "session_id": session_id,
                    "turn_id": current_turn_id,
                    "cwd": current_cwd,
                    "model": current_model,
                    "transcript_path": str(path),
                    "response": message,
                    "phase": payload.get("phase"),
                    "started_at": timestamp,
                },
                deterministic_id(path, "AfterAgentResponse", current_turn_id, timestamp, message),
                timestamp,
            )
            continue

        if item_type == "event_msg" and payload.get("type") == "task_complete":
            add(
                {
                    "hook_event_name": "Stop",
                    "session_id": session_id,
                    "turn_id": payload.get("turn_id") or current_turn_id,
                    "cwd": current_cwd,
                    "model": current_model,
                    "transcript_path": str(path),
                    "last_assistant_message": payload.get("last_agent_message"),
                    "duration_ms": payload.get("duration_ms"),
                    "started_at": timestamp,
                },
                deterministic_id(path, "Stop", payload.get("turn_id") or current_turn_id, timestamp),
                timestamp,
            )
            continue

        if item_type == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                tool_calls[call_id] = {
                    "timestamp": timestamp,
                    "name": payload.get("name"),
                    "input": parse_json_object(payload.get("arguments", payload.get("input"))),
                }
            continue

        if item_type == "response_item" and payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                continue
            call = tool_calls.get(call_id, {})
            tool_input = call.get("input")
            output = payload.get("output")
            exit_code = output_exit_code(output)
            add(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "turn_id": current_turn_id,
                    "cwd": cwd_from_tool_input(tool_input, current_cwd),
                    "model": current_model,
                    "transcript_path": str(path),
                    "tool_name": call.get("name"),
                    "tool_input": tool_input,
                    "tool_response": output,
                    "exit_code": exit_code,
                    "started_at": call.get("timestamp"),
                    "ended_at": timestamp,
                },
                deterministic_id(path, "PostToolUse", call_id),
                timestamp,
            )
            continue

    return records


def chunks(records: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    bounded = max(1, size)
    for index in range(0, len(records), bounded):
        yield records[index : index + bounded]


def upload_records(
    records: list[dict[str, Any]],
    cfg: dict[str, Any],
    batch_size: int,
    *,
    ledger: BackfillLedger | None = None,
    collector_url: str | None = None,
    force: bool = False,
) -> dict[str, int]:
    summary = {
        "scanned": len(records),
        "skipped_local": 0,
        "skipped_existing": 0,
        "attempted": 0,
        "accepted": 0,
        "duplicates": 0,
    }
    for batch in chunks(records, batch_size):
        by_pk = {journal.record_pk(record): record for record in batch}
        pks = list(by_pk)
        if ledger is not None and collector_url is not None and not force:
            local_uploaded = ledger.uploaded_pks(collector_url, pks)
        else:
            local_uploaded = set()
        summary["skipped_local"] += len(local_uploaded)

        remote_check_pks = [pk for pk in pks if pk not in local_uploaded]
        if not remote_check_pks:
            continue

        existing = replay.existing_record_pks(remote_check_pks, cfg)
        if ledger is not None and collector_url is not None:
            ledger.mark_uploaded(collector_url, existing)
        missing = [by_pk[pk] for pk in remote_check_pks if pk not in existing]
        summary["skipped_existing"] += len(existing)
        if not missing:
            continue
        result = replay.upload_records(missing, cfg)
        if ledger is not None and collector_url is not None:
            ledger.mark_uploaded(collector_url, [journal.record_pk(record) for record in missing])
        summary["attempted"] += len(missing)
        summary["accepted"] += result["accepted"]
        summary["duplicates"] += result["duplicates"]
    return summary


def merge_counts(target: dict[str, Any], counts: dict[str, int]) -> None:
    for key, value in counts.items():
        target[key] = int(target.get(key, 0)) + int(value)


def update_type_counts(target: dict[str, int], records: list[dict[str, Any]]) -> None:
    for record in records:
        key = str(record.get("record_type") or "unknown")
        target[key] = int(target.get(key, 0)) + 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Codex ~/.codex/sessions transcript JSONL files.")
    parser.add_argument("--sessions-root", default=str(Path.home() / ".codex" / "sessions"))
    parser.add_argument("--config", default=os.environ.get("AI_WORKLOG_CONFIG") or str(journal.DEFAULT_CONFIG_PATH))
    parser.add_argument("--server-url", help="Collector /events endpoint. Overrides config.")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int, help="Maximum transcript files to process, after sorting newest first.")
    parser.add_argument("--upload-state", help="Local SQLite progress ledger. Defaults to ~/.ai-worklog/codex_backfill_state.sqlite3.")
    parser.add_argument("--force", action="store_true", help="Ignore local backfill progress and let the server deduplicate again.")
    parser.add_argument("--dry-run", action="store_true", help="Build records but do not upload.")
    args = parser.parse_args()

    cfg = journal.merged_config(Path(args.config).expanduser())
    cfg["capture"]["token_usage_from_transcript"] = False
    if args.server_url:
        cfg["server_url"] = args.server_url
    if args.upload_state:
        cfg["backfill_state_path"] = args.upload_state
    journal.environment_metadata = cached_environment_metadata

    paths = list(iter_transcript_paths(Path(args.sessions_root).expanduser()))
    paths.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    if args.limit is not None:
        paths = paths[: max(0, args.limit)]

    summary: dict[str, Any] = {
        "transcripts": len(paths),
        "transcripts_skipped_local": 0,
        "transcripts_failed": 0,
        "records": 0,
        "by_type": {},
        "dry_run": args.dry_run,
    }
    seen_dry_run: set[str] = set()

    if args.dry_run:
        for path in paths:
            try:
                records = []
                for record in events_from_transcript(path, cfg):
                    pk = journal.record_pk(record)
                    if pk in seen_dry_run:
                        continue
                    seen_dry_run.add(pk)
                    records.append(record)
            except Exception as exc:
                summary["transcripts_failed"] += 1
                print(f"skipping transcript {path}: {exc}", file=sys.stderr)
                continue
            summary["records"] += len(records)
            update_type_counts(summary["by_type"], records)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0

    if not cfg.get("server_url"):
        raise ValueError("server_url is required unless --dry-run is used")

    collector_url = replay.collector_key(cfg)
    ledger = BackfillLedger(upload_state_path(cfg))
    upload_summary = {
        "scanned": 0,
        "skipped_local": 0,
        "skipped_existing": 0,
        "attempted": 0,
        "accepted": 0,
        "duplicates": 0,
    }
    for path in paths:
        if not args.force and ledger.transcript_complete(collector_url, path):
            summary["transcripts_skipped_local"] += 1
            continue
        try:
            records = events_from_transcript(path, cfg)
        except Exception as exc:
            ledger.mark_transcript(collector_url, path, "error", error=str(exc))
            summary["transcripts_failed"] += 1
            print(f"skipping transcript {path}: {exc}", file=sys.stderr)
            continue

        summary["records"] += len(records)
        update_type_counts(summary["by_type"], records)
        try:
            result = upload_records(
                records,
                cfg,
                args.batch_size,
                ledger=ledger,
                collector_url=collector_url,
                force=args.force,
            )
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            ledger.mark_transcript(collector_url, path, "upload_error", error=str(exc))
            summary["upload"] = upload_summary
            summary["upload_error"] = str(exc)
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            print(f"codex backfill upload failed: {exc}", file=sys.stderr)
            return 1
        merge_counts(upload_summary, result)
        ledger.mark_transcript(collector_url, path, "complete", result)

    summary["upload"] = upload_summary

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"codex backfill failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
