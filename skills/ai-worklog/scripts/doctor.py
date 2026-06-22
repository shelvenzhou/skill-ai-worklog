#!/usr/bin/env python3
"""Diagnose AI Worklog skill installation and hook health."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

import journal
import platform_io
import platforms
import surfaces


STATUSES = {"ok": 0, "warn": 1, "fail": 2}


def check(name: str, status: str, message: str, *, surface: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "message": message,
        **({"surface": surface} if surface else {}),
        **({"details": details} if details else {}),
    }


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(platform_io.read_text(path, encoding="utf-8-sig"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def today_log_path(cfg: dict[str, Any]) -> Path:
    local_dir = Path(str(cfg.get("local_log_dir") or journal.DEFAULT_HOME / "events")).expanduser()
    return local_dir / f"{dt.datetime.now().strftime('%Y-%m-%d')}.jsonl"


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def hook_command_from_entry(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    direct_command = str(entry.get("command") or entry.get("commandWindows") or "")
    if "ai-worklog" in direct_command and ("journal.py" in direct_command or "ai-worklog-hook" in direct_command):
        return direct_command
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return None
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = str(hook.get("command") or hook.get("commandWindows") or "")
        if "ai-worklog" in command and ("journal.py" in command or "ai-worklog-hook" in command):
            return command
    return None


def find_hook_command(spec: surfaces.SurfaceSpec, hook_event: str | None = None) -> str | None:
    doc = load_json(spec.hooks_path)
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return None
    event_names = [hook_event] if hook_event else list(hooks.keys())
    for event_name in event_names:
        if event_name is None:
            continue
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            command = hook_command_from_entry(entry)
            if command:
                return command
    return None


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


def recent_path(directory: Path, glob: str = "*") -> Path | None:
    if not directory.exists():
        return None
    candidates = [path for path in directory.glob(glob) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def recent_state_checks(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    event_log = recent_path(Path(str(cfg.get("local_log_dir") or journal.DEFAULT_HOME / "events")).expanduser(), "*.jsonl")
    if event_log:
        checks.append(check("recent_events", "ok", f"Recent local event log: {event_log}", details={"path": str(event_log)}))
    else:
        checks.append(check("recent_events", "warn", "No local event JSONL files found yet. Run doctor --smoke-write or start a new agent session."))

    error_log = recent_path(journal.DEFAULT_HOME / "errors")
    runtime_log = journal.DEFAULT_HOME / "errors" / "runtime.log"
    if runtime_log.exists():
        checks.append(check("runtime_errors", "warn", f"Runtime error log exists: {runtime_log}", details={"path": str(runtime_log)}))
    elif error_log:
        checks.append(check("runtime_errors", "warn", f"Recent hook exception log exists: {error_log}", details={"path": str(error_log)}))
    else:
        checks.append(check("runtime_errors", "ok", "No AI Worklog runtime error logs found."))

    for name, filename in (("async_upload", "async_upload.log"), ("codex_backfill", "codex_backfill.log")):
        path = journal.DEFAULT_HOME / filename
        if path.exists():
            checks.append(check(name, "ok", f"{name} log exists: {path}", details={"path": str(path)}))

    update_state = load_json(journal.DEFAULT_HOME / "skill_update_state.json")
    if update_state.get("update_available") is True:
        checks.append(check("skill_update", "warn", "A newer AI Worklog skill version is available.", details=update_state))
    elif update_state:
        checks.append(check("skill_update", "ok", "Skill update state found.", details=update_state))
    return checks


def smoke_payload(spec: surfaces.SurfaceSpec) -> dict[str, Any]:
    session = f"doctor-{int(time.time())}"
    return {
        "hook_event_name": spec.smoke_event,
        "session_id": session,
        "turn_id": "doctor",
        "cwd": str(Path.cwd()),
        "prompt": "AI Worklog doctor smoke-write",
    }


def run_smoke_write(spec: surfaces.SurfaceSpec, cfg: dict[str, Any]) -> dict[str, Any]:
    command = find_hook_command(spec, spec.smoke_event)
    if not command:
        return check("smoke_write", "fail", f"No installed AI Worklog hook command found for {spec.smoke_event}.", surface=spec.name)

    before_count = len(iter_jsonl(today_log_path(cfg)))
    env = platform_io.utf8_subprocess_env()
    env["AI_WORKLOG_SOURCE_ID"] = "ai-worklog-doctor"
    env["AI_WORKLOG_CONFIG"] = str(journal.DEFAULT_CONFIG_PATH)
    env["AI_WORKLOG_DISABLE_BACKGROUND"] = "1"
    try:
        completed = subprocess.run(
            command,
            shell=True,
            input=json.dumps(smoke_payload(spec), ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env=env,
        )
    except Exception as exc:
        return check("smoke_write", "fail", f"Smoke hook execution failed: {exc}", surface=spec.name)

    records = iter_jsonl(today_log_path(cfg))
    wrote_doctor_event = any(record.get("source_id") == "ai-worklog-doctor" for record in records[before_count:])
    if completed.returncode == 0 and wrote_doctor_event:
        return check("smoke_write", "ok", "Installed hook command wrote a doctor event.", surface=spec.name)
    return check(
        "smoke_write",
        "fail",
        "Installed hook command did not write a doctor event.",
        surface=spec.name,
        details={"returncode": completed.returncode, "stderr": completed.stderr[-1000:]},
    )


def find_codex_executable() -> Path | None:
    from_path = shutil.which("codex")
    if from_path:
        return Path(from_path)
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        candidates: list[Path] = []
        if local_appdata:
            candidates.extend(Path(local_appdata).glob("OpenAI/Codex/bin/*/codex.exe"))
        candidates.extend((Path.home() / ".codex" / "packages" / "standalone" / "releases").glob("*/bin/codex.exe"))
        existing = [path for path in candidates if path.exists()]
        if existing:
            return max(existing, key=lambda path: path.stat().st_mtime)
    return None


def codex_app_server_hooks_check_from_response(executable: Path, response: dict[str, Any]) -> dict[str, Any]:
    if response.get("error"):
        return check("codex_app_server_hooks", "warn", "Codex app-server hooks/list returned an error.", surface="codex", details=response)

    entries = response.get("result", {}).get("data", [])
    ai_hooks: list[dict[str, Any]] = []
    warnings: list[Any] = []
    errors: list[Any] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            warnings.extend(entry.get("warnings") or [])
            errors.extend(entry.get("errors") or [])
            for hook in entry.get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                command = str(hook.get("command") or "")
                if "ai-worklog" in command:
                    ai_hooks.append(hook)

    details = {
        "executable": str(executable),
        "hook_count": len(ai_hooks),
        "trust_statuses": sorted({str(hook.get("trustStatus")) for hook in ai_hooks}),
        "enabled_count": sum(1 for hook in ai_hooks if hook.get("enabled") is True),
        "warnings": warnings,
        "errors": errors,
    }
    if errors:
        return check("codex_app_server_hooks", "fail", "Codex app-server reported hook discovery errors.", surface="codex", details=details)
    if not ai_hooks:
        return check("codex_app_server_hooks", "fail", "Codex app-server hooks/list did not discover AI Worklog hooks.", surface="codex", details=details)
    untrusted = [hook for hook in ai_hooks if hook.get("trustStatus") in {"untrusted", "modified"}]
    if untrusted:
        return check(
            "codex_app_server_hooks",
            "warn",
            f"Codex app-server discovered {len(ai_hooks)} AI Worklog hooks, but {len(untrusted)} need approval before Codex will run them.",
            surface="codex",
            details=details,
        )
    return check("codex_app_server_hooks", "ok", f"Codex app-server discovered {len(ai_hooks)} trusted AI Worklog hooks.", surface="codex", details=details)


def codex_app_server_hooks_check() -> dict[str, Any]:
    executable = find_codex_executable()
    if executable is None:
        return check("codex_app_server_hooks", "warn", "Codex executable was not found; cannot query app-server hooks/list.", surface="codex")

    try:
        process = subprocess.Popen(
            [str(executable), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=platform_io.utf8_subprocess_env(),
        )
    except Exception as exc:
        return check("codex_app_server_hooks", "warn", f"Failed to query Codex app-server hooks/list: {exc}", surface="codex")

    stdout_queue: queue.Queue[str] = queue.Queue()
    stderr_queue: queue.Queue[str] = queue.Queue()

    def reader(stream: Any, target: queue.Queue[str]) -> None:
        try:
            for line in stream:
                target.put(line.rstrip("\n"))
        except Exception:
            pass

    threading.Thread(target=reader, args=(process.stdout, stdout_queue), daemon=True).start()
    threading.Thread(target=reader, args=(process.stderr, stderr_queue), daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise BrokenPipeError("Codex app-server stdin is closed")
        process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def read_response(request_id: int, timeout_seconds: float) -> dict[str, Any] | None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if process.poll() is not None and stdout_queue.empty():
                return None
            try:
                line = stdout_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                return message
        return None

    response: dict[str, Any] | None = None
    try:
        send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {"name": "ai_worklog_doctor", "title": "AI Worklog Doctor", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        init_response = read_response(1, 20)
        if init_response is not None:
            send({"method": "initialized", "params": {}})
            send({"method": "hooks/list", "id": 2, "params": {"cwds": [str(Path.cwd())]}})
            response = read_response(2, 20)
    except Exception as exc:
        return check("codex_app_server_hooks", "warn", f"Failed to exchange hooks/list messages with Codex app-server: {exc}", surface="codex")
    finally:
        try:
            process.terminate()
        except Exception:
            pass

    if response is None:
        stderr_text = "\n".join(list(stderr_queue.queue))
        stdout_text = "\n".join(list(stdout_queue.queue))
        return check(
            "codex_app_server_hooks",
            "warn",
            "Codex app-server did not return a hooks/list response.",
            surface="codex",
            details={"returncode": process.poll(), "stderr": stderr_text[-2000:], "stdout": stdout_text[-2000:]},
        )
    return codex_app_server_hooks_check_from_response(executable, response)


def diagnose_surface(spec: surfaces.SurfaceSpec, cfg: dict[str, Any], *, smoke_write: bool, app_server_check: bool) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append(
        check(
            "skill_dir",
            "ok" if spec.skill_dir.exists() else "fail",
            f"Skill directory {'exists' if spec.skill_dir.exists() else 'missing'}: {spec.skill_dir}",
            surface=spec.name,
            details={"path": str(spec.skill_dir)},
        )
    )
    for required in ("SKILL.md", "skill-version.json", "scripts/journal.py", "scripts/install.py"):
        path = spec.skill_dir / required
        checks.append(
            check(
                "skill_file",
                "ok" if path.exists() else "fail",
                f"{required} {'exists' if path.exists() else 'is missing'}",
                surface=spec.name,
                details={"path": str(path)},
            )
        )
    acl_ok, acl_details = platforms.current_platform().skill_acl_is_readable(spec.skill_dir)
    checks.append(
        check(
            "skill_acl",
            "ok" if acl_ok else "fail",
            "Skill directory ACL is readable by Windows app/sandbox users." if acl_ok else "Skill directory ACL may be unreadable by Codex/Cursor.",
            surface=spec.name,
            details={"path": str(spec.skill_dir), "acl": acl_details[-2000:]},
        )
    )

    installed_events = [event for event in spec.events(str(cfg.get("hook_set") or "minimal")) if find_hook_command(spec, event)]
    status = "ok" if installed_events else "fail"
    details = {"installed_events": installed_events}
    if spec.config_toml_path is not None:
        details["config_toml_path"] = str(spec.config_toml_path)
    details["hooks_path"] = str(spec.hooks_path)
    checks.append(
        check(
            "hooks",
            status,
            f"AI Worklog hook handlers found for {len(installed_events)} expected events.",
            surface=spec.name,
            details=details,
        )
    )

    if spec.config_toml_path is not None:
        enabled = codex_hooks_enabled(spec.config_toml_path)
        checks.append(
            check(
                "codex_hooks_feature",
                "ok" if enabled else "fail",
                f"Codex hooks feature is {'enabled' if enabled else 'not enabled'} in {spec.config_toml_path}",
                surface=spec.name,
            )
        )

    runtime = platforms.current_platform().detect_python_runtime()
    runtime_status = "ok" if runtime.ok else ("fail" if spec.name == "cursor" else "warn")
    checks.append(check("python_runtime", runtime_status, runtime.message, surface=spec.name, details=runtime.__dict__))

    if smoke_write:
        checks.append(run_smoke_write(spec, cfg))
    if app_server_check and spec.name == "codex":
        checks.append(codex_app_server_hooks_check())
    return checks


def summarize(checks: list[dict[str, Any]]) -> dict[str, Any]:
    worst = "ok"
    for item in checks:
        if STATUSES[item["status"]] > STATUSES[worst]:
            worst = str(item["status"])
    return {
        "status": worst,
        "ok": sum(1 for item in checks if item["status"] == "ok"),
        "warn": sum(1 for item in checks if item["status"] == "warn"),
        "fail": sum(1 for item in checks if item["status"] == "fail"),
    }


def recommended_commands(selection: str, checks: list[dict[str, Any]]) -> list[str]:
    commands: list[str] = []
    if any(item["status"] == "fail" and item["name"] in {"skill_dir", "skill_file", "hooks", "codex_hooks_feature"} for item in checks):
        commands.append(f"{sys.executable or 'python3'} {Path(__file__).resolve().with_name('install.py')} --surface {selection} --level full")
    if any(item["status"] == "fail" and item["name"] == "python_runtime" for item in checks):
        commands.append("Set AI_WORKLOG_PYTHON to a Python executable or install Python, then rerun doctor.")
    if any(item["status"] == "warn" and item["name"] == "skill_update" for item in checks):
        commands.append(f"{sys.executable or 'python3'} {Path(__file__).resolve().with_name('update_skill.py')} --surface {selection}")
    return commands


def diagnose(selection: str, *, smoke_write: bool = False, app_server_check: bool = False) -> dict[str, Any]:
    cfg = journal.merged_config(journal.DEFAULT_CONFIG_PATH)
    checks: list[dict[str, Any]] = []
    config_exists = journal.DEFAULT_CONFIG_PATH.exists()
    checks.append(
        check(
            "config",
            "ok" if config_exists else "fail",
            f"Config {'exists' if config_exists else 'missing'}: {journal.DEFAULT_CONFIG_PATH}",
            details={"path": str(journal.DEFAULT_CONFIG_PATH)},
        )
    )
    for spec in surfaces.surface_specs(selection):
        checks.extend(diagnose_surface(spec, cfg, smoke_write=smoke_write, app_server_check=app_server_check))
    checks.extend(recent_state_checks(cfg))
    return {
        "summary": summarize(checks),
        "checks": checks,
        "recommended_commands": recommended_commands(selection, checks),
    }


def print_human(report: dict[str, Any], *, verbose: bool) -> None:
    summary = report["summary"]
    print(f"AI Worklog doctor: {summary['status']} ({summary['ok']} ok, {summary['warn']} warn, {summary['fail']} fail)")
    for item in report["checks"]:
        if not verbose and item["status"] == "ok":
            continue
        prefix = item["status"].upper()
        surface = f"[{item['surface']}] " if item.get("surface") else ""
        print(f"{prefix} {surface}{item['name']}: {item['message']}")
    if report["recommended_commands"]:
        print("\nRecommended commands:")
        for command in report["recommended_commands"]:
            print(f"- {command}")


def main() -> int:
    platform_io.configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Diagnose AI Worklog installation and hook health.")
    parser.add_argument("--surface", choices=["codex", "cursor", "both"], default="both")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--verbose", action="store_true", help="Show passing checks in text output.")
    parser.add_argument("--smoke-write", action="store_true", help="Execute installed hook command with a synthetic diagnostic event.")
    parser.add_argument("--app-server-check", action="store_true", help="Query Codex app-server hooks/list to verify Codex can discover and trust installed hooks.")
    args = parser.parse_args()

    report = diagnose(args.surface, smoke_write=args.smoke_write, app_server_check=args.app_server_check)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    else:
        print_human(report, verbose=args.verbose)
    return 1 if report["summary"]["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
