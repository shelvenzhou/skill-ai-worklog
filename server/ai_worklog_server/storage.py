from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

TOKEN_TOTAL_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


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
create index if not exists idx_records_type_ingested on records(record_type, ingested_at);
create index if not exists idx_records_session_type_ingested on records(session_id, record_type, ingested_at);
create index if not exists idx_records_surface_type_ingested on records(surface, record_type, ingested_at);

create table if not exists identity_mappings (
  identity_kind text not null,
  identity_value text not null,
  user_email text not null,
  display_name text,
  source text,
  created_at text not null,
  updated_at text not null,
  primary key (identity_kind, identity_value)
);
"""

OPTIONAL_COLUMNS = {
    "token_usage_identity": "text",
    "token_model": "text",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def stable_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def normalize_identity_kind(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def normalize_identity_value(kind: str, value: str) -> str:
    value = value.strip()
    if kind in {"email", "git_email", "user_email", "windows_upn", "hostname", "os_user", "os_user_host", "user_domain"}:
        return value.lower()
    return value


def month_bounds(month: str | None) -> tuple[str, str, str]:
    if month:
        try:
            start_date = dt.datetime.strptime(month, "%Y-%m").replace(tzinfo=dt.timezone.utc)
        except ValueError as exc:
            raise ValueError("month must use YYYY-MM") from exc
    else:
        now = dt.datetime.now(dt.timezone.utc)
        start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_date.month == 12:
        end_date = start_date.replace(year=start_date.year + 1, month=1)
    else:
        end_date = start_date.replace(month=start_date.month + 1)
    label = start_date.strftime("%Y-%m")
    return (
        label,
        start_date.isoformat().replace("+00:00", "Z"),
        end_date.isoformat().replace("+00:00", "Z"),
    )


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

    return {key: as_int(key) for key in TOKEN_TOTAL_KEYS}


def token_usage_identity(record: dict[str, Any]) -> str | None:
    tokens = token_usage(record)
    if all(value is None for value in tokens.values()):
        return None

    usage = record.get("usage")
    usage_timestamp = usage.get("timestamp") if isinstance(usage, dict) else None
    usage_turn = (usage.get("turn_id") or usage.get("turnId")) if isinstance(usage, dict) else None
    turn_or_timestamp = usage_timestamp or usage_turn or record.get("turn_id") or record.get("turnId")
    if not turn_or_timestamp:
        return record_pk(record)

    return "|".join(
        [
            str(record.get("session_id") or "unknown"),
            str(turn_or_timestamp),
            stable_hash(tokens),
        ]
    )


def token_totals(records: list[dict[str, Any]]) -> dict[str, int]:
    totals = {key: 0 for key in TOKEN_TOTAL_KEYS}
    seen_usage: set[str] = set()
    for record in records:
        identity = token_usage_identity(record)
        if identity is None or identity in seen_usage:
            continue
        seen_usage.add(identity)
        usage = token_usage(record)
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
    return totals


def session_models(records: list[dict[str, Any]]) -> dict[str, str]:
    models: dict[str, str] = {}
    for record in records:
        session_id = record.get("session_id")
        model = record.get("model")
        if isinstance(session_id, str) and session_id and isinstance(model, str) and model:
            models.setdefault(session_id, model)
            continue

        if record.get("record_type") != "snapshot" or record.get("snapshot_type") != "session":
            continue
        session = record.get("session")
        if not isinstance(session, dict):
            continue
        session_id = session.get("session_id")
        model = session.get("model")
        if isinstance(session_id, str) and session_id and isinstance(model, str) and model:
            models.setdefault(session_id, model)
    return models


def token_model(record: dict[str, Any], model_by_session: dict[str, str] | None = None) -> str:
    for key in ("model", "model_name", "modelName"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value

    for usage_key in ("usage", "hook_usage"):
        usage = record.get(usage_key)
        if not isinstance(usage, dict):
            continue
        for key in ("model", "model_name", "modelName"):
            value = usage.get(key)
            if isinstance(value, str) and value:
                return value

    session_id = record.get("session_id")
    if isinstance(session_id, str) and model_by_session and model_by_session.get(session_id):
        return model_by_session[session_id]

    return "unknown"


def token_totals_by_model(
    records: list[dict[str, Any]],
    model_by_session: dict[str, str] | None = None,
) -> dict[str, dict[str, int]]:
    model_by_session = model_by_session or session_models(records)
    totals_by_model: dict[str, dict[str, int]] = {}
    seen_usage: set[str] = set()
    for record in records:
        identity = token_usage_identity(record)
        if identity is None or identity in seen_usage:
            continue
        seen_usage.add(identity)
        model = token_model(record, model_by_session)
        totals = totals_by_model.setdefault(model, {key: 0 for key in TOKEN_TOTAL_KEYS})
        usage = token_usage(record)
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
    return dict(sorted(totals_by_model.items()))


def empty_token_totals() -> dict[str, int]:
    return {key: 0 for key in TOKEN_TOTAL_KEYS}


def add_token_totals(target: dict[str, int], row: sqlite3.Row | dict[str, Any]) -> None:
    for key in TOKEN_TOTAL_KEYS:
        target[key] = int(target.get(key) or 0) + int(row[key] or 0)


def record_pk(record: dict[str, Any]) -> str:
    event_id = record.get("event_id")
    if event_id:
        return f"event:{event_id}"
    snapshot_id = record.get("snapshot_id")
    if snapshot_id:
        return f"snapshot:{snapshot_id}"
    return f"hash:{stable_hash(record)}"


def string_value(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def nested(record: dict[str, Any], key: str) -> dict[str, Any]:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def identity_candidates(record: dict[str, Any], session: dict[str, Any], environment: dict[str, Any]) -> list[dict[str, str]]:
    env_identity = nested(environment, "identity")
    git = nested(environment, "git")
    hostname = string_value(environment.get("hostname")) or string_value(env_identity.get("hostname"))
    os_user = string_value(environment.get("user")) or string_value(env_identity.get("os_user"))
    user_domain = string_value(environment.get("user_domain")) or string_value(env_identity.get("user_domain"))

    raw_candidates = [
        ("user_email", session.get("user_email"), "session"),
        ("user_email", record.get("user_email"), "event"),
        ("user_email", env_identity.get("user_email"), "environment"),
        ("git_email", env_identity.get("git_user_email") or git.get("user_email"), "git"),
        ("git_email", env_identity.get("global_git_user_email"), "global_git"),
        ("windows_upn", env_identity.get("windows_upn"), "windows"),
        ("hostname", hostname, "environment"),
        ("os_user", os_user, "environment"),
        ("user_domain", user_domain, "environment"),
    ]
    if os_user and hostname:
        raw_candidates.append(("os_user_host", f"{os_user}@{hostname}", "environment"))
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, value, source in raw_candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        normalized_kind = normalize_identity_kind(kind)
        normalized_value = normalize_identity_value(normalized_kind, value)
        key = (normalized_kind, normalized_value)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"kind": normalized_kind, "value": normalized_value, "source": source})
    return candidates


def direct_email_candidate(candidates: list[dict[str, str]]) -> dict[str, str] | None:
    for candidate in candidates:
        if candidate["kind"] in {"user_email", "git_email", "windows_upn"} and "@" in candidate["value"]:
            return candidate
    return None


class WorklogStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.expanduser()
        self.raw_dir = self.data_dir / "raw"
        self.db_path = self.data_dir / "worklog.sqlite3"
        self._raw_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats_cache: tuple[int, dict[str, Any]] | None = None
        self._write_version = 0
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma busy_timeout = 30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("pragma journal_mode = wal")
            conn.execute("pragma synchronous = normal")
            conn.executescript(SCHEMA)
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("pragma table_info(records)").fetchall()}
        for name, definition in OPTIONAL_COLUMNS.items():
            if name not in columns:
                conn.execute(f"alter table records add column {name} {definition}")
        self._backfill_token_columns(conn)

    def _backfill_token_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            select record_pk, raw_json
            from records
            where token_usage_identity is null
            """
        ).fetchall()
        updates: list[tuple[str | None, str, str]] = []
        for row in rows:
            try:
                record = json.loads(row["raw_json"])
            except json.JSONDecodeError:
                continue
            identity = token_usage_identity(record)
            if identity is None:
                continue
            updates.append((identity, token_model(record), str(row["record_pk"])))
        if updates:
            conn.executemany(
                "update records set token_usage_identity = ?, token_model = ? where record_pk = ?",
                updates,
            )

    def append_raw(self, record: dict[str, Any], ingested_at: str) -> None:
        self.append_raw_many([(record, ingested_at)])

    def append_raw_many(self, records: list[tuple[dict[str, Any], str]]) -> None:
        by_day: dict[str, list[str]] = {}
        for record, ingested_at in records:
            raw = dict(record)
            raw["_server_ingested_at"] = ingested_at
            by_day.setdefault(ingested_at[:10], []).append(json_dumps(raw))

        with self._raw_lock:
            for day, lines in by_day.items():
                path = self.raw_dir / f"{day}.jsonl"
                with path.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines))
                    fh.write("\n")

    def insert_record(self, record: dict[str, Any]) -> bool:
        return self.insert_many([record])["accepted"] == 1

    def insert_many(self, records: list[dict[str, Any]]) -> dict[str, int]:
        accepted = 0
        duplicates = 0
        accepted_raw: list[tuple[dict[str, Any], str]] = []
        with self._connect() as conn:
            for record in records:
                ingested_at = utc_now()
                pk = record_pk(record)
                tokens = token_usage(record)
                try:
                    conn.execute(
                        """
                        insert into records (
                          record_pk, record_type, event_id, snapshot_id, snapshot_type,
                          source_id, surface, session_id, turn_id, hook_event_name,
                          environment_ref, session_ref, collection_level,
                          client_received_at, ingested_at,
                          input_tokens, cached_input_tokens, output_tokens,
                          reasoning_output_tokens, total_tokens,
                          token_usage_identity, token_model, raw_json
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            token_usage_identity(record),
                            token_model(record),
                            json_dumps(record),
                        ),
                    )
                except sqlite3.IntegrityError:
                    duplicates += 1
                    continue
                accepted += 1
                accepted_raw.append((record, ingested_at))
        if accepted_raw:
            self.append_raw_many(accepted_raw)
            with self._stats_lock:
                self._write_version += 1
                self._stats_cache = None
        return {"accepted": accepted, "duplicates": duplicates}

    def existing_record_pks(self, record_pks: list[str]) -> set[str]:
        keys = sorted({str(key) for key in record_pks if key})
        if not keys:
            return set()
        placeholders = ",".join("?" for _ in keys)
        sql = f"select record_pk from records where record_pk in ({placeholders})"
        with self._connect() as conn:
            rows = conn.execute(sql, keys).fetchall()
        return {str(row["record_pk"]) for row in rows}

    def count_records(self) -> int:
        with self._connect() as conn:
            row = conn.execute("select count(*) as count from records").fetchone()
        return int(row["count"])

    def cache_version(self) -> int:
        with self._stats_lock:
            return self._write_version

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

    def query_records_for_metrics(
        self,
        *,
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
        sql += " order by ingested_at asc"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def query_events_for_analysis(
        self,
        *,
        surface: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ["record_type = 'event'"]
        args: list[Any] = []
        if surface:
            where.append("surface = ?")
            args.append(surface)
        if session_id:
            where.append("session_id = ?")
            args.append(session_id)
        sql = "select raw_json from records where " + " and ".join(where) + " order by ingested_at asc"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def query_events_for_session_index(
        self,
        *,
        surface: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        bounded_limit = max(1, min(int(limit), 500))
        where = ["record_type = 'event'"]
        args: list[Any] = []
        if surface:
            where.append("surface = ?")
            args.append(surface)
        where_sql = " and ".join(where)
        with self._connect() as conn:
            total_row = conn.execute(
                f"select count(*) as count from (select coalesce(session_id, 'unknown') from records where {where_sql} group by coalesce(session_id, 'unknown'))",
                args,
            ).fetchone()
            session_rows = conn.execute(
                f"""
                select coalesce(session_id, 'unknown') as session_key
                from records
                where {where_sql}
                group by session_key
                order by max(coalesce(client_received_at, ingested_at)) desc
                limit ?
                """,
                [*args, bounded_limit],
            ).fetchall()
            session_ids = [str(row["session_key"]) for row in session_rows]
            if not session_ids:
                return [], 0

            event_where = [where_sql]
            event_args = list(args)
            unknown_requested = "unknown" in session_ids
            concrete_ids = [session_id for session_id in session_ids if session_id != "unknown"]
            session_clauses: list[str] = []
            if concrete_ids:
                placeholders = ",".join("?" for _ in concrete_ids)
                session_clauses.append(f"session_id in ({placeholders})")
                event_args.extend(concrete_ids)
            if unknown_requested:
                session_clauses.append("(session_id is null or session_id = '')")
            event_where.append("(" + " or ".join(session_clauses) + ")")
            rows = conn.execute(
                "select raw_json from records where " + " and ".join(event_where) + " order by ingested_at asc",
                event_args,
            ).fetchall()
        return [json.loads(row["raw_json"]) for row in rows], int(total_row["count"])

    def query_snapshots_by_ids(self, snapshot_ids: list[str]) -> list[dict[str, Any]]:
        ids = sorted({str(item) for item in snapshot_ids if item})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        sql = f"select raw_json from records where record_type = 'snapshot' and snapshot_id in ({placeholders})"
        with self._connect() as conn:
            rows = conn.execute(sql, ids).fetchall()
        return [json.loads(row["raw_json"]) for row in rows]

    def identity_mappings(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select identity_kind, identity_value, user_email, display_name, source, created_at, updated_at
                from identity_mappings
                order by user_email, identity_kind, identity_value
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_identity_mapping(
        self,
        *,
        identity_kind: str,
        identity_value: str,
        user_email: str,
        display_name: str | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        kind = normalize_identity_kind(identity_kind)
        value = normalize_identity_value(kind, identity_value)
        email = user_email.strip().lower()
        if not kind or not value or not email or "@" not in email:
            raise ValueError("identity_kind, identity_value, and valid user_email are required")
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into identity_mappings (
                  identity_kind, identity_value, user_email, display_name, source, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(identity_kind, identity_value) do update set
                  user_email = excluded.user_email,
                  display_name = excluded.display_name,
                  source = excluded.source,
                  updated_at = excluded.updated_at
                """,
                (kind, value, email, display_name, source, now, now),
            )
        with self._stats_lock:
            self._write_version += 1
            self._stats_cache = None
        return {
            "identity_kind": kind,
            "identity_value": value,
            "user_email": email,
            "display_name": display_name,
            "source": source,
            "updated_at": now,
        }

    def delete_identity_mapping(self, *, identity_kind: str, identity_value: str) -> bool:
        kind = normalize_identity_kind(identity_kind)
        value = normalize_identity_value(kind, identity_value)
        with self._connect() as conn:
            cursor = conn.execute(
                "delete from identity_mappings where identity_kind = ? and identity_value = ?",
                (kind, value),
            )
            deleted = cursor.rowcount > 0
        if deleted:
            with self._stats_lock:
                self._write_version += 1
                self._stats_cache = None
        return deleted

    def token_report(self, month: str | None = None) -> dict[str, Any]:
        month_label, start, end = month_bounds(month)
        with self._connect() as conn:
            token_rows = conn.execute(
                """
                select session_id, token_usage_identity, token_model, ingested_at,
                       input_tokens, cached_input_tokens, output_tokens,
                       reasoning_output_tokens, total_tokens, raw_json
                from records
                where token_usage_identity is not null
                  and ingested_at >= ?
                  and ingested_at < ?
                order by ingested_at asc
                """,
                (start, end),
            ).fetchall()
            snapshot_rows = conn.execute(
                """
                select raw_json
                from records
                where record_type = 'snapshot'
                """
            ).fetchall()
            mapping_rows = conn.execute(
                """
                select identity_kind, identity_value, user_email, display_name, source
                from identity_mappings
                """
            ).fetchall()

        mappings = {
            (str(row["identity_kind"]), str(row["identity_value"])): {
                "user_email": str(row["user_email"]),
                "display_name": row["display_name"],
                "source": row["source"],
            }
            for row in mapping_rows
        }
        environments_by_ref: dict[str, dict[str, Any]] = {}
        sessions_by_ref: dict[str, dict[str, Any]] = {}
        sessions_by_id: dict[str, dict[str, Any]] = {}
        for row in snapshot_rows:
            try:
                snapshot = json.loads(row["raw_json"])
            except json.JSONDecodeError:
                continue
            snapshot_id = string_value(snapshot.get("snapshot_id"))
            if snapshot.get("snapshot_type") == "environment" and snapshot_id:
                environments_by_ref[snapshot_id] = nested(snapshot, "environment")
            elif snapshot.get("snapshot_type") == "session" and snapshot_id:
                session = nested(snapshot, "session")
                sessions_by_ref[snapshot_id] = session
                session_id = string_value(session.get("session_id"))
                if session_id:
                    sessions_by_id.setdefault(session_id, session)

        totals = empty_token_totals()
        users: dict[str, dict[str, Any]] = {}
        unclaimed: dict[str, dict[str, Any]] = {}
        seen_usage: set[str] = set()

        def group_for(
            record: dict[str, Any],
            row: sqlite3.Row,
            candidates: list[dict[str, str]],
        ) -> tuple[str, dict[str, Any], bool]:
            direct = direct_email_candidate(candidates)
            if direct:
                return direct["value"], {"user_email": direct["value"], "display_name": None, "source": direct["kind"]}, True
            for candidate in candidates:
                mapping = mappings.get((candidate["kind"], candidate["value"]))
                if mapping:
                    return str(mapping["user_email"]), mapping, True
            fallback = next((item for item in candidates if item["kind"] == "hostname"), None) or next(iter(candidates), None)
            if fallback:
                key = f"{fallback['kind']}:{fallback['value']}"
            else:
                key = f"session:{row['session_id'] or record.get('session_id') or 'unknown'}"
            return key, {"user_email": None, "display_name": None, "source": "unclaimed"}, False

        for row in token_rows:
            identity = str(row["token_usage_identity"])
            if identity in seen_usage:
                continue
            seen_usage.add(identity)
            try:
                record = json.loads(row["raw_json"])
            except json.JSONDecodeError:
                record = {}
            session_id = string_value(row["session_id"]) or string_value(record.get("session_id"))
            session = sessions_by_ref.get(str(record.get("session_ref") or "")) or sessions_by_id.get(session_id or "") or {}
            environment = environments_by_ref.get(str(record.get("environment_ref") or "")) or {}
            candidates = identity_candidates(record, session, environment)
            group_key, identity_info, claimed = group_for(record, row, candidates)
            target_root = users if claimed else unclaimed
            target = target_root.setdefault(
                group_key,
                {
                    "user_email": identity_info.get("user_email"),
                    "display_name": identity_info.get("display_name"),
                    "identity_key": group_key,
                    "identity_source": identity_info.get("source"),
                    "token_totals": empty_token_totals(),
                    "token_totals_by_model": {},
                    "sessions": set(),
                    "hostnames": set(),
                    "os_users": set(),
                    "git_emails": set(),
                    "candidate_identities": {},
                },
            )
            add_token_totals(totals, row)
            add_token_totals(target["token_totals"], row)
            model = str(row["token_model"] or record.get("model") or "unknown")
            model_totals = target["token_totals_by_model"].setdefault(model, empty_token_totals())
            add_token_totals(model_totals, row)
            if session_id:
                target["sessions"].add(session_id)
            for candidate in candidates:
                target["candidate_identities"].setdefault(candidate["kind"], set()).add(candidate["value"])
                if candidate["kind"] == "hostname":
                    target["hostnames"].add(candidate["value"])
                elif candidate["kind"] == "os_user":
                    target["os_users"].add(candidate["value"])
                elif candidate["kind"] == "git_email":
                    target["git_emails"].add(candidate["value"])

        def finalize(groups: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for item in groups.values():
                finalized = dict(item)
                finalized["sessions"] = sorted(item["sessions"])
                finalized["session_count"] = len(finalized["sessions"])
                finalized["hostnames"] = sorted(item["hostnames"])
                finalized["os_users"] = sorted(item["os_users"])
                finalized["git_emails"] = sorted(item["git_emails"])
                finalized["candidate_identities"] = {
                    key: sorted(values)
                    for key, values in sorted(item["candidate_identities"].items())
                }
                items.append(finalized)
            items.sort(key=lambda value: int(value["token_totals"].get("total_tokens") or 0), reverse=True)
            return items

        return {
            "month": month_label,
            "range": {"start": start, "end": end},
            "token_usage_identities": len(seen_usage),
            "token_totals": totals,
            "users": finalize(users),
            "unclaimed": finalize(unclaimed),
            "identity_mappings": self.identity_mappings(),
        }

    def stats(self) -> dict[str, Any]:
        with self._stats_lock:
            if self._stats_cache and self._stats_cache[0] == self._write_version:
                return self._stats_cache[1]
            stats = self._stats_uncached()
            self._stats_cache = (self._write_version, stats)
            return stats

    def _stats_uncached(self) -> dict[str, Any]:
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
            token_rows = conn.execute(
                """
                select session_id, token_usage_identity, token_model,
                       input_tokens, cached_input_tokens, output_tokens,
                       reasoning_output_tokens, total_tokens
                from records
                where token_usage_identity is not null
                order by ingested_at asc
                """
            ).fetchall()
            session_snapshot_rows = conn.execute(
                """
                select raw_json
                from records
                where record_type = 'snapshot' and snapshot_type = 'session'
                """
            ).fetchall()

        model_by_session = session_models([json.loads(row["raw_json"]) for row in session_snapshot_rows])
        totals = {key: 0 for key in TOKEN_TOTAL_KEYS}
        totals_by_model: dict[str, dict[str, int]] = {}
        seen_usage: set[str] = set()
        for row in token_rows:
            identity = str(row["token_usage_identity"])
            if identity in seen_usage:
                continue
            seen_usage.add(identity)
            model = str(row["token_model"] or "")
            session_id = row["session_id"]
            if (not model or model == "unknown") and isinstance(session_id, str):
                model = model_by_session.get(session_id, model)
            if not model:
                model = "unknown"
            model_totals = totals_by_model.setdefault(model, {key: 0 for key in TOKEN_TOTAL_KEYS})
            for key in TOKEN_TOTAL_KEYS:
                value = int(row[key] or 0)
                totals[key] += value
                model_totals[key] += value
        return {
            "total_records": int(total),
            "by_record_type": {row["key"]: int(row["count"]) for row in by_type},
            "by_surface": {row["key"]: int(row["count"]) for row in by_surface},
            "by_hook_event_name": {row["key"]: int(row["count"]) for row in by_hook},
            "token_totals": totals,
            "token_totals_by_model": dict(sorted(totals_by_model.items())),
        }
