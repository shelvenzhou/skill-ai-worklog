---
name: ai-worklog
description: Install and operate an internal AI-assisted worklog for Codex and Cursor. Use when setting up automated session journaling of prompts, agent responses, tool inputs/results, compact session/environment snapshots, transcript-derived token usage, local JSONL records, or optional upload to a server through Codex/Cursor hooks.
---

# AI Worklog

## Objective

Install a local worklog that turns Codex and Cursor hook events into structured AI-assisted work records. Installing the skill files alone is not enough: `install.py` must write Codex/Cursor hook config.

The default internal mode is `full`, which records prompt text, agent responses exposed to hooks, tool inputs/results, compact environment/session snapshots, and best-effort token usage from Codex transcripts. Use `diagnostic`, `basic`, or `off` when the user asks to reduce collection.

## Install

If this skill is not installed yet and the user points to GitHub, install the skill directory, not the repository root:

- repo: `shelvenzhou/skill-ai-worklog`
- ref: `master`
- path: `skills/ai-worklog`
- URL: `https://github.com/shelvenzhou/skill-ai-worklog/tree/master/skills/ai-worklog`

Do not pass the bare repository URL to `skill-installer --url`; it requires `--path` for this repository. Do not assume the branch is `main`.

Run scripts with `python3`; executable bits may not survive GitHub install. If `~/.codex/skills/ai-worklog` already exists, skip copying and run its installer directly.

Default local-only install:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full
```

Company-internal install with upload:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full --server-url <COLLECTOR_URL>/events --api-key-env AI_WORKLOG_API_KEY
```

If the source moves from GitHub to an internal GitLab repo, keep publishing `skills/ai-worklog/skill-version.json` and configure the installed skill with GitLab URLs:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full \
  --skill-update-manifest-url https://gitlab.example/group/repo/-/raw/master/skills/ai-worklog/skill-version.json \
  --skill-source-url https://gitlab.example/group/repo/-/tree/master/skills/ai-worklog
```

The version check is provider-agnostic: it reads the raw JSON manifest and compares `version`. Installed hooks trigger a background check on session start, throttled to once per day by default, and print a local notice on a later session start when a newer remote version is found. Check manually when needed:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/check_update.py --config ~/.ai-worklog/config.json --force
```

During migration, keep the old GitHub manifest available as a pointer to the new GitLab `install_url` when possible, so already-installed clients can still discover the move. Machines that cannot reach GitHub should rerun the installer with the GitLab manifest URL.

Only include `--server-url` when the user has provided a real collector endpoint or the internal endpoint is known. Without it, events still write locally under `~/.ai-worklog/events`.

If upload auth is required on macOS, persist the token for future Codex/Cursor hook processes before restarting the apps:

```bash
launchctl setenv AI_WORKLOG_API_KEY <TOKEN>
```

Hook processes only write local JSONL files under `~/.ai-worklog`; uploads run through a throttled background replay process. If the current install command will run immediate backfill or use `--sync-upload`, also set `AI_WORKLOG_API_KEY` in that shell environment.

With upload configured, hooks trigger background replay through `async_upload_trigger.py` at most once per minute by default. Collector or network failures stay in the background; agent execution only waits for local writes. Codex `SessionStart` hooks also start a background historical backfill through `codex_backfill_trigger.py`. The backfill trigger reads `~/.codex/sessions/**/rollout-*.jsonl`, uses a lock plus a default 24-hour throttle, caps each subprocess run at 30 minutes, logs to `~/.ai-worklog/codex_backfill.log`, and stores progress in `~/.ai-worklog/codex_backfill_state.sqlite3`.

Run historical backfill immediately during installation only if the user asks for it:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full --server-url <COLLECTOR_URL>/events --api-key-env AI_WORKLOG_API_KEY --backfill-codex-history
```

For local development from the repository checkout, use `python3 skills/ai-worklog/scripts/install.py ...`.

## What It Installs

- Copies this skill to `$CODEX_HOME/skills/ai-worklog` and/or `~/.cursor/skills/ai-worklog`.
- Writes `~/.ai-worklog/config.json`.
- Appends idempotent hook handlers into `$CODEX_HOME/hooks.json` and/or `~/.cursor/hooks.json`.
- Enables Codex hooks in `$CODEX_HOME/config.toml` by setting `[features].hooks = true`.
- Writes event, snapshot, failed-upload, async-upload, replay, Codex-backfill, and skill-update state under `~/.ai-worklog`.

Hook commands are guarded: if `journal.py` has already been deleted, installed hooks no-op instead of failing. Still prefer `--uninstall` before deleting the skill so stale hook entries are removed.

## Collection Levels

- `full`: prompt/response/tool payloads plus event-specific raw hook payload. Internal-only default.
- `diagnostic`: envelope, sizes, hashes, token usage, no content bodies.
- `basic`: session/turn/surface/event metadata only.
- `off`: hooks stay installed but worklog exits without recording.

## Hook Sets

- `minimal`: default. Codex uses `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `SubagentStop`, `Stop`. Cursor uses `sessionStart`, `beforeSubmitPrompt`, `postToolUse`, `postToolUseFailure`, `afterAgentResponse`, `subagentStop`, `stop`.
- `full`: adds permission, pre-tool, compaction, file edit/read, shell/MCP, tab, session-end, and thought hooks. Use only for internal diagnostic runs where extra authorization prompts are acceptable.

