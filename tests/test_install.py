from __future__ import annotations

import importlib.util
import io
import json
import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


def load_installer():
    path = Path(__file__).resolve().parents[1] / "skills" / "ai-worklog" / "scripts" / "install.py"
    scripts_dir = path.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
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

    def test_python_command_uses_cmd_launcher_on_windows(self) -> None:
        installer = load_installer()
        original_os_name = installer.os.name
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "ai-worklog"
            config_path = Path(tmp) / "config.json"
            try:
                installer.os.name = "nt"
                command = installer.python_command(skill_dir, "codex", config_path)
            finally:
                installer.os.name = original_os_name

            launcher = skill_dir / "scripts" / "ai-worklog-hook-codex.cmd"
            content = launcher.read_text(encoding="utf-8")
            self.assertIn(str(launcher), command)
            self.assertNotIn("/bin/sh", command)
            self.assertIn("if not exist", content)
            self.assertIn("journal.py", content)
            self.assertIn("PYTHONUTF8=1", content)
            self.assertIn("PYTHONIOENCODING=utf-8", content)
            self.assertIn("AI_WORKLOG_PYTHON", content)
            self.assertNotIn("uv run", content)
            self.assertIn("runtime.log", content)
            self.assertIn("--surface \"codex\"", content)

            try:
                installer.os.name = "nt"
                entry = installer.hook_entry(command)
            finally:
                installer.os.name = original_os_name
            hook = entry["hooks"][0]
            self.assertEqual(hook["command"], command)
            self.assertEqual(hook["commandWindows"], command)

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

    def test_read_json_accepts_utf8_bom(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            path.write_text('{"hooks": {}}\n', encoding="utf-8-sig")

            self.assertEqual(installer.read_json(path), {"hooks": {}})

    def test_write_json_keeps_existing_file_when_atomic_replace_fails(self) -> None:
        installer = load_installer()
        original_replace = installer.os.replace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            original = {"version": 1, "hooks": {"sessionStart": [{"command": "python old.py"}]}}
            path.write_text(json.dumps(original) + "\n", encoding="utf-8")

            def fail_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
                raise OSError("replace failed")

            try:
                installer.os.replace = fail_replace
                with self.assertRaises(OSError):
                    installer.write_json(path, {"version": 1, "hooks": {}}, dry_run=False)
            finally:
                installer.os.replace = original_replace

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertFalse(list(Path(tmp).glob(".hooks.json.tmp-*")))

    def test_windows_upgrade_rewrites_existing_hooks_without_bom(self) -> None:
        installer = load_installer()
        original_os_name = installer.os.name
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            old_command = r"C:\Users\user\.codex\skills\ai-worklog\scripts\ai-worklog-hook-codex.cmd"
            other_command = "python other.py"
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": old_command},
                                        {"type": "command", "command": other_command},
                                    ]
                                }
                            ]
                        }
                    }
                )
                + "\n",
                encoding="utf-8-sig",
            )
            try:
                installer.os.name = "nt"
                installer.merge_hooks(hooks_path, ["UserPromptSubmit"], old_command, versioned=False, dry_run=False)
            finally:
                installer.os.name = original_os_name

            raw = hooks_path.read_bytes()
            self.assertEqual(raw[:1], b"{")
            doc = json.loads(raw.decode("utf-8"))
            entries = doc["hooks"]["UserPromptSubmit"]
            commands = [hook["command"] for entry in entries for hook in entry["hooks"]]
            self.assertEqual(commands.count(old_command), 1)
            self.assertIn(other_command, commands)
            worklog_hook = next(hook for entry in entries for hook in entry["hooks"] if hook["command"] == old_command)
            self.assertEqual(worklog_hook["commandWindows"], old_command)

    def test_cursor_merge_writes_flat_hooks_and_migrates_old_nested_entries(self) -> None:
        installer = load_installer()
        original_os_name = installer.os.name
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            old_command = r"C:\Users\user\.cursor\skills\ai-worklog\scripts\ai-worklog-hook-cursor.cmd"
            other_command = "python other.py"
            hooks_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "hooks": {
                            "sessionStart": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": old_command,
                                            "commandWindows": old_command,
                                        },
                                        {
                                            "type": "command",
                                            "command": other_command,
                                        },
                                    ]
                                },
                                {"command": old_command},
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                installer.os.name = "nt"
                installer.merge_hooks(
                    hooks_path,
                    ["sessionStart"],
                    old_command,
                    versioned=True,
                    dry_run=False,
                    entry_style="cursor",
                )
            finally:
                installer.os.name = original_os_name

            doc = json.loads(hooks_path.read_text(encoding="utf-8"))
            entries = doc["hooks"]["sessionStart"]
            self.assertEqual(doc["version"], 1)
            self.assertEqual([entry["command"] for entry in entries], [other_command, old_command])
            for entry in entries:
                self.assertNotIn("hooks", entry)
                self.assertNotIn("commandWindows", entry)
                self.assertNotIn("type", entry)

    def test_cursor_remove_handles_flat_and_legacy_nested_entries(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"
            old_command = r"C:\Users\user\.cursor\skills\ai-worklog\scripts\ai-worklog-hook-cursor.cmd"
            other_command = "python other.py"
            hooks_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "hooks": {
                            "postToolUse": [
                                {"command": old_command},
                                {
                                    "hooks": [
                                        {"type": "command", "command": old_command},
                                        {"type": "command", "command": other_command},
                                    ]
                                },
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            removed = installer.remove_hooks(hooks_path, versioned=True, dry_run=False, entry_style="cursor")
            doc = json.loads(hooks_path.read_text(encoding="utf-8"))

            self.assertEqual(removed, 2)
            self.assertEqual(doc["hooks"]["postToolUse"], [{"command": other_command}])

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

    def test_replace_skill_repairs_acl_after_move(self) -> None:
        installer = load_installer()
        calls: list[Path] = []

        class FakePlatform:
            def repair_skill_acl(self, path: Path) -> tuple[bool, str]:
                calls.append(path)
                return True, "ok"

        def make_skill(path: Path) -> None:
            (path / "scripts").mkdir(parents=True)
            (path / "SKILL.md").write_text("# skill\n", encoding="utf-8")
            (path / "skill-version.json").write_text("{}\n", encoding="utf-8")
            (path / "scripts" / "journal.py").write_text("# journal\n", encoding="utf-8")
            (path / "scripts" / "install.py").write_text("# install\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dest = root / "dest"
            make_skill(src)
            make_skill(dest)
            original_current_platform = installer.platforms.current_platform
            original_config_home = installer.CONFIG_HOME
            try:
                installer.platforms.current_platform = lambda: FakePlatform()
                installer.CONFIG_HOME = root / "home"
                installer.replace_skill_from_source(src, dest, dry_run=False, label="codex")
            finally:
                installer.platforms.current_platform = original_current_platform
                installer.CONFIG_HOME = original_config_home

            self.assertEqual(calls, [dest])
            self.assertTrue((root / "home" / "backups" / "skills").exists())

    def test_codex_install_uses_hooks_json_and_removes_stale_inline_block(self) -> None:
        installer = load_installer()

        def make_skill(path: Path) -> None:
            (path / "scripts").mkdir(parents=True)
            (path / "SKILL.md").write_text("# skill\n", encoding="utf-8")
            (path / "skill-version.json").write_text("{}\n", encoding="utf-8")
            (path / "scripts" / "journal.py").write_text("# journal\n", encoding="utf-8")
            (path / "scripts" / "install.py").write_text("# install\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            skill_src = root / "src"
            make_skill(skill_src)
            config_toml = codex_home / "config.toml"
            config_toml.parent.mkdir(parents=True)
            config_toml.write_text(
                "[features]\n"
                "hooks = false\n\n"
                "# BEGIN AI_WORKLOG_HOOKS\n"
                "old = true\n"
                "# END AI_WORKLOG_HOOKS\n",
                encoding="utf-8",
            )
            old_source_skill_dir = installer.source_skill_dir
            old_codex_home = installer.codex_home
            try:
                installer.source_skill_dir = lambda: skill_src
                installer.codex_home = lambda: codex_home
                args = argparse.Namespace(dry_run=False, hook_set="minimal")

                installer.install_codex(args)
            finally:
                installer.source_skill_dir = old_source_skill_dir
                installer.codex_home = old_codex_home

            hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("SessionStart", hooks["hooks"])
            self.assertIn("UserPromptSubmit", hooks["hooks"])
            config_text = config_toml.read_text(encoding="utf-8")
            self.assertIn("[features]\nhooks = true", config_text)
            self.assertNotIn("BEGIN AI_WORKLOG_HOOKS", config_text)
            self.assertNotIn("old = true", config_text)

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
                clear_server_url=False,
                api_key_env="AI_WORKLOG_API_KEY",
                timeout=2.0,
                sync_upload=False,
                no_upload_preflight=False,
                max_transcript_bytes=1024,
                hook_set="minimal",
                async_upload_batch_size=100,
                async_upload_trigger_interval_seconds=60,
                async_upload_lock_stale_seconds=600,
                async_upload_lock_wait_seconds=30,
                async_upload_max_runtime_seconds=120,
                no_skill_update_check=False,
                skill_update_manifest_url="https://example.com/manifest.json",
                skill_source_url="https://example.com/skill",
                skill_update_trigger_interval_seconds=86400,
                auto_skill_update=True,
                no_auto_codex_backfill=False,
                backfill_batch_size=250,
                backfill_trigger_interval_seconds=86400,
                backfill_lock_stale_seconds=21600,
                backfill_lock_wait_seconds=30,
                backfill_max_runtime_seconds=1800,
                backfill_limit=10,
                backfill_upload_state=str(Path(tmp) / "backfill.sqlite3"),
            )
            try:
                installer.update_config(args, dry_run=False)
            finally:
                installer.CONFIG_PATH = original_config_path

            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(cfg["server_url"], "http://collector.example/events")
            self.assertEqual(cfg["upload_mode"], "async")
            self.assertEqual(
                cfg["async_upload"],
                {
                    "enabled": True,
                    "batch_size": 100,
                    "trigger_interval_seconds": 60,
                    "lock_stale_seconds": 600,
                    "lock_wait_seconds": 30,
                    "max_runtime_seconds": 120,
                },
            )
            self.assertEqual(
                cfg["skill_update"],
                {
                    "enabled": True,
                    "name": "ai-worklog",
                    "current_version": "0.3.5",
                    "manifest_url": "https://example.com/manifest.json",
                    "source_url": "https://example.com/skill",
                    "trigger_interval_seconds": 86400,
                    "auto_update": True,
                    "self_heal_enabled": True,
                },
            )
            self.assertEqual(
                cfg["codex_history_backfill"],
                {
                    "enabled": True,
                    "batch_size": 250,
                    "trigger_interval_seconds": 86400,
                    "lock_stale_seconds": 21600,
                    "lock_wait_seconds": 30,
                    "max_runtime_seconds": 1800,
                    "limit": 10,
                    "upload_state": str(Path(tmp) / "backfill.sqlite3"),
                },
            )

    def test_update_config_preserves_existing_server_url_when_omitted(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"server_url": "http://collector.example/events"}) + "\n", encoding="utf-8")
            original_config_path = installer.CONFIG_PATH
            installer.CONFIG_PATH = config_path
            args = argparse.Namespace(
                level="full",
                local_log_dir=str(Path(tmp) / "events"),
                snapshot_log_dir=str(Path(tmp) / "snapshots"),
                failed_log_dir=str(Path(tmp) / "failed"),
                server_url=None,
                clear_server_url=False,
                api_key_env="AI_WORKLOG_API_KEY",
                timeout=2.0,
                sync_upload=False,
                no_upload_preflight=False,
                max_transcript_bytes=1024,
                hook_set="minimal",
                async_upload_batch_size=100,
                async_upload_trigger_interval_seconds=60,
                async_upload_lock_stale_seconds=600,
                async_upload_lock_wait_seconds=30,
                async_upload_max_runtime_seconds=120,
                no_skill_update_check=False,
                skill_update_manifest_url="https://example.com/manifest.json",
                skill_source_url="https://example.com/skill",
                skill_update_trigger_interval_seconds=86400,
                auto_skill_update=False,
                no_auto_codex_backfill=False,
                backfill_batch_size=250,
                backfill_trigger_interval_seconds=86400,
                backfill_lock_stale_seconds=21600,
                backfill_lock_wait_seconds=30,
                backfill_max_runtime_seconds=1800,
                backfill_limit=None,
                backfill_upload_state=None,
            )
            try:
                installer.update_config(args, dry_run=False)
            finally:
                installer.CONFIG_PATH = original_config_path

            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(cfg["server_url"], "http://collector.example/events")

    def test_update_config_can_explicitly_clear_server_url(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"server_url": "http://collector.example/events"}) + "\n", encoding="utf-8")
            original_config_path = installer.CONFIG_PATH
            installer.CONFIG_PATH = config_path
            args = argparse.Namespace(
                level="full",
                local_log_dir=str(Path(tmp) / "events"),
                snapshot_log_dir=str(Path(tmp) / "snapshots"),
                failed_log_dir=str(Path(tmp) / "failed"),
                server_url=None,
                clear_server_url=True,
                api_key_env="AI_WORKLOG_API_KEY",
                timeout=2.0,
                sync_upload=False,
                no_upload_preflight=False,
                max_transcript_bytes=1024,
                hook_set="minimal",
                async_upload_batch_size=100,
                async_upload_trigger_interval_seconds=60,
                async_upload_lock_stale_seconds=600,
                async_upload_lock_wait_seconds=30,
                async_upload_max_runtime_seconds=120,
                no_skill_update_check=False,
                skill_update_manifest_url="https://example.com/manifest.json",
                skill_source_url="https://example.com/skill",
                skill_update_trigger_interval_seconds=86400,
                auto_skill_update=False,
                no_auto_codex_backfill=False,
                backfill_batch_size=250,
                backfill_trigger_interval_seconds=86400,
                backfill_lock_stale_seconds=21600,
                backfill_lock_wait_seconds=30,
                backfill_max_runtime_seconds=1800,
                backfill_limit=None,
                backfill_upload_state=None,
            )
            try:
                installer.update_config(args, dry_run=False)
            finally:
                installer.CONFIG_PATH = original_config_path

            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIsNone(cfg["server_url"])

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
