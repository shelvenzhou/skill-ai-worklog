#!/usr/bin/env python3
"""Deterministically update the installed AI Worklog skill from a release manifest."""

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

import check_update
import install
import journal
import platform_io
import skill_release
import surfaces


def manifest_url(cfg: dict[str, Any], explicit: str | None = None) -> str:
    value = explicit or os.environ.get("AI_WORKLOG_UPDATE_MANIFEST_URL")
    if value:
        return value
    section = cfg.get("skill_update")
    if isinstance(section, dict) and section.get("manifest_url"):
        return str(section["manifest_url"])
    value = skill_release.MANIFEST.get("remote_manifest_url")
    if value:
        return str(value)
    raise ValueError("missing manifest URL; pass --manifest-url or configure skill_update.manifest_url")


def current_version(cfg: dict[str, Any]) -> str:
    section = cfg.get("skill_update")
    if isinstance(section, dict) and section.get("current_version"):
        return str(section["current_version"])
    return skill_release.VERSION


def source_url(cfg: dict[str, Any], manifest: dict[str, Any]) -> str | None:
    for key in ("install_url", "source_url", "url"):
        if manifest.get(key):
            return str(manifest[key])
    section = cfg.get("skill_update")
    if isinstance(section, dict) and section.get("source_url"):
        return str(section["source_url"])
    return None


def archive_url(manifest: dict[str, Any]) -> str:
    if manifest.get("archive_url"):
        return str(manifest["archive_url"])
    repo = manifest.get("repo")
    ref = manifest.get("ref")
    if repo and ref:
        return f"https://github.com/{repo}/archive/{ref}.tar.gz"
    raise ValueError("remote manifest lacks machine-installable fields; expected archive_url or repo/ref/path")


def manifest_path(manifest: dict[str, Any]) -> str:
    value = manifest.get("path")
    if not value:
        raise ValueError("remote manifest lacks machine-installable path")
    return str(value).strip("/")


def fetch_manifest(url: str, timeout: float) -> dict[str, Any]:
    return check_update.fetch_manifest(url, timeout)


def download_archive(url: str, target: Path, timeout: float) -> None:
    request = urllib.request.Request(url, headers={"Accept": "application/gzip,application/octet-stream"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        target.write_bytes(response.read())


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
    install.validate_skill_dir(skill_dir)
    return skill_dir


def installer_args(cfg: dict[str, Any], selection: str, manifest_url_value: str, source: str | None) -> list[str]:
    args = [
        "--surface",
        selection,
        "--level",
        str(cfg.get("collection_level") or "full"),
        "--hook-set",
        str(cfg.get("hook_set") or "minimal"),
        "--local-log-dir",
        str(Path(str(cfg.get("local_log_dir") or journal.DEFAULT_HOME / "events")).expanduser()),
        "--snapshot-log-dir",
        str(Path(str(cfg.get("snapshot_log_dir") or journal.DEFAULT_HOME / "snapshots")).expanduser()),
        "--failed-log-dir",
        str(Path(str(cfg.get("failed_log_dir") or journal.DEFAULT_HOME / "failed")).expanduser()),
        "--timeout",
        str(cfg.get("request_timeout_seconds") or journal.DEFAULT_REQUEST_TIMEOUT_SECONDS),
        "--api-key-env",
        str(cfg.get("api_key_env") or "AI_WORKLOG_API_KEY"),
        "--skill-update-manifest-url",
        manifest_url_value,
    ]
    if source:
        args.extend(["--skill-source-url", source])
    if cfg.get("server_url"):
        args.extend(["--server-url", str(cfg["server_url"])])
    if cfg.get("upload_mode") == "sync":
        args.append("--sync-upload")
    if cfg.get("upload_preflight") is False:
        args.append("--no-upload-preflight")
    skill_update = cfg.get("skill_update")
    if isinstance(skill_update, dict) and skill_update.get("enabled") is False:
        args.append("--no-skill-update-check")
    if isinstance(skill_update, dict) and skill_update.get("auto_update") is True:
        args.append("--auto-skill-update")
    codex_backfill = cfg.get("codex_history_backfill")
    if isinstance(codex_backfill, dict) and codex_backfill.get("enabled") is False:
        args.append("--no-auto-codex-backfill")
    return args


def update(selection: str, *, manifest_url_override: str | None = None, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    cfg = journal.merged_config(journal.DEFAULT_CONFIG_PATH)
    url = manifest_url(cfg, manifest_url_override)
    manifest = fetch_manifest(url, check_update.timeout_seconds(cfg))
    remote = str(manifest.get("version") or "")
    if not remote:
        raise ValueError("remote manifest is missing version")
    relation = check_update.version_relation(current_version(cfg), remote)
    if relation not in {"newer", "different"} and not force:
        return {"updated": False, "reason": "already_current", "current_version": current_version(cfg), "remote_version": remote}

    path_in_archive = manifest_path(manifest)
    archive = archive_url(manifest)
    source = source_url(cfg, manifest)
    selected_specs = surfaces.surface_specs(selection)
    with tempfile.TemporaryDirectory(prefix="ai-worklog-update-") as tmp:
        workdir = Path(tmp)
        archive_file = workdir / "skill.tar.gz"
        if dry_run:
            return {
                "updated": False,
                "reason": "dry_run",
                "remote_version": remote,
                "archive_url": archive,
                "path": path_in_archive,
            }
        download_archive(archive, archive_file, check_update.timeout_seconds(cfg))
        staged_skill = extract_skill_dir(archive_file, path_in_archive, workdir)
        for spec in selected_specs:
            install.replace_skill_from_source(staged_skill, spec.skill_dir, dry_run=False, label=spec.name)

    install_script = selected_specs[0].skill_dir / "scripts" / "install.py"
    command = [sys.executable or "python3", str(install_script), *installer_args(cfg, selection, url, source)]
    completed = subprocess.run(command, check=False, env=platform_io.utf8_subprocess_env())
    if completed.returncode != 0:
        raise RuntimeError(f"installer failed after skill replacement with exit code {completed.returncode}")
    return {"updated": True, "current_version": current_version(cfg), "remote_version": remote, "source_url": source}


def main() -> int:
    platform_io.configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Update installed AI Worklog skill from its remote manifest.")
    parser.add_argument("--surface", choices=["codex", "cursor", "both"], default="both")
    parser.add_argument("--manifest-url", help="Override configured remote manifest URL.")
    parser.add_argument("--force", action="store_true", help="Reinstall even when the remote version is not newer.")
    parser.add_argument("--dry-run", action="store_true", help="Check manifest and report planned update without replacing files.")
    args = parser.parse_args()

    try:
        result = update(args.surface, manifest_url_override=args.manifest_url, force=args.force, dry_run=args.dry_run)
    except Exception as exc:
        print(f"AI Worklog skill update failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
