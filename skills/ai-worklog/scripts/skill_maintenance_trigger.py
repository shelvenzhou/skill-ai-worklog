#!/usr/bin/env python3
"""SessionStart background maintenance for AI Worklog.

This trigger repairs local hook wiring when skill files were updated without
running the installer, and optionally applies remote skill updates.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import check_update
import journal
import platform_io
import skill_release
import surfaces
import update_skill


DEFAULT_LOCK_STALE_SECONDS = 30 * 60
DEFAULT_LOCK_WAIT_SECONDS = 0
DEFAULT_MAX_RUNTIME_SECONDS = 10 * 60
DEFAULT_AUTO_UPDATE_INTERVAL_SECONDS = 24 * 60 * 60


def update_config(cfg: dict[str, Any]) -> dict[str, Any]:
    section = cfg.get("skill_update")
    return section if isinstance(section, dict) else {}


def self_heal_enabled(cfg: dict[str, Any]) -> bool:
    section = update_config(cfg)
    return section.get("self_heal_enabled") is not False


def auto_update_enabled(cfg: dict[str, Any]) -> bool:
    section = update_config(cfg)
    return section.get("enabled") is not False and section.get("auto_update") is True


def lock_path(cfg: dict[str, Any]) -> Path:
    explicit = update_config(cfg).get("maintenance_lock_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "skill_maintenance.lock"


def log_path(cfg: dict[str, Any]) -> Path:
    explicit = update_config(cfg).get("maintenance_log_path")
    if explicit:
        return Path(str(explicit)).expanduser()
    return journal.DEFAULT_HOME / "skill_maintenance.log"


def max_runtime_seconds(cfg: dict[str, Any]) -> int:
    value = update_config(cfg).get("maintenance_max_runtime_seconds")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RUNTIME_SECONDS


def lock_stale_seconds(cfg: dict[str, Any]) -> int:
    value = update_config(cfg).get("maintenance_lock_stale_seconds")
    try:
        return max(60, int(value))
    except (TypeError, ValueError):
        return DEFAULT_LOCK_STALE_SECONDS


def lock_wait_seconds(cfg: dict[str, Any]) -> int:
    value = update_config(cfg).get("maintenance_lock_wait_seconds")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_LOCK_WAIT_SECONDS


def auto_update_interval_seconds(cfg: dict[str, Any]) -> int:
    value = update_config(cfg).get("auto_update_interval_seconds")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_AUTO_UPDATE_INTERVAL_SECONDS


def acquire_lock(path: Path, stale_seconds: int, wait_seconds: int = 0) -> int | None:
    deadline = time.time() + max(0, wait_seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0
            if age >= stale_seconds:
                try:
                    path.unlink()
                except OSError:
                    pass
                else:
                    continue
            if time.time() >= deadline:
                return None
            time.sleep(0.5)


def hook_command_from_entry(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    command = str(entry.get("command") or entry.get("commandWindows") or "")
    if is_worklog_journal_command(command):
        return command
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return None
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = str(hook.get("command") or hook.get("commandWindows") or "")
        if is_worklog_journal_command(command):
            return command
    return None


def is_worklog_journal_command(command: str) -> bool:
    return "ai-worklog" in command and ("journal.py" in command or "ai-worklog-hook" in command)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(platform_io.read_text(path, encoding="utf-8-sig"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def event_has_worklog_hook(path: Path, event_name: str) -> bool:
    doc = load_json(path)
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(event_name)
    if not isinstance(entries, list):
        return False
    return any(hook_command_from_entry(entry) for entry in entries)


def codex_hooks_enabled(path: Path) -> bool:
    if not path.exists():
        return False
    text = platform_io.read_text(path, encoding="utf-8-sig", errors="replace")
    in_features = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[features]":
            in_features = True
            continue
        if in_features and stripped.startswith("[") and stripped.endswith("]"):
            return False
        if in_features and stripped.replace(" ", "") == "hooks=true":
            return True
    return False


def selected_specs(selection: str) -> list[surfaces.SurfaceSpec]:
    if selection in {"codex", "cursor", "both"}:
        return surfaces.surface_specs(selection)
    return surfaces.surface_specs("both")


def missing_hook_repair_needed(selection: str, cfg: dict[str, Any]) -> bool:
    hook_set = str(cfg.get("hook_set") or "minimal")
    for spec in selected_specs(selection):
        if any(not event_has_worklog_hook(spec.hooks_path, event) for event in spec.events(hook_set)):
            return True
        if spec.config_toml_path is not None and not codex_hooks_enabled(spec.config_toml_path):
            return True
    return False


def version_repair_needed(cfg: dict[str, Any]) -> bool:
    current = update_config(cfg).get("current_version")
    return bool(current) and str(current) != skill_release.VERSION


def self_heal_needed(selection: str, cfg: dict[str, Any]) -> bool:
    return self_heal_enabled(cfg) and (version_repair_needed(cfg) or missing_hook_repair_needed(selection, cfg))


def install_command(selection: str, cfg: dict[str, Any], config_path: Path) -> list[str]:
    manifest_url_value = update_skill.manifest_url(cfg)
    source = update_config(cfg).get("source_url")
    script = Path(__file__).resolve().with_name("install.py")
    command = [sys.executable or "python3", str(script), *update_skill.installer_args(cfg, selection, manifest_url_value, str(source) if source else None)]
    command.extend(["--smoke-test"])
    return command


def run_logged(command: list[str], cfg: dict[str, Any], label: str) -> int:
    log = log_path(cfg)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n[{journal.utc_now()}] starting {label}: {' '.join(command)}\n")
        fh.flush()
        try:
            completed = subprocess.run(
                command,
                stdout=fh,
                stderr=fh,
                check=False,
                env=platform_io.utf8_subprocess_env(),
                timeout=max_runtime_seconds(cfg),
            )
        except subprocess.TimeoutExpired:
            fh.write(f"[{journal.utc_now()}] {label} timed out\n")
            return 124
        fh.write(f"[{journal.utc_now()}] {label} exited {completed.returncode}\n")
        return int(completed.returncode)


def should_auto_update_attempt(cfg: dict[str, Any], state: dict[str, Any], now: float | None = None) -> bool:
    if not auto_update_enabled(cfg):
        return False
    now = time.time() if now is None else now
    try:
        last_attempt = float(state.get("last_auto_update_attempt_epoch") or 0)
    except (TypeError, ValueError):
        last_attempt = 0
    return now - last_attempt >= auto_update_interval_seconds(cfg)


def mark_auto_update_attempt(state_file: Path, state: dict[str, Any]) -> None:
    state["last_auto_update_attempt_at"] = journal.utc_now()
    state["last_auto_update_attempt_epoch"] = time.time()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    platform_io.write_text(state_file, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def maybe_run_auto_update(selection: str, cfg: dict[str, Any], config_path: Path) -> int:
    if not check_update.enabled(cfg):
        return 0
    check_script = Path(__file__).resolve().with_name("check_update.py")
    check_rc = run_logged([sys.executable or "python3", str(check_script), "--config", str(config_path), "--quiet"], cfg, "skill update check")
    if check_rc != 0:
        return check_rc

    state_file = check_update.state_path(cfg)
    state = load_json(state_file)
    if state.get("update_available") is not True or not should_auto_update_attempt(cfg, state):
        return 0

    mark_auto_update_attempt(state_file, state)
    update_script = Path(__file__).resolve().with_name("update_skill.py")
    update_rc = run_logged([sys.executable or "python3", str(update_script), "--surface", selection], cfg, "skill auto update")
    if update_rc == 0:
        run_logged([sys.executable or "python3", str(check_script), "--config", str(config_path), "--force", "--quiet"], cfg, "post-update skill check")
    return update_rc


def run_maintenance(selection: str, config_path: Path) -> int:
    cfg = journal.merged_config(config_path)
    lock = lock_path(cfg)
    fd = acquire_lock(lock, lock_stale_seconds(cfg), lock_wait_seconds(cfg))
    if fd is None:
        return 0
    try:
        os.write(fd, f"pid={os.getpid()} started={journal.utc_now()}\n".encode("utf-8"))
        os.close(fd)
        if self_heal_needed(selection, cfg):
            rc = run_logged(install_command(selection, cfg, config_path), cfg, "skill self-heal install")
            if rc != 0:
                return rc
            cfg = journal.merged_config(config_path)
        return maybe_run_auto_update(selection, cfg, config_path)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def main() -> int:
    platform_io.configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Run AI Worklog SessionStart maintenance in the background.")
    parser.add_argument("--surface", choices=["codex", "cursor", "both"], default="both")
    parser.add_argument("--config", default=os.environ.get("AI_WORKLOG_CONFIG") or str(journal.DEFAULT_CONFIG_PATH))
    args = parser.parse_args()
    return run_maintenance(args.surface, Path(args.config).expanduser())


if __name__ == "__main__":
    raise SystemExit(main())
