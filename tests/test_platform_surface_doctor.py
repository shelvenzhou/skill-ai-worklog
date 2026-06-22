from __future__ import annotations

import importlib.util
import json
import os
import sys
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


class PlatformSurfaceDoctorTests(unittest.TestCase):
    def test_surface_paths_use_environment_overrides(self) -> None:
        surfaces = load_script("surfaces")
        with tempfile.TemporaryDirectory() as tmp:
            old_codex = os.environ.get("CODEX_HOME")
            old_cursor = os.environ.get("CURSOR_HOME")
            try:
                os.environ["CODEX_HOME"] = str(Path(tmp) / "codex-home")
                os.environ["CURSOR_HOME"] = str(Path(tmp) / "cursor-home")

                self.assertEqual(surfaces.CODEX.home, Path(tmp) / "codex-home")
                self.assertEqual(surfaces.CODEX.skill_dir, Path(tmp) / "codex-home" / "skills" / "ai-worklog")
                self.assertEqual(surfaces.CURSOR.hooks_path, Path(tmp) / "cursor-home" / "hooks.json")
                self.assertFalse(surfaces.CODEX.hook_schema_versioned)
                self.assertTrue(surfaces.CURSOR.hook_schema_versioned)
            finally:
                if old_codex is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_codex
                if old_cursor is None:
                    os.environ.pop("CURSOR_HOME", None)
                else:
                    os.environ["CURSOR_HOME"] = old_cursor

    def test_windows_runtime_detection_fails_without_python_candidates(self) -> None:
        platforms = load_script("platforms")
        old_executable = platforms.sys.executable
        old_which = platforms.shutil.which
        old_env = os.environ.get("AI_WORKLOG_PYTHON")
        try:
            os.environ.pop("AI_WORKLOG_PYTHON", None)
            platforms.sys.executable = ""
            platforms.shutil.which = lambda name: None

            runtime = platforms.PlatformSpec(name="windows", is_windows=True).detect_python_runtime()

            self.assertFalse(runtime.ok)
            self.assertIn("No Python runtime", runtime.message)
        finally:
            platforms.sys.executable = old_executable
            platforms.shutil.which = old_which
            if old_env is None:
                os.environ.pop("AI_WORKLOG_PYTHON", None)
            else:
                os.environ["AI_WORKLOG_PYTHON"] = old_env

    def test_windows_acl_detection_requires_readable_principal(self) -> None:
        platforms = load_script("platforms")
        spec = platforms.PlatformSpec(name="windows", is_windows=True)
        original_run = platforms.subprocess.run

        class Completed:
            returncode = 0
            stdout = "C:\\\\skill NT AUTHORITY\\\\SYSTEM:(OI)(CI)(F)\\n          OWNER RIGHTS:(OI)(CI)(F)"

        try:
            platforms.subprocess.run = lambda *args, **kwargs: Completed()
            ok, details = spec.skill_acl_is_readable(Path("C:/skill"))
        finally:
            platforms.subprocess.run = original_run

        self.assertFalse(ok)
        self.assertIn("OWNER RIGHTS", details)

    def test_doctor_reads_flat_cursor_hook_entry(self) -> None:
        doctor = load_script("doctor")
        command = r"C:\Users\user\.cursor\skills\ai-worklog\scripts\ai-worklog-hook-cursor.cmd"

        self.assertEqual(doctor.hook_command_from_entry({"command": command}), command)

    def test_doctor_json_reports_hook_and_update_status(self) -> None:
        doctor = load_script("doctor")
        journal = load_script("journal")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            skill_dir = codex_home / "skills" / "ai-worklog"
            (skill_dir / "scripts").mkdir(parents=True)
            for rel in ("SKILL.md", "skill-version.json", "scripts/journal.py", "scripts/install.py"):
                (skill_dir / rel).write_text("{}\n", encoding="utf-8")
            (codex_home / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /tmp/ai-worklog/scripts/journal.py --surface codex",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (codex_home / "config.toml").write_text("[features]\nhooks = true\n", encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"local_log_dir": str(root / "events"), "hook_set": "minimal"}) + "\n", encoding="utf-8")
            (root / "skill_update_state.json").write_text(json.dumps({"update_available": True}) + "\n", encoding="utf-8")

            old_home = os.environ.get("CODEX_HOME")
            old_default_home = journal.DEFAULT_HOME
            old_default_config = journal.DEFAULT_CONFIG_PATH
            old_doctor_home = doctor.journal.DEFAULT_HOME
            old_doctor_config = doctor.journal.DEFAULT_CONFIG_PATH
            try:
                os.environ["CODEX_HOME"] = str(codex_home)
                journal.DEFAULT_HOME = root
                journal.DEFAULT_CONFIG_PATH = config_path
                doctor.journal.DEFAULT_HOME = root
                doctor.journal.DEFAULT_CONFIG_PATH = config_path

                report = doctor.diagnose("codex")

                self.assertEqual(report["summary"]["status"], "warn")
                self.assertTrue(any(item["name"] == "hooks" and item["status"] == "ok" for item in report["checks"]))
                self.assertTrue(any(item["name"] == "skill_update" and item["status"] == "warn" for item in report["checks"]))
                self.assertTrue(any("update_skill.py" in command for command in report["recommended_commands"]))
            finally:
                if old_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_home
                journal.DEFAULT_HOME = old_default_home
                journal.DEFAULT_CONFIG_PATH = old_default_config
                doctor.journal.DEFAULT_HOME = old_doctor_home
                doctor.journal.DEFAULT_CONFIG_PATH = old_doctor_config

    def test_codex_app_server_check_warns_for_untrusted_hooks(self) -> None:
        doctor = load_script("doctor")
        response = {
            "id": 2,
            "result": {
                "data": [
                    {
                        "cwd": "/tmp/project",
                        "hooks": [
                            {
                                "eventName": "userPromptSubmit",
                                "command": "/tmp/.codex/skills/ai-worklog/scripts/ai-worklog-hook-codex",
                                "enabled": True,
                                "trustStatus": "untrusted",
                            }
                        ],
                        "warnings": [],
                        "errors": [],
                    }
                ]
            },
        }

        result = doctor.codex_app_server_hooks_check_from_response(Path("/usr/bin/codex"), response)

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["name"], "codex_app_server_hooks")
        self.assertEqual(result["details"]["hook_count"], 1)
        self.assertEqual(result["details"]["trust_statuses"], ["untrusted"])

if __name__ == "__main__":
    unittest.main()
