#!/usr/bin/env python3
"""Backfill Cursor agent transcript JSONL files into AI Worklog records."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

import journal
import platform_io
import replay


def default_transcripts_root() -> Path:
    return Path.home() / ".cursor" / "projects"


def iter_transcript_paths(root: Path) -> Iterable[Path]:
    root = root.expanduser()
    if root.is_file():
        yield root
        return
    if not root.exists():
        return
    yield from sorted(root.glob("**/agent-transcripts/**/*.jsonl"))


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8-sig") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc
            if isinstance(value, dict):
                yield line_no, value


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(part for part in parts if part)
    return ""


def tool_uses_from_content(content: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(content, list):
        return
    for item in content:
        if isinstance(item, dict) and item.get("type") in {"tool_use", "toolUse"}:
            yield item


def session_id_from_path(path: Path) -> str:
    parent = path.parent.name
    if parent:
        return parent
    return path.stem


def event_time(path: Path, line_no: int) -> str:
    # Cursor transcript lines usually do not carry timestamps. Use file mtime
    # plus a deterministic line offset so timeline ordering remains stable.
    base = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return (base + dt.timedelta(milliseconds=line_no)).isoformat().replace("+00:00", "Z")


def stable_event_id(path: Path, line_no: int, kind: str) -> str:
    return "cursor-backfill-" + journal.sha256_text(f"{path.resolve()}:{line_no}:{kind}")[:32]


def payloads_from_item(path: Path, line_no: int, item: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    session_id = session_id_from_path(path)
    common = {
        "session_id": session_id,
        "trace_id": session_id,
        "transcript_path": str(path),
        "cursor_backfill": True,
        "transcript_line": line_no,
        "timestamp": event_time(path, line_no),
        "sequence_no": line_no,
    }
    role = item.get("role")
    message = item.get("message") if isinstance(item.get("message"), dict) else {}
    content = message.get("content") if isinstance(message, dict) else item.get("content")
    text = text_from_content(content)

    if role == "user" and text:
        yield "user", {**common, "hook_event_name": "beforeSubmitPrompt", "prompt": text}

    if role == "assistant":
        if text:
            yield "assistant", {**common, "hook_event_name": "afterAgentResponse", "response": text}
        for index, tool_use in enumerate(tool_uses_from_content(content), start=1):
            tool_name = tool_use.get("name") or tool_use.get("tool_name") or tool_use.get("toolName") or "unknown"
            tool_input = tool_use.get("input") if "input" in tool_use else tool_use.get("args")
            yield (
                f"tool-{index}",
                {
                    **common,
                    "hook_event_name": "postToolUse",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_call_id": tool_use.get("id") or tool_use.get("tool_call_id"),
                },
            )

    if item.get("type") == "turn_ended":
        yield (
            "turn-ended",
            {
                **common,
                "hook_event_name": "stop",
                "status": item.get("status"),
                "success": item.get("status") in {None, "success", "completed"},
            },
        )


def existing_event_ids(cfg: dict[str, Any]) -> set[str]:
    local_log_dir = Path(str(cfg.get("local_log_dir") or journal.DEFAULT_HOME / "events")).expanduser()
    seen: set[str] = set()
    if not local_log_dir.exists():
        return seen
    for path in sorted(local_log_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8-sig") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("event_id"), str):
                    seen.add(record["event_id"])
    return seen


def build_backfill_records(path: Path, cfg: dict[str, Any], existing_ids: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    for line_no, item in iter_jsonl(path):
        for kind, payload in payloads_from_item(path, line_no, item):
            event_id = stable_event_id(path, line_no, kind)
            if event_id in existing_ids:
                continue
            event, event_snapshots = journal.build_records(payload, cfg, "cursor", "cursor-backfill")
            if event is None:
                continue
            event["event_id"] = event_id
            event["received_at"] = payload["timestamp"]
            event["timeline"]["span_id"] = event_id
            event["timeline"]["started_at"] = payload["timestamp"]
            event["backfill"] = {
                "source": "cursor_agent_transcript",
                "transcript_path": str(path),
                "line": line_no,
            }
            events.append(event)
            snapshots.extend(event_snapshots)
            existing_ids.add(event_id)
    return events, snapshots


def backfill(root: Path, cfg: dict[str, Any], *, limit: int | None = None, dry_run: bool = False) -> dict[str, int]:
    paths = list(iter_transcript_paths(root))
    if limit is not None:
        paths = paths[: max(0, limit)]
    existing_ids = existing_event_ids(cfg)
    summary = {"transcripts": len(paths), "events": 0, "snapshots": 0}
    local_log_dir = Path(str(cfg.get("local_log_dir") or journal.DEFAULT_HOME / "events")).expanduser()
    for path in paths:
        events, snapshots = build_backfill_records(path, cfg, existing_ids)
        summary["events"] += len(events)
        if dry_run:
            continue
        upload_candidates = journal.write_new_snapshots(snapshots, cfg)
        summary["snapshots"] += len(upload_candidates)
        for event in events:
            journal.append_jsonl(local_log_dir, event)
    return summary


def main() -> int:
    platform_io.configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Backfill Cursor ~/.cursor/projects agent transcript JSONL files.")
    parser.add_argument("--transcripts-root", default=str(default_transcripts_root()))
    parser.add_argument("--config", default=str(journal.DEFAULT_CONFIG_PATH))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upload", action="store_true", help="Run replay after writing local backfill records.")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    cfg = journal.merged_config(Path(args.config).expanduser())
    summary = backfill(Path(args.transcripts_root).expanduser(), cfg, limit=args.limit, dry_run=args.dry_run)
    if args.upload and not args.dry_run:
        upload_summary = replay.replay(cfg, args.batch_size)
        summary.update({f"upload_{key}": value for key, value in upload_summary.items()})
    print(journal.json_dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
