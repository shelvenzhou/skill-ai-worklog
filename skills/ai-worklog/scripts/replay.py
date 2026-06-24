#!/usr/bin/env python3
"""Replay local AI Worklog JSONL records to a collector in batches."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import journal
import platform_io


FAILED_UPLOAD_KEYS = {"upload_error", "upload_failed_at"}


def quarantine_dir(cfg: dict[str, Any]) -> Path:
    explicit = cfg.get("quarantine_log_dir")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "quarantine"


def quarantine_invalid_line(path: Path, line_no: int, line: str, error: str, cfg: dict[str, Any]) -> None:
    record = {
        "record_type": "invalid_jsonl_line",
        "source_path": str(path),
        "line_no": line_no,
        "error": error,
        "captured_at": journal.utc_now(),
        "raw_line": line.rstrip("\n"),
    }
    journal.append_jsonl(quarantine_dir(cfg), record)


def iter_jsonl_records(
    directory: Path,
    *,
    cfg: dict[str, Any] | None = None,
    invalid_stats: dict[str, int] | None = None,
) -> Iterable[dict[str, Any]]:
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.jsonl")):
        with path.open("r", encoding="utf-8-sig") as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    if invalid_stats is not None:
                        invalid_stats["invalid_lines"] = invalid_stats.get("invalid_lines", 0) + 1
                    if cfg is not None:
                        quarantine_invalid_line(path, line_no, line, str(exc), cfg)
                    continue
                if not isinstance(record, dict):
                    if invalid_stats is not None:
                        invalid_stats["invalid_lines"] = invalid_stats.get("invalid_lines", 0) + 1
                    if cfg is not None:
                        quarantine_invalid_line(path, line_no, line, "expected a JSON object", cfg)
                    continue
                yield normalize_record(record)


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    clean = dict(record)
    for key in FAILED_UPLOAD_KEYS:
        clean.pop(key, None)
    return clean


def load_replay_records(cfg: dict[str, Any], invalid_stats: dict[str, int] | None = None) -> list[dict[str, Any]]:
    directories = [
        Path(str(cfg.get("snapshot_log_dir") or journal.DEFAULT_HOME / "snapshots")).expanduser(),
        Path(str(cfg.get("local_log_dir") or journal.DEFAULT_HOME / "events")).expanduser(),
        Path(str(cfg.get("failed_log_dir") or journal.DEFAULT_HOME / "failed")).expanduser(),
    ]
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in directories:
        for record in iter_jsonl_records(directory, cfg=cfg, invalid_stats=invalid_stats):
            pk = journal.record_pk(record)
            if pk in seen:
                continue
            seen.add(pk)
            records.append(record)
    return records


def chunks(records: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    bounded = max(1, size)
    for index in range(0, len(records), bounded):
        yield records[index : index + bounded]


def upload_state_path(cfg: dict[str, Any]) -> Path:
    explicit = cfg.get("upload_state_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "upload_state.sqlite3"


def collector_key(cfg: dict[str, Any]) -> str:
    server_url = cfg.get("server_url")
    if not server_url:
        raise ValueError("server_url is required; pass --server-url or set AI_WORKLOG_SERVER_URL")
    return str(server_url).rstrip("/")


class UploadLedger:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists uploaded_records (
                  collector_url text not null,
                  record_pk text not null,
                  uploaded_at text not null,
                  primary key (collector_url, record_pk)
                )
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


def existing_record_pks(record_pks: list[str], cfg: dict[str, Any]) -> set[str]:
    if not cfg.get("upload_preflight", True):
        return set()
    server_url = cfg.get("server_url")
    if not server_url:
        return set()
    payload = {"record_pks": record_pks}
    request = urllib.request.Request(
        journal.preflight_url(str(server_url)),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=journal.upload_headers(cfg),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=journal.request_timeout_seconds(cfg)) as response:
        if not (200 <= response.status < 300):
            raise RuntimeError(f"preflight failed with HTTP {response.status}")
        result = json.loads(response.read().decode("utf-8"))
    existing = result.get("existing") if isinstance(result, dict) else None
    if not isinstance(existing, list):
        raise RuntimeError("preflight response missing existing list")
    return {str(item) for item in existing}


def upload_records(records: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, int]:
    server_url = collector_key(cfg)

    payload = journal.json_dumps(records, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(server_url, data=payload, headers=journal.upload_headers(cfg), method="POST")
    with urllib.request.urlopen(request, timeout=journal.request_timeout_seconds(cfg)) as response:
        if not (200 <= response.status < 300):
            raise RuntimeError(f"upload failed with HTTP {response.status}")
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("upload response must be a JSON object")
    return {
        "accepted": int(result.get("accepted") or 0),
        "duplicates": int(result.get("duplicates") or 0),
    }


def replay(cfg: dict[str, Any], batch_size: int, *, force: bool = False) -> dict[str, int]:
    collector_url = collector_key(cfg)
    ledger = UploadLedger(upload_state_path(cfg))
    invalid_stats = {"invalid_lines": 0}
    records = load_replay_records(cfg, invalid_stats)
    summary = {
        "scanned": len(records),
        "invalid_lines": invalid_stats["invalid_lines"],
        "skipped_local": 0,
        "skipped_existing": 0,
        "attempted": 0,
        "accepted": 0,
        "duplicates": 0,
    }
    for batch in chunks(records, batch_size):
        batch_by_pk = {journal.record_pk(record): record for record in batch}
        pks = list(batch_by_pk.keys())
        if force:
            local_uploaded: set[str] = set()
        else:
            local_uploaded = ledger.uploaded_pks(collector_url, pks)
        summary["skipped_local"] += len(local_uploaded)
        remote_check_pks = [pk for pk in pks if pk not in local_uploaded]
        if not remote_check_pks:
            continue
        existing = existing_record_pks(remote_check_pks, cfg)
        ledger.mark_uploaded(collector_url, existing)
        missing = [batch_by_pk[pk] for pk in remote_check_pks if pk not in existing]
        summary["skipped_existing"] += len(existing)
        if not missing:
            continue
        result = upload_records(missing, cfg)
        ledger.mark_uploaded(collector_url, [journal.record_pk(record) for record in missing])
        summary["attempted"] += len(missing)
        summary["accepted"] += result["accepted"]
        summary["duplicates"] += result["duplicates"]
    return summary


def main() -> int:
    platform_io.configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Replay local AI Worklog events, snapshots, and failed uploads.")
    parser.add_argument(
        "--config",
        default=os.environ.get("AI_WORKLOG_CONFIG") or os.environ.get("AI_USAGE_COLLECTOR_CONFIG") or str(journal.DEFAULT_CONFIG_PATH),
    )
    parser.add_argument("--server-url", help="Collector /events endpoint. Overrides config and AI_WORKLOG_SERVER_URL.")
    parser.add_argument("--batch-size", type=int, default=100, help="Records per preflight/upload request.")
    parser.add_argument("--upload-state", help="Local SQLite upload ledger path. Defaults to ~/.ai-worklog/upload_state.sqlite3.")
    parser.add_argument("--force", action="store_true", help="Ignore the local upload ledger and let the server deduplicate.")
    args = parser.parse_args()

    cfg = journal.merged_config(Path(args.config).expanduser())
    if args.server_url:
        cfg["server_url"] = args.server_url
    if args.upload_state:
        cfg["upload_state_path"] = args.upload_state
    try:
        summary = replay(cfg, args.batch_size, force=args.force)
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
