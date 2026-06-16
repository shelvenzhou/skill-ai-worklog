# AI Worklog Observability

Internal observability package for Codex and Cursor usage.

- `skills/ai-worklog`: installs Codex/Cursor hooks and writes local worklog records.
- `server/ai_worklog_server`: receives records, stores raw JSONL, indexes SQLite, and serves a small browser dashboard.

The default rollout mode is intentionally rich: `--level full` records prompts, agent responses, tool payloads, environment/session snapshots, and best-effort token usage exposed by hooks or Codex transcripts. Treat collector access as sensitive.

## Install

For teammate installs, give the agent the installable skill directory, not the repository root:

```text
请用 skill-installer 从 shelvenzhou/skill-ai-worklog 的 master 分支安装 skills/ai-worklog，然后运行：
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full
```

Add upload only when a real collector is available:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full --server-url <COLLECTOR_URL>/events --api-key-env AI_WORKLOG_API_KEY
```

If the skill source moves from GitHub to an internal GitLab repo, publish the same `skills/ai-worklog/skill-version.json` file in that repo and point installs at GitLab's raw file URL:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full \
  --skill-update-manifest-url https://gitlab.example/group/repo/-/raw/master/skills/ai-worklog/skill-version.json \
  --skill-source-url https://gitlab.example/group/repo/-/tree/master/skills/ai-worklog
```

During migration, keep the old GitHub manifest available as a pointer to the new GitLab `install_url` when possible, so already-installed clients can still discover the move. Machines that cannot reach GitHub should rerun the installer with the GitLab manifest URL.

Installed hooks trigger a background remote version check on session start, throttled to once per day by default. If the remote manifest has a newer version, the next session start prints a local update notice. To check manually:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/check_update.py --config ~/.ai-worklog/config.json --force
```

On macOS, persist upload auth for future Codex/Cursor hook processes:

```bash
launchctl setenv AI_WORKLOG_API_KEY <TOKEN>
```

Hook processes only write local JSONL files under `~/.ai-worklog`; uploads run through a throttled background replay process. If the current shell command will run an immediate backfill or use `--sync-upload`, also set `AI_WORKLOG_API_KEY` in that shell.

Local checkout install for development:

```bash
python3 skills/ai-worklog/scripts/install.py --surface both --level full --server-url http://127.0.0.1:8765/events
```

Reduce or stop collection:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level diagnostic
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level off
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --uninstall
```

`--uninstall` removes hook handlers and leaves existing `~/.ai-worklog` logs in place. Run it before deleting the installed skill files when possible.

## Collector

Run locally:

```bash
python3 -m server.ai_worklog_server --host 127.0.0.1 --port 8765 --data-dir ./data
```

With bearer-token authentication:

```bash
export AI_WORKLOG_SERVER_TOKEN=server-secret
python3 -m server.ai_worklog_server --token-env AI_WORKLOG_SERVER_TOKEN
```

Open `http://127.0.0.1:8765/ui` for the dashboard.

## Versioning

`skills/ai-worklog/skill-version.json` is the source of truth for the client skill release:

- `version`: the installed skill/client release, also used by hook records and the `X-AI-Worklog-Version` upload header.
- `release_tag`: the Git tag expected for that release, currently `ai-worklog-v0.3.0`.
- `event_schema_version`: the record schema version; this can stay stable across skill releases.
- `package_version`: the Python project package version, kept aligned with the skill release.

When publishing a release, update `skill-version.json`, keep `pyproject.toml` and `uv.lock` in sync, publish the raw manifest at the configured `remote_manifest_url`, then tag the commit with `release_tag`.

## Data Flow

Client files:

- `~/.ai-worklog/events/YYYY-MM-DD.jsonl`: local event records.
- `~/.ai-worklog/snapshots/YYYY-MM-DD.jsonl`: deduplicated environment/session snapshots.
- `~/.ai-worklog/failed/YYYY-MM-DD.jsonl`: failed uploads for replay.
- `~/.ai-worklog/async_upload.log`: background replay upload log.
- `~/.ai-worklog/upload_state.sqlite3`: replay upload ledger.
- `~/.ai-worklog/codex_backfill_state.sqlite3`: Codex history backfill ledger.

Server files:

- `data/raw/YYYY-MM-DD.jsonl`: accepted records with ingest metadata.
- `data/worklog.sqlite3`: query index for events, snapshots, sessions, tokens, and metrics.

Replay local backlog:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/replay.py --server-url http://127.0.0.1:8765/events --batch-size 100
```

Backfill historical Codex transcripts:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/codex_backfill.py --sessions-root ~/.codex/sessions --server-url http://127.0.0.1:8765/events --batch-size 250
```

When `server_url` is configured, hook events trigger background replay uploads at most once per minute by default, so collector/network failures do not block Codex or Cursor execution. Installed Codex `SessionStart` hooks also trigger background history backfill automatically unless installed with `--no-auto-codex-backfill`. Add `--backfill-codex-history` to `install.py` only when the first history upload should run immediately during installation.

## API

- `GET /` or `GET /ui`: browser dashboard.
- `GET /healthz`: health check and record count.
- `POST /events`: accepts one JSON object, a JSON array, or NDJSON.
- `POST /events/exists`: preflight deduplication by record primary key.
- `GET /records?limit=50&record_type=event&surface=codex&session_id=...`: indexed records.
- `GET /sessions?limit=50&surface=codex`: session summaries.
- `GET /sessions/<session_id>?limit=200&surface=codex`: session timeline and detail.
- `GET /stats`: aggregate counts and token totals.
- `GET /metrics/code?surface=codex&session_id=...`: generated/adopted/uncommitted code metrics.

## Metrics

Session APIs aggregate hook counts, operation/tool/skill counts, failures, token totals, model totals, and code metrics.

Code metric definitions are intentionally approximate:

- `generated_code`: successful write/patch payloads, plus transcript-derived apply_patch events when available.
- `adopted_code`: generated code still present in the latest workspace diff, or observed in a successful `git commit` summary.
- `uncommitted_code`: generated code still visible after the latest observed commit.
- `latest_git_commit_code`: latest parsed `git commit` summary.

The client records compact `workspace_diff` numstat snapshots on `Stop` / `sessionEnd`; it does not upload full diff bodies.

## Validation

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 skills/ai-worklog/scripts/test_journal.py
```
