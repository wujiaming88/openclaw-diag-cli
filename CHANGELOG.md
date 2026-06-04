# Changelog

## v1.4.3 — panorama section consolidation + honest truncation (2026-06-04)

`panorama` is restructured from 10 sections to a tighter 7-section layout.
Three sections were folded into adjacent ones; redundant data was dropped;
log filtering and timeline truncation became honest about what they
discard. JSON envelope `report.data["runtime_context"]` is preserved for
backward-compat; `report.data["model_decisions"]` and
`report.data["delivery"]` are removed (their content lives elsewhere now).

### Changed

- **Window-bound correlated logs.** `filter_log_files` matches against the
  whole log file regardless of time. Panorama now bounds the result to
  `[window_start − 5s, window_end + 5s]` so reused `sessionKey` /
  `toolCallId` no longer drags in noise from a different conversation
  hours later. Entries with no parseable timestamp are kept (we can't
  prove they belong elsewhere) but counted separately. The same
  window-bounded list feeds the timeline.
  - `logs.summary.data` adds `out_of_window_dropped` and `ts_less_kept`.
- **Surface ALL in-window ERROR log entries**, not just the first 10.
  Safety cap of 200 with an explicit `+N more` line; WARN entries keep
  the head-10 cap with the same `+N more` treatment.
- **Timeline truncation honesty.** When the cap drops the middle of the
  timeline, `dropped_middle` and `truncated:true` ride on the JSON
  envelope and a WARN line surfaces in the section. Records with no
  parseable timestamp are now counted (`skipped_no_ts`) instead of being
  silently dropped.
- **Runtime Context merged into Session Overview.** The standalone
  "Panorama · Runtime Context" pretty section is removed; useful fields
  (harness version + node, plugins activated + plugin load errors,
  skill count + names, system prompt budget, bootstrap truncation,
  compiled tool/message counts, stream strategy) are folded into Session
  Overview. `report.data["runtime_context"]` remains unchanged.
- **Model Calls per-call enrichment.**
  - Per-call line now shows input tokens (`in=…`) alongside `out=…`, plus
    per-call throughput `tok/s` (or `n/a` when duration is zero).
  - One-line note clarifies durations are round-trip wall-clock (last
    input msg → assistant msg), NOT pure model API latency — the
    trajectory has no native `durationMs`/TTFT.
  - **Model-call errors surfaced.** When a run shows
    `promptErrorSource`, `aborted`, `externalAbort`, `timedOut`,
    `idleTimedOut`, `timedOutDuringCompaction`, or
    `timedOutDuringToolExecution`, a FAIL line names the flags. When
    that happens with no `model_calls` recorded, an extra WARN states
    "model call failed with no usage record (see Health Signals)" — a
    best-effort inference noted in code as such.
- **Model Decisions removed; signals folded into Health Signals.** The
  redundant `model_select` entries (model identity is already in Session
  Overview) are gone. Log-marker decisions
  (`model_fallback_decision` / `harness_select` / `context_overflow` /
  `compaction_triggered`) are emitted as `health_signals` entries with
  `kind="log_decision"`. `report.data["model_decisions"]` is removed.
- **Delivery removed; events folded into Timeline.** Cron run records and
  messaging-tool sends now appear in the timeline with `source="delivery"`
  and `event_type="delivery"`. Failed cron deliveries
  (`deliveryStatus` ∈ failed/error/errored/undelivered) emit a
  `kind="cron_delivery_failed"` health signal so the verdict still
  degrades correctly. `report.data["delivery"]` is no longer set.

### Tests
- 11 new tests in `tests/test_panorama.py` covering window-bound logs,
  out-of-window drop counters, timeline `dropped_middle` / `skipped_no_ts`,
  Session-Overview-with-runtime-fields, removed-section/data assertions,
  per-call `in=`/`tok/s` rendering, delivery-in-timeline, cron-delivery
  failure routed to health signals, and log-marker → health signal
  routing. Total: 48 passing (was 37).

## v1.4.2 — panorama duration unit fix (2026-06-04)

### Fixed
- `panorama` Model Calls / health `long_tool_call` durations were stored in
  milliseconds but passed straight to `fmt_duration()` (which expects seconds),
  so a sub-second model call (e.g. 547ms gap) rendered as `9.1m` and a 2s call
  as `33.3m`. Added the missing `/1000` at all three render sites
  (per-model `avg_dur`, per-call `dur`, and `long_tool_call` health signal).
- Underlying `model_calls[].duration_ms` data was already correct; only the
  pretty/text rendering was wrong.

### Added
- Regression test `test_model_call_duration_renders_in_seconds` — verifies a
  1s/2s call renders as `1s`/`2s`, never `16.7m`/`33.3m`. Proven to fail on the
  pre-fix code.

## v1.1.0 — format modes, structured errors, skill auto-install (2026-06-02)

### Added
- `--format pretty|json|ndjson` flag (TTY-friendly default, `ndjson` for streaming pipelines).
- JSON envelope: `{ok, data:{module, verdict, summary, sections, ...}, error}`.
- Structured `DiagError` payload: `{code, message, retryable, hint, details}`.
- Exit codes: `0` ok, `1` warn/fail, `2` input error, `3` runtime error.
- `examples` subcommand and per-subcommand help epilogs.
- `skill/SKILL.md` (skill-creator standard).
- `skill/install.py` auto-deploys to OpenClaw / Claude Code / Codex / Cursor.
- `openclaw-diag skill-install` subcommand and npm `postinstall` hook.

