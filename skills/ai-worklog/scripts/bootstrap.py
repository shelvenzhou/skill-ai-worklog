#!/usr/bin/env python3
"""Bootstrap AI Worklog from the remote release manifest.

This file is intentionally self-contained so it can be run through:

    curl -fsSL <raw bootstrap.py> | python3 -
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from typing import Any
import urllib.request


DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/shelvenzhou/skill-ai-worklog/master/skills/ai-worklog/skill-version.json"
DEFAULT_MANIFEST: dict[str, Any] = {
    "name": "ai-worklog",
    "version": "0.3.5",
    "repo": "shelvenzhou/skill-ai-worklog",
    "ref": "master",
    "path": "skills/ai-worklog",
    "remote_manifest_url": DEFAULT_MANIFEST_URL,
    "install_url": "https://github.com/shelvenzhou/skill-ai-worklog/tree/master/skills/ai-worklog",
}


def surface_skill_dirs(selection: str) -> list[Path]:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    cursor_home = Path(os.environ.get("CURSOR_HOME") or Path.home() / ".cursor").expanduser()
    if selection == "codex":
        return [codex_home / "skills" / "ai-worklog"]
    if selection == "cursor":
        return [cursor_home / "skills" / "ai-worklog"]
    if selection == "both":
        return [codex_home / "skills" / "ai-worklog", cursor_home / "skills" / "ai-worklog"]
    raise ValueError(f"unknown surface: {selection}")


def fetch_manifest(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json,text/plain"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8-sig", errors="replace")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("remote manifest must be a JSON object")
    return value


def merged_manifest(url: str, timeout: float, *, allow_fallback: bool = False) -> dict[str, Any]:
    manifest = dict(DEFAULT_MANIFEST)
    try:
        manifest.update(fetch_manifest(url, timeout))
    except Exception as exc:
        if not allow_fallback:
            raise
        print(f"AI Worklog bootstrap: manifest fetch failed; using baked source fields: {exc}", file=sys.stderr)
    return manifest


def archive_url(manifest: dict[str, Any]) -> str:
    if manifest.get("archive_url"):
        return str(manifest["archive_url"])
    repo = manifest.get("repo")
    ref = manifest.get("ref")
    if repo and ref:
        return f"https://github.com/{repo}/archive/{ref}.tar.gz"
    raise ValueError("manifest lacks machine-installable archive fields")


def manifest_path(manifest: dict[str, Any]) -> str:
    value = str(manifest.get("path") or "").strip("/")
    if not value:
        raise ValueError("manifest lacks machine-installable path")
    return value


def download_archive(url: str, target: Path, timeout: float) -> None:
    request = urllib.request.Request(url, headers={"Accept": "application/gzip,application/octet-stream"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        target.write_bytes(response.read())


def validate_skill_dir(path: Path) -> None:
    required = [
        path / "SKILL.md",
        path / "skill-version.json",
        path / "scripts" / "install.py",
        path / "scripts" / "doctor.py",
    ]
    missing = [str(item) for item in required if not item.exists()]
    if missing:
        raise ValueError(f"downloaded skill is missing required files: {', '.join(missing)}")


def extract_skill_dir(archive: Path, path_in_archive: str, workdir: Path) -> Path:
    extract_root = workdir / "archive"
    extract_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        try:
            tf.extractall(extract_root, filter="data")
        except TypeError:
            tf.extractall(extract_root)
    candidates = [path for path in extract_root.glob(f"*/{path_in_archive}") if path.is_dir()]
    candidates.extend(path for path in extract_root.glob(path_in_archive) if path.is_dir())
    if not candidates:
        raise ValueError(f"archive does not contain {path_in_archive}")
    skill_dir = candidates[0]
    validate_skill_dir(skill_dir)
    return skill_dir


def run_command(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(command, check=False, text=True, capture_output=capture, env=env)


def doctor_script(selection: str, staged_skill: Path) -> Path:
    for skill_dir in surface_skill_dirs(selection):
        candidate = skill_dir / "scripts" / "doctor.py"
        if candidate.exists():
            return candidate
    return staged_skill / "scripts" / "doctor.py"


def run_doctor(selection: str, staged_skill: Path) -> tuple[int, dict[str, Any] | None, str]:
    command = [sys.executable or "python3", str(doctor_script(selection, staged_skill)), "--surface", selection, "--json"]
    completed = run_command(command, capture=True)
    report = None
    if completed.stdout.strip():
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError:
            report = None
    return completed.returncode, report, (completed.stderr or completed.stdout)[-2000:]


def hook_counts(report: dict[str, Any] | None) -> dict[str, int]:
    counts = {"codex": 0, "cursor": 0}
    if not isinstance(report, dict):
        return counts
    for item in report.get("checks") or []:
        if not isinstance(item, dict) or item.get("name") != "hooks":
            continue
        surface = item.get("surface")
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        if surface in counts:
            counts[str(surface)] = len(details.get("installed_events") or [])
    return counts


def first_failure(report: dict[str, Any] | None, fallback: str) -> str:
    if isinstance(report, dict):
        for item in report.get("checks") or []:
            if isinstance(item, dict) and item.get("status") == "fail":
                return f"{item.get('surface') + ' ' if item.get('surface') else ''}{item.get('name')}: {item.get('message')}"
    return fallback


def print_marker(ok: bool, report: dict[str, Any] | None = None, reason: str | None = None) -> None:
    counts = hook_counts(report)
    if ok:
        summary = report.get("summary", {}) if isinstance(report, dict) else {}
        warn = int(summary.get("warn") or 0)
        print(f"AI_WORKLOG_INSTALL: PASS (codex_hooks={counts['codex']} cursor_hooks={counts['cursor']} warn={warn})")
        return
    message = reason or first_failure(report, "unknown failure")
    print(f"AI_WORKLOG_INSTALL: FAIL: {message}")


def install_args(args: argparse.Namespace, manifest_url_value: str, source_url: str | None) -> list[str]:
    command = [
        "--surface",
        args.surface,
        "--level",
        args.level,
        "--hook-set",
        args.hook_set,
        "--skill-update-manifest-url",
        manifest_url_value,
    ]
    if source_url:
        command.extend(["--skill-source-url", source_url])
    if args.server_url:
        command.extend(["--server-url", args.server_url])
    if args.api_key_env:
        command.extend(["--api-key-env", args.api_key_env])
    if args.auto_skill_update:
        command.append("--auto-skill-update")
    return command


def bootstrap(args: argparse.Namespace) -> int:
    explicit_manifest_url = bool(args.manifest_url or os.environ.get("AI_WORKLOG_UPDATE_MANIFEST_URL"))
    manifest_url_value = args.manifest_url or os.environ.get("AI_WORKLOG_UPDATE_MANIFEST_URL") or DEFAULT_MANIFEST_URL
    with tempfile.TemporaryDirectory(prefix="ai-worklog-bootstrap-") as tmp:
        workdir = Path(tmp)
        try:
            manifest = merged_manifest(manifest_url_value, args.timeout, allow_fallback=not explicit_manifest_url)
            archive = archive_url(manifest)
            path_in_archive = manifest_path(manifest)
            archive_file = workdir / "skill.tar.gz"
            download_archive(archive, archive_file, args.timeout)
            staged_skill = extract_skill_dir(archive_file, path_in_archive, workdir)
        except Exception as exc:
            print_marker(False, reason=f"download failed: {exc}")
            return 1

        install_script = staged_skill / "scripts" / "install.py"
        source_url = str(manifest.get("install_url") or manifest.get("source_url") or "") or None
        install_command = [sys.executable or "python3", str(install_script), *install_args(args, manifest_url_value, source_url)]
        completed = run_command(install_command)
        if completed.returncode != 0:
            rc, report, output = run_doctor(args.surface, staged_skill)
            reason = first_failure(report, f"installer exited {completed.returncode}; {output.strip()}")
            print_marker(False, report, reason)
            return completed.returncode or rc or 1

        rc, report, output = run_doctor(args.surface, staged_skill)
        ok = rc == 0 and isinstance(report, dict) and report.get("summary", {}).get("status") != "fail"
        print_marker(ok, report, None if ok else first_failure(report, output.strip()))
        return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch, install, and verify AI Worklog in one deterministic command.")
    parser.add_argument("--surface", choices=["codex", "cursor", "both"], default="both")
    parser.add_argument("--level", choices=["full", "diagnostic", "basic", "off"], default="full")
    parser.add_argument("--hook-set", choices=["minimal", "full"], default="minimal")
    parser.add_argument("--manifest-url", help="Raw skill-version.json URL. Defaults to the baked release manifest.")
    parser.add_argument("--server-url", default=os.environ.get("AI_WORKLOG_SERVER_URL") or os.environ.get("AI_USAGE_COLLECTOR_SERVER_URL"))
    parser.add_argument("--api-key-env", default="AI_WORKLOG_API_KEY")
    parser.add_argument("--auto-skill-update", action="store_true", help="Opt in to automatic background skill updates after install.")
    parser.add_argument("--timeout", type=float, default=10.0)
    return bootstrap(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
