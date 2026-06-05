---
name: openclaw-diag
description: "OpenClaw diagnostics CLI. Use for OpenClaw health checks, slow responses, stuck sessions, gateway/connectivity errors, cron not firing, plugin issues, recent errors, task/subagent failures, task timeouts, stuck task runs, session trace/extract, full-session 360° diagnosis (panorama). 中文触发：系统慢、卡住、报错、健康检查、性能差、定时任务没触发、插件异常、网关连不上、session 卡死、查看 session、任务失败、子 Agent 异常、任务超时、任务卡住、session 全景、session 全方位诊断。"
metadata:
  requires:
    bins: ["openclaw-diag"]
---

# openclaw-diag

Use `openclaw-diag` to diagnose OpenClaw systems. Diagnostic commands are observer-only and must not modify OpenClaw runtime state.

## Safety Rules

- Prefer `--format json` for Agent workflows.
- Treat exit code `1` as "diagnostic found warn/fail", not command failure.
- Treat top-level `{ok:false}` as command/input/runtime failure.
- Do not paste large raw JSON back to the user; summarize module verdicts, evidence, and next checks.
- Some collectors perform read-only DNS/TCP/HTTP probes. Do not run fix/restart/write commands unless the user separately asks.

Masking policy (global):
- `trace` and `panorama` are UNMASKED by default; pass `--mask` unless output stays local AND the user explicitly needs raw content.
- `extract` is MASKED by default; use `--unmask` only for trusted local analysis.
- Config/log collectors redact secrets by default.

## Preflight

Before diagnosing, confirm the target execution context.

1. Run `openclaw-diag --version`.
2. If the command is missing, try `npx openclaw-diag-cli --version` or ask the user where to run it.
3. If running outside the OpenClaw host/container, do not infer from local files; ask for SSH/container access or explicit `--openclaw-home`, `--log-dir`, `--sessions-base`, `--config`.
4. Run `openclaw-diag doctor --format json` when environment access is uncertain.

## Decision Ladder

- No session UUID + broad symptom → `all --format json`.
- Session UUID + open-ended question / health / slow / stuck / "why" → `panorama <uuid> --format json --mask`.
- Session UUID + one specific user message/turn → `trace <uuid> --format json --mask`.
- Need raw transcript / count / filter records → `extract <uuid> --summary --format json` first.
- A collector verdict is warn/fail → run that collector alone only if more detail is needed.

## Routing

| Symptom | Start with |
|---|---|
| General health / unknown issue | `openclaw-diag all --format json` |
| Slow response / high latency | `openclaw-diag performance --format json` |
| Gateway down / cannot connect | `openclaw-diag gateway --format json` |
| Recent failures / errors | `openclaw-diag recent_errors --format json` |
| Cron not firing / no delivery | `openclaw-diag cron_jobs --format json` |
| Task/subagent 失败 | `openclaw-diag task_health --format json` |
| Plugin issue | `openclaw-diag plugin_diag --format json` |
| Session health / why / 全貌 | `openclaw-diag panorama <uuid> --format json --mask` |
| Specific message stuck/slow → trace | `openclaw-diag trace <uuid> --format json --mask` |
| Inspect session records | `openclaw-diag extract <uuid> --summary --format json` |

## Workflow

1. For broad or unclear symptoms, run:

```bash
openclaw-diag all --format json
```

2. Parse each JSON line independently. For each envelope:
   - `ok=false`: report the `error.code`, `message`, and hint.
   - `ok=true`: inspect `data.module`, `data.verdict`, `data.summary`, and non-ok checks in `data.sections`.

3. Drill down only where needed:
   - `gateway != ok` → run `openclaw-diag gateway --format json`
   - `performance != ok` → run `openclaw-diag performance --format json`
   - `recent_errors != ok` → run `openclaw-diag recent_errors --format json`
   - `cron_jobs != ok` → run `openclaw-diag cron_jobs --format json`
   - `plugin_diag != ok` → run `openclaw-diag plugin_diag --format json`
   - `run_health != ok` → run `openclaw-diag run_health --format json`, then use `trace` if a session UUID is involved
   - `task_health != ok` → run `openclaw-diag task_health --format json`, check failure samples, top_error_patterns, timeout_analysis, stuck_analysis, and runtime breakdown
   - `sessions_diag != ok` → run `openclaw-diag sessions_diag --format json`, then use `extract <uuid> --summary --format json` when a session UUID is known

4. Final answer should include:
   - Overall verdict
   - Top 3-5 concrete findings
   - Evidence from check messages, not full dumps
   - Recommended next commands or manual checks
   - Any limits: missing logs, missing trajectory, ambiguous session, unavailable tools
   - Do not present a root cause unless directly supported by checks/logs.
   - Structure the answer as: Evidence observed → Interpretation → Confidence / limits → Safe next checks.
   - Do not recommend restart/config changes unless the user asks for remediation.

