# AI Worklog 数据采集与统计口径

这份文档说明当前 `ai-worklog` 采集了哪些数据，以及服务端/看板统计了哪些指标、口径是什么。它主要用于自己理解系统行为和排查指标，不是对外隐私声明。

## 1. 我们收集了哪些数据

### 1.1 数据来源

当前有三类来源：

1. 实时 hook 事件
   - Codex 和 Cursor 的 hook 会调用 `skills/ai-worklog/scripts/journal.py`。
   - 默认安装的 `minimal` hook 集：
     - Codex: `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `SubagentStop`, `Stop`
     - Cursor: `sessionStart`, `beforeSubmitPrompt`, `postToolUse`, `postToolUseFailure`, `afterAgentResponse`, `subagentStop`, `stop`
   - `full` hook 集会额外采集权限请求、pre tool、compaction、shell/MCP、文件读写、tab 文件读写、thought 等事件。

2. 本地快照
   - 每个事件会关联环境快照和会话快照。
   - 快照按内容稳定 hash 去重，不会每个事件都重复写一份相同快照。

3. Codex 历史 transcript 回填
   - `codex_backfill.py` 会读取 `~/.codex/sessions/**/rollout-*.jsonl`。
   - 从历史 transcript 重建 `SessionStart`、`UserPromptSubmit`、`AfterAgentResponse`、`Stop`、`PostToolUse` 等事件。
   - 回填记录会带 `backfill.source = codex_transcript` 和 `backfill.transcript_path`。

### 1.2 本地与服务端落盘

客户端本地：

- `~/.ai-worklog/events/YYYY-MM-DD.jsonl`: 本地事件记录。
- `~/.ai-worklog/snapshots/YYYY-MM-DD.jsonl`: 去重后的环境/会话快照。
- `~/.ai-worklog/failed/YYYY-MM-DD.jsonl`: 上传失败后等待 replay 的记录。
- `~/.ai-worklog/upload_state.sqlite3`: replay 上传账本。
- `~/.ai-worklog/codex_backfill_state.sqlite3`: Codex 历史回填账本。
- `~/.ai-worklog/config.json`: 采集配置。

服务端：

- `data/raw/YYYY-MM-DD.jsonl`: 服务端接受的原始记录，会额外写入 `_server_ingested_at`。
- `data/worklog.sqlite3`: 查询索引，包含 records、token 字段、identity mappings 等。

### 1.3 记录类型

服务端统一接收 record，目前主要两类：

- `record_type = event`
  - 一次 hook 或 transcript 重建出来的一次操作/消息/工具事件。
- `record_type = snapshot`
  - 重复信息的去重快照。
  - `snapshot_type = environment`: 环境快照。
  - `snapshot_type = session`: 会话快照。

服务端主键口径：

- 有 `event_id` 时，主键是 `event:<event_id>`。
- 有 `snapshot_id` 时，主键是 `snapshot:<snapshot_id>`。
- 两者都没有时，用整条记录的稳定 hash。
- 重复主键会被判为 duplicate，不重复进入统计。

### 1.4 event 里收集的字段

每条 event 主要包含以下信息：

- 基础元数据
  - `event_schema_version`
  - `collector_version`
  - `source_id`
  - `surface`: 例如 `codex`、`cursor`
  - `collection_level`: `full`、`diagnostic`、`basic`
  - `hook_event_name`
  - `session_id`
  - `turn_id`
  - `agent_id`
  - `agent_type`
  - `model`
  - `environment_ref`
  - `session_ref`

- 时间线
  - `timeline.trace_id`
  - `timeline.span_id`
  - `timeline.parent_span_id`
  - `timeline.sequence_no`
  - `timeline.started_at`
  - `timeline.ended_at`
  - `timeline.duration_ms`

- 归一化操作信息
  - `operation.category`: session / prompt / response / thought / tool / shell / mcp / file_edit / file_read / subagent / compaction / approval / unknown
  - `operation.phase`: start / stop / submit / complete / before / after / failure / request / event 等
  - `operation.name`: 原始 hook 名
  - `operation.success`: 能判断时才有；失败 hook、错误字段、非 0 exit code 会判失败
  - `operation.error_type`

- 工具信息
  - `tool.name`
  - `tool.type`
  - `tool.cwd`
  - `tool.command`
  - `tool.exit_code`
  - `tool.success`
  - `tool.duration_ms`
  - `tool.files_read`
  - `tool.files_written`

- skill 信息
  - `skill.name`
  - `skill.path`
  - `skill.version`
  - `skill.phase`
  - `skill.success`

- 内容字段
  - `content.prompt`
  - `content.response`
  - `content.thought`
  - `content.tool_input`
  - `content.tool_response`

- token 使用
  - `usage`: 通常来自 Codex transcript 里的最新 `token_count` 事件。
  - `hook_usage`: 如果 hook payload 自己带了 usage/token_usage，也会记录。

- 原始 hook payload
  - `raw_hook_input`: `full` 级别才记录。
  - 常见 envelope 字段会从 raw 里剥离，避免重复；敏感 key 会脱敏。

- 工作区 diff
  - 只在 session stop 类 hook 上尝试采集。
  - 字段是 `workspace_diff`，来源是 `git diff --numstat HEAD --` 加上 untracked 文件行数估算。
  - 包含 staged、unstaged、untracked。
  - 只记录 numstat 风格的文件增删行数，不记录完整 diff 内容。

### 1.5 environment snapshot 里收集的字段

环境快照包含：

- OS/运行时信息
  - `os`
  - `system`
  - `release`
  - `machine`
  - `python`
  - `hostname`
  - `user`
  - `user_domain`
  - `shell`
  - `term_program`
  - `cwd`

- git 信息
  - `git.root`
  - `git.branch`
  - `git.commit`
  - `git.dirty`
  - `git.user_email`
  - `git.user_name`

- identity 候选信息
  - `identity.user_email`: 显式环境变量 `AI_WORKLOG_USER_EMAIL` 或兼容旧变量。
  - `identity.git_user_email`
  - `identity.git_user_name`
  - `identity.global_git_user_email`
  - `identity.global_git_user_name`
  - `identity.windows_upn`
  - `identity.os_user`
  - `identity.user_domain`
  - `identity.hostname`

### 1.6 session snapshot 里收集的字段

会话快照包含：

- `surface`
- `session_id`
- `agent_id`
- `agent_type`
- `model`
- `permission_mode`
- `user_email`
- `cwd`
- `transcript_path`

其中 `user_email` 会优先用 hook payload 显式字段；没有时，从 identity 候选里选第一个可用邮箱。

### 1.7 采集级别

安装脚本支持四个级别：

- `full`
  - 记录 prompt、response、tool input/output、raw hook input。
  - raw 和内容字段会按 key 名脱敏：包含 `api_key`、`authorization`、`cookie`、`password`、`secret`、`token` 等片段的字段会变成 `[REDACTED]`。
  - 默认内部模式是这个级别，所以 collector 权限应当按敏感数据处理。

- `diagnostic`
  - 仍记录 envelope、操作、工具、token 等诊断信息。
  - 内容 body 不直接保存，字符串/list/dict 会被转成类型、长度、hash 或 key 列表等摘要。

- `basic`
  - 只保留 session/turn/surface/event 这类基础元数据。
  - 不采集 content、token usage、raw hook input。

- `off`
  - hook 保留，但不写记录。

### 1.8 transcript 额外提取的数据

实时 hook 运行时，如果有 `transcript_path`：

- 从 transcript 尾部读取最近内容，默认最多 5MB。
- 提取最新 `event_msg.payload.type == token_count` 作为 usage。
- 从 `session_meta` 或 `turn_context` 里补 model。

历史回填时：

- `session_meta` 会生成 `SessionStart`。
- `event_msg.user_message` 会生成 `UserPromptSubmit`。
- `event_msg.agent_message` 会生成 `AfterAgentResponse`。
- `event_msg.task_complete` 会生成 `Stop`。
- `response_item.function_call/custom_tool_call` 和对应 output 会生成 `PostToolUse`。

session detail 和 code metrics 还会从 transcript 里额外恢复 `apply_patch` 调用：

- 读取 `response_item.custom_tool_call` 中 `name == apply_patch` 的输入。
- 读取 `event_msg.patch_apply_end` 判断成功与文件变化。
- 如果同一个 tool use 已经由 hook 记录过，就不重复生成 transcript-only 事件。

### 1.9 明确没有完整采集的内容

这些点容易误解：

- 工作区 diff 不上传完整 diff body，只上传每个文件的增删行数、是否二进制、是否 untracked、是否代码文件。
- 二进制文件不做内容行数统计。
- token usage 是 best-effort：Codex hook payload 本身不稳定直接给 token，主要从 transcript 的 `token_count` 提取。
- assistant 回复里的代码块不直接计入 generated code，除非它出现在实际写文件/patch payload 里。
- encrypted reasoning 不解析；raw reasoning 默认不采集。

## 2. 统计了哪些数据，口径是什么

### 2.1 全局 records 统计

接口：`GET /stats`

统计项：

- `total_records`
  - SQLite `records` 表中去重后的总记录数。

- `by_record_type`
  - 按 `record_type` 分组计数，例如 event、snapshot、unknown。

- `by_surface`
  - 按 `surface` 分组计数，例如 codex、cursor、unknown。

- `by_hook_event_name`
  - 按 `hook_event_name` 分组计数。
  - 只统计 `hook_event_name is not null` 的记录。
  - 最多返回前 50 个 hook。

- `token_totals`
  - 全量 token 聚合，具体口径见 2.4。

- `token_totals_by_model`
  - 按模型聚合 token，具体口径见 2.4。

### 2.2 sessions 统计

接口：

- `GET /sessions?limit=50&surface=...`
- `GET /sessions/<session_id>?limit=200&surface=...`

session 分组口径：

- 只用 `record_type = event` 的记录建立 session。
- `session_id` 为空时归为 `"unknown"`。
- session 列表按 session 内最后事件时间倒序。
- `limit` 最小 1、最大 500。

每个 session summary 包含：

- `session_id`
- `surfaces`
- `first_seen`
- `last_seen`
- `event_count`
- `hook_event_counts`
- `collection_levels`
- `token_totals`
- `token_totals_by_model`
- `process`
- `code_metrics`
- `environment_refs`
- `session_refs`

session detail 额外包含：

- `events`: 最近 N 条原始 event，默认最多 200，最大 1000。
- `assistant_messages`: 从 transcript 恢复的 agent message。
- `transcript_tool_events`: 从 transcript 恢复的 apply_patch 事件。
- `timeline`: 事件、transcript tool event、assistant message 合并后的时间线摘要。
- `snapshots`: 关联到的 environment/session/other 快照。
- `trellis_signals`: 该 session 的 Trellis 推断信号。

### 2.3 process 统计

`process_summary` 用于 sessions 和 session detail。

统计项：

- `operation_category_counts`
  - 按 `operation.category` 计数。
  - 如果没有 category，会从 hook 名推断：包含 tool/prompt/response/subagent 或 stop/sessionStart 等。
  - 仍无法判断则归为 `unknown`。

- `operation_phase_counts`
  - 按 `operation.phase` 计数。
  - 没有 phase 时归为 `event`。

- `tool_counts`
  - 按 `tool.name` 计数。
  - 只统计 event 里存在 tool name 的记录。

- `skill_counts`
  - 按 `skill.name` 计数。
  - 只统计 event 里存在 skill name 的记录。

- `failure_count`
  - `operation.success is False` 或 `tool.success is False` 的事件数。
  - 没有 success 字段的不算失败。

- `duration_ms_by_category`
  - 对有 `timeline.duration_ms` 的事件，按 operation category 聚合。
  - 每类返回：
    - `total`: 总耗时毫秒。
    - `events`: 有 duration 的事件数。
    - `avg`: `total / events`。

### 2.4 token 统计

token 字段：

- `input_tokens`
- `cached_input_tokens`
- `output_tokens`
- `reasoning_output_tokens`
- `total_tokens`

数据来源优先级：

1. `usage.info.last_token_usage`
2. `hook_usage.last_token_usage`
3. `hook_usage` 本身

只有这些位置存在 token 字段时，这条记录才进入 token 统计。

去重口径：

- 因为同一 turn 的最新 token_count 可能被多个 hook event 附带，统计会按 `token_usage_identity` 去重。
- identity 规则：
  - 如果有 usage timestamp，用 `session_id | usage.timestamp | hash(tokens)`。
  - 否则使用 usage turn id、record turn id。
  - 再没有时，退化为 record 主键。
- 同一个 identity 只加一次。

模型归因口径：

模型按以下顺序取：

1. record 顶层 `model` / `model_name` / `modelName`
2. `usage` 或 `hook_usage` 里的 model 字段
3. 同 session 的 session snapshot 里的 model
4. `unknown`

全局 token 统计：

- `GET /stats` 返回全量 token 总计和按模型总计。
- 时间范围不筛选，基于全部已入库记录。

月度/用户 token 统计：

- 接口：`GET /metrics/tokens?month=YYYY-MM`
- `month` 不传时默认当前 UTC 月。
- 月份范围按 UTC 月初到下月月初，筛选字段优先用客户端 `received_at`，没有时退回 `ingested_at`。
- 返回：
  - `month`
  - `range.start/end`
  - `token_usage_identities`
  - `token_totals`
  - `users`
  - `unclaimed`
  - `identity_mappings`

用户归因口径：

1. 先从 event/session/environment 里构造 identity candidates：
   - `user_email`
   - `git_email`
   - `windows_upn`
   - `hostname`
   - `os_user`
   - `user_domain`
   - `os_user_host`
2. 先查 `identity_mappings` 手工映射。
3. 否则如果 candidates 里有直接邮箱类 identity，例如 user_email/git_email/windows_upn 且包含 `@`，直接归到该邮箱。
4. 仍无法归属时进入 `unclaimed`：
   - 优先用 `hostname:<hostname>` 作为 identity key。
   - 没 hostname 时用第一个 candidate。
   - 都没有时用 `session:<session_id or unknown>`。

每个用户/未归属分组还会返回：

- `token_totals`
- `token_totals_by_model`
- `sessions`
- `session_count`
- `hostnames`
- `os_users`
- `git_emails`
- `candidate_identities`

### 2.5 code metrics 统计

接口：`GET /metrics/code?surface=...&session_id=...`

也会出现在 `GET /sessions` 和 `GET /sessions/<session_id>` 的 `code_metrics` 里。

代码文件判定：

- 文件名白名单：
  - `Dockerfile`, `Makefile`, `Rakefile`, `Gemfile`, `go.mod`, `go.sum`, `package.json`, `pyproject.toml`, `requirements.txt`, `tsconfig.json`
- 扩展名白名单：
  - `.c`, `.cc`, `.clj`, `.cljs`, `.cpp`, `.cs`, `.css`, `.dart`, `.ex`, `.exs`, `.go`, `.h`, `.hpp`, `.html`, `.java`, `.js`, `.jsx`, `.kt`, `.kts`, `.lua`, `.m`, `.mm`, `.php`, `.pl`, `.py`, `.rb`, `.rs`, `.scala`, `.sh`, `.sql`, `.swift`, `.ts`, `.tsx`, `.vue`

#### generated_code

定义：从成功的写入/patch 类事件里解析出的代码增删行。

计入口径：

- 只看 `record_type = event`。
- hook 必须是 post-write 类：
  - `PostToolUse`
  - `AfterFileEdit`
  - `AfterTabFileEdit`
- 如果 `operation.success is False`，不计入。
- 从 `content.tool_input`、`content.tool_response`、`raw_hook_input` 里解析：
  - path + content 结构：按 content 行数算新增行。
  - patch/diff 文本：解析 `*** Begin Patch`、`diff --git`、`@@` hunk 中的 `+` / `-`。
- 只统计代码文件。
- 同一事件里的重复 patch 文本或重复 path+content 会按 hash 去重。

返回字段：

- `additions`
- `deletions`
- `files`
- `events`

限制：

- assistant 回复里的代码块不算，除非进入实际写文件/patch payload。
- 这是“弱口径”，因为它依赖 hook/tool payload 是否完整暴露。

#### adopted_code

定义：被认为已经被用户/工作区采纳的代码。

优先口径：

1. 最新 workspace diff
   - session stop 时如果有 `workspace_diff`，取该 session 最新一次 workspace diff 里的代码文件。
   - 这表示生成代码仍然体现在当前工作区 diff 中。
   - `adoption_source = workspace_diff`

2. 成功 git commit
   - 如果同一 session 里观察到成功的 `git commit` 命令：
     - 优先解析 commit 输出里的 `N files changed, X insertions(+), Y deletions(-)`。
     - 有 commit summary 时，采用 summary 的 files/additions/deletions。
     - `adoption_source = git_commit_summary`
   - 如果没有 commit summary，则退化为 commit 发生前本 session 内已生成的代码文件。
     - `adoption_source = git_commit_generated_code`

返回字段：

- 全局：`additions`, `deletions`, `files`, `sessions`
- session：还包含 `adoption_source`、`git_commit_events`、`latest_git_commit_code`、`latest_git_commit_event_id`、`latest_git_commit_received_at` 等。

限制：

- 这是“中等强度近似口径”，不是严格证明代码被人工接受。
- workspace diff 只能说明代码还在工作区差异里。
- git commit 只能说明当前 session 观察到了 commit；没有 commit summary 时会退回到 generated code 估算。

#### uncommitted_code

定义：被认为还未 commit 的代码。

口径：

- 如果 session 有最新 workspace diff：
  - 取最新 workspace diff 中的代码文件作为 uncommitted。
- 如果没有 workspace diff：
  - 取最新成功 git commit 之后的 pending generated files。
  - 如果完全没有 commit，则取当前 pending generated files。

返回字段：

- 全局：`additions`, `deletions`, `files`, `sessions`
- session：`additions`, `deletions`, `files`, `events`

限制：

- 依赖 stop hook 是否成功采集 workspace diff。
- 没有 workspace diff 时只能用“commit 之后的生成事件”估算。

#### latest_git_commit_code

定义：session 内最新一次成功 git commit 对应的代码量。

口径：

- 优先使用 git commit 输出里的 summary。
- 没有 summary 时，用该 session 当前 generated files 估算。
- 只在 session 级指标里返回。

### 2.6 Trellis metrics 统计

接口：`GET /metrics/trellis?surface=...&session_id=...`

这是完全基于事件文本和命令的推断统计。

Trellis 事件识别：

- 事件文本里命中以下信号之一：
  - `.trellis`
  - `trellis-`
  - `task.py`
  - `get_context.py`
  - `finish-work.md`
  - `Requirement exploration`

任务引用：

- 从文本里匹配 `.trellis/tasks/<task_id>`。
- 返回 `task_id` 和 `task_path`。

阶段推断：

- `requirements`: `prd.md`、`trellis-brainstorm`、`Requirement exploration`、或命令里 `--step 1.`
- `design`: `design.md` 或 `trellis-design`
- `implementation`: `implement.md`、`implement.jsonl`、`trellis-implement`
- `check`: `check.jsonl`、`trellis-check`、或命令里 ` check`
- `finish`: `finish-work.md` 或 `task.py finish`
- `workflow_context`: `get_context.py --mode phase`
- `context`: `get_context.py` 或 `.trellis/spec`
- `unknown`: 其他 Trellis 信号

问题信号：

- 结构化失败：
  - `operation.success is False` 或 `tool.success is False`
- 文本匹配：
  - 英文：`error`, `failed`, `failure`, `exception`, `not found`, `timeout`, `traceback`, `permission denied`, `missing`, `cannot`, `can't`, `unable`
  - 中文：`测试失败`, `缺少`, `无法`, `失败`

