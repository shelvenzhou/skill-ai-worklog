from __future__ import annotations

import json
import threading
import tempfile
import unittest
import urllib.request
from pathlib import Path

from server.ai_worklog_server.analysis import build_session_detail, build_sessions_index
from server.ai_worklog_server.app import build_server, parse_records
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

    def test_queries_events_and_snapshots_for_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorklogStore(Path(tmp))
            store.insert_many(
                [
                    {
                        "record_type": "event",
                        "event_id": "e1",
                        "surface": "codex",
                        "session_id": "s1",
                        "environment_ref": "env1",
                        "session_ref": "sess1",
                    },
                    {
                        "record_type": "snapshot",
                        "snapshot_id": "env1",
                        "snapshot_type": "environment",
                        "surface": "codex",
                        "environment": {"cwd": "/tmp/project"},
                    },
                    {
                        "record_type": "snapshot",
                        "snapshot_id": "sess1",
                        "snapshot_type": "session",
                        "surface": "codex",
                        "session": {"session_id": "s1"},
                    },
                ]
            )
            events = store.query_events_for_analysis(session_id="s1")
            snapshots = store.query_snapshots_by_ids(["env1", "sess1"])
            self.assertEqual([event["event_id"] for event in events], ["e1"])
            self.assertEqual({snapshot["snapshot_id"] for snapshot in snapshots}, {"env1", "sess1"})


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


class SessionAnalysisTests(unittest.TestCase):
    def test_builds_session_index_and_detail_with_code_metrics(self) -> None:
        patch = """*** Begin Patch
*** Add File: src/app.py
+print("hello")
+print("world")
*** End Patch
"""
        events = [
            {
                "record_type": "event",
                "event_id": "e1",
                "received_at": "2026-06-16T01:00:00Z",
                "surface": "codex",
                "session_id": "s1",
                "hook_event_name": "PostToolUse",
                "environment_ref": "env1",
                "session_ref": "sess1",
                "content": {"tool_input": patch},
                "hook_usage": {"last_token_usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}},
            },
            {
                "record_type": "event",
                "event_id": "e2",
                "received_at": "2026-06-16T01:01:00Z",
                "surface": "codex",
                "session_id": "s1",
                "hook_event_name": "Stop",
                "environment_ref": "env1",
                "session_ref": "sess1",
                "workspace_diff": {"files": [{"path": "src/app.py", "additions": 2, "deletions": 0, "is_code": True}]},
            },
            {
                "record_type": "event",
                "event_id": "e3",
                "received_at": "2026-06-16T02:00:00Z",
                "surface": "cursor",
                "session_id": "s2",
                "hook_event_name": "stop",
            },
        ]
        snapshots = [
            {"record_type": "snapshot", "snapshot_id": "env1", "snapshot_type": "environment"},
            {"record_type": "snapshot", "snapshot_id": "sess1", "snapshot_type": "session"},
        ]

        index = build_sessions_index(events)
        self.assertEqual(index["total_sessions"], 2)
        first = index["sessions"][0]
        self.assertEqual(first["session_id"], "s2")
        s1 = next(session for session in index["sessions"] if session["session_id"] == "s1")
        self.assertEqual(s1["code_metrics"]["generated_code"]["additions"], 2)
        self.assertEqual(s1["code_metrics"]["adopted_code"]["additions"], 2)
        self.assertEqual(s1["token_totals"]["total_tokens"], 3)

        detail = build_session_detail("s1", events, snapshots)
        self.assertEqual(detail["event_count"], 2)
        self.assertEqual(detail["snapshots"]["environment"][0]["snapshot_id"], "env1")
        self.assertEqual(detail["code_metrics"]["generated_code"]["additions"], 2)
        self.assertEqual([event["event_id"] for event in detail["events"]], ["e1", "e2"])


class AppEndpointTests(unittest.TestCase):
    def test_session_and_code_metric_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = build_server("127.0.0.1", 0, Path(tmp), None)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                records = [
                    {
                        "record_type": "event",
                        "event_id": "e1",
                        "received_at": "2026-06-16T01:00:00Z",
                        "surface": "codex",
                        "session_id": "s1",
                        "hook_event_name": "PostToolUse",
                        "environment_ref": "env1",
                        "session_ref": "sess1",
                        "content": {"tool_input": "*** Begin Patch\n*** Add File: src/app.py\n+print(1)\n*** End Patch\n"},
                    },
                    {
                        "record_type": "event",
                        "event_id": "e2",
                        "received_at": "2026-06-16T01:01:00Z",
                        "surface": "codex",
                        "session_id": "s1",
                        "hook_event_name": "Stop",
                        "environment_ref": "env1",
                        "session_ref": "sess1",
                        "workspace_diff": {"files": [{"path": "src/app.py", "additions": 1, "deletions": 0, "is_code": True}]},
                    },
                    {"record_type": "snapshot", "snapshot_id": "env1", "snapshot_type": "environment"},
                    {"record_type": "snapshot", "snapshot_id": "sess1", "snapshot_type": "session"},
                ]
                request = urllib.request.Request(
                    f"{base_url}/events",
                    data=json.dumps(records).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 202)

                with urllib.request.urlopen(f"{base_url}/sessions", timeout=5) as response:
                    sessions = json.loads(response.read().decode("utf-8"))
                self.assertEqual(sessions["sessions"][0]["session_id"], "s1")
                self.assertEqual(sessions["sessions"][0]["code_metrics"]["generated_code"]["additions"], 1)

                with urllib.request.urlopen(f"{base_url}/sessions/s1", timeout=5) as response:
                    detail = json.loads(response.read().decode("utf-8"))
                self.assertEqual(detail["event_count"], 2)
                self.assertEqual(detail["snapshots"]["environment"][0]["snapshot_id"], "env1")

                with urllib.request.urlopen(f"{base_url}/metrics/code", timeout=5) as response:
                    metrics = json.loads(response.read().decode("utf-8"))
                self.assertEqual(metrics["generated_code"]["additions"], 1)
                self.assertEqual(metrics["adopted_code"]["additions"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
