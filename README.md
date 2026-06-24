# AI Worklog Observability

Internal observability package for Codex and Cursor usage.

- `skills/ai-worklog`: installs Codex/Cursor hooks and writes local worklog records.
- `server/ai_worklog_server`: receives records, stores raw JSONL, indexes SQLite, and serves a small browser dashboard.

The default rollout mode is intentionally rich: `--level full` records prompts, agent responses, tool payloads, environment/session snapshots, and best-effort token usage exposed by hooks or Codex transcripts. Treat collector access as sensitive.

## Install

Preferred install path is a single bootstrap command. It fetches the release manifest, downloads the skill source, runs the deterministic installer, runs doctor, and ends with a machine-readable marker:

```bash
curl -fsSL https://raw.githubusercontent.com/shelvenzhou/skill-ai-worklog/master/skills/ai-worklog/scripts/bootstrap.py | python3 -
```

Install is complete only when the terminal prints:

```text
AI_WORKLOG_INSTALL: PASS (...)
```

Agent prompt for teammate installs:

```text
运行 curl -fsSL https://raw.githubusercontent.com/shelvenzhou/skill-ai-worklog/master/skills/ai-worklog/scripts/bootstrap.py | python3 -，看到 AI_WORKLOG_INSTALL: PASS 即完成。
```

The bootstrap defaults to `--surface both --level full`. Optional flags are still available for explicit rollout choices:

Add upload only when a real collector is available:

```bash
curl -fsSL https://raw.githubusercontent.com/shelvenzhou/skill-ai-worklog/master/skills/ai-worklog/scripts/bootstrap.py | python3 - --server-url <COLLECTOR_URL>/events --api-key-env AI_WORKLOG_API_KEY
```

If the skill source moves from GitHub to an internal GitLab repo, publish the same `skills/ai-worklog/skill-version.json` file in that repo and point installs at GitLab's raw file URL:

```bash
curl -fsSL https://gitlab.example/group/repo/-/raw/master/skills/ai-worklog/scripts/bootstrap.py | python3 - \
  --manifest-url https://gitlab.example/group/repo/-/raw/master/skills/ai-worklog/skill-version.json
```

During migration, keep the old GitHub manifest available as a pointer to the new GitLab `install_url` when possible, so already-installed clients can still discover the move. Machines that cannot reach GitHub should rerun the installer with the GitLab manifest URL.

Installed hooks trigger background maintenance on session start. Local maintenance self-heals hook wiring when skill files changed but hooks were not rewritten. Remote version checks are throttled to once per day by default; if the manifest has a newer version, the next session start prints a local update notice. Automatic remote updates are opt-in with `--auto-skill-update`. To check manually:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/check_update.py --config ~/.ai-worklog/config.json --force
```

Verify an install from the client machine:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/doctor.py --surface both
python3 ~/.codex/skills/ai-worklog/scripts/doctor.py --surface both --json
```

Use `--smoke-write` only when you want an end-to-end hook write test; it records one local diagnostic event with `source_id=ai-worklog-doctor`.

Update an installed skill when the manifest reports a newer version:

```bash
python3 ~/.codex/skills/ai-worklog/scripts/update_skill.py --surface both
```

The updater only runs when the manifest has machine-installable fields (`archive_url` plus `path`, or GitHub `repo`/`ref`/`path`). It backs up the old skill directory, replaces it, rebuilds hooks with the current config, and runs doctor.

On macOS, persist upload auth for future Codex/Cursor hook processes:

```bash
launchctl setenv AI_WORKLOG_API_KEY <TOKEN>
```

Hook processes only write local JSONL files under `~/.ai-worklog`; uploads run through a throttled background replay process. If the current shell command will run an immediate backfill or use `--sync-upload`, also set `AI_WORKLOG_API_KEY` in that shell.

Local checkout install for development:

```bash
python3 skills/ai-worklog/scripts/install.py --surface both --level full --server-url http://127.0.0.1:8765/events
```

Cursor on Windows does not provide a bundled Python runtime. Run the installer from an environment that has Python available, then restart Cursor after hooks are written. The installer records that Python path into the Windows hook launcher:

```powershell
$installer = "$HOME\.codex\skills\ai-worklog\scripts\install.py"
if (!(Test-Path $installer)) { $installer = "$HOME\.cursor\skills\ai-worklog\scripts\install.py" }
python $installer --surface cursor --level full
```

