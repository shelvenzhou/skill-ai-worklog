#!/usr/bin/env python3
"""Install or uninstall AI worklog hooks for Codex and Cursor."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


SKILL_NAME = "ai-worklog"
CONFIG_HOME = Path.home() / ".ai-worklog"
CONFIG_PATH = CONFIG_HOME / "config.json"
CODEX_MINIMAL_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "SubagentStop",
    "Stop",
]
CODEX_FULL_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SubagentStop",
    "Stop",
]
CURSOR_MINIMAL_EVENTS = [
    "sessionStart",
    "beforeSubmitPrompt",
    "postToolUse",
    "postToolUseFailure",
    "afterAgentResponse",
    "subagentStop",
    "stop",
]
CURSOR_FULL_EVENTS = [
    "workspaceOpen",
    "sessionStart",
    "sessionEnd",
    "beforeSubmitPrompt",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "subagentStart",
    "subagentStop",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "beforeTabFileRead",
    "afterTabFileEdit",
    "afterAgentResponse",
    "afterAgentThought",
    "preCompact",
    "stop",
]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        print(f"Backed up unreadable JSON {path} to {backup}: {exc}")
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict[str, Any], dry_run: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    if dry_run:
        print(f"[dry-run] would write {path}:\n{data}")
        return
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    path.write_text(data + "\n", encoding="utf-8")


def source_skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def copy_skill(destination: Path, dry_run: bool) -> Path:
    src = source_skill_dir()
    dest = destination.expanduser()
    if src == dest:
        return dest
    if dry_run:
        print(f"[dry-run] would copy {src} to {dest}")
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    shutil.copytree(src, dest, ignore=ignore)
    return dest


def python_command(skill_dir: Path, surface: str, config_path: Path) -> str:
    journal = skill_dir / "scripts" / "journal.py"
    python = sys.executable or "python3"
    return (
        "/bin/sh -c "
        + shell_quote('test -f "$1" || exit 0; exec "$2" "$1" --surface "$3" --config "$4" --source-id "$5"')
        + f" {shell_quote(SKILL_NAME + '-hook')}"
        + f" {shell_quote(str(journal))}"
        + f" {shell_quote(python)}"
        + f" {shell_quote(surface)}"
        + f" {shell_quote(str(config_path))}"
        + f" {shell_quote(SKILL_NAME)}"
    )


def shell_quote(value: str) -> str:
    if not value:
        return "''"
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-=.,/:@%"
    if all(ch in safe for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def hook_entry(command: str) -> dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
            }
        ]
    }


def is_worklog_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if isinstance(hook, dict):
            command = str(hook.get("command") or "")
            if ("ai-worklog" in command or "ai-usage-collector" in command) and (
                "journal.py" in command or "collector.py" in command
            ):
                return True
    return False


def merge_hooks(path: Path, events: list[str], command: str, versioned: bool, dry_run: bool) -> None:
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
        filtered = [entry for entry in event_entries if not is_worklog_entry(entry)]
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
        event_entries.append(hook_entry(command))
        added += 1

    write_json(path, doc, dry_run)
    print(f"Installed {added} hook handlers in {path}")


def remove_hooks(path: Path, versioned: bool, dry_run: bool) -> int:
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
            if is_worklog_entry(entry):
                removed += 1
            else:
                filtered.append(entry)
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


def update_config(args: argparse.Namespace, dry_run: bool) -> None:
    cfg = read_json(CONFIG_PATH)
    cfg.update(
        {
            "enabled": args.level != "off",
            "collection_level": args.level,
            "local_log_dir": str(Path(args.local_log_dir).expanduser()),
            "snapshot_log_dir": str(Path(args.snapshot_log_dir).expanduser()),
            "failed_log_dir": str(Path(args.failed_log_dir).expanduser()),
            "server_url": args.server_url,
            "api_key_env": args.api_key_env,
            "request_timeout_seconds": args.timeout,
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
    write_json(CONFIG_PATH, cfg, dry_run)
    print(f"Configured worklog at {CONFIG_PATH}")


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
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def cursor_home() -> Path:
    return Path(os.environ.get("CURSOR_HOME") or Path.home() / ".cursor").expanduser()


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


def ensure_codex_hooks_feature(home: Path, dry_run: bool) -> None:
    config_path = home / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    updated = set_toml_feature(existing, "hooks", "true")
    if updated == existing:
        print(f"Codex hooks feature already enabled in {config_path}")
        return
    if dry_run:
        print(f"[dry-run] would write {config_path}:\n{updated}")
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_suffix(config_path.suffix + ".bak"))
    config_path.write_text(updated, encoding="utf-8")
    print(f"Enabled Codex hooks feature in {config_path}")


def install_codex(args: argparse.Namespace) -> None:
    home = codex_home()
    skill_dir = copy_skill(home / "skills" / SKILL_NAME, args.dry_run)
    command = python_command(skill_dir, "codex", CONFIG_PATH)
    events = CODEX_FULL_EVENTS if args.hook_set == "full" else CODEX_MINIMAL_EVENTS
    merge_hooks(home / "hooks.json", events, command, versioned=False, dry_run=args.dry_run)
    ensure_codex_hooks_feature(home, args.dry_run)
    print(f"Codex skill path: {skill_dir}")


def install_cursor(args: argparse.Namespace) -> None:
    home = cursor_home()
    skill_dir = copy_skill(home / "skills" / SKILL_NAME, args.dry_run)
    command = python_command(skill_dir, "cursor", CONFIG_PATH)
    events = CURSOR_FULL_EVENTS if args.hook_set == "full" else CURSOR_MINIMAL_EVENTS
    merge_hooks(home / "hooks.json", events, command, versioned=True, dry_run=args.dry_run)
    print(f"Cursor skill path: {skill_dir}")


def uninstall_codex(args: argparse.Namespace) -> None:
    home = codex_home()
    remove_hooks(home / "hooks.json", versioned=False, dry_run=args.dry_run)
    print(f"Codex hook removal complete in {home}")


def uninstall_cursor(args: argparse.Namespace) -> None:
    home = cursor_home()
    remove_hooks(home / "hooks.json", versioned=True, dry_run=args.dry_run)
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

    update_config(args, args.dry_run)
    if args.surface in {"codex", "both"}:
        install_codex(args)
    if args.surface in {"cursor", "both"}:
        install_cursor(args)

    print("AI worklog installation complete.")
    print(f"Local event logs: {Path(args.local_log_dir).expanduser()}")
    print(f"Local snapshots: {Path(args.snapshot_log_dir).expanduser()}")
    if args.server_url:
        print(f"Upload endpoint: {args.server_url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or uninstall AI worklog hooks.")
    parser.add_argument("--surface", choices=["codex", "cursor", "both"], default="both")
    parser.add_argument("--level", choices=["full", "diagnostic", "basic", "off"], default="full")
    parser.add_argument("--hook-set", choices=["minimal", "full"], default="minimal")
    parser.add_argument("--uninstall", action="store_true", help="Remove AI worklog hook handlers and disable collection without deleting logs.")
    parser.add_argument("--server-url", default=os.environ.get("AI_WORKLOG_SERVER_URL") or os.environ.get("AI_USAGE_COLLECTOR_SERVER_URL"))
    parser.add_argument("--api-key-env", default="AI_WORKLOG_API_KEY")
    parser.add_argument("--local-log-dir", default=str(CONFIG_HOME / "events"))
    parser.add_argument("--snapshot-log-dir", default=str(CONFIG_HOME / "snapshots"))
    parser.add_argument("--failed-log-dir", default=str(CONFIG_HOME / "failed"))
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--max-transcript-bytes", type=int, default=5 * 1024 * 1024)
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


if __name__ == "__main__":
    raise SystemExit(main())
