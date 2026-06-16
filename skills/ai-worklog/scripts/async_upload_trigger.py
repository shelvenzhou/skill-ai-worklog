#!/usr/bin/env python3
"""Background uploader trigger for locally spooled AI Worklog records."""

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


def upload_config(cfg: dict[str, Any]) -> dict[str, Any]:
    value = cfg.get("async_upload")
    return value if isinstance(value, dict) else {}


def enabled(cfg: dict[str, Any]) -> bool:
    section = upload_config(cfg)
    if section.get("enabled") is False:
        return False
    return bool(cfg.get("server_url")) and journal.upload_mode(cfg) == "async"


def state_path(cfg: dict[str, Any]) -> Path:
    explicit = upload_config(cfg).get("trigger_state_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "async_upload_trigger.json"


def lock_path(cfg: dict[str, Any]) -> Path:
    explicit = upload_config(cfg).get("lock_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "async_upload.lock"


def log_path(cfg: dict[str, Any]) -> Path:
    explicit = upload_config(cfg).get("log_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "async_upload.log"


def interval_seconds(cfg: dict[str, Any]) -> int:
    value = upload_config(cfg).get("trigger_interval_seconds")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return journal.DEFAULT_ASYNC_UPLOAD_INTERVAL_SECONDS


def lock_stale_seconds(cfg: dict[str, Any]) -> int:
    value = upload_config(cfg).get("lock_stale_seconds")
    try:
        return max(60, int(value))
    except (TypeError, ValueError):
        return journal.DEFAULT_ASYNC_UPLOAD_LOCK_STALE_SECONDS


def max_runtime_seconds(cfg: dict[str, Any]) -> int:
    value = upload_config(cfg).get("max_runtime_seconds")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return journal.DEFAULT_ASYNC_UPLOAD_MAX_RUNTIME_SECONDS


def batch_size(cfg: dict[str, Any]) -> int:
    value = upload_config(cfg).get("batch_size")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 100


def upload_state_path(cfg: dict[str, Any]) -> Path | None:
    explicit = upload_config(cfg).get("upload_state")
    if explicit:
        return Path(str(explicit)).expanduser()
    return None


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


def run_upload(config_path: Path, cfg: dict[str, Any]) -> int:
    script = Path(__file__).resolve().with_name("replay.py")
    command = [
        sys.executable or "python3",
        str(script),
        "--config",
        str(config_path),
        "--batch-size",
        str(batch_size(cfg)),
    ]
    upload_state = upload_state_path(cfg)
    if upload_state is not None:
        command.extend(["--upload-state", str(upload_state)])

    log = log_path(cfg)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n[{journal.utc_now()}] starting async upload replay\n")
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
            fh.write(f"[{journal.utc_now()}] async upload timed out\n")
            return 124
        fh.write(f"[{journal.utc_now()}] async upload exited {completed.returncode}\n")
        return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger AI Worklog background upload replay.")
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
        return run_upload(config_path, cfg)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