The installed Windows hook launcher tries `AI_WORKLOG_PYTHON`, the install-time Python path, `py -3`, then `python`. If none are available, it writes `~/.ai-worklog/errors/runtime.log` and exits successfully so Cursor is not blocked by observability.

The installer and doctor do not install Python automatically. On Windows, set `AI_WORKLOG_PYTHON` to a managed Python executable, install Python through your organization's distribution channel, or use a standard installer such as `winget install Python.Python.3.12`.

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

With separate upload and dashboard tokens:

```bash
export AI_WORKLOG_SERVER_TOKEN=shared-upload-secret
export AI_WORKLOG_UI_TOKEN=personal-ui-secret
python3 -m server.ai_worklog_server --token-env AI_WORKLOG_SERVER_TOKEN --ui-token-env AI_WORKLOG_UI_TOKEN
```

Client uploads send `AI_WORKLOG_API_KEY` as `Authorization: Bearer ...`; set it to the server's upload token. The UI token is separate and only authorizes the browser dashboard and read APIs. Open `http://127.0.0.1:8765/ui` and sign in with the UI token.

## Versioning

`skills/ai-worklog/skill-version.json` is the source of truth for the client skill release:

- `version`: the installed skill/client release, also used by hook records and the `X-AI-Worklog-Version` upload header.
- `release_tag`: the Git tag expected for that release, currently `ai-worklog-v0.3.5`.
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

Backfill historical Cursor agent transcripts:

```bash
python3 ~/.cursor/skills/ai-worklog/scripts/cursor_backfill.py --config ~/.ai-worklog/config.json --upload
```

When `server_url` is configured, hook events trigger background replay uploads at most once per minute by default, so collector/network failures do not block Codex or Cursor execution. Installed Codex `SessionStart` hooks also trigger background history backfill automatically unless installed with `--no-auto-codex-backfill`. Add `--backfill-codex-history` to `install.py` only when the first history upload should run immediately during installation.

## API

- `GET /` or `GET /ui`: browser dashboard, protected by the UI token when configured.
- `GET /healthz`: public health check; record count is only included for authorized UI requests.
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

## Troubleshooting

### Windows: Codex App hooks not running after install

**Symptom**: Hooks are installed and `doctor.py` reports them as present, but they never fire when using the Codex desktop app. The Codex App Settings → Hooks page shows empty.

**Cause**: The Codex App does not show a trust prompt for hooks installed via `hooks.json`. Hooks must have a `trusted_hash` entry in `~/.codex/config.toml` before the app will execute them. The Codex CLI shows this trust prompt at startup; the app does not.

**Fix**: Open the Codex CLI (terminal) at least once after installation. It will display a trust prompt for any new hook commands. Approve them, then restart the Codex App. The CLI and App share the same `config.toml`, so trust granted in the CLI takes effect for the App immediately.

```powershell
# Run any Codex CLI session to trigger the trust prompt
codex
```

After approving, verify hooks are trusted:

```powershell
python $HOME\.codex\skills\ai-worklog\scripts\doctor.py --surface codex --app-server-check
```

The doctor should report `"Codex app-server discovered N trusted AI Worklog hooks."` If it reports untrusted hooks, approve them again in the CLI.

**Note**: The Settings → Hooks page in the Codex App only shows enterprise-managed hooks (MDM/cloud-pushed). Hooks installed via `hooks.json` do not appear there even when working correctly — this is expected.

### Cursor: prompt-based install skips hook wiring

**Symptom**: Cursor's agent copies skill files but hooks never fire, because `install.py` was not run.

**Fix**: After any file-copy install in Cursor, always run the installer explicitly:

```bash
# macOS/Linux
python3 ~/.cursor/skills/ai-worklog/scripts/install.py --surface cursor --level full
python3 ~/.cursor/skills/ai-worklog/scripts/doctor.py --surface cursor
```

```powershell
# Windows
python "$HOME\.cursor\skills\ai-worklog\scripts\install.py" --surface cursor --level full
python "$HOME\.cursor\skills\ai-worklog\scripts\doctor.py" --surface cursor
```

The install is only complete when doctor reports hooks as written and usable.

## Validation

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 skills/ai-worklog/scripts/test_journal.py
```
