from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
create table if not exists records (
  record_pk text primary key,
  record_type text,
  event_id text,
  snapshot_id text,
  snapshot_type text,
  source_id text,
  surface text,
  session_id text,
  turn_id text,
  hook_event_name text,
  environment_ref text,
  session_ref text,
  collection_level text,
  client_received_at text,
  ingested_at text not null,
  input_tokens integer,
  cached_input_tokens integer,
  output_tokens integer,
  reasoning_output_tokens integer,
  total_tokens integer,
  raw_json text not null
);

create index if not exists idx_records_ingested_at on records(ingested_at);
create index if not exists idx_records_session on records(session_id);
create index if not exists idx_records_surface on records(surface);
create index if not exists idx_records_type on records(record_type);
create index if not exists idx_records_hook on records(hook_event_name);
"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def stable_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def token_usage(record: dict[str, Any]) -> dict[str, int | None]:
    usage = record.get("usage")
    info = usage.get("info") if isinstance(usage, dict) else None
    last = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(last, dict):
        hook_usage = record.get("hook_usage")
        if isinstance(hook_usage, dict) and isinstance(hook_usage.get("last_token_usage"), dict):
            last = hook_usage["last_token_usage"]
        else:
            last = hook_usage if isinstance(hook_usage, dict) else {}

    def as_int(key: str) -> int | None:
        value = last.get(key)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return None

    return {
        "input_tokens": as_int("input_tokens"),
        "cached_input_tokens": as_int("cached_input_tokens"),
        "output_tokens": as_int("output_tokens"),
        "reasoning_output_tokens": as_int("reasoning_output_tokens"),
        "total_tokens": as_int("total_tokens"),
    }


def record_pk(record: dict[str, Any]) -> str:
    event_id = record.get("event_id")
    if event_id:
        return f"event:{event_id}"
    snapshot_id = record.get("snapshot_id")
    if snapshot_id:
        return f"snapshot:{snapshot_id}"
    return f"hash:{stable_hash(record)}"


class WorklogStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.expanduser()
        self.raw_dir = self.data_dir / "raw"
        self.db_path = self.data_dir / "worklog.sqlite3"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def append_raw(self, record: dict[str, Any], ingested_at: str) -> None:
        day = ingested_at[:10]
        path = self.raw_dir / f"{day}.jsonl"
        raw = dict(record)
        raw["_server_ingested_at"] = ingested_at
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json_dumps(raw))
            fh.write("\n")

    def insert_record(self, record: dict[str, Any]) -> bool:
        ingested_at = utc_now()
        pk = record_pk(record)
        tokens = token_usage(record)
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    insert into records (
                      record_pk, record_type, event_id, snapshot_id, snapshot_type,
                      source_id, surface, session_id, turn_id, hook_event_name,
                      environment_ref, session_ref, collection_level,
                      client_received_at, ingested_at,
                      input_tokens, cached_input_tokens, output_tokens,
                      reasoning_output_tokens, total_tokens, raw_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pk,
                        record.get("record_type"),
                        record.get("event_id"),
                        record.get("snapshot_id"),
                        record.get("snapshot_type"),
                        record.get("source_id"),
                        record.get("surface"),
                        record.get("session_id"),
                        record.get("turn_id"),
                        record.get("hook_event_name"),
                        record.get("environment_ref"),
                        record.get("session_ref"),
                        record.get("collection_level"),
                        record.get("received_at"),
                        ingested_at,
                        tokens["input_tokens"],
                        tokens["cached_input_tokens"],
                        tokens["output_tokens"],
                        tokens["reasoning_output_tokens"],
                        tokens["total_tokens"],
                        json_dumps(record),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        self.append_raw(record, ingested_at)
        return True

    def insert_many(self, records: list[dict[str, Any]]) -> dict[str, int]:
        accepted = 0
        duplicates = 0
        for record in records:
            if self.insert_record(record):
                accepted += 1
            else:
                duplicates += 1
        return {"accepted": accepted, "duplicates": duplicates}

    def count_records(self) -> int:
        with self._connect() as conn:
            row = conn.execute("select count(*) as count from records").fetchone()
        return int(row["count"])

    def query_records(
        self,
        *,
        limit: int = 50,
        record_type: str | None = None,
        surface: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        if record_type:
            where.append("record_type = ?")
            args.append(record_type)
        if surface:
            where.append("surface = ?")
            args.append(surface)
        if session_id:
            where.append("session_id = ?")
            args.append(session_id)
        sql = "select raw_json from records"
        if where:
            sql += " where " + " and ".join(where)
        sql += " order by ingested_at desc limit ?"
        args.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("select count(*) as count from records").fetchone()["count"]
            by_type = conn.execute(
                "select coalesce(record_type, 'unknown') as key, count(*) as count from records group by key order by count desc"
            ).fetchall()
            by_surface = conn.execute(
                "select coalesce(surface, 'unknown') as key, count(*) as count from records group by key order by count desc"
            ).fetchall()
            by_hook = conn.execute(
                """
                select coalesce(hook_event_name, 'unknown') as key, count(*) as count
                from records
                where hook_event_name is not null
                group by key
                order by count desc
                limit 50
                """
            ).fetchall()
            token_row = conn.execute(
                """
                select
                  coalesce(sum(input_tokens), 0) as input_tokens,
                  coalesce(sum(cached_input_tokens), 0) as cached_input_tokens,
                  coalesce(sum(output_tokens), 0) as output_tokens,
                  coalesce(sum(reasoning_output_tokens), 0) as reasoning_output_tokens,
                  coalesce(sum(total_tokens), 0) as total_tokens
                from records
                """
            ).fetchone()
        return {
            "total_records": int(total),
            "by_record_type": {row["key"]: int(row["count"]) for row in by_type},
            "by_surface": {row["key"]: int(row["count"]) for row in by_surface},
            "by_hook_event_name": {row["key"]: int(row["count"]) for row in by_hook},
            "token_totals": {key: int(token_row[key]) for key in token_row.keys()},
        }
