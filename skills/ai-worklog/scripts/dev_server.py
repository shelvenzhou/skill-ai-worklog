#!/usr/bin/env python3
"""Tiny local HTTP receiver for smoke-testing worklog uploads."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Receiver(BaseHTTPRequestHandler):
    output_dir: Path

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        try:
            event = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"invalid json\n")
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{dt.datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str))
            fh.write("\n")

        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local worklog upload receiver.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", default=str(Path.home() / ".ai-worklog" / "server-events"))
    args = parser.parse_args()

    Receiver.output_dir = Path(args.output_dir).expanduser()
    server = ThreadingHTTPServer((args.host, args.port), Receiver)
    print(f"Listening on http://{args.host}:{args.port}/events")
    print(f"Writing JSONL to {Receiver.output_dir}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
