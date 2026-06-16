from __future__ import annotations

import importlib.util
import io
import json
import argparse
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


def load_installer():
    path = Path(__file__).resolve().parents[1] / "skills" / "ai-worklog" / "scripts" / "install.py"
    spec = importlib.util.spec_from_file_location("ai_worklog_install", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallScriptTests(unittest.TestCase):
    def test_python_command_exits_zero_when_journal_is_missing(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "ai-worklog"
            command = installer.python_command(skill_dir, "codex", Path(tmp) / "config.json")

            result = subprocess.run(command, shell=True, input="{}", text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0)
            self.assertNotIn("can't open file", result.stderr)

    def test_python_command_runs_existing_journal(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "ai-worklog"
            journal = skill_dir / "scripts" / "journal.py"
            journal.parent.mkdir(parents=True)
            journal.write_text(
                "import sys\n"
                "payload = sys.stdin.read()\n"
                "print(payload)\n",
                encoding="utf-8",
            )
            command = installer.python_command(skill_dir, "codex", Path(tmp) / "config.json")

            result = subprocess.run(command, shell=True, input="payload", text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "payload")

    def test_remove_hooks_removes_only_ai_worklog_entries(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PostToolUse": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /tmp/ai-worklog/scripts/journal.py --surface codex",
                                        }
                                    ]
                                },
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /tmp/other.py",
                                        }
                                    ]
                                },
                            ],
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /tmp/ai-usage-collector/collector.py",
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            removed = installer.remove_hooks(hooks_path, versioned=False, dry_run=False)
            doc = json.loads(hooks_path.read_text(encoding="utf-8"))

            self.assertEqual(removed, 2)
            self.assertEqual(list(doc["hooks"].keys()), ["PostToolUse"])
            self.assertEqual(doc["hooks"]["PostToolUse"][0]["hooks"][0]["command"], "python3 /tmp/other.py")

    def test_disable_config_marks_collection_off_without_deleting_paths(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "collection_level": "full",
                        "local_log_dir": "/tmp/events",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            original_config_path = installer.CONFIG_PATH
            installer.CONFIG_PATH = config_path
            try:
                installer.disable_config(dry_run=False)
            finally:
                installer.CONFIG_PATH = original_config_path

            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["collection_level"], "off")
            self.assertTrue(cfg["uninstalled"])
            self.assertEqual(cfg["local_log_dir"], "/tmp/events")

    def test_update_config_writes_auto_backfill_defaults(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            original_config_path = installer.CONFIG_PATH
            installer.CONFIG_PATH = config_path
            args = argparse.Namespace(
                level="full",
                local_log_dir=str(Path(tmp) / "events"),
                snapshot_log_dir=str(Path(tmp) / "snapshots"),
                failed_log_dir=str(Path(tmp) / "failed"),
                server_url="http://collector.example/events",
                api_key_env="AI_WORKLOG_API_KEY",
                timeout=2.0,
                no_upload_preflight=False,
                max_transcript_bytes=1024,
                hook_set="minimal",
                no_auto_codex_backfill=False,
                backfill_batch_size=250,
                backfill_trigger_interval_seconds=86400,
                backfill_lock_stale_seconds=21600,
                backfill_limit=10,
                backfill_upload_state=str(Path(tmp) / "backfill.sqlite3"),
            )
            try:
                installer.update_config(args, dry_run=False)
            finally:
                installer.CONFIG_PATH = original_config_path

            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(cfg["server_url"], "http://collector.example/events")
            self.assertEqual(
                cfg["codex_history_backfill"],
                {
                    "enabled": True,
                    "batch_size": 250,
                    "trigger_interval_seconds": 86400,
                    "lock_stale_seconds": 21600,
                    "limit": 10,
                    "upload_state": str(Path(tmp) / "backfill.sqlite3"),
                },
            )

    def test_main_reports_permission_error_without_traceback(self) -> None:
        installer = load_installer()
        original_run = installer.run
        original_argv = sys.argv
        stderr = io.StringIO()

        def raise_permission_error(args):
            raise PermissionError("cannot write hooks.json.bak")

        installer.run = raise_permission_error
        sys.argv = ["install.py", "--surface", "codex"]
        try:
            with redirect_stderr(stderr):
                rc = installer.main()
        finally:
            installer.run = original_run
            sys.argv = original_argv

        output = stderr.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("Permission denied while updating AI worklog configuration", output)
        self.assertIn("cannot write hooks.json.bak", output)
        self.assertNotIn("Traceback", output)


if __name__ == "__main__":
    unittest.main()
