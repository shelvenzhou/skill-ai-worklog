#!/usr/bin/env python3
"""Install or uninstall AI worklog hooks for Codex and Cursor."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import platform_io
import platforms
import skill_release
import surfaces

SKILL_NAME = skill_release.NAME
SKILL_VERSION = skill_release.VERSION
CONFIG_HOME = Path.home() / ".ai-worklog"
CONFIG_PATH = CONFIG_HOME / "config.json"
DEFAULT_BACKFILL_MAX_RUNTIME_SECONDS = 30 * 60
DEFAULT_BACKFILL_LOCK_WAIT_SECONDS = 30
DEFAULT_ASYNC_UPLOAD_INTERVAL_SECONDS = 60
DEFAULT_ASYNC_UPLOAD_LOCK_STALE_SECONDS = 10 * 60
DEFAULT_ASYNC_UPLOAD_MAX_RUNTIME_SECONDS = 2 * 60
DEFAULT_ASYNC_UPLOAD_LOCK_WAIT_SECONDS = 30
DEFAULT_SKILL_UPDATE_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_SKILL_UPDATE_MANIFEST_URL = (
    str(skill_release.MANIFEST.get("remote_manifest_url") or "")
    or "https://raw.githubusercontent.com/shelvenzhou/skill-ai-worklog/master/skills/ai-worklog/skill-version.json"
)
DEFAULT_SKILL_SOURCE_URL = (
    str(skill_release.MANIFEST.get("install_url") or "")
    or "https://github.com/shelvenzhou/skill-ai-worklog/tree/master/skills/ai-worklog"
)
CODEX_MINIMAL_EVENTS = surfaces.CODEX_MINIMAL_EVENTS
CODEX_FULL_EVENTS = surfaces.CODEX_FULL_EVENTS
CURSOR_MINIMAL_EVENTS = surfaces.CURSOR_MINIMAL_EVENTS
CURSOR_FULL_EVENTS = surfaces.CURSOR_FULL_EVENTS


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(platform_io.read_text(path, encoding="utf-8-sig"))
    except Exception as exc:
        backup = platform_io.backup_existing(path)
        print(f"Backed up unreadable JSON {path} to {backup}: {exc}")
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict[str, Any], dry_run: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    if dry_run:
        print(f"[dry-run] would write {path}:\n{data}")
        return
    backup = platform_io.backup_existing(path)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        platform_io.write_text(tmp_path, data + "\n")
        json.loads(platform_io.read_text(tmp_path, encoding="utf-8"))
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    if backup:
        print(f"Backed up existing JSON {path} to {backup}")


def source_skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def validate_skill_dir(path: Path) -> None:
    required = [
        path / "SKILL.md",
        path / "skill-version.json",
        path / "scripts" / "journal.py",
        path / "scripts" / "install.py",
    ]
    missing = [str(item) for item in required if not item.exists()]
    if missing:
        raise ValueError(f"skill directory is missing required files: {', '.join(missing)}")


def replace_skill_from_source(src: Path, destination: Path, dry_run: bool, label: str | None = None) -> Path:
    src = src.expanduser().resolve()
    dest = destination.expanduser()
    if dest.exists() and src == dest.resolve():
        validate_skill_dir(dest)
        return dest
    if dry_run:
        print(f"[dry-run] would copy {src} to {dest}")
        return dest
    validate_skill_dir(src)
    backup_root = CONFIG_HOME / "backups" / "skills"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    with tempfile.TemporaryDirectory(prefix=f"{SKILL_NAME}-install-") as tmp:
        staged = Path(tmp) / SKILL_NAME
        shutil.copytree(src, staged, ignore=ignore)
        validate_skill_dir(staged)
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if dest.exists():
            backup = platform_io.backup_path(dest, backup_root=backup_root, label=label or dest.name)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(backup))
            print(f"Backed up existing skill {dest} to {backup}")
        try:
            shutil.move(str(staged), str(dest))
        except Exception:
            if backup is not None and backup.exists() and not dest.exists():
                shutil.move(str(backup), str(dest))
            raise
        ok, output = platforms.current_platform().repair_skill_acl(dest)
        if not ok:
            raise RuntimeError(f"failed to repair Windows ACL for {dest}: {output}")
    return dest


def copy_skill(destination: Path, dry_run: bool, label: str | None = None) -> Path:
    src = source_skill_dir()
    return replace_skill_from_source(src, destination, dry_run, label=label)


def cmd_file_literal(value: str) -> str:
    return platforms.cmd_file_literal(value)


def windows_config_literal(path: Path) -> str:
    return platforms.windows_config_literal(path)


def windows_python_launcher_lines(python: str, script_args: str) -> str:
    return platforms.windows_python_launcher_lines(python, script_args)


def write_windows_hook_launcher(skill_dir: Path, surface: str, config_path: Path) -> Path:
    return platforms.current_platform().write_windows_hook_launcher(skill_dir, surface, config_path, SKILL_NAME)


def python_command(skill_dir: Path, surface: str, config_path: Path) -> str:
    return platforms.current_platform().hook_command(skill_dir, surface, config_path, SKILL_NAME)


def shell_quote(value: str) -> str:
    return platforms.shell_quote(value)


def hook_command(hook: Any) -> str:
    if not isinstance(hook, dict):
        return ""
    return str(hook.get("command") or hook.get("commandWindows") or "")


def is_worklog_command(command: str) -> bool:
    return ("ai-worklog" in command or "ai-usage-collector" in command) and (
        "journal.py" in command
        or "ai-worklog-hook" in command
        or "collector.py" in command
        or "codex_backfill.py" in command
        or "codex_backfill_trigger.py" in command
    )


def is_worklog_hook(hook: Any) -> bool:
    return is_worklog_command(hook_command(hook))


def hook_entry(command: str, entry_style: str = "codex") -> dict[str, Any]:
    if entry_style == "cursor":
        return {"command": command}
    if entry_style != "codex":
        raise ValueError(f"unsupported hook entry style: {entry_style}")
    hook: dict[str, Any] = {
        "type": "command",
        "command": command,
    }
    if os.name == "nt":
        hook["commandWindows"] = command
    return {
        "hooks": [
            hook
        ]
    }


def cursor_entry_from_hook(hook: Any, inherited_matcher: Any = None) -> Any:
    command = hook_command(hook)
    if not command:
        return hook
    entry: dict[str, Any] = {"command": command}
    matcher = hook.get("matcher") if isinstance(hook, dict) else None
    if matcher is None:
        matcher = inherited_matcher
    if isinstance(matcher, str) and matcher:
        entry["matcher"] = matcher
    return entry


def entries_without_worklog(entry: Any, entry_style: str) -> tuple[list[Any], int]:
    if entry_style not in {"codex", "cursor"}:
        raise ValueError(f"unsupported hook entry style: {entry_style}")
    if not isinstance(entry, dict):
        return [entry], 0
    if is_worklog_hook(entry):
        return [], 1

    entry_hooks = entry.get("hooks")
    if not isinstance(entry_hooks, list):
        return [entry], 0

    removed = 0
    kept_hooks = []
    for hook in entry_hooks:
        if is_worklog_hook(hook):
            removed += 1
            continue
        kept_hooks.append(hook)

    if entry_style == "cursor":
        matcher = entry.get("matcher")
        return [cursor_entry_from_hook(hook, matcher) for hook in kept_hooks], removed

    if not kept_hooks:
        return [], removed
    updated = dict(entry)
    updated["hooks"] = kept_hooks
    return [updated], removed


def merge_hooks(
    path: Path,
    events: list[str],
    command: str,
    versioned: bool,
    dry_run: bool,
    entry_style: str = "codex",
) -> None:
    doc = read_json(path)
    if versioned:
        doc.setdefault("version", 1)
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        doc["hooks"] = hooks

    for existing_event in list(hooks.keys()):
        event_entries = hooks.get(existing_event)
        if not isinstance(event_entries, list):
            continue
        filtered = []
        for entry in event_entries:
            kept_entries, _removed = entries_without_worklog(entry, entry_style)
            filtered.extend(kept_entries)
        if filtered:
            hooks[existing_event] = filtered
        else:
            hooks.pop(existing_event, None)

    added = 0
    for event_name in events:
        event_entries = hooks.setdefault(event_name, [])
        if not isinstance(event_entries, list):
            event_entries = []
            hooks[event_name] = event_entries
        event_entries.append(hook_entry(command, entry_style))
        added += 1

    write_json(path, doc, dry_run)
    print(f"Installed {added} hook handlers in {path}")


def remove_hooks(path: Path, versioned: bool, dry_run: bool, entry_style: str = "codex") -> int:
    doc = read_json(path)
    if versioned and doc:
        doc.setdefault("version", 1)
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        print(f"No hook handlers found in {path}")
        return 0

    removed = 0
    for existing_event in list(hooks.keys()):
        event_entries = hooks.get(existing_event)
        if not isinstance(event_entries, list):
            continue
        filtered = []
        for entry in event_entries:
            kept_entries, removed_count = entries_without_worklog(entry, entry_style)
            removed += removed_count
            filtered.extend(kept_entries)
        if filtered:
            hooks[existing_event] = filtered
        else:
            hooks.pop(existing_event, None)

    if removed:
        write_json(path, doc, dry_run)
    else:
        print(f"No AI worklog hook handlers found in {path}")
    print(f"Removed {removed} hook handlers from {path}")
    return removed


def resolved_server_url(args: argparse.Namespace, cfg: dict[str, Any]) -> str | None:
    if args.clear_server_url:
        return None
    if args.server_url is not None:
        return args.server_url
    existing = cfg.get("server_url")
    return str(existing) if existing else None


def update_config(args: argparse.Namespace, dry_run: bool) -> dict[str, Any]:
    cfg = read_json(CONFIG_PATH)
    server_url = resolved_server_url(args, cfg)
    cfg.update(
        {
            "enabled": args.level != "off",
            "collection_level": args.level,
            "local_log_dir": str(Path(args.local_log_dir).expanduser()),
            "snapshot_log_dir": str(Path(args.snapshot_log_dir).expanduser()),
            "failed_log_dir": str(Path(args.failed_log_dir).expanduser()),
            "server_url": server_url,
            "api_key_env": args.api_key_env,
            "request_timeout_seconds": args.timeout,
            "upload_mode": "sync" if args.sync_upload else "async",
            "upload_preflight": not args.no_upload_preflight,
            "max_transcript_bytes": args.max_transcript_bytes,
            "hook_set": args.hook_set,
        }
    )
    capture = cfg.setdefault("capture", {})
    capture.update(
        {
            "raw_hook_input": args.level == "full",
            "prompt": args.level == "full",
            "response": args.level == "full",
            "tool_payloads": args.level == "full",
            "environment": True,
            "token_usage_from_transcript": True,
            "reasoning_summary": True,
            "raw_reasoning": False,
        }
    )
    cfg["codex_history_backfill"] = {
        "enabled": not args.no_auto_codex_backfill,
        "batch_size": args.backfill_batch_size,
        "trigger_interval_seconds": args.backfill_trigger_interval_seconds,
        "lock_stale_seconds": args.backfill_lock_stale_seconds,
        "lock_wait_seconds": args.backfill_lock_wait_seconds,
        "max_runtime_seconds": args.backfill_max_runtime_seconds,
    }
    cfg["async_upload"] = {
        "enabled": not args.sync_upload,
        "batch_size": args.async_upload_batch_size,
        "trigger_interval_seconds": args.async_upload_trigger_interval_seconds,
        "lock_stale_seconds": args.async_upload_lock_stale_seconds,
        "lock_wait_seconds": args.async_upload_lock_wait_seconds,
        "max_runtime_seconds": args.async_upload_max_runtime_seconds,
    }
    cfg["skill_update"] = {
        "enabled": not args.no_skill_update_check,
        "name": SKILL_NAME,
        "current_version": SKILL_VERSION,
        "manifest_url": args.skill_update_manifest_url,
        "source_url": args.skill_source_url,
        "trigger_interval_seconds": args.skill_update_trigger_interval_seconds,
    }
    if args.backfill_limit is not None:
        cfg["codex_history_backfill"]["limit"] = args.backfill_limit
    if args.backfill_upload_state:
        cfg["codex_history_backfill"]["upload_state"] = str(Path(args.backfill_upload_state).expanduser())
    write_json(CONFIG_PATH, cfg, dry_run)
    print(f"Configured worklog at {CONFIG_PATH}")
    return cfg


def disable_config(dry_run: bool) -> None:
    cfg = read_json(CONFIG_PATH)
    cfg.update(
        {
            "enabled": False,
            "collection_level": "off",
            "uninstalled": True,
        }
    )
    write_json(CONFIG_PATH, cfg, dry_run)
    print(f"Disabled worklog config at {CONFIG_PATH}")


def codex_home() -> Path:
    return surfaces.CODEX.home


def cursor_home() -> Path:
    return surfaces.CURSOR.home


def set_toml_feature(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    features_start: int | None = None
    features_end = len(lines)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[features]":
            features_start = idx
            continue
        if features_start is not None and idx > features_start and stripped.startswith("[") and stripped.endswith("]"):
            features_end = idx
            break
    feature_line = f"{key} = {value}"
    if features_start is None:
        prefix = text.rstrip()
        return (prefix + "\n\n" if prefix else "") + "[features]\n" + feature_line + "\n"

    for idx in range(features_start + 1, features_end):
        stripped = lines[idx].strip()
        if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
            lines[idx] = feature_line
            return "\n".join(lines) + "\n"
    lines.insert(features_start + 1, feature_line)
    return "\n".join(lines) + "\n"


AI_WORKLOG_TOML_HOOKS_BEGIN = "# BEGIN AI_WORKLOG_HOOKS"
AI_WORKLOG_TOML_HOOKS_END = "# END AI_WORKLOG_HOOKS"


def remove_ai_worklog_toml_hooks(text: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(AI_WORKLOG_TOML_HOOKS_BEGIN)}\n.*?\n{re.escape(AI_WORKLOG_TOML_HOOKS_END)}\n?",
        re.DOTALL,
    )
    return pattern.sub("\n", text).rstrip() + ("\n" if text.strip() else "")


def ensure_codex_hooks_feature(home: Path, dry_run: bool) -> None:
    config_path = home / "config.toml"
    existing = platform_io.read_text(config_path, encoding="utf-8-sig") if config_path.exists() else ""
    updated = set_toml_feature(existing, "hooks", "true")
    if updated == existing:
        print(f"Codex hooks feature already enabled in {config_path}")
        return
    if dry_run:
        print(f"[dry-run] would write {config_path}:\n{updated}")
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    platform_io.write_text_with_backup(config_path, updated)
    print(f"Enabled Codex hooks feature in {config_path}")


def remove_stale_codex_inline_hooks(home: Path, dry_run: bool) -> None:
    config_path = home / "config.toml"
    if not config_path.exists():
        return
    existing = platform_io.read_text(config_path, encoding="utf-8-sig") if config_path.exists() else ""
    updated = remove_ai_worklog_toml_hooks(existing)
    if updated == existing:
        return
    if dry_run:
        print(f"[dry-run] would remove stale Codex inline hooks from {config_path}")
    else:
        platform_io.write_text_with_backup(config_path, updated)
        print(f"Removed stale Codex inline hooks from {config_path}")


def install_codex(args: argparse.Namespace) -> Path:
    home = codex_home()
    skill_dir = copy_skill(home / "skills" / SKILL_NAME, args.dry_run, label="codex")
    command = python_command(skill_dir, "codex", CONFIG_PATH)
    events = CODEX_FULL_EVENTS if args.hook_set == "full" else CODEX_MINIMAL_EVENTS
    merge_hooks(home / "hooks.json", events, command, versioned=False, dry_run=args.dry_run)
    remove_stale_codex_inline_hooks(home, args.dry_run)
    ensure_codex_hooks_feature(home, args.dry_run)
    print(f"Codex skill path: {skill_dir}")
    return skill_dir


def install_cursor(args: argparse.Namespace) -> None:
    home = cursor_home()
    skill_dir = copy_skill(home / "skills" / SKILL_NAME, args.dry_run, label="cursor")
    command = python_command(skill_dir, "cursor", CONFIG_PATH)
    events = CURSOR_FULL_EVENTS if args.hook_set == "full" else CURSOR_MINIMAL_EVENTS
    merge_hooks(home / "hooks.json", events, command, versioned=True, dry_run=args.dry_run, entry_style="cursor")
    print(f"Cursor skill path: {skill_dir}")


def run_codex_backfill(args: argparse.Namespace, skill_dir: Path) -> None:
    cfg = read_json(CONFIG_PATH)
    server_url = resolved_server_url(args, cfg)
    if not server_url and not args.dry_run:
        raise ValueError("--backfill-codex-history requires --server-url")
    script = skill_dir / "scripts" / "codex_backfill.py"
    command = [
        sys.executable or "python3",
        str(script),
        "--config",
        str(CONFIG_PATH),
        "--batch-size",
        str(args.backfill_batch_size),
    ]
    if server_url:
        command.extend(["--server-url", str(server_url)])
    if args.backfill_limit is not None:
        command.extend(["--limit", str(args.backfill_limit)])
    if args.backfill_upload_state:
        command.extend(["--upload-state", str(Path(args.backfill_upload_state).expanduser())])
    if args.dry_run:
        command.append("--dry-run")
        print("[dry-run] would run Codex historical session backfill:")
        print(" ".join(shell_quote(part) for part in command))
        return
    print("Running Codex historical session backfill...")
    subprocess.run(command, check=True, env=platform_io.utf8_subprocess_env(), timeout=args.backfill_max_runtime_seconds)


def uninstall_codex(args: argparse.Namespace) -> None:
    home = codex_home()
    remove_hooks(home / "hooks.json", versioned=False, dry_run=args.dry_run)
    remove_stale_codex_inline_hooks(home, args.dry_run)
    print(f"Codex hook removal complete in {home}")


def uninstall_cursor(args: argparse.Namespace) -> None:
    home = cursor_home()
    remove_hooks(home / "hooks.json", versioned=True, dry_run=args.dry_run, entry_style="cursor")
    print(f"Cursor hook removal complete in {home}")


def run(args: argparse.Namespace) -> int:
    if args.uninstall:
        disable_config(args.dry_run)
        if args.surface in {"codex", "both"}:
            uninstall_codex(args)
        if args.surface in {"cursor", "both"}:
            uninstall_cursor(args)
        print("AI worklog uninstallation complete.")
        print("Existing logs under ~/.ai-worklog are left in place.")
        return 0

    if args.backfill_codex_history:
        if args.surface not in {"codex", "both"}:
            raise ValueError("--backfill-codex-history requires --surface codex or --surface both")
        if not resolved_server_url(args, read_json(CONFIG_PATH)) and not args.dry_run:
            raise ValueError("--backfill-codex-history requires --server-url")

    cfg = update_config(args, args.dry_run)
    codex_skill_dir: Path | None = None
    if args.surface in {"codex", "both"}:
        codex_skill_dir = install_codex(args)
    if args.surface in {"cursor", "both"}:
        install_cursor(args)
    if args.backfill_codex_history:
        if codex_skill_dir is None:
            raise ValueError("--backfill-codex-history requires --surface codex or --surface both")
        run_codex_backfill(args, codex_skill_dir)

    print("AI worklog installation complete.")
    print(f"Local event logs: {Path(args.local_log_dir).expanduser()}")
    print(f"Local snapshots: {Path(args.snapshot_log_dir).expanduser()}")
    if cfg.get("server_url"):
        print(f"Upload endpoint: {cfg['server_url']}")
    if not args.dry_run:
        import doctor

        report = doctor.diagnose(args.surface, smoke_write=args.smoke_test)
        doctor.print_human(report, verbose=False)
        if report["summary"]["status"] == "fail":
            return 1
    return 0


def main() -> int:
    platform_io.configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Install or uninstall AI worklog hooks.")
    parser.add_argument("--surface", choices=["codex", "cursor", "both"], default="both")
    parser.add_argument("--level", choices=["full", "diagnostic", "basic", "off"], default="full")
    parser.add_argument("--hook-set", choices=["minimal", "full"], default="minimal")
    parser.add_argument("--uninstall", action="store_true", help="Remove AI worklog hook handlers and disable collection without deleting logs.")
    parser.add_argument("--server-url", default=os.environ.get("AI_WORKLOG_SERVER_URL") or os.environ.get("AI_USAGE_COLLECTOR_SERVER_URL"))
    parser.add_argument("--clear-server-url", action="store_true", help="Explicitly clear any configured upload endpoint and keep logging local-only.")
    parser.add_argument("--api-key-env", default="AI_WORKLOG_API_KEY")
    parser.add_argument("--local-log-dir", default=str(CONFIG_HOME / "events"))
    parser.add_argument("--snapshot-log-dir", default=str(CONFIG_HOME / "snapshots"))
    parser.add_argument("--failed-log-dir", default=str(CONFIG_HOME / "failed"))
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--sync-upload", action="store_true", help="Upload from the hook process instead of using background replay.")
    parser.add_argument("--no-upload-preflight", action="store_true", help="Upload records directly without checking /events/exists first.")
    parser.add_argument("--max-transcript-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--async-upload-batch-size", type=int, default=100, help="Records per background replay upload batch.")
    parser.add_argument("--async-upload-trigger-interval-seconds", type=int, default=DEFAULT_ASYNC_UPLOAD_INTERVAL_SECONDS, help="Minimum seconds between background upload trigger attempts.")
    parser.add_argument("--async-upload-lock-stale-seconds", type=int, default=DEFAULT_ASYNC_UPLOAD_LOCK_STALE_SECONDS, help="Seconds before a background upload lock is considered stale.")
    parser.add_argument("--async-upload-lock-wait-seconds", type=int, default=DEFAULT_ASYNC_UPLOAD_LOCK_WAIT_SECONDS, help="Seconds a background upload trigger may wait for an active upload lock.")
    parser.add_argument("--async-upload-max-runtime-seconds", type=int, default=DEFAULT_ASYNC_UPLOAD_MAX_RUNTIME_SECONDS, help="Maximum seconds a background upload replay subprocess may run.")
    parser.add_argument("--backfill-codex-history", action="store_true", help="After installing Codex hooks, upload historical ~/.codex/sessions transcripts.")
    parser.add_argument("--backfill-batch-size", type=int, default=250, help="Records per historical backfill preflight/upload request.")
    parser.add_argument("--backfill-limit", type=int, help="Maximum historical Codex transcript files to process, newest first.")
    parser.add_argument("--backfill-upload-state", help="SQLite progress ledger for historical Codex backfill.")
    parser.add_argument("--no-auto-codex-backfill", action="store_true", help="Disable automatic background Codex history backfill from SessionStart hooks.")
    parser.add_argument("--backfill-trigger-interval-seconds", type=int, default=24 * 60 * 60, help="Minimum seconds between automatic Codex history backfill trigger attempts.")
    parser.add_argument("--backfill-lock-stale-seconds", type=int, default=6 * 60 * 60, help="Seconds before an automatic backfill lock is considered stale.")
    parser.add_argument("--backfill-lock-wait-seconds", type=int, default=DEFAULT_BACKFILL_LOCK_WAIT_SECONDS, help="Seconds an automatic backfill trigger may wait for an active backfill lock.")
    parser.add_argument(
        "--backfill-max-runtime-seconds",
        type=int,
        default=DEFAULT_BACKFILL_MAX_RUNTIME_SECONDS,
        help="Maximum seconds a Codex historical backfill subprocess may run.",
    )
    parser.add_argument(
        "--skill-update-manifest-url",
        default=os.environ.get("AI_WORKLOG_UPDATE_MANIFEST_URL") or DEFAULT_SKILL_UPDATE_MANIFEST_URL,
        help="Raw JSON manifest URL used to check for newer ai-worklog skill versions.",
    )
    parser.add_argument(
        "--skill-source-url",
        default=os.environ.get("AI_WORKLOG_SKILL_SOURCE_URL") or DEFAULT_SKILL_SOURCE_URL,
        help="Human-readable skill source URL shown in update prompts.",
    )
    parser.add_argument(
        "--skill-update-trigger-interval-seconds",
        type=int,
        default=DEFAULT_SKILL_UPDATE_INTERVAL_SECONDS,
        help="Minimum seconds between background remote skill version checks.",
    )
    parser.add_argument("--no-skill-update-check", action="store_true", help="Disable background remote skill version checks.")
    parser.add_argument("--smoke-test", action="store_true", help="After installing, execute installed hooks with a synthetic doctor event.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        return run(args)
    except PermissionError as exc:
        print(
            "Permission denied while updating AI worklog configuration. "
            "Allow this installer to write Codex/Cursor hook config files, then rerun the same command. "
            f"Original error: {exc}",
            file=sys.stderr,
        )
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"AI worklog installation failed: Codex history backfill timed out after {exc.timeout} seconds", file=sys.stderr)
        return 1
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"AI worklog installation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
