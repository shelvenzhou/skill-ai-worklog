from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
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


def make_skill(path: Path, version: str) -> None:
    (path / "scripts").mkdir(parents=True)
    (path / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (path / "skill-version.json").write_text(json.dumps({"name": "ai-worklog", "version": version}) + "\n", encoding="utf-8")
    (path / "scripts" / "journal.py").write_text("# journal\n", encoding="utf-8")
    (path / "scripts" / "install.py").write_text("# install\n", encoding="utf-8")


class UpdateSkillTests(unittest.TestCase):
    def test_update_replaces_skill_and_runs_installer_with_preserved_config(self) -> None:
        update_skill = load_script("update_skill")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            installed = codex_home / "skills" / "ai-worklog"
            make_skill(installed, "0.1.0")

            archive_src_root = root / "repo-main" / "skills" / "ai-worklog"
            make_skill(archive_src_root, "0.2.0")
            archive = root / "repo.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                tf.add(root / "repo-main", arcname="repo-main")

            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "collection_level": "diagnostic",
                        "hook_set": "minimal",
                        "local_log_dir": str(root / "events"),
                        "snapshot_log_dir": str(root / "snapshots"),
                        "failed_log_dir": str(root / "failed"),
                        "server_url": "http://collector/events",
                        "api_key_env": "TOKEN_ENV",
                        "request_timeout_seconds": 1.5,
                        "skill_update": {
                            "current_version": "0.1.0",
                            "manifest_url": "https://example.test/manifest.json",
                            "auto_update": True,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            calls: list[list[str]] = []

            def fake_fetch(url: str, timeout: float) -> dict[str, object]:
                self.assertEqual(url, "https://example.test/manifest.json")
                return {
                    "version": "0.2.0",
                    "archive_url": "https://example.test/repo.tar.gz",
                    "path": "skills/ai-worklog",
                    "install_url": "https://example.test/tree/skills/ai-worklog",
                }

            def fake_download(url: str, target: Path, timeout: float) -> None:
                self.assertEqual(url, "https://example.test/repo.tar.gz")
                shutil.copy2(archive, target)

            def fake_run(command: list[str], check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(command, 0)

            old_home = os.environ.get("CODEX_HOME")
            old_default_config = update_skill.journal.DEFAULT_CONFIG_PATH
            old_default_home = update_skill.journal.DEFAULT_HOME
            old_config_home = update_skill.install.CONFIG_HOME
            old_fetch = update_skill.fetch_manifest
            old_download = update_skill.download_archive
            old_run = update_skill.subprocess.run
            try:
                os.environ["CODEX_HOME"] = str(codex_home)
                update_skill.journal.DEFAULT_CONFIG_PATH = config
                update_skill.journal.DEFAULT_HOME = root
                update_skill.install.CONFIG_HOME = root
                update_skill.fetch_manifest = fake_fetch
                update_skill.download_archive = fake_download
                update_skill.subprocess.run = fake_run

                result = update_skill.update("codex")

                self.assertTrue(result["updated"])
                self.assertEqual(json.loads((installed / "skill-version.json").read_text(encoding="utf-8"))["version"], "0.2.0")
                self.assertTrue((root / "backups" / "skills").exists())
                self.assertEqual(len(calls), 1)
                self.assertIn("--server-url", calls[0])
                self.assertIn("http://collector/events", calls[0])
                self.assertIn("--api-key-env", calls[0])
                self.assertIn("TOKEN_ENV", calls[0])
                self.assertIn("--auto-skill-update", calls[0])
            finally:
                if old_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_home
                update_skill.journal.DEFAULT_CONFIG_PATH = old_default_config
                update_skill.journal.DEFAULT_HOME = old_default_home
                update_skill.install.CONFIG_HOME = old_config_home
                update_skill.fetch_manifest = old_fetch
                update_skill.download_archive = old_download
                update_skill.subprocess.run = old_run

    def test_update_rejects_manifest_without_machine_install_fields(self) -> None:
        update_skill = load_script("update_skill")
        with self.assertRaises(ValueError):
            update_skill.archive_url({"version": "0.2.0", "install_url": "https://example.test/human"})


if __name__ == "__main__":
    unittest.main()
