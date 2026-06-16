from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
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


class UpdateCheckTests(unittest.TestCase):
    def test_version_relation_detects_newer_remote_version(self) -> None:
        check_update = load_script("check_update")

        self.assertEqual(check_update.version_relation("0.3.0", "0.3.1"), "newer")
        self.assertEqual(check_update.version_relation("0.3.0", "0.3.0"), "equal")
        self.assertEqual(check_update.version_relation("0.3.1", "0.3.0"), "older")

    def test_check_writes_notice_for_newer_manifest(self) -> None:
        check_update = load_script("check_update")
        journal = load_script("journal")

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "name": "ai-worklog",
                        "version": "0.3.1",
                        "install_url": "https://gitlab.example/group/repo/-/tree/master/skills/ai-worklog",
                    }
                ).encode("utf-8")

        original_urlopen = check_update.urllib.request.urlopen

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            self.assertEqual(timeout, 1.0)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = journal.default_config()
            cfg["request_timeout_seconds"] = 1.0
            cfg["skill_update"].update(
                {
                    "current_version": "0.3.0",
                    "manifest_url": "https://gitlab.example/group/repo/-/raw/master/skills/ai-worklog/skill-version.json",
                    "state_path": str(root / "state.json"),
                    "notice_path": str(root / "notice.txt"),
                }
            )

            try:
                check_update.urllib.request.urlopen = fake_urlopen
                result = check_update.check(cfg, force=True)
            finally:
                check_update.urllib.request.urlopen = original_urlopen

            self.assertTrue(result["update_available"])
            notice = (root / "notice.txt").read_text(encoding="utf-8")
            self.assertIn("installed 0.3.0, remote 0.3.1", notice)
            self.assertIn("gitlab.example", notice)

    def test_journal_emits_existing_update_notice_on_session_start(self) -> None:
        journal = load_script("journal")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            notice = root / "notice.txt"
            state.write_text(json.dumps({"update_available": True}) + "\n", encoding="utf-8")
            notice.write_text("update available\n", encoding="utf-8")
            cfg = journal.default_config()
            cfg["skill_update"].update(
                {
                    "manifest_url": "https://example.com/manifest.json",
                    "state_path": str(state),
                    "notice_path": str(notice),
                    "notify_interval_seconds": 0,
                }
            )

            stderr = StringIO()
            with redirect_stderr(stderr):
                journal.maybe_emit_skill_update_notice({"hook_event_name": "SessionStart"}, cfg)

            self.assertIn("update available", stderr.getvalue())
            updated_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertIn("last_notified_epoch", updated_state)


if __name__ == "__main__":
    unittest.main()
