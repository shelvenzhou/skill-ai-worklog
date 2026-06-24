from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
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


class SkillMaintenanceTests(unittest.TestCase):
    def test_self_heal_detects_missing_hook_and_version_drift(self) -> None:
        maintenance = load_script("skill_maintenance_trigger")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            codex_home.mkdir()
            (codex_home / "hooks.json").write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
            (codex_home / "config.toml").write_text("[features]\nhooks = true\n", encoding="utf-8")

            old_codex = os.environ.get("CODEX_HOME")
            try:
                os.environ["CODEX_HOME"] = str(codex_home)
                cfg = {
                    "hook_set": "minimal",
                    "skill_update": {
                        "current_version": "0.0.1",
                        "self_heal_enabled": True,
                    },
                }

                self.assertTrue(maintenance.self_heal_needed("codex", cfg))
            finally:
                if old_codex is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_codex

    def test_auto_update_attempt_is_opt_in_and_throttled(self) -> None:
        maintenance = load_script("skill_maintenance_trigger")
        now = time.time()
        cfg = {
            "skill_update": {
                "enabled": True,
                "auto_update": True,
                "auto_update_interval_seconds": 60,
            }
        }

        self.assertFalse(maintenance.should_auto_update_attempt({"skill_update": {"auto_update": False}}, {}, now=now))
        self.assertFalse(maintenance.should_auto_update_attempt(cfg, {"last_auto_update_attempt_epoch": now - 30}, now=now))
        self.assertTrue(maintenance.should_auto_update_attempt(cfg, {"last_auto_update_attempt_epoch": now - 61}, now=now))


if __name__ == "__main__":
    unittest.main()
