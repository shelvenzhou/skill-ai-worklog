---
name: ai-worklog
description: Install and operate an internal AI-assisted worklog for Codex and Cursor. Use when setting up automated session journaling of prompts, agent responses, tool inputs/results, compact session/environment snapshots, transcript-derived token usage, local JSONL records, or optional upload to a server through Codex/Cursor hooks.
---

# AI Worklog

## Objective

Install a local worklog that turns Codex and Cursor hook events into structured AI-assisted work records. The default internal mode is `full`, which records prompt text, agent responses exposed to hooks, tool inputs/results, compact environment/session snapshots, and best-effort token usage from Codex transcripts.

When asked to install or enable this skill, run the installer. Installing the skill files alone is not enough because hooks must be written into the host app config.

## Install

When a user asks to install or enable `ai-worklog`, install this skill first, then run the installer from the installed skill path. Installing the skill files only copies instructions and scripts; it does not enable worklog collection until `install.py` writes Codex/Cursor hooks.

If this skill is not installed yet and the user points to the GitHub repository, use these source details with `skill-installer`:

- repo: `shelvenzhou/skill-ai-worklog`
- ref: `master`
- path: `skills/ai-worklog`

Do not assume the repository uses `main`, and do not install the repo root as the skill. If `~/.codex/skills/ai-worklog` already exists, skip the copy step and run the installed `install.py` directly.

Run scripts with `python3`; do not rely on executable file permissions surviving the GitHub install path.

Default local install for Codex and Cursor:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full
```

Company-internal rollout with upload enabled:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full --server-url <COLLECTOR_URL>/events --api-key-env AI_WORKLOG_API_KEY
```

Only include `--server-url` when the user has provided a real collector endpoint or the internal endpoint is known. If no collector is provided, install local-only by omitting `--server-url`; events still write to `~/.ai-worklog/events`.

If upload requires auth on macOS, persist the token for future Codex/Cursor hook processes before running the installer or before restarting the apps:

```bash
launchctl setenv AI_WORKLOG_API_KEY <TOKEN>
```

After installation, show the user how to reduce or disable collection:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level diagnostic
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level off
```

For local development from the repository checkout, the equivalent path is `python3 skills/ai-worklog/scripts/install.py ...`.

## What It Installs

- Copies this skill to `$CODEX_HOME/skills/ai-worklog` and/or `~/.cursor/skills/ai-worklog`.
- Writes `~/.ai-worklog/config.json`.
- Appends idempotent hook handlers into `$CODEX_HOME/hooks.json` and/or `~/.cursor/hooks.json`.
- Enables Codex hooks in `$CODEX_HOME/config.toml` by setting `[features].hooks = true`.
- Records per-interaction JSONL events under `~/.ai-worklog/events/YYYY-MM-DD.jsonl`.
- Records non-repeated environment/session snapshots under `~/.ai-worklog/snapshots/YYYY-MM-DD.jsonl`.
- Spools failed uploads under `~/.ai-worklog/failed/YYYY-MM-DD.jsonl`.

## Collection Levels

- `full`: prompt/response/tool payloads plus event-specific raw hook payload. Internal-only default.
- `diagnostic`: envelope, sizes, hashes, token usage, no content bodies.
- `basic`: session/turn/surface/event metadata only.
- `off`: hooks stay installed but worklog exits without recording.

## Hook Sets

- `minimal`: default. Codex uses `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `SubagentStop`, `Stop`. Cursor uses `sessionStart`, `beforeSubmitPrompt`, `postToolUse`, `postToolUseFailure`, `afterAgentResponse`, `subagentStop`, `stop`. This keeps authorization short while preserving prompts, responses, tool inputs/results, failed tool outputs when exposed, subagent completion, and Codex token usage through transcript reads.
- `full`: adds permission, pre-tool, compaction, file edit/read, shell/MCP, tab, session-end, and thought hooks. Use only for internal diagnostic runs where extra authorization prompts are acceptable.

## Data Model

Event records are written one per hook event:

- `record_type = "event"`, `event_id`, `received_at`, `collector_version`
- `surface`, `hook_event_name`, `session_id`, `turn_id`, `agent_id`, `agent_type`
- `timeline`: `trace_id`, `span_id`, optional `parent_span_id`, per-session `sequence_no`, start/end timestamps, and duration when exposed
- `operation`: normalized category, phase, success state, and error type when available
- `tool`: normalized tool name/type, command, exit code, touched files, and duration when exposed by the hook payload
- `skill`: optional skill name/path/version/phase when the host product or skill emits those fields
- `content`: prompt, response, tool input/result, thought/summary when exposed by the product and enabled
- `usage`: best-effort `token_count` info from transcript or hook payload
- `environment_ref` and `session_ref`: stable hashes that point to snapshot records
- `raw_hook_input`: present only at `full`, with common envelope keys removed
- `workspace_diff`: present on `Stop` / `sessionEnd` when the workspace is a git repo; stores compact numstat-style line counts, not full diff bodies

Snapshot records are written once per environment hash and once per session id. They contain model, cwd, transcript path, user email, OS, hostname, git root/branch/commit/dirty state, and other global metadata that would otherwise repeat on every event.

The collector server also exposes `GET /metrics/code` for post-processed code metrics:

- `generated_code`: weak definition, parsed from successful post-write patch/file-edit payloads.
- `adopted_code`: medium definition, latest session-end `git diff HEAD` code line counts still present in the worktree.

The collector server exposes `GET /sessions` and `GET /sessions/<session_id>` for session browsing. These endpoints consume the structured `timeline`, `operation`, `tool`, and `skill` blocks to return process summaries, compact timelines, tool counts, skill counts, failure counts, and code metrics.

## Operations

Inspect local data:

```bash
tail -n 20 ~/.ai-worklog/events/$(date +%F).jsonl
```

Inspect snapshots:

```bash
tail -n 20 ~/.ai-worklog/snapshots/$(date +%F).jsonl
```

Change collection level:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level diagnostic
```

Enable full hook coverage:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --hook-set full
```

Disable collection without removing hooks:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level off
```

Smoke-test upload without a real backend:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/dev_server.py --port 8765
python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --server-url http://127.0.0.1:8765/events
```

Run the bundled collector server from the repository root:

```bash
python3 -m server.ai_worklog_server --host 127.0.0.1 --port 8765 --data-dir ./data
```

## One-Line Prompt For Teammates

After publishing this skill in the internal skill source, teammates should only need a short request like:

```text
请用 skill-installer 从 shelvenzhou/skill-ai-worklog 的 master 分支安装 skills/ai-worklog，然后运行 python3 ~/.codex/skills/ai-worklog/scripts/install.py --surface both --level full
```

Add `--server-url <COLLECTOR_URL>/events --api-key-env AI_WORKLOG_API_KEY` to that request only when upload to a known collector is required. The full installer command, auth instruction, and `diagnostic`/`off` commands are intentionally kept in this skill's `Install` section so the agent sees them when it activates the skill.

## Notes

- Codex hook payloads do not directly include token usage. The worklog reads the Codex `transcript_path` tail and extracts the latest `event_msg.type == "token_count"` record when available.
- Raw reasoning is not a stable product surface. The worklog records reasoning summaries or thought events only when they are explicitly exposed by the host product. Encrypted reasoning content in transcripts is ignored.
- The hook scripts never block agent execution intentionally; upload failures are written to the failed spool and return success.
