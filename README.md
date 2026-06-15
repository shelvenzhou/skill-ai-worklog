# AI Worklog Observability

AI Worklog Observability is a small internal observability package for Codex and Cursor usage. It contains:

- `skills/ai-worklog`: a Codex/Cursor skill that installs hooks and records local AI worklog events.
- `server/ai_worklog_server`: a lightweight collector server that receives those records, stores raw JSONL, and indexes them into SQLite.

The first rollout target is internal usage. The defaults intentionally collect rich records when the client is installed with `--level full`.

## Quick Start

Run the collector server:

```bash
python3 -m server.ai_worklog_server --host 127.0.0.1 --port 8765 --data-dir ./data
```

Install hooks on a teammate machine:

```bash
python3 skills/ai-worklog/scripts/install.py --surface both --level full --server-url http://127.0.0.1:8765/events
```

With bearer-token authentication:

```bash
export AI_WORKLOG_SERVER_TOKEN=server-secret
python3 -m server.ai_worklog_server --token-env AI_WORKLOG_SERVER_TOKEN

export AI_WORKLOG_API_KEY=server-secret
python3 skills/ai-worklog/scripts/install.py --surface both --level full --server-url http://127.0.0.1:8765/events --api-key-env AI_WORKLOG_API_KEY
```

## Teammate Install Prompt

After publishing this repository in an internal location, teammates should only need to paste a short request to an agent:

```text
帮我安装 ai-worklog
```

Keep the operational install details in `skills/ai-worklog/SKILL.md` so the agent sees them after installing or activating the skill. Before publishing, replace `<INTRANET_SERVER_URL>` with the internal collector endpoint and decide whether upload auth is required.

The agent-facing default installer command is:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full --server-url <INTRANET_SERVER_URL>/events --api-key-env AI_WORKLOG_API_KEY
```

If upload authentication is required on macOS, persist the token for future Codex/Cursor hook processes before installing or restarting the apps:

```bash
launchctl setenv AI_WORKLOG_API_KEY <TOKEN>
```

The collection is intended for company-internal control and usage observability. `full` records prompts, agent responses, and tool payloads exposed by hooks. To reduce collection later:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level diagnostic
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level off
```

## Endpoints

- `GET /healthz`: health check and record count.
- `POST /events`: accepts one JSON record, a JSON array of records, or NDJSON.
- `GET /records?limit=50&record_type=event&surface=codex&session_id=...`: recent indexed records.
- `GET /stats`: aggregate counts and token totals.

## Data Layout

The server writes:

- `data/raw/YYYY-MM-DD.jsonl`: every accepted record, exactly as received plus ingest metadata.
- `data/worklog.sqlite3`: query index for sessions, events, snapshots, and token totals.

The client writes:

- `~/.ai-worklog/events/YYYY-MM-DD.jsonl`: local per-interaction records.
- `~/.ai-worklog/snapshots/YYYY-MM-DD.jsonl`: deduplicated environment/session snapshots.
- `~/.ai-worklog/failed/YYYY-MM-DD.jsonl`: upload failures for later replay.

## Validation

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 skills/ai-worklog/scripts/test_journal.py
```
