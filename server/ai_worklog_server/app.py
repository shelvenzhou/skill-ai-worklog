from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import build_session_detail, build_sessions_index
from .metrics import compute_code_metrics
from .storage import WorklogStore


def parse_records(body: bytes, content_type: str) -> list[dict[str, Any]]:
    text = body.decode("utf-8")
    if not text.strip():
        return []
    if "application/x-ndjson" in content_type or "\n" in text.strip():
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("NDJSON lines must be JSON objects")
            records.append(item)
        return records

    item = json.loads(text)
    if isinstance(item, list):
        if not all(isinstance(record, dict) for record in item):
            raise ValueError("JSON array items must be objects")
        return item
    if isinstance(item, dict):
        return [item]
    raise ValueError("request body must be a JSON object, array, or NDJSON")


def parse_record_pks(body: bytes) -> list[str]:
    text = body.decode("utf-8")
    if not text.strip():
        return []
    item = json.loads(text)
    if isinstance(item, list):
        values = item
    elif isinstance(item, dict):
        values = item.get("record_pks")
    else:
        raise ValueError("request body must be a JSON object or array")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("record_pks must be an array of strings")
    return values


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any] | list[Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class WorklogHandler(BaseHTTPRequestHandler):
    store: WorklogStore
    bearer_token: str | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            write_json(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "records": self.store.count_records(),
                },
            )
            return

        if parsed.path == "/stats":
            write_json(self, HTTPStatus.OK, self.store.stats())
            return

        if parsed.path == "/metrics/code":
            query = parse_qs(parsed.query)
            records = self.store.query_records_for_metrics(
                record_type="event",
                surface=(query.get("surface") or [None])[0],
                session_id=(query.get("session_id") or [None])[0],
            )
            write_json(self, HTTPStatus.OK, compute_code_metrics(records))
            return

        if parsed.path == "/sessions":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["50"])[0])
            records = self.store.query_events_for_analysis(surface=(query.get("surface") or [None])[0])
            write_json(self, HTTPStatus.OK, build_sessions_index(records, limit=limit))
            return

        if parsed.path.startswith("/sessions/"):
            query = parse_qs(parsed.query)
            session_id = unquote(parsed.path.removeprefix("/sessions/"))
            if not session_id:
                write_json(self, HTTPStatus.BAD_REQUEST, {"error": "missing session id"})
                return
            limit = int((query.get("limit") or ["200"])[0])
            surface = (query.get("surface") or [None])[0]
            query_session_id = None if session_id == "unknown" else session_id
            events = self.store.query_events_for_analysis(surface=surface, session_id=query_session_id)
            snapshot_ids: list[str] = []
            for event in events:
                for key in ("environment_ref", "session_ref"):
                    value = event.get(key)
                    if value:
                        snapshot_ids.append(str(value))
            snapshots = self.store.query_snapshots_by_ids(snapshot_ids)
            write_json(self, HTTPStatus.OK, build_session_detail(session_id, events, snapshots, limit=limit))
            return

        if parsed.path == "/records":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["50"])[0])
            records = self.store.query_records(
                limit=limit,
                record_type=(query.get("record_type") or [None])[0],
                surface=(query.get("surface") or [None])[0],
                session_id=(query.get("session_id") or [None])[0],
            )
            write_json(self, HTTPStatus.OK, {"records": records})
            return

        write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/events", "/events/exists"}:
            write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self.authorized():
            write_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)

        if parsed.path == "/events/exists":
            try:
                record_pks = parse_record_pks(body)
            except Exception as exc:
                write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            existing = self.store.existing_record_pks(record_pks)
            write_json(
                self,
                HTTPStatus.OK,
                {
                    "existing": [key for key in record_pks if key in existing],
                    "missing": [key for key in record_pks if key not in existing],
                },
            )
            return

        try:
            records = parse_records(body, self.headers.get("Content-Type") or "")
        except Exception as exc:
            write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        result = self.store.insert_many(records)
        write_json(self, HTTPStatus.ACCEPTED, result)

    def authorized(self) -> bool:
        if not self.bearer_token:
            return True
        expected = f"Bearer {self.bearer_token}"
        return self.headers.get("Authorization") == expected

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def build_server(host: str, port: int, data_dir: Path, token: str | None) -> ThreadingHTTPServer:
    WorklogHandler.store = WorklogStore(data_dir)
    WorklogHandler.bearer_token = token
    return ThreadingHTTPServer((host, port), WorklogHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AI Worklog collector server.")
    parser.add_argument("--host", default=os.environ.get("AI_WORKLOG_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_WORKLOG_SERVER_PORT", "8765")))
    parser.add_argument("--data-dir", default=os.environ.get("AI_WORKLOG_SERVER_DATA_DIR", "./data"))
    parser.add_argument("--token-env", default="AI_WORKLOG_SERVER_TOKEN")
    args = parser.parse_args()

    token = os.environ.get(args.token_env) if args.token_env else None
    server = build_server(args.host, args.port, Path(args.data_dir), token)
    print(f"AI Worklog collector listening on http://{args.host}:{args.port}")
    print(f"POST endpoint: http://{args.host}:{args.port}/events")
    print(f"Data directory: {Path(args.data_dir).expanduser().resolve()}")
    if token:
        print(f"Authorization: bearer token from ${args.token_env}")
    server.serve_forever()
    return 0