### Changed
- `--json` is now an alias for `--format json` (backward compatible).
- Inspectors (`trace`, `extract`) emit structured `DiagError` codes
  (`SESSION_NOT_FOUND`, `AMBIGUOUS_SESSION`, `INVALID_QUERY`, …).

## v0.6.1 — stuck-run tool name fallback (2026-05-26)

### Fixed
- `run_health` active leak 渲染：当 stuck run 的 `trace.artifacts.toolMetas` 为空时（典型于 ACP turn 超时切断的 未收尾 tool calls），现在 fallback 到 `model.completed.messagesSnapshot.toolCall.name`。原来显示 `tools=[?]` 有诊断价值损失；现在显示 `tools=[read]` 或实际卡的工具名后缀 ` [snapshot]` 用于区分来源。

### Added
- `Run.last_tool_call_names: List[str]` — 从 messagesSnapshot 提取的未配对 toolCall name。
- 新 fixture `tool_leak_no_meta_run` — 复现 ArkClaw 生产场景（stuck=1, toolMetas=[], messagesSnapshot 有未配对 toolCall）。
- 1 个新 collector 集成测试验证 fallback 路径；1 个新 fixture verdict assertion。

## v0.6.0 — Trajectory Integration (2026-05-26)

OpenClaw 2026.5.x 引入了 `<sessionId>.trajectory.jsonl` 运行时观察层（每次 run 写 7 个事件）。本版本把它吸收为 first-class 数据源，为 11 个诊断模块提供 run-级信号。

### 新增

- **`run_health` 模块** —— 全局运行健康度（24h / 7d / 30d 多窗口），含 abort 分类、active leak 检测、P95 wall 耗时（per-trigger）、最近 7d 最慢 10 个 run。
- **`ocdiag/trajectory.py` 共享 loader** —— 流式解析、ThreadPoolExecutor 并发、tolerant 截断/schema drift。`OCDIAG_TRAJECTORY_WORKERS` 控制并发度（默认 4）。

### 增强（已有 collector）

- `sessions` —— trajectory run 健康度（trigger 分布、incomplete 比例、active_count 泄漏检测、top leak runs）。
- `recent_errors` —— 7d trajectory abort/timeout 分布、promptErrorSource、最常失败工具、24h compaction 率。
- `cron_jobs` —— cron 投递审计 + 静默 cron 检测（5.7 bug pattern）+ 跨 trigger delivery audit（吸收旧 `delivery_audit` 设计）。
- `performance` —— cache health（broke 率、cacheRead/total 比）、compaction stats、per-trigger wall latency、system prompt budget（吸收旧 `prompt_budget` 设计）。
- `plugin_diag` —— trajectory plugin snapshot、与 config 的 drift 检测、imported_unused IDs。
- `environment` —— 14d harness/Node/invocation 漂移。
- `configuration` —— 最新 run 的 effective runtime config + `skills.snapshotVersion` 漂移。
- `gateway` —— 24h run 频率直方图。
- `trace` —— 新增 outcome / lifecycle / cache / delivery / plugin snapshot 显示；新 flag `--show-tool-metas`、`--show-plugin-snapshot`、`--mask`。

### 敏感性变更（**故意偏离 DESIGN.md 公理 #7**）

trajectory 来源的自由文本字段（`assistantTexts`、`messagingToolSentTexts`、`prompt`、`finalPromptText`、`toolMetas[].meta`）**默认明文展示**，便于诊断。`--mask` 显式打开脱敏。**其他所有自由文本来源**（shell history、plugin 错误样本、systemd 配置、session 消息体）**保持默认脱敏**，行为不变。

### 性能

参考数据集（78 trajectory 文件 / 329 runs / 100MB）的 wall 耗时：

| 操作 | 耗时 |
|---|---|
| `summarize_trajectory` 全数据 | ~970ms |
| `collect_runs` 全数据（完整 Run dataclass） | ~920ms |
| `openclaw-diag run_health` | ~1.0s |
| `openclaw-diag run_health --json` | ~1.0s |

峰值内存约 40MB（远低于 500MB 上限）。

### 测试

- `tests/run_trajectory_tests.py` —— 10 个合成 fixture 覆盖 happy path / aborted / idle timeout / tool leak / silent cron / cache break / incomplete / multi-run / schema drift / truncated last line。
- `tests/run_perf_smoke.py` —— 性能门禁，在参考数据集上跑过 5s/8s 阈值即视为回归。

### 已知约束

- `all --json` 端到端耗时 ~19s（远超 plan 中 8s 目标）。瓶颈在 gateway 模块的 7.7s curl probe 和 sys_health 的 DNS 查询，与 trajectory 无关。后续可考虑 dispatcher 内部并发（与本 plan 解耦）。

## v0.5.1
- fix: replace token estimate with actual first-call input tokens

## v0.5.0
- feat: progress indicator + token display fixes

## v0.4.1
- chore: README rewrite sync to npm

## v0.4.0
- feat: banner+verdict+footer rewrite for diagnostic output
