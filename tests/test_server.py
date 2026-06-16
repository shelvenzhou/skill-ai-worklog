from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.ai_worklog_server.app import parse_records
from server.ai_worklog_server.metrics import compute_code_metrics
from server.ai_worklog_server.storage import WorklogStore


class ParseRecordsTests(unittest.TestCase):
    def test_parse_object_array_and_ndjson(self) -> None:
        self.assertEqual(parse_records(b'{"record_type":"event"}', "application/json")[0]["record_type"], "event")
        self.assertEqual(len(parse_records(b'[{"record_type":"event"},{"record_type":"snapshot"}]', "application/json")), 2)
        self.assertEqual(len(parse_records(b'{"record_type":"event"}\n{"record_type":"snapshot"}\n', "application/x-ndjson")), 2)


class StoreTests(unittest.TestCase):
    def test_insert_indexes_and_deduplicates_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorklogStore(Path(tmp))
            record = {
                "record_type": "event",
                "event_id": "e1",
                "source_id": "ai-worklog",
                "surface": "codex",
                "session_id": "s1",
                "hook_event_name": "UserPromptSubmit",
                "usage": {
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 2,
                            "output_tokens": 5,
                            "reasoning_output_tokens": 1,
                            "total_tokens": 16,
                        }
                    }
                },
            }
            self.assertEqual(store.insert_many([record]), {"accepted": 1, "duplicates": 0})
            self.assertEqual(store.insert_many([record]), {"accepted": 0, "duplicates": 1})
            self.assertEqual(store.count_records(), 1)
            self.assertEqual(store.query_records(session_id="s1")[0]["event_id"], "e1")
            stats = store.stats()
            self.assertEqual(stats["by_surface"], {"codex": 1})
            self.assertEqual(stats["token_totals"]["total_tokens"], 16)
            raw_files = list((Path(tmp) / "raw").glob("*.jsonl"))
            self.assertEqual(len(raw_files), 1)
            self.assertEqual(json.loads(raw_files[0].read_text(encoding="utf-8").splitlines()[0])["event_id"], "e1")

    def test_indexes_hook_usage_token_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorklogStore(Path(tmp))
            store.insert_many(
                [
                    {
                        "record_type": "event",
                        "event_id": "e2",
                        "hook_usage": {
                            "last_token_usage": {
                                "input_tokens": 2,
                                "output_tokens": 3,
                                "total_tokens": 5,
                            }
                        },
                    }
                ]
            )
            self.assertEqual(store.stats()["token_totals"]["total_tokens"], 5)


class CodeMetricsTests(unittest.TestCase):
    def test_computes_generated_code_from_patch_payload(self) -> None:
        patch = """*** Begin Patch
*** Update File: src/app.py
@@
-old = 1
+new = 1
+extra = 2
*** End Patch
"""
        metrics = compute_code_metrics(
            [
                {
                    "record_type": "event",
                    "event_id": "e1",
                    "session_id": "s1",
                    "hook_event_name": "PostToolUse",
                    "content": {"tool_input": patch},
                },
                {
                    "record_type": "event",
                    "event_id": "e2",
                    "session_id": "s1",
                    "hook_event_name": "afterAgentResponse",
                    "content": {"response": "```python\nprint('not counted')\n```"},
                },
            ]
        )
        self.assertEqual(metrics["generated_code"]["additions"], 2)
        self.assertEqual(metrics["generated_code"]["deletions"], 1)
        self.assertEqual(metrics["generated_code"]["files"], 1)
        self.assertEqual(metrics["generated_code"]["events"], 1)
        self.assertEqual(metrics["by_session"]["s1"]["generated"]["additions"], 2)

    def test_computes_adopted_code_from_latest_workspace_diff(self) -> None:
        metrics = compute_code_metrics(
            [
                {
                    "record_type": "event",
                    "event_id": "old",
                    "session_id": "s1",
                    "received_at": "2026-06-16T01:00:00Z",
                    "hook_event_name": "Stop",
                    "workspace_diff": {
                        "files": [
                            {"path": "src/app.py", "additions": 10, "deletions": 1, "is_code": True},
                            {"path": "README.md", "additions": 20, "deletions": 0, "is_code": False},
                        ]
                    },
                },
                {
                    "record_type": "event",
                    "event_id": "new",
                    "session_id": "s1",
                    "received_at": "2026-06-16T02:00:00Z",
                    "hook_event_name": "Stop",
                    "workspace_diff": {
                        "files": [
                            {"path": "src/app.py", "additions": 7, "deletions": 2, "is_code": True},
                            {"path": "src/new.ts", "additions": 3, "deletions": 0, "is_code": True},
                        ]
                    },
                },
            ]
        )
        self.assertEqual(metrics["adopted_code"]["additions"], 10)
        self.assertEqual(metrics["adopted_code"]["deletions"], 2)
        self.assertEqual(metrics["adopted_code"]["files"], 2)
        self.assertEqual(metrics["adopted_code"]["sessions"], 1)
        self.assertEqual(metrics["by_session"]["s1"]["latest_workspace_diff_event_id"], "new")


if __name__ == "__main__":
    unittest.main()