返回字段：

- `total_sessions`
- `trellis_sessions`
- `non_trellis_sessions`
- `trellis_event_count`
- `phase_counts`
- `task_counts`
- `problem_signal_count`
- `sessions`

session 级 Trellis summary 还包括：

- `uses_trellis`
- `event_count`
- `first_seen`
- `last_seen`
- `phase_counts`
- `tool_counts`
- `task_ids`
- `task_paths`
- `artifacts`
- `problem_signal_count`
- `problem_signals`
- `repeated_commands`: 同一命令出现 3 次及以上才列出
- `events`: 最多 200 条 Trellis signal

### 2.7 看板首页显示的指标

`/ui` 首页顶部目前显示：

- `records`
  - 来自 `/stats.total_records`。
- `sessions`
  - 来自 `/sessions.total_sessions`。
- `tool events`
  - 来自 `/sessions.process.operation_category_counts.tool`。
- `generated lines`
  - 来自 `/sessions.code_metrics.generated_code.additions`。
- `uncommitted lines`
  - 来自 `/sessions.code_metrics.uncommitted_code.additions`。
- `tokens`
  - 来自 `/stats.token_totals.total_tokens`。

这些是展示层指标，不是新的统计口径。

## 3. 读指标时要注意的边界

- `received_at` 是客户端/构造记录时间；`ingested_at` 是服务端入库时间。月度 token 报表按 `ingested_at`。
- token 是去重后的 token usage identity，不是 event 条数简单相加。
- `generated_code` 偏“生成动作”，`adopted_code` 偏“工作区/commit 结果”，二者不应该直接相减解释成人工丢弃量。
- `adopted_code.files` 在全局统计里是按 session 汇总后的文件数相加，不是全局唯一文件路径数。
- workspace diff 统计的是相对 `HEAD` 的当前差异，所以它会受用户手工改动、格式化、回滚影响。
- 当前系统采集能力取决于 host 产品实际暴露的 hook payload；不同 Codex/Cursor 版本可能字段不完全一致。
