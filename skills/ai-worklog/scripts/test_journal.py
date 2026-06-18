#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path

import journal
import async_upload_trigger
import codex_backfill
import codex_backfill_trigger
import platform_io
import replay


class JournalTests(unittest.TestCase):
    def test_platform_decode_text_accepts_windows_code_page_fallback(self) -> None:
        original_os_name = platform_io.os.name
        try:
            platform_io.os.name = "nt"
            self.assertEqual(platform_io.decode_text("中文路径".encode("gb18030")), "中文路径")
        finally:
            platform_io.os.name = original_os_name

    def test_load_json_accepts_utf8_bom_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"enabled": False}) + "\n", encoding="utf-8-sig")

            self.assertEqual(journal.load_json(path), {"enabled": False})

    def test_read_stdin_json_decodes_utf8_bytes_independent_of_locale(self) -> None:
        class BinaryStdin:
            def __init__(self, data: bytes) -> None:
                self.buffer = BytesIO(data)

            def read(self) -> str:
                raise AssertionError("text stdin should not be used when buffer is available")

        original_stdin = sys.stdin
        try:
            sys.stdin = BinaryStdin(json.dumps({"prompt": "好，按你的推荐实现"}, ensure_ascii=False).encode("utf-8"))  # type: ignore[assignment]
            self.assertEqual(journal.read_stdin_json()["prompt"], "好，按你的推荐实现")
        finally:
            sys.stdin = original_stdin

    def test_session_identity_uses_git_email_without_explicit_env(self) -> None:
        original_run = journal.run_metadata_command
        original_user_email = os.environ.pop("AI_WORKLOG_USER_EMAIL", None)
        original_legacy_user_email = os.environ.pop("AI_USAGE_COLLECTOR_USER_EMAIL", None)

        def fake_run(args, cwd=None, timeout=1.5):
            if args == ["git", "config", "--get", "user.email"]:
                return "dev@example.com"
            if args == ["git", "config", "--get", "user.name"]:
                return "Dev User"
            return None

        try:
            journal.run_metadata_command = fake_run
            session = journal.compact_session_metadata({}, "codex", "/tmp", None)
            identity = journal.identity_metadata("/tmp")
        finally:
            journal.run_metadata_command = original_run
            if original_user_email is not None:
                os.environ["AI_WORKLOG_USER_EMAIL"] = original_user_email
            if original_legacy_user_email is not None:
                os.environ["AI_USAGE_COLLECTOR_USER_EMAIL"] = original_legacy_user_email

        self.assertEqual(session["user_email"], "dev@example.com")
        self.assertEqual(identity["git_user_email"], "dev@example.com")

    def test_codex_backfill_builds_stable_records_from_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-2026-06-16T00-00-00-s1.jsonl"
            lines = [
                {
                    "timestamp": "2026-06-16T00:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "s1", "cwd": tmp, "model": "gpt-test"},
                },
                {
                    "timestamp": "2026-06-16T00:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "t1", "cwd": tmp, "model": "gpt-test"},
                },
                {
                    "timestamp": "2026-06-16T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "commit something"},
                },
                {
                    "timestamp": "2026-06-16T00:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "call_1",
                        "arguments": json.dumps({"cmd": "git commit -m init", "workdir": tmp}),
                    },
                },
                {
                    "timestamp": "2026-06-16T00:00:04Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "Process exited with code 0\nOutput:\n[main abc123] init\n 1 file changed, 2 insertions(+), 1 deletion(-)\n",
                    },
                },
                {
                    "timestamp": "2026-06-16T00:00:05Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "done"},
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            cfg = journal.default_config()
            cfg["capture"]["token_usage_from_transcript"] = False

            records = codex_backfill.events_from_transcript(path, cfg)
            events = [record for record in records if record.get("record_type") == "event"]
            second = codex_backfill.events_from_transcript(path, cfg)
            second_events = [record for record in second if record.get("record_type") == "event"]

            self.assertEqual([event["event_id"] for event in events], [event["event_id"] for event in second_events])
            self.assertEqual([event["hook_event_name"] for event in events], ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"])
            tool_event = next(event for event in events if event["hook_event_name"] == "PostToolUse")
            self.assertEqual(tool_event["tool"]["command"], "git commit -m init")
            self.assertEqual(tool_event["tool"]["exit_code"], 0)

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
        self.assertEqual(event["model"], "gpt-test")
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
                        json.dumps({"type": "turn_context", "payload": {"model": "gpt-transcript"}}),
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
            context = journal.extract_transcript_context(str(path), 1024)
            self.assertEqual(context["model"], "gpt-transcript")

    def test_build_event_uses_transcript_model_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text(
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-transcript"}}) + "\n",
                encoding="utf-8",
            )
            event, snapshots = journal.build_records(
                {"hook_event_name": "SessionStart", "session_id": "s1", "transcript_path": str(path)},
                journal.default_config(),
                "codex",
                "test",
            )
            assert event is not None
            self.assertEqual(event["model"], "gpt-transcript")
            self.assertEqual(snapshots[1]["session"]["model"], "gpt-transcript")

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

    def test_stop_event_does_not_duplicate_agent_response_content(self) -> None:
        event, _ = journal.build_records(
            {
                "hook_event_name": "Stop",
                "session_id": "s1",
                "response": "final answer",
                "last_assistant_message": "final answer",
            },
            journal.default_config(),
            "codex",
            "test",
        )
        assert event is not None
        self.assertEqual(event["operation"]["category"], "session")
        self.assertEqual(event["operation"]["phase"], "stop")
        self.assertNotIn("response", event["content"])
        self.assertEqual(event["raw_hook_input"]["response"], "final answer")

    def test_agent_response_event_keeps_response_content(self) -> None:
        event, _ = journal.build_records(
            {
                "hook_event_name": "afterAgentResponse",
                "session_id": "s1",
                "response": "final answer",
            },
            journal.default_config(),
            "cursor",
            "test",
        )
        assert event is not None
        self.assertEqual(event["operation"]["category"], "response")
        self.assertEqual(event["content"]["response"], "final answer")

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

    def test_invalid_request_timeout_config_falls_back_to_default(self) -> None:
        cfg = journal.default_config()
        cfg["request_timeout_seconds"] = "not-a-number"
        self.assertEqual(journal.request_timeout_seconds(cfg), journal.DEFAULT_REQUEST_TIMEOUT_SECONDS)
        cfg["request_timeout_seconds"] = -1
        self.assertEqual(journal.request_timeout_seconds(cfg), journal.DEFAULT_REQUEST_TIMEOUT_SECONDS)

    def test_journal_main_async_upload_only_writes_local_files(self) -> None:
        original_argv = sys.argv
        original_stdin = sys.stdin
        original_upload_event = journal.upload_event
        original_spawn_async = journal.maybe_spawn_async_upload
        calls = {"upload": 0, "spawn": 0}

        def fail_upload(event: dict[str, object], cfg: dict[str, object]) -> tuple[bool, str | None]:
            calls["upload"] += 1
            raise AssertionError("hook should not upload synchronously")

        def fake_spawn(cfg: dict[str, object], config_path: Path) -> None:
            calls["spawn"] += 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "server_url": "http://collector.example/events",
                        "local_log_dir": str(root / "events"),
                        "snapshot_log_dir": str(root / "snapshots"),
                        "state_path": str(root / "state.json"),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            sys.argv = ["journal.py", "--surface", "codex", "--config", str(config)]
            sys.stdin = StringIO(json.dumps({"hook_event_name": "PostToolUse", "session_id": "s1"}))
            journal.upload_event = fail_upload
            journal.maybe_spawn_async_upload = fake_spawn
            try:
                rc = journal.main()
            finally:
                journal.upload_event = original_upload_event
                journal.maybe_spawn_async_upload = original_spawn_async
                sys.argv = original_argv
                sys.stdin = original_stdin

            self.assertEqual(rc, 0)
            self.assertEqual(calls, {"upload": 0, "spawn": 1})
            self.assertEqual(len(list((root / "events").glob("*.jsonl"))), 1)
            self.assertEqual(len(list((root / "snapshots").glob("*.jsonl"))), 1)

    def test_append_jsonl_replaces_invalid_surrogates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = journal.append_jsonl(
                Path(tmp),
                {
                    "record_type": "event",
                    "event_id": "bad-surrogate",
                    "content": {"tool_response": "bad\udcaevalue"},
                },
            )

            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["content"]["tool_response"], "bad?value")

    def test_journal_main_sync_upload_preserves_direct_upload_mode(self) -> None:
        original_argv = sys.argv
        original_stdin = sys.stdin
        original_upload_event = journal.upload_event
        original_spawn_async = journal.maybe_spawn_async_upload
        calls = {"upload": 0, "spawn": 0}

        def fake_upload(event: dict[str, object], cfg: dict[str, object]) -> tuple[bool, str | None]:
            calls["upload"] += 1
            return True, None

        def fake_spawn(cfg: dict[str, object], config_path: Path) -> None:
            calls["spawn"] += 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "server_url": "http://collector.example/events",
                        "upload_mode": "sync",
                        "local_log_dir": str(root / "events"),
                        "snapshot_log_dir": str(root / "snapshots"),
                        "state_path": str(root / "state.json"),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            sys.argv = ["journal.py", "--surface", "codex", "--config", str(config)]
            sys.stdin = StringIO(json.dumps({"hook_event_name": "PostToolUse", "session_id": "s1"}))
            journal.upload_event = fake_upload
            journal.maybe_spawn_async_upload = fake_spawn
            try:
                rc = journal.main()
            finally:
                journal.upload_event = original_upload_event
                journal.maybe_spawn_async_upload = original_spawn_async
                sys.argv = original_argv
                sys.stdin = original_stdin

            self.assertEqual(rc, 0)
            self.assertEqual(calls["upload"], 3)
            self.assertEqual(calls["spawn"], 0)

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
    def write_replay_event(self, root: Path) -> dict[str, object]:
        cfg = journal.default_config()
        cfg["snapshot_log_dir"] = str(root / "snapshots")
        cfg["local_log_dir"] = str(root / "events")
        cfg["failed_log_dir"] = str(root / "failed")
        cfg["upload_state_path"] = str(root / "upload_state.sqlite3")
        cfg["server_url"] = "http://collector.example/events"
        (root / "snapshots").mkdir()
        (root / "events").mkdir()
        (root / "failed").mkdir()
        (root / "events" / "2026-06-16.jsonl").write_text(
            json.dumps({"record_type": "event", "event_id": "e1"}) + "\n",
            encoding="utf-8",
        )
        return cfg

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

    def test_replay_records_successful_uploads_in_local_ledger(self) -> None:
        calls = {"preflight": 0, "upload": 0}
        original_existing = replay.existing_record_pks
        original_upload = replay.upload_records

        def fake_existing(record_pks: list[str], cfg: dict[str, object]) -> set[str]:
            calls["preflight"] += 1
            return set()

        def fake_upload(records: list[dict[str, object]], cfg: dict[str, object]) -> dict[str, int]:
            calls["upload"] += 1
            return {"accepted": len(records), "duplicates": 0}

        try:
            replay.existing_record_pks = fake_existing
            replay.upload_records = fake_upload
            with tempfile.TemporaryDirectory() as tmp:
                cfg = self.write_replay_event(Path(tmp))
                first = replay.replay(cfg, batch_size=100)
                second = replay.replay(cfg, batch_size=100)
        finally:
            replay.existing_record_pks = original_existing
            replay.upload_records = original_upload

        self.assertEqual(first["attempted"], 1)
        self.assertEqual(second["skipped_local"], 1)
        self.assertEqual(second["attempted"], 0)
        self.assertEqual(calls, {"preflight": 1, "upload": 1})

    def test_replay_force_ignores_local_ledger(self) -> None:
        calls = {"preflight": 0, "upload": 0}
        original_existing = replay.existing_record_pks
        original_upload = replay.upload_records

        def fake_existing(record_pks: list[str], cfg: dict[str, object]) -> set[str]:
            calls["preflight"] += 1
            return set()

        def fake_upload(records: list[dict[str, object]], cfg: dict[str, object]) -> dict[str, int]:
            calls["upload"] += 1
            return {"accepted": len(records), "duplicates": 0}

        try:
            replay.existing_record_pks = fake_existing
            replay.upload_records = fake_upload
            with tempfile.TemporaryDirectory() as tmp:
                cfg = self.write_replay_event(Path(tmp))
                replay.replay(cfg, batch_size=100)
                forced = replay.replay(cfg, batch_size=100, force=True)
        finally:
            replay.existing_record_pks = original_existing
            replay.upload_records = original_upload

        self.assertEqual(forced["skipped_local"], 0)
        self.assertEqual(forced["attempted"], 1)
        self.assertEqual(calls, {"preflight": 2, "upload": 2})


class CodexBackfillTests(unittest.TestCase):
    def test_async_upload_trigger_requires_async_mode_and_respects_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = journal.default_config()
            cfg["server_url"] = "http://collector.example/events"
            cfg["async_upload"] = {
                "trigger_state_path": str(root / "async-trigger.json"),
                "trigger_interval_seconds": 100,
            }
            cfg["local_log_dir"] = str(root / "events")
            self.assertTrue(async_upload_trigger.should_run(cfg, now=1000))

            async_upload_trigger.mark_started(Path(cfg["async_upload"]["trigger_state_path"]))
            self.assertFalse(async_upload_trigger.should_run(cfg, now=time.time()))

            event_dir = root / "events"
            event_dir.mkdir()
            event_file = event_dir / "2026-06-17.jsonl"
            event_file.write_text('{"record_type":"event","event_id":"e1"}\n', encoding="utf-8")
            future = time.time() + 5
            os.utime(event_file, (future, future))
            self.assertTrue(async_upload_trigger.should_run(cfg, now=time.time()))

            cfg["upload_mode"] = "sync"
            self.assertFalse(async_upload_trigger.should_run(cfg, now=10000))

    def test_async_upload_trigger_times_out_stuck_replay(self) -> None:
        original_run = async_upload_trigger.subprocess.run

        def fake_run(*args: object, **kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd=kwargs.get("args") or "replay.py", timeout=1)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = journal.default_config()
            cfg["server_url"] = "http://collector.example/events"
            cfg["async_upload"] = {
                "log_path": str(Path(tmp) / "async.log"),
                "max_runtime_seconds": 1,
            }
            try:
                async_upload_trigger.subprocess.run = fake_run
                rc = async_upload_trigger.run_upload(Path(tmp) / "config.json", cfg)
            finally:
                async_upload_trigger.subprocess.run = original_run

            self.assertEqual(rc, 124)
            self.assertIn("async upload timed out", (Path(tmp) / "async.log").read_text(encoding="utf-8"))

    def test_backfill_upload_records_uses_local_ledger_on_rerun(self) -> None:
        calls = {"preflight": 0, "upload": 0}
        original_existing = replay.existing_record_pks
        original_upload = replay.upload_records

        def fake_existing(record_pks: list[str], cfg: dict[str, object]) -> set[str]:
            calls["preflight"] += 1
            return set()

        def fake_upload(records: list[dict[str, object]], cfg: dict[str, object]) -> dict[str, int]:
            calls["upload"] += 1
            return {"accepted": len(records), "duplicates": 0}

        try:
            replay.existing_record_pks = fake_existing
            replay.upload_records = fake_upload
            with tempfile.TemporaryDirectory() as tmp:
                cfg = journal.default_config()
                cfg["server_url"] = "http://collector.example/events"
                ledger = codex_backfill.BackfillLedger(Path(tmp) / "codex_backfill_state.sqlite3")
                records = [{"record_type": "event", "event_id": "e1"}]

                first = codex_backfill.upload_records(
                    records,
                    cfg,
                    batch_size=100,
                    ledger=ledger,
                    collector_url="http://collector.example/events",
                )
                second = codex_backfill.upload_records(
                    records,
                    cfg,
                    batch_size=100,
                    ledger=ledger,
                    collector_url="http://collector.example/events",
                )
        finally:
            replay.existing_record_pks = original_existing
            replay.upload_records = original_upload

        self.assertEqual(first["attempted"], 1)
        self.assertEqual(second["skipped_local"], 1)
        self.assertEqual(second["attempted"], 0)
        self.assertEqual(calls, {"preflight": 1, "upload": 1})

    def test_backfill_ledger_tracks_completed_transcript_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "rollout-2026-06-16T00-00-00-s1.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            ledger = codex_backfill.BackfillLedger(root / "state.sqlite3")

            self.assertFalse(ledger.transcript_complete("collector", path))
            ledger.mark_transcript("collector", path, "complete", {"scanned": 1, "accepted": 1})
            self.assertTrue(ledger.transcript_complete("collector", path))

            path.write_text("{}\n{}\n", encoding="utf-8")
            self.assertFalse(ledger.transcript_complete("collector", path))

    def test_backfill_dry_run_skips_bad_transcript(self) -> None:
        original_argv = sys.argv
        stdout = StringIO()
        stderr = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "rollout-2026-06-16T00-00-00-good.jsonl"
            bad = root / "rollout-2026-06-16T00-00-00-bad.jsonl"
            config = root / "config.json"
            config.write_text("{}\n", encoding="utf-8")
            good.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "s1", "cwd": tmp}}) + "\n",
                encoding="utf-8",
            )
            bad.write_text("{not json}\n", encoding="utf-8")
            sys.argv = [
                "codex_backfill.py",
                "--sessions-root",
                str(root),
                "--config",
                str(config),
                "--dry-run",
            ]
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    rc = codex_backfill.main()
            finally:
                sys.argv = original_argv

        self.assertEqual(rc, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["transcripts"], 2)
        self.assertEqual(summary["transcripts_failed"], 1)
        self.assertEqual(summary["records"], 3)
        self.assertIn("skipping transcript", stderr.getvalue())

    def test_backfill_trigger_requires_server_url_and_respects_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = journal.default_config()
            cfg["server_url"] = None
            cfg["codex_history_backfill"] = {
                "trigger_state_path": str(Path(tmp) / "trigger.json"),
                "trigger_interval_seconds": 100,
            }
            self.assertFalse(codex_backfill_trigger.should_run(cfg, now=1000))

            cfg["server_url"] = "http://collector.example/events"
            self.assertTrue(codex_backfill_trigger.should_run(cfg, now=1000))

            codex_backfill_trigger.mark_started(Path(cfg["codex_history_backfill"]["trigger_state_path"]))
            self.assertFalse(codex_backfill_trigger.should_run(cfg, now=time.time()))

    def test_backfill_trigger_times_out_stuck_subprocess(self) -> None:
        original_run = codex_backfill_trigger.subprocess.run

        def fake_run(*args: object, **kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd=kwargs.get("args") or "codex_backfill.py", timeout=1)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = journal.default_config()
            cfg["server_url"] = "http://collector.example/events"
            cfg["codex_history_backfill"] = {
                "log_path": str(Path(tmp) / "backfill.log"),
                "max_runtime_seconds": 1,
            }
            try:
                codex_backfill_trigger.subprocess.run = fake_run
                rc = codex_backfill_trigger.run_backfill(Path(tmp) / "config.json", cfg)
            finally:
                codex_backfill_trigger.subprocess.run = original_run

            self.assertEqual(rc, 124)
            self.assertIn("backfill timed out", (Path(tmp) / "backfill.log").read_text(encoding="utf-8"))

    def test_backfill_trigger_accepts_specific_sessions_root(self) -> None:
        calls: list[list[str]] = []
        original_run = codex_backfill_trigger.subprocess.run

        class Completed:
            returncode = 0

        def fake_run(command: list[str], **kwargs: object) -> object:
            calls.append(command)
            return Completed()

        with tempfile.TemporaryDirectory() as tmp:
            cfg = journal.default_config()
            cfg["server_url"] = "http://collector.example/events"
            cfg["codex_history_backfill"] = {
                "log_path": str(Path(tmp) / "backfill.log"),
            }
            transcript = Path(tmp) / "rollout-test.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            try:
                codex_backfill_trigger.subprocess.run = fake_run
                rc = codex_backfill_trigger.run_backfill(Path(tmp) / "config.json", cfg, str(transcript))
            finally:
                codex_backfill_trigger.subprocess.run = original_run

            self.assertEqual(rc, 0)
            self.assertIn("--sessions-root", calls[0])
            self.assertIn(str(transcript), calls[0])


if __name__ == "__main__":
    unittest.main()
