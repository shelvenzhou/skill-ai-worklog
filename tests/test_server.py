from __future__ import annotations

import json
import threading
import tempfile
import unittest
import urllib.request
from pathlib import Path

from server.ai_worklog_server.analysis import build_session_detail, build_sessions_index
from server.ai_worklog_server.app import build_server, parse_record_pks, parse_records
from server.ai_worklog_server.metrics import compute_code_metrics
from server.ai_worklog_server.storage import WorklogStore


class ParseRecordsTests(unittest.TestCase):
    def test_parse_object_array_and_ndjson(self) -> None:
        self.assertEqual(parse_records(b'{"record_type":"event"}', "application/json")[0]["record_type"], "event")
        self.assertEqual(len(parse_records(b'[{"record_type":"event"},{"record_type":"snapshot"}]', "application/json")), 2)
        self.assertEqual(len(parse_records(b'{"record_type":"event"}\n{"record_type":"snapshot"}\n', "application/x-ndjson")), 2)

    def test_parse_pretty_printed_json_with_newlines(self) -> None:
        body = json.dumps(
            [
                {"record_type": "event", "event_id": "e1"},
                {"record_type": "snapshot", "snapshot_id": "s1"},
            ],
            indent=2,
        ).encode("utf-8")

        records = parse_records(body, "application/json")

        self.assertEqual([record["record_type"] for record in records], ["event", "snapshot"])

    def test_parse_record_pks(self) -> None:
        self.assertEqual(parse_record_pks(b'{"record_pks":["event:e1"]}'), ["event:e1"])
        self.assertEqual(parse_record_pks(b'["snapshot:s1"]'), ["snapshot:s1"])


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
            self.assertEqual(store.existing_record_pks(["event:e1", "event:e2"]), {"event:e1"})
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

    def test_stats_deduplicate_repeated_transcript_token_usage(self) -> None:
        usage = {
            "source": "transcript_token_count",
            "timestamp": "2026-06-16T00:00:00Z",
            "info": {
                "last_token_usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = WorklogStore(Path(tmp))
            store.insert_many(
                [
                    {
                        "record_type": "event",
                        "event_id": "e1",
                        "session_id": "s1",
                        "hook_event_name": "PostToolUse",
                        "usage": usage,
                    },
                    {
                        "record_type": "event",
                        "event_id": "e2",
                        "session_id": "s1",
                        "hook_event_name": "Stop",
                        "usage": usage,
                    },
                ]
            )
            self.assertEqual(store.stats()["token_totals"]["total_tokens"], 15)

    def test_stats_groups_token_totals_by_model(self) -> None:
        repeated_usage = {
            "source": "transcript_token_count",
            "timestamp": "2026-06-16T00:00:00Z",
            "info": {"last_token_usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 5, "total_tokens": 15}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = WorklogStore(Path(tmp))
            store.insert_many(
                [
                    {
                        "record_type": "event",
                        "event_id": "e1",
                        "session_id": "s1",
                        "model": "gpt-a",
                        "usage": repeated_usage,
                    },
                    {
                        "record_type": "event",
                        "event_id": "e2",
                        "session_id": "s1",
                        "model": "gpt-a",
                        "usage": repeated_usage,
                    },
                    {
                        "record_type": "snapshot",
                        "snapshot_id": "sess2",
                        "snapshot_type": "session",
                        "session": {"session_id": "s2", "model": "gpt-b"},
                    },
                    {
                        "record_type": "event",
                        "event_id": "e3",
                        "session_id": "s2",
                        "hook_usage": {"last_token_usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}},
                    },
                ]
            )
            by_model = store.stats()["token_totals_by_model"]
            self.assertEqual(by_model["gpt-a"]["total_tokens"], 15)
            self.assertEqual(by_model["gpt-a"]["cached_input_tokens"], 4)
            self.assertEqual(by_model["gpt-b"]["total_tokens"], 5)

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

    def test_generated_code_ignores_failed_post_write_operations(self) -> None:
        patch = """*** Begin Patch
*** Add File: src/app.py
+print("failed")
*** End Patch
"""
        metrics = compute_code_metrics(
            [
                {
                    "record_type": "event",
                    "event_id": "e1",
                    "session_id": "s1",
                    "hook_event_name": "PostToolUse",
                    "operation": {"success": False},
                    "content": {"tool_input": patch},
                }
            ]
        )
        self.assertEqual(metrics["generated_code"]["additions"], 0)
        self.assertEqual(metrics["generated_code"]["events"], 0)

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
        self.assertEqual(metrics["uncommitted_code"]["additions"], 10)
        self.assertEqual(metrics["uncommitted_code"]["deletions"], 2)
        self.assertEqual(metrics["uncommitted_code"]["files"], 2)
        self.assertEqual(metrics["by_session"]["s1"]["latest_workspace_diff_event_id"], "new")

    def test_adopted_code_from_successful_git_commit_uses_commit_summary(self) -> None:
        patch = """*** Begin Patch
*** Add File: src/app.py
+print("hello")
+print("world")
*** Add File: README.md
+# Not code
*** End Patch
"""
        metrics = compute_code_metrics(
            [
                {
                    "record_type": "event",
                    "event_id": "write",
                    "session_id": "s1",
                    "hook_event_name": "PostToolUse",
                    "operation": {"success": True},
                    "content": {"tool_input": patch},
                },
                {
                    "record_type": "event",
                    "event_id": "commit",
                    "session_id": "s1",
                    "received_at": "2026-06-16T01:00:00Z",
                    "hook_event_name": "PostToolUse",
                    "operation": {"success": True},
                    "tool": {"name": "Bash", "command": "git commit -m init"},
                    "content": {
                        "tool_response": "[main abc123] init\n 2 files changed, 3 insertions(+), 1 deletion(-)\n create mode 100644 src/app.py\n create mode 100644 README.md\n"
                    },
                },
                {
                    "record_type": "event",
                    "event_id": "after-commit-write",
                    "session_id": "s1",
                    "received_at": "2026-06-16T01:01:00Z",
                    "hook_event_name": "PostToolUse",
                    "operation": {"success": True},
                    "content": {
                        "tool_input": "*** Begin Patch\n*** Add File: src/later.py\n+print(\"later\")\n*** End Patch\n"
                    },
                },
            ]
        )

        self.assertEqual(metrics["generated_code"]["additions"], 3)
        self.assertEqual(metrics["adopted_code"]["additions"], 3)
        self.assertEqual(metrics["adopted_code"]["deletions"], 1)
        self.assertEqual(metrics["adopted_code"]["files"], 2)
        self.assertEqual(metrics["adopted_code"]["sessions"], 1)
        self.assertEqual(metrics["uncommitted_code"]["additions"], 1)
        self.assertEqual(metrics["uncommitted_code"]["files"], 1)
        self.assertEqual(metrics["by_session"]["s1"]["uncommitted"]["additions"], 1)
        self.assertEqual(metrics["by_session"]["s1"]["adoption_source"], "git_commit_summary")
        self.assertEqual(metrics["by_session"]["s1"]["latest_git_commit_code"]["additions"], 3)
        self.assertEqual(metrics["by_session"]["s1"]["latest_git_commit_code"]["deletions"], 1)
        self.assertEqual(metrics["by_session"]["s1"]["latest_git_commit_code"]["files"], 2)
        self.assertEqual(metrics["by_session"]["s1"]["latest_git_commit_event_id"], "commit")

    def test_adopted_code_from_successful_git_commit_falls_back_to_generated_files(self) -> None:
        metrics = compute_code_metrics(
            [
                {
                    "record_type": "event",
                    "event_id": "write",
                    "session_id": "s1",
                    "hook_event_name": "PostToolUse",
                    "operation": {"success": True},
                    "content": {"tool_input": "*** Begin Patch\n*** Add File: src/app.py\n+print(1)\n*** End Patch\n"},
                },
                {
                    "record_type": "event",
                    "event_id": "commit",
                    "session_id": "s1",
                    "received_at": "2026-06-16T01:00:00Z",
                    "hook_event_name": "PostToolUse",
                    "operation": {"success": True},
                    "tool": {"name": "Bash", "command": "git commit -m init"},
                    "content": {"tool_response": "[main abc123] init\n"},
                },
            ]
        )

        self.assertEqual(metrics["adopted_code"]["additions"], 1)
        self.assertEqual(metrics["adopted_code"]["files"], 1)
        self.assertEqual(metrics["by_session"]["s1"]["latest_git_commit_code"]["additions"], 1)
        self.assertEqual(metrics["by_session"]["s1"]["adoption_source"], "git_commit_generated_code")


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
                "timeline": {"trace_id": "s1", "span_id": "e1", "sequence_no": 1, "duration_ms": 20},
                "operation": {"category": "tool", "phase": "after", "name": "PostToolUse", "success": True},
                "tool": {"name": "apply_patch", "type": "tool", "files_written": ["src/app.py"]},
                "skill": {"name": "code-fix", "phase": "after"},
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
                "timeline": {"trace_id": "s1", "span_id": "e2", "sequence_no": 2},
                "operation": {"category": "session", "phase": "stop", "name": "Stop", "success": True},
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
            {
                "record_type": "snapshot",
                "snapshot_id": "sess1",
                "snapshot_type": "session",
                "session": {"session_id": "s1", "model": "gpt-test"},
            },
        ]

        index = build_sessions_index(events, snapshot_records=snapshots)
        self.assertEqual(index["total_sessions"], 2)
        first = index["sessions"][0]
        self.assertEqual(first["session_id"], "s2")
        s1 = next(session for session in index["sessions"] if session["session_id"] == "s1")
        self.assertEqual(s1["code_metrics"]["generated_code"]["additions"], 2)
        self.assertEqual(s1["code_metrics"]["adopted_code"]["additions"], 2)
        self.assertEqual(s1["code_metrics"]["uncommitted_code"]["additions"], 2)
        self.assertEqual(s1["token_totals"]["total_tokens"], 3)
        self.assertEqual(s1["token_totals_by_model"]["gpt-test"]["total_tokens"], 3)
        self.assertEqual(s1["process"]["operation_category_counts"]["tool"], 1)
        self.assertEqual(s1["process"]["tool_counts"]["apply_patch"], 1)
        self.assertEqual(s1["process"]["skill_counts"]["code-fix"], 1)
        self.assertEqual(s1["process"]["duration_ms_by_category"]["tool"]["total"], 20)

        detail = build_session_detail("s1", events, snapshots)
        self.assertEqual(detail["event_count"], 2)
        self.assertEqual(detail["snapshots"]["environment"][0]["snapshot_id"], "env1")
        self.assertEqual(detail["code_metrics"]["generated_code"]["additions"], 2)
        self.assertEqual(detail["code_metrics"]["uncommitted_code"]["additions"], 2)
        self.assertEqual(detail["session"]["token_totals_by_model"]["gpt-test"]["total_tokens"], 3)
        self.assertEqual([event["event_id"] for event in detail["events"]], ["e1", "e2"])
        self.assertEqual(detail["timeline"][0]["category"], "tool")
        self.assertEqual(detail["timeline"][0]["tool"]["name"], "apply_patch")
        self.assertEqual(detail["process"]["skill_counts"]["code-fix"], 1)

    def test_session_token_totals_deduplicate_repeated_transcript_usage(self) -> None:
        usage = {
            "source": "transcript_token_count",
            "timestamp": "2026-06-16T00:00:00Z",
            "info": {
                "last_token_usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "total_tokens": 3,
                }
            },
        }
        index = build_sessions_index(
            [
                {"record_type": "event", "event_id": "e1", "session_id": "s1", "usage": usage},
                {"record_type": "event", "event_id": "e2", "session_id": "s1", "usage": usage},
            ]
        )
        self.assertEqual(index["sessions"][0]["token_totals"]["total_tokens"], 3)

    def test_session_detail_limit_returns_latest_events(self) -> None:
        events = [
            {
                "record_type": "event",
                "event_id": f"e{index}",
                "received_at": f"2026-06-16T01:0{index}:00Z",
                "session_id": "s1",
                "hook_event_name": "PostToolUse",
            }
            for index in range(5)
        ]

        detail = build_session_detail("s1", events, [], limit=2)

        self.assertEqual([event["event_id"] for event in detail["events"]], ["e3", "e4"])
        self.assertEqual([event["event_id"] for event in detail["timeline"]], ["e3", "e4"])
        self.assertEqual(detail["event_count"], 5)
        self.assertEqual(detail["returned_events"], 2)

    def test_session_detail_includes_transcript_agent_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-06-16T01:00:01Z",
                                "type": "event_msg",
                                "payload": {
                                    "type": "agent_message",
                                    "message": "我会先检查工作区。",
                                    "phase": "commentary",
                                },
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-06-16T01:00:02Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "custom_tool_call",
                                    "status": "completed",
                                    "call_id": "call_patch",
                                    "name": "apply_patch",
                                    "input": "*** Begin Patch\n*** Add File: src/app.py\n+print(1)\n*** End Patch\n",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-06-16T01:00:03Z",
                                "type": "event_msg",
                                "payload": {
                                    "type": "patch_apply_end",
                                    "call_id": "call_patch",
                                    "success": True,
                                    "changes": {"src/app.py": {"type": "add"}},
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            events = [
                {
                    "record_type": "event",
                    "event_id": "e1",
                    "received_at": "2026-06-16T01:00:00Z",
                    "session_id": "s1",
                    "hook_event_name": "UserPromptSubmit",
                }
            ]
            snapshots = [
                {
                    "record_type": "snapshot",
                    "snapshot_id": "sess1",
                    "snapshot_type": "session",
                    "session": {"session_id": "s1", "transcript_path": str(transcript)},
                }
            ]

            detail = build_session_detail("s1", events, snapshots)
            self.assertEqual(len(detail["assistant_messages"]), 1)
            self.assertEqual(detail["assistant_messages"][0]["content"]["response"], "我会先检查工作区。")
            self.assertEqual(detail["assistant_messages"][0]["hook_event_name"], "AgentMessage")
            self.assertEqual(len(detail["transcript_tool_events"]), 1)
            self.assertEqual(detail["session"]["code_metrics"]["uncommitted_code"]["additions"], 1)
            self.assertEqual(detail["session"]["code_metrics"]["generated_code"]["additions"], 1)
            self.assertEqual(
                [item["hook_event_name"] for item in detail["timeline"]],
                ["UserPromptSubmit", "AgentMessage", "PostToolUse"],
            )


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
                        "timeline": {"trace_id": "s1", "span_id": "e1", "sequence_no": 1, "duration_ms": 5},
                        "operation": {"category": "tool", "phase": "after", "name": "PostToolUse", "success": True},
                        "tool": {"name": "apply_patch", "files_written": ["src/app.py"]},
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
                        "timeline": {"trace_id": "s1", "span_id": "e2", "sequence_no": 2},
                        "operation": {"category": "session", "phase": "stop", "name": "Stop", "success": True},
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

                exists_request = urllib.request.Request(
                    f"{base_url}/events/exists",
                    data=json.dumps({"record_pks": ["event:e1", "event:missing", "snapshot:env1"]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(exists_request, timeout=5) as response:
                    exists = json.loads(response.read().decode("utf-8"))
                self.assertEqual(exists["existing"], ["event:e1", "snapshot:env1"])
                self.assertEqual(exists["missing"], ["event:missing"])

                with urllib.request.urlopen(f"{base_url}/sessions", timeout=5) as response:
                    sessions = json.loads(response.read().decode("utf-8"))
                self.assertEqual(sessions["sessions"][0]["session_id"], "s1")
                self.assertEqual(sessions["sessions"][0]["code_metrics"]["generated_code"]["additions"], 1)
                self.assertEqual(sessions["sessions"][0]["code_metrics"]["uncommitted_code"]["additions"], 1)
                self.assertEqual(sessions["sessions"][0]["process"]["tool_counts"]["apply_patch"], 1)

                with urllib.request.urlopen(f"{base_url}/sessions/s1", timeout=5) as response:
                    detail = json.loads(response.read().decode("utf-8"))
                self.assertEqual(detail["event_count"], 2)
                self.assertEqual(detail["snapshots"]["environment"][0]["snapshot_id"], "env1")
                self.assertEqual(detail["timeline"][0]["tool"]["name"], "apply_patch")

                with urllib.request.urlopen(f"{base_url}/metrics/code", timeout=5) as response:
                    metrics = json.loads(response.read().decode("utf-8"))
                self.assertEqual(metrics["generated_code"]["additions"], 1)
                self.assertEqual(metrics["adopted_code"]["additions"], 1)
                self.assertEqual(metrics["uncommitted_code"]["additions"], 1)

                with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                    content_type = response.headers.get("Content-Type")
                self.assertIn("text/html", content_type)
                self.assertIn("AI Worklog", html)
                self.assertIn("uncommitted", html)
                self.assertIn('getJson("/stats")', html)
                self.assertIn('api("/sessions"', html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
