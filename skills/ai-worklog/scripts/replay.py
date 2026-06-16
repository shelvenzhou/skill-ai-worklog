#!/usr/bin/env python3
"""Replay local AI Worklog JSONL records to a collector in batches."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import journal


FAILED_UPLOAD_KEYS = {"upload_error", "upload_failed_at"}


def iter_jsonl_records(directory: Path) -> Iterable[dict[str, Any]]:
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_no}: {exc}") from exc
                if not isinstance(record, dict):
                    raise ValueError(f"{path}:{line_no}: expected a JSON object")
                yield normalize_record(record)


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    clean = dict(record)
    for key in FAILED_UPLOAD_KEYS:
        clean.pop(key, None)
    return clean


def load_replay_records(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    directories = [
        Path(str(cfg.get("snapshot_log_dir") or journal.DEFAULT_HOME / "snapshots")).expanduser(),
        Path(str(cfg.get("local_log_dir") or journal.DEFAULT_HOME / "events")).expanduser(),
        Path(str(cfg.get("failed_log_dir") or journal.DEFAULT_HOME / "failed")).expanduser(),
    ]
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in directories:
        for record in iter_jsonl_records(directory):
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
    with urllib.request.urlopen(request, timeout=float(cfg.get("request_timeout_seconds") or 2.0)) as response:
        if not (200 <= response.status < 300):
            raise RuntimeError(f"preflight failed with HTTP {response.status}")
        result = json.loads(response.read().decode("utf-8"))
    existing = result.get("existing") if isinstance(result, dict) else None
    if not isinstance(existing, list):
        raise RuntimeError("preflight response missing existing list")
    return {str(item) for item in existing}


def upload_records(records: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, int]:
    server_url = cfg.get("server_url")
    if not server_url:
        raise ValueError("server_url is required; pass --server-url or set AI_WORKLOG_SERVER_URL")

    payload = json.dumps(records, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(str(server_url), data=payload, headers=journal.upload_headers(cfg), method="POST")
    with urllib.request.urlopen(request, timeout=float(cfg.get("request_timeout_seconds") or 2.0)) as response:
        if not (200 <= response.status < 300):
            raise RuntimeError(f"upload failed with HTTP {response.status}")
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("upload response must be a JSON object")
    return {
        "accepted": int(result.get("accepted") or 0),
        "duplicates": int(result.get("duplicates") or 0),
    }


def replay(cfg: dict[str, Any], batch_size: int) -> dict[str, int]:
    records = load_replay_records(cfg)
    summary = {
        "scanned": len(records),
        "skipped_existing": 0,
        "attempted": 0,
        "accepted": 0,
        "duplicates": 0,
    }
    for batch in chunks(records, batch_size):
        pks = [journal.record_pk(record) for record in batch]
        existing = existing_record_pks(pks, cfg)
        missing = [record for record in batch if journal.record_pk(record) not in existing]
        summary["skipped_existing"] += len(batch) - len(missing)
        if not missing:
            continue
        result = upload_records(missing, cfg)
        summary["attempted"] += len(missing)
        summary["accepted"] += result["accepted"]
        summary["duplicates"] += result["duplicates"]
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay local AI Worklog events, snapshots, and failed uploads.")
    parser.add_argument(
        "--config",
        default=os.environ.get("AI_WORKLOG_CONFIG") or os.environ.get("AI_USAGE_COLLECTOR_CONFIG") or str(journal.DEFAULT_CONFIG_PATH),
    )
    parser.add_argument("--server-url", help="Collector /events endpoint. Overrides config and AI_WORKLOG_SERVER_URL.")
    parser.add_argument("--batch-size", type=int, default=100, help="Records per preflight/upload request.")
    args = parser.parse_args()

    cfg = journal.merged_config(Path(args.config).expanduser())
    if args.server_url:
        cfg["server_url"] = args.server_url
    try:
        summary = replay(cfg, args.batch_size)
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
