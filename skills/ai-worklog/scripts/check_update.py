#!/usr/bin/env python3
"""Check whether the installed AI Worklog skill is behind the remote manifest."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import journal


DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60


def update_config(cfg: dict[str, Any]) -> dict[str, Any]:
    section = cfg.get("skill_update")
    return section if isinstance(section, dict) else {}


def enabled(cfg: dict[str, Any]) -> bool:
    section = update_config(cfg)
    if section.get("enabled") is False:
        return False
    return bool(manifest_url(cfg))


def manifest_url(cfg: dict[str, Any]) -> str | None:
    env_url = os.environ.get("AI_WORKLOG_UPDATE_MANIFEST_URL")
    if env_url:
        return env_url
    value = update_config(cfg).get("manifest_url")
    return str(value) if value else None


def current_version(cfg: dict[str, Any]) -> str:
    value = update_config(cfg).get("current_version")
    return str(value) if value else journal.VERSION


def state_path(cfg: dict[str, Any]) -> Path:
    explicit = update_config(cfg).get("state_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "skill_update_state.json"


def notice_path(cfg: dict[str, Any]) -> Path:
    explicit = update_config(cfg).get("notice_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "skill_update_notice.txt"


def interval_seconds(cfg: dict[str, Any]) -> int:
    value = update_config(cfg).get("trigger_interval_seconds")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS


def timeout_seconds(cfg: dict[str, Any]) -> float:
    value = update_config(cfg).get("request_timeout_seconds") or cfg.get("request_timeout_seconds")
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS


def should_run(cfg: dict[str, Any], force: bool = False, now: float | None = None) -> bool:
    if force:
        return True
    if not enabled(cfg):
        return False
    now = time.time() if now is None else now
    path = state_path(cfg)
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_checked = float(data.get("last_checked_epoch") or 0)
    except Exception:
        last_checked = path.stat().st_mtime
    return now - last_checked >= interval_seconds(cfg)


def fetch_manifest(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json,text/plain"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        value = {"version": body.strip()}
    if not isinstance(value, dict):
        raise ValueError("remote manifest must be a JSON object or plain version string")
    return value


def version_parts(version: str) -> list[int]:
    return [int(part) for part in re.findall(r"\d+", version)]


def version_relation(current: str, remote: str) -> str:
    if current == remote:
        return "equal"
    current_parts = version_parts(current)
    remote_parts = version_parts(remote)
    if not current_parts or not remote_parts:
        return "different"
    width = max(len(current_parts), len(remote_parts))
    current_padded = current_parts + [0] * (width - len(current_parts))
    remote_padded = remote_parts + [0] * (width - len(remote_parts))
    if remote_padded > current_padded:
        return "newer"
    if remote_padded < current_padded:
        return "older"
    return "different"


def update_available(current: str, manifest: dict[str, Any]) -> bool:
    remote = str(manifest.get("version") or "")
    if not remote:
        raise ValueError("remote manifest is missing version")
    relation = version_relation(current, remote)
    return relation in {"newer", "different"}


def source_url(cfg: dict[str, Any], manifest: dict[str, Any]) -> str | None:
    for key in ("install_url", "source_url", "url"):
        value = manifest.get(key)
        if value:
            return str(value)
    value = update_config(cfg).get("source_url")
    return str(value) if value else None


def notice_text(cfg: dict[str, Any], manifest: dict[str, Any]) -> str:
    current = current_version(cfg)
    remote = str(manifest.get("version") or "unknown")
    source = source_url(cfg, manifest)
    lines = [
        f"AI Worklog skill update available: installed {current}, remote {remote}.",
    ]
    if source:
        lines.append(f"Source: {source}")
    lines.append("Run check_update.py --force for details, then reinstall the skill from the source above.")
    return "\n".join(lines)


def write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_notice(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def clear_notice(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def check(cfg: dict[str, Any], force: bool = False) -> dict[str, Any]:
    if not should_run(cfg, force=force):
        return {"checked": False, "reason": "throttled"}
    url = manifest_url(cfg)
    if not url:
        return {"checked": False, "reason": "missing_manifest_url"}

    checked_at = journal.utc_now()
    try:
        manifest = fetch_manifest(url, timeout_seconds(cfg))
        current = current_version(cfg)
        available = update_available(current, manifest)
        remote_version = str(manifest.get("version") or "")
        state = {
            "checked": True,
            "last_checked_at": checked_at,
            "last_checked_epoch": time.time(),
            "current_version": current,
            "remote_version": remote_version,
            "manifest_url": url,
            "update_available": available,
            "source_url": source_url(cfg, manifest),
        }
        if available:
            text = notice_text(cfg, manifest)
            state["notice"] = text
            write_notice(notice_path(cfg), text)
        else:
            clear_notice(notice_path(cfg))
        write_state(state_path(cfg), state)
        return state
    except (OSError, ValueError, urllib.error.URLError) as exc:
        state = {
            "checked": True,
            "last_checked_at": checked_at,
            "last_checked_epoch": time.time(),
            "manifest_url": url,
            "update_available": False,
            "error": str(exc),
        }
        write_state(state_path(cfg), state)
        return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the remote AI Worklog skill version manifest.")
    parser.add_argument("--config", default=os.environ.get("AI_WORKLOG_CONFIG") or str(journal.DEFAULT_CONFIG_PATH))
    parser.add_argument("--force", action="store_true", help="Check now even if the throttle interval has not elapsed.")
    parser.add_argument("--quiet", action="store_true", help="Suppress normal output.")
    args = parser.parse_args()

    cfg = journal.merged_config(Path(args.config).expanduser())
    if not enabled(cfg):
        if not args.quiet:
            print("AI Worklog skill update check is disabled or missing a manifest URL.")
        return 0

    result = check(cfg, force=args.force)
    if args.quiet:
        return 0
    if not result.get("checked"):
        print(f"AI Worklog skill update check skipped: {result.get('reason')}")
    elif result.get("error"):
        print(f"AI Worklog skill update check failed: {result['error']}", file=sys.stderr)
        return 1
    elif result.get("update_available"):
        print(result.get("notice") or "AI Worklog skill update available.")
    else:
        print(f"AI Worklog skill is up to date: {result.get('current_version')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
