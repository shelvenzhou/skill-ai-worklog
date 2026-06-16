#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import journal
import replay


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

    def test_structured_operation_tool_and_skill_metadata(self) -> None:
        event, _ = journal.build_records(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s1",
                "tool_name": "apply_patch",
                "tool_input": {"path": "src/app.py", "content": "print(1)\n"},
                "duration_ms": 12,
                "skill_name": "code-fix",
                "skill_version": "abc123",
            },
            journal.default_config(),
            "codex",
            "test",
        )
        assert event is not None
        self.assertEqual(event["event_schema_version"], "0.3")
        self.assertEqual(event["timeline"]["trace_id"], "s1")
        self.assertEqual(event["timeline"]["span_id"], event["event_id"])
        self.assertEqual(event["operation"]["category"], "tool")
        self.assertEqual(event["operation"]["phase"], "after")
        self.assertTrue(event["operation"]["success"])
        self.assertEqual(event["tool"]["name"], "apply_patch")
        self.assertEqual(event["tool"]["files_written"], ["src/app.py"])
        self.assertEqual(event["skill"]["name"], "code-fix")
        self.assertEqual(event["skill"]["version"], "abc123")

    def test_assigns_session_sequence_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = journal.default_config()
            cfg["state_path"] = str(Path(tmp) / "state.json")
            first, _ = journal.build_records({"session_id": "s1"}, cfg, "codex", "test")
            second, _ = journal.build_records({"session_id": "s1"}, cfg, "codex", "test")
            assert first is not None
            assert second is not None
            journal.assign_event_sequence(first, cfg)
            journal.assign_event_sequence(second, cfg)
            self.assertEqual(first["timeline"]["sequence_no"], 1)
            self.assertEqual(second["timeline"]["sequence_no"], 2)

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
            self.assertEqual(len(second), 2)
            snapshot_files = list((Path(tmp) / "snapshots").glob("*.jsonl"))
            self.assertEqual(len(snapshot_files), 1)
            self.assertEqual(len(snapshot_files[0].read_text(encoding="utf-8").splitlines()), 2)
            for snapshot in snapshots:
                journal.mark_remote_snapshot_known(snapshot, cfg)
            third = journal.write_new_snapshots(snapshots, cfg)
            self.assertEqual(len(third), 0)

    def test_upload_preflight_skips_existing_record(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"existing":["event:e1"],"missing":[]}'

        requests = []
        original_urlopen = journal.urllib.request.urlopen

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            requests.append(request)
            return FakeResponse()

        try:
            journal.urllib.request.urlopen = fake_urlopen
            cfg = journal.default_config()
            cfg["server_url"] = "http://127.0.0.1:8765/events"
            ok, error = journal.upload_event({"record_type": "event", "event_id": "e1"}, cfg)
        finally:
            journal.urllib.request.urlopen = original_urlopen

        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].full_url, "http://127.0.0.1:8765/events/exists")

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


class ReplayTests(unittest.TestCase):
    def test_load_replay_records_orders_snapshots_first_and_deduplicates_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = journal.default_config()
            cfg["snapshot_log_dir"] = str(root / "snapshots")
            cfg["local_log_dir"] = str(root / "events")
            cfg["failed_log_dir"] = str(root / "failed")

            (root / "snapshots").mkdir()
            (root / "events").mkdir()
            (root / "failed").mkdir()
            (root / "snapshots" / "2026-06-16.jsonl").write_text(
                json.dumps({"record_type": "snapshot", "snapshot_id": "s1"}) + "\n",
                encoding="utf-8",
            )
            (root / "events" / "2026-06-16.jsonl").write_text(
                json.dumps({"record_type": "event", "event_id": "e1"}) + "\n",
                encoding="utf-8",
            )
            (root / "failed" / "2026-06-16.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"record_type": "event", "event_id": "e1", "upload_error": "offline"}),
                        json.dumps({"record_type": "event", "event_id": "e2", "upload_failed_at": "now"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = replay.load_replay_records(cfg)
            self.assertEqual([journal.record_pk(record) for record in records], ["snapshot:s1", "event:e1", "event:e2"])
            self.assertNotIn("upload_failed_at", records[2])


if __name__ == "__main__":
    unittest.main()
