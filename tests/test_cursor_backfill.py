from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "ai-worklog" / "scripts"


def load_script(name: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CursorBackfillTests(unittest.TestCase):
    def test_backfills_cursor_agent_transcript_once(self) -> None:
        cursor_backfill = load_script("cursor_backfill")
        journal = load_script("journal")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".cursor" / "projects"
            transcript = root / "project-1" / "agent-transcripts" / "session-1" / "session-1.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "hello"}]}}),
                        json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
                        json.dumps({"type": "turn_ended", "status": "success"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            cfg = journal.default_config()
            cfg["local_log_dir"] = str(Path(tmp) / "events")
            cfg["snapshot_log_dir"] = str(Path(tmp) / "snapshots")
            cfg["state_path"] = str(Path(tmp) / "state.json")

            first = cursor_backfill.backfill(root, cfg)
            second = cursor_backfill.backfill(root, cfg)

            self.assertEqual(first["transcripts"], 1)
            self.assertEqual(first["events"], 3)
            self.assertEqual(second["events"], 0)

            event_lines = list((Path(tmp) / "events").glob("*.jsonl"))[0].read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in event_lines]
            self.assertEqual([event["surface"] for event in events], ["cursor", "cursor", "cursor"])
            self.assertEqual([event["session_id"] for event in events], ["session-1", "session-1", "session-1"])
            self.assertEqual(events[0]["hook_event_name"], "beforeSubmitPrompt")
            self.assertEqual(events[1]["hook_event_name"], "afterAgentResponse")
            self.assertEqual(events[2]["hook_event_name"], "stop")


if __name__ == "__main__":
    unittest.main()