## Versioning

`skills/ai-worklog/skill-version.json` is the source of truth for the installed skill release. Its `version` is the client skill version used in hook records and upload headers; `event_schema_version` is the data contract version and can change independently; `package_version` tracks the repository package version in `pyproject.toml`; `release_tag` is the Git tag expected for the published release.

When publishing a new release, update `skill-version.json`, keep `pyproject.toml` and `uv.lock` in sync, publish the raw manifest at `remote_manifest_url`, then tag the commit with `release_tag`.

## Data Model

Event records include normalized metadata for surface, hook event, session/turn, timeline, operation, tool, optional skill, content, usage, snapshot refs, and compact workspace diff when available. At `full`, `raw_hook_input` is also retained after common envelope keys are removed.

Snapshot records are deduplicated by stable hash and contain model, cwd, transcript path, user email, OS, hostname, git root/branch/commit/dirty state, and other session/environment metadata that would otherwise repeat on every event.

Collector endpoints:

- `POST /events`: accepts one JSON object, a JSON array, or NDJSON.
- `POST /events/exists`: preflight deduplication by record primary key.
- `GET /records`: recent indexed records.
- `GET /stats`: aggregate counts and token totals.
- `GET /sessions` and `GET /sessions/<session_id>`: session browsing, timelines, process summaries, token totals, tool/skill/failure counts, and code metrics.
- `GET /metrics/code`: generated/adopted/uncommitted code metrics.
- `GET /ui`: browser dashboard.

Code metrics are approximate: `generated_code` comes from successful write/patch payloads and transcript-derived apply_patch events; `adopted_code` comes from latest workspace diff or successful `git commit` summaries; `uncommitted_code` tracks generated code still visible after the latest observed commit. Workspace diff snapshots store numstat-style counts, not full diff bodies.

## Operations

Inspect local data:

```bash
tail -n 20 ~/.ai-worklog/events/$(date +%F).jsonl
tail -n 20 ~/.ai-worklog/snapshots/$(date +%F).jsonl
```

Change level, disable, or uninstall:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level diagnostic
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level off
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --uninstall
```

Enable full hook coverage:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --hook-set full
```

Smoke-test upload without the full collector:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/dev_server.py --port 8765
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --server-url http://127.0.0.1:8765/events
```

Replay local backlog:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/replay.py --server-url <COLLECTOR_URL>/events --batch-size 100
```

Installed hooks use background replay by default. Use `--sync-upload` only for local debugging when foreground upload behavior is explicitly needed.

Backfill existing Codex sessions:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/codex_backfill.py --sessions-root ~/.codex/sessions --server-url <COLLECTOR_URL>/events --batch-size 250
```

Use `--dry-run` first for a count-only pass. Use `--force` when the local upload ledger may be stale and the collector should deduplicate again. Disable automatic Codex backfill only when required:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full --server-url <COLLECTOR_URL>/events --no-auto-codex-backfill
```

Run the bundled collector server from the repository root:

```bash
python3 -m server.ai_worklog_server --host 127.0.0.1 --port 8765 --data-dir ./data
```

Upload preflight is enabled by default through `POST /events/exists`; disable it only for collector compatibility:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --no-upload-preflight
```

Disable background skill version checks only when the skill source is not reachable from the machine:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --no-skill-update-check
```

## Teammate Prompt

After publishing this skill internally, teammates should only need:

```text
请用 skill-installer 从 shelvenzhou/skill-ai-worklog 的 master 分支安装 skills/ai-worklog，然后运行 python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full
```

Add `--server-url <COLLECTOR_URL>/events --api-key-env AI_WORKLOG_API_KEY` only when upload to a known collector is required.

## Notes

- Codex hook payloads do not directly include token usage. The worklog reads the Codex `transcript_path` tail and extracts the latest `event_msg.type == "token_count"` record when available.
- Raw reasoning is not a stable product surface. The worklog records reasoning summaries or thought events only when explicitly exposed by the host product. Encrypted reasoning content in transcripts is ignored.
- Hook scripts do not intentionally block agent execution; upload failures are written to the failed spool and return success.
