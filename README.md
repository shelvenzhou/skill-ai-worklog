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

This repository's default branch is `master`, and the skill lives under `skills/ai-worklog` rather than at the repo root. For a first-time install, give the agent the exact source location:

```text
请用 skill-installer 从 GitHub 安装 ai-worklog skill：
- repo: shelvenzhou/skill-ai-worklog
- ref: master
- path: skills/ai-worklog

安装 skill 文件后不要停下；继续用 python3 运行已安装 skill 里的脚本来写入 Codex/Cursor hooks：
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full

如果我提供了内网 collector 地址，则给 install.py 增加：
--server-url <COLLECTOR_URL>/events --api-key-env AI_WORKLOG_API_KEY

如果没有提供 collector 地址，先按本地记录模式安装，不要用 <INTRANET_SERVER_URL> 占位符。
```

If the skill is already installed at `~/.codex/skills/ai-worklog`, skip the GitHub copy step and run the installed `install.py` directly with `python3`.

Keep the operational install details in `skills/ai-worklog/SKILL.md` so the agent sees them after installing or activating the skill. Before publishing, decide whether upload auth is required and document the internal collector endpoint if there is a stable one.

The agent-facing company-internal installer command with upload enabled is:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full --server-url <COLLECTOR_URL>/events --api-key-env AI_WORKLOG_API_KEY
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
- `GET /sessions?limit=50&surface=codex`: session summaries with hook counts, token totals, and code metrics.
- `GET /sessions/<session_id>?limit=200&surface=codex`: chronological session events, snapshots, and session code metrics.
- `GET /stats`: aggregate counts and token totals.
- `GET /metrics/code?surface=codex&session_id=...`: post-processed generated/adopted code line metrics.

## Data Layout

The server writes:

- `data/raw/YYYY-MM-DD.jsonl`: every accepted record, exactly as received plus ingest metadata.
- `data/worklog.sqlite3`: query index for sessions, events, snapshots, and token totals.

The client writes:

- `~/.ai-worklog/events/YYYY-MM-DD.jsonl`: local per-interaction records.
- `~/.ai-worklog/snapshots/YYYY-MM-DD.jsonl`: deduplicated environment/session snapshots.
- `~/.ai-worklog/failed/YYYY-MM-DD.jsonl`: upload failures for later replay.

## Server Analysis

The collector includes server-side analysis endpoints for session browsing and code metrics:

```bash
curl 'http://127.0.0.1:8765/sessions?limit=20'
curl 'http://127.0.0.1:8765/sessions/<SESSION_ID>?limit=200'
curl 'http://127.0.0.1:8765/metrics/code'
```

`/sessions` returns per-session summaries: event count, hook counts, token totals, environment/session refs, and generated/adopted code metrics.

`/sessions/<SESSION_ID>` returns chronological events for one session plus the referenced environment/session snapshots and the same code metrics scoped to that session.

Current metric definitions:

- `generated_code`: weak definition. Counts code additions/deletions parsed from successful post-write hook payloads such as patch/file-edit tool inputs. Assistant response code blocks are not counted unless they appear in a write/patch payload.
- `adopted_code`: medium definition. Counts code additions/deletions still present in the latest session-end workspace `git diff HEAD` snapshot. This is not proof that code was committed or merged.

To support `adopted_code`, the client records a compact `workspace_diff` numstat snapshot on `Stop` / `sessionEnd` hooks when the hook payload has a git worktree `cwd`. The snapshot stores file paths and line counts, not full diff bodies.

## Validation

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 skills/ai-worklog/scripts/test_journal.py
```
