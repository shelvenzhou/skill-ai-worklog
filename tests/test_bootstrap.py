from __future__ import annotations

import argparse
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "ai-worklog" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_script(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_skill(path: Path) -> None:
    (path / "scripts").mkdir(parents=True)
    (path / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (path / "skill-version.json").write_text(json.dumps({"name": "ai-worklog", "version": "0.3.5"}) + "\n", encoding="utf-8")
    (path / "scripts" / "install.py").write_text("# install\n", encoding="utf-8")
    (path / "scripts" / "doctor.py").write_text("# doctor\n", encoding="utf-8")


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_runs_installer_doctor_and_prints_pass_marker(self) -> None:
        bootstrap = load_script("bootstrap")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_src = root / "repo-master" / "skills" / "ai-worklog"
            make_skill(archive_src)
            archive = root / "repo.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                tf.add(root / "repo-master", arcname="repo-master")

            commands: list[list[str]] = []

            def fake_fetch(url: str, timeout: float) -> dict[str, object]:
                return {
                    "version": "0.3.5",
                    "archive_url": "https://example.test/repo.tar.gz",
                    "path": "skills/ai-worklog",
                    "install_url": "https://example.test/tree/skills/ai-worklog",
                }

            def fake_download(url: str, target: Path, timeout: float) -> None:
                shutil.copy2(archive, target)

            def fake_run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                if "doctor.py" in command[1]:
                    report = {
                        "summary": {"status": "warn", "ok": 3, "warn": 1, "fail": 0},
                        "checks": [
                            {"name": "hooks", "surface": "codex", "status": "ok", "details": {"installed_events": ["SessionStart", "Stop"]}},
                            {"name": "hooks", "surface": "cursor", "status": "ok", "details": {"installed_events": ["sessionStart"]}},
                        ],
                    }
                    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(report), stderr="")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            old_fetch = bootstrap.fetch_manifest
            old_download = bootstrap.download_archive
            old_run = bootstrap.run_command
            try:
                bootstrap.fetch_manifest = fake_fetch
                bootstrap.download_archive = fake_download
                bootstrap.run_command = fake_run
                args = argparse.Namespace(
                    surface="both",
                    level="full",
                    hook_set="minimal",
                    manifest_url="https://example.test/manifest.json",
                    server_url=None,
                    api_key_env="AI_WORKLOG_API_KEY",
                    auto_skill_update=False,
                    timeout=1.0,
                )
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    rc = bootstrap.bootstrap(args)
            finally:
                bootstrap.fetch_manifest = old_fetch
                bootstrap.download_archive = old_download
                bootstrap.run_command = old_run

            self.assertEqual(rc, 0)
            self.assertIn("AI_WORKLOG_INSTALL: PASS (codex_hooks=2 cursor_hooks=1 warn=1)", stdout.getvalue())
            self.assertIn("--surface", commands[0])
            self.assertIn("both", commands[0])
            self.assertIn("--skill-update-manifest-url", commands[0])
            self.assertEqual(len(commands), 2)

    def test_merged_manifest_falls_back_to_baked_source_fields(self) -> None:
        bootstrap = load_script("bootstrap")
        old_fetch = bootstrap.fetch_manifest
        try:
            bootstrap.fetch_manifest = lambda url, timeout: (_ for _ in ()).throw(OSError("offline"))
            manifest = bootstrap.merged_manifest("https://example.test/manifest.json", 1.0)
        finally:
            bootstrap.fetch_manifest = old_fetch

        self.assertEqual(manifest["repo"], "shelvenzhou/skill-ai-worklog")
        self.assertEqual(manifest["ref"], "master")
        self.assertEqual(manifest["path"], "skills/ai-worklog")


if __name__ == "__main__":
    unittest.main()
