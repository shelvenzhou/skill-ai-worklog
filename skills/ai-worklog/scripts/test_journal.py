#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import journal


class JournalTests(unittest.TestCase):
    def test_build_full_event_with_prompt_and_tool_result(self) -> None:
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "turn_id": "t1",
            "model": "gpt-test",
            "cwd": "/tmp",
            "prompt": "fix this",
            "tool_input": {"cmd": "echo hi"},
            "tool_response": {"output": "hi"},
        }
        event, snapshots = journal.build_records(payload, journal.default_config(), "codex", "test")
        assert event is not None
        self.assertEqual(event["surface"], "codex")
        self.assertEqual(event["content"]["prompt"], "fix this")
        self.assertEqual(event["content"]["tool_input"], {"cmd": "echo hi"})
        self.assertEqual(len(snapshots), 2)
        self.assertIn("environment_ref", event)
        self.assertIn("session_ref", event)
        self.assertNotIn("environment", event)
        self.assertIn("raw_hook_input", event)
        self.assertNotIn("session_id", event["raw_hook_input"])

    def test_diagnostic_summarizes_content(self) -> None:
        cfg = journal.default_config()
        cfg["collection_level"] = "diagnostic"
        event, _ = journal.build_records({"prompt": "secret prompt"}, cfg, "cursor", "test")
        assert event is not None
        self.assertEqual(event["content"]["prompt"]["length"], 13)
        self.assertNotIn("raw_hook_input", event)

    def test_extract_transcript_token_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "other"}}),
                        json.dumps(
                            {
                                "timestamp": "2026-06-15T00:00:00Z",
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "last_token_usage": {
                                            "input_tokens": 1,
                                            "cached_input_tokens": 0,
                                            "output_tokens": 2,
                                            "reasoning_output_tokens": 3,
                                            "total_tokens": 6,
                                        }
                                    },
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            usage = journal.extract_transcript_usage(str(path), 1024)
            assert usage is not None
            self.assertEqual(usage["info"]["last_token_usage"]["reasoning_output_tokens"], 3)

    def test_sensitive_keys_are_redacted(self) -> None:
        event, _ = journal.build_records(
            {"tool_input": {"api_key": "abc", "query": "ok"}},
            journal.default_config(),
            "codex",
            "test",
        )
        assert event is not None
        self.assertEqual(event["content"]["tool_input"]["api_key"], "[REDACTED]")
        self.assertEqual(event["content"]["tool_input"]["query"], "ok")

    def test_snapshot_written_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = journal.default_config()
            cfg["local_log_dir"] = str(Path(tmp) / "events")
            cfg["snapshot_log_dir"] = str(Path(tmp) / "snapshots")
            cfg["state_path"] = str(Path(tmp) / "state.json")
            _, snapshots = journal.build_records({"session_id": "s1"}, cfg, "codex", "test")
            assert snapshots
            first = journal.write_new_snapshots(snapshots, cfg)
            second = journal.write_new_snapshots(snapshots, cfg)
            self.assertEqual(len(first), 2)
            self.assertEqual(len(second), 0)

    def test_stop_event_records_workspace_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (repo / "app.py").write_text("old = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-m",
                    "init",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            (repo / "app.py").write_text("old = 1\nnew = 2\n", encoding="utf-8")
            (repo / "new.ts").write_text("export const value = 1;\n", encoding="utf-8")
            event, _ = journal.build_records(
                {"hook_event_name": "Stop", "session_id": "s1", "cwd": str(repo)},
                journal.default_config(),
                "codex",
                "test",
            )
            assert event is not None
            self.assertIn("workspace_diff", event)
            paths = {item["path"]: item for item in event["workspace_diff"]["files"]}
            self.assertEqual(paths["app.py"]["additions"], 1)
            self.assertTrue(paths["app.py"]["is_code"])
            self.assertEqual(paths["new.ts"]["additions"], 1)
            self.assertTrue(paths["new.ts"]["untracked"])


if __name__ == "__main__":
    unittest.main()
