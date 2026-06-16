#!/usr/bin/env python3
"""Background trigger for Codex transcript backfill."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import journal


DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_LOCK_STALE_SECONDS = 6 * 60 * 60
DEFAULT_MAX_RUNTIME_SECONDS = 30 * 60


def backfill_config(cfg: dict[str, Any]) -> dict[str, Any]:
    value = cfg.get("codex_history_backfill")
    return value if isinstance(value, dict) else {}


def enabled(cfg: dict[str, Any]) -> bool:
    section = backfill_config(cfg)
    if section.get("enabled") is False:
        return False
    return bool(cfg.get("server_url"))


def state_path(cfg: dict[str, Any]) -> Path:
    explicit = backfill_config(cfg).get("trigger_state_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "codex_backfill_trigger.json"


def lock_path(cfg: dict[str, Any]) -> Path:
    explicit = backfill_config(cfg).get("lock_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "codex_backfill.lock"


def log_path(cfg: dict[str, Any]) -> Path:
    explicit = backfill_config(cfg).get("log_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "codex_backfill.log"


def interval_seconds(cfg: dict[str, Any]) -> int:
    value = backfill_config(cfg).get("trigger_interval_seconds")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS


def lock_stale_seconds(cfg: dict[str, Any]) -> int:
    value = backfill_config(cfg).get("lock_stale_seconds")
    try:
        return max(60, int(value))
    except (TypeError, ValueError):
        return DEFAULT_LOCK_STALE_SECONDS


def max_runtime_seconds(cfg: dict[str, Any]) -> int:
    value = backfill_config(cfg).get("max_runtime_seconds")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RUNTIME_SECONDS


def should_run(cfg: dict[str, Any], now: float | None = None) -> bool:
    if not enabled(cfg):
        return False
    now = time.time() if now is None else now
    path = state_path(cfg)
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_started = float(data.get("last_started_epoch") or 0)
    except Exception:
        last_started = path.stat().st_mtime
    return now - last_started >= interval_seconds(cfg)


def acquire_lock(path: Path, stale_seconds: int) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age < stale_seconds:
            return None
        try:
            path.unlink()
        except OSError:
            return None
    try:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None


def mark_started(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_started_at": journal.utc_now(),
        "last_started_epoch": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def run_backfill(config_path: Path, cfg: dict[str, Any]) -> int:
    section = backfill_config(cfg)
    script = Path(__file__).resolve().with_name("codex_backfill.py")
    command = [
        sys.executable or "python3",
        str(script),
        "--config",
        str(config_path),
        "--batch-size",
        str(section.get("batch_size") or 250),
    ]
    if section.get("limit") is not None:
        command.extend(["--limit", str(section["limit"])])
    if section.get("upload_state"):
        command.extend(["--upload-state", str(Path(str(section["upload_state"])).expanduser())])

    log = log_path(cfg)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n[{journal.utc_now()}] starting codex history backfill\n")
        fh.flush()
        try:
            completed = subprocess.run(
                command,
                stdout=fh,
                stderr=fh,
                check=False,
                timeout=max_runtime_seconds(cfg),
            )
        except subprocess.TimeoutExpired:
            fh.write(f"[{journal.utc_now()}] backfill timed out\n")
            return 124
        fh.write(f"[{journal.utc_now()}] backfill exited {completed.returncode}\n")
        return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger Codex history backfill in the background.")
    parser.add_argument("--config", default=os.environ.get("AI_WORKLOG_CONFIG") or str(journal.DEFAULT_CONFIG_PATH))
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    cfg = journal.merged_config(config_path)
    if not should_run(cfg):
        return 0

    lock = lock_path(cfg)
    fd = acquire_lock(lock, lock_stale_seconds(cfg))
    if fd is None:
        return 0
    try:
        os.write(fd, f"pid={os.getpid()} started={journal.utc_now()}\n".encode("utf-8"))
        os.close(fd)
        mark_started(state_path(cfg))
        return run_backfill(config_path, cfg)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