## Trace

Use trace when the user provides a session UUID or asks why one specific message got stuck/slow.

```bash
openclaw-diag trace <uuid> --format json --mask
openclaw-diag trace <uuid> --msg-index 0 --format json --mask
openclaw-diag trace <uuid> --msg-match "text" --format json --mask
```

If `SESSION_NOT_FOUND`, ask for a longer UUID prefix or use the recent-session hint. Apply the global masking policy.

## Extract

Use extract when the user asks to inspect session contents or count records.

```bash
openclaw-diag extract <uuid> --summary --format json
openclaw-diag extract <uuid> --format json
openclaw-diag extract <uuid> --all --format json
```

Apply the global masking policy (extract is masked by default).

## Panorama

Use panorama when given a session UUID and asked an open-ended health
question ("是否健康?", "为什么这条 session 卡住?", "把所有相关信息拉出来",
"执行慢不慢", "工具有没有异常", "模型性能好不好"). It walks every standard
data source and produces a complete execution picture.

**Panorama sections currently include:**
- **Findings** — objective triage summary, rendered first. Re-surfaces the worst already-computed problem signals using deterministic severity ordering. No inferred causes, no recommendations.
- **Session Overview** — IDs, trigger, model, time window, activity stats (model calls / tool calls / errors / tokens / cost), sources, verdict, plus runtime context (prompt chars, tools/skills/plugins, workspace files, bootstrap truncation, stream strategy).
- **Timeline** — first/last events, first error/stall timestamp, plus a sampled middle slice for long runs.
- **Model Calls** — per-model breakdown and every model call with output tokens, stopReason, triggered tools, cache stats. Model-selection/fallback events surface in Correlated Logs & Signals as `log_decision` entries.
- **Tool Execution** — per-call args + result/error, with timing stats (avg/p50/p95/max).
- **Correlated Logs & Signals** — Findings-aligned signals (sorted by severity), positive signals, ERROR/WARN raw entries, representative INFO when the window is quiet.
- **Child Tasks** — failed tasks with error message, succeeded count.

```bash
openclaw-diag panorama <uuid> --format json --mask
openclaw-diag panorama <uuid> --all-runs --format json --mask
openclaw-diag panorama <uuid> --strict-correlation --format json --mask
```

**Key design:** zero subjective filtering. Inclusion is determined by the
correlation graph expanded from `sessionId` (sessionKey, runIds,
toolCallIds, childSessionIds, cronJobId). Each correlated log entry is
annotated with `correlation.path`.

- Default: latest run only. Use `--run-index N` or `--all-runs` for persistent multi-run sessions.
- `--strict-correlation` only matches sessionId/runId (drops sessionKey/toolCallId matches).

Interpret empty correlated logs carefully (the section surfaces one of these check names):
- `logs.not_retained`: the session-window log file is missing/rotated from log_dir. Do NOT claim the session produced no log evidence — the evidence existed but was rotated away (retention issue, not a session bug).
- `logs.uncorrelated`: the session-date log file exists but no line carries this sessionId/runId. Mention a possible logging/correlation gap (e.g. older harness not stamping ids) worth investigating.
- `logs.missing`: no app log files were found in log_dir at all.
- `logs.none`: the session window is unknown/unclassifiable, so correlation can't be scoped — report it as a limitation.

Verdict mapping:
- `fail`: trajectory `aborted/timedOut`, child task failed, or any ERROR-level correlated log.
- `warn`: WARN-level correlated log, model fallback / context overflow decision, stall log, plugin activation error, or E2E > 5min.
- `ok`: everything clean.

## JSON Notes

- Single-module commands emit one JSON envelope.
- `all --format json` emits one JSON envelope per module, one per line.
- `--format ndjson` emits section-level objects, useful for streaming but less convenient for summaries.
- Exit code `0`: all ok.
- Exit code `1`: command succeeded and found warn/fail diagnostics.
- Exit code `2`: bad input or missing/ambiguous session.
- Exit code `3`: runtime failure.

## Common Pitfalls

- Exit code `1` means diagnostics found warn/fail signals — it is NOT a CLI failure.
- `ok=true` with `verdict=fail` means the command succeeded AND found a real diagnostic failure. Report the failure, not a tool error.
- Missing trajectory/logs limits confidence — always report the missing source.
- `logs.not_retained` is a log-retention artifact, NOT proof that no error happened.
- `panorama` defaults to the latest run only; earlier failed attempts may be hidden — use `--all-runs` for persistent multi-run sessions.
