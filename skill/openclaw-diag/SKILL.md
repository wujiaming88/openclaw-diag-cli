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
- Trajectory-sourced fields may contain plaintext message/tool content. Use `trace --mask` when output may be shared.
- `extract` is masked by default. Use `--unmask` only for trusted local analysis.
- Some collectors perform read-only DNS/TCP/HTTP probes. Do not run fix/restart/write commands unless the user separately asks.

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
| Session stuck | `openclaw-diag trace <uuid> --format json --mask` |
| Inspect session records | `openclaw-diag extract <uuid> --summary --format json` |
| Full session diagnosis (everything correlated to one UUID) | `openclaw-diag panorama <uuid> --format json` |

## Trace

Use trace when the user provides a session UUID or asks why one message got stuck/slow.

```bash
openclaw-diag trace <uuid> --format json --mask
openclaw-diag trace <uuid> --msg-index 0 --format json --mask
openclaw-diag trace <uuid> --msg-match "text" --format json --mask
```

If `SESSION_NOT_FOUND`, ask for a longer UUID prefix or use the recent-session hint.

## Extract

Use extract when the user asks to inspect session contents or count records.

```bash
openclaw-diag extract <uuid> --summary --format json
openclaw-diag extract <uuid> --format json
openclaw-diag extract <uuid> --all --format json
```

Avoid `--unmask` unless the user explicitly needs raw local content.

## Panorama

Use panorama when given a session UUID and asked an open-ended health
question ("是否健康?", "为什么这条 session 卡住?", "把所有相关信息拉出来"). It
walks every standard data source — `session.jsonl`, `trajectory.jsonl`,
`sessions.json`, OpenClaw app log, `runs.sqlite`, and any matching
`cron/runs/<jobId>.jsonl` — and emits a single Report with sections for
session overview, correlation graph, timeline, runtime context, tool
execution, correlated logs, model decisions, child tasks, delivery, and
health signals.

```bash
openclaw-diag panorama <uuid> --format json
openclaw-diag panorama <uuid> --all-runs --format json
openclaw-diag panorama <uuid> --strict-correlation --format json --mask
```

Inclusion of every record is determined by the correlation graph expanded
from `sessionId` (sessionKey, runIds, toolCallIds, childSessionIds, cronJobId).
Each correlated log entry is annotated with `correlation.path` so the
"why was this included" answer is auditable.

- Default: latest run only. Use `--run-index N` (negative ok) or
  `--all-runs` for persistent multi-run sessions.
- `--strict-correlation` drops sessionKey-only and toolCallId-only
  matches; useful on noisy multi-tenant logs.
- `--mask` sanitizes tool arguments and message-style text. Default is
  unmasked since panorama is intended for local diagnosis.

Verdict mapping:
- `fail`: trajectory `aborted/timedOut`, child task failed, or any
  ERROR-level correlated log.
- `warn`: WARN-level correlated log, model fallback / context overflow
  decision, stall log, plugin activation error, or E2E > 5min.
- `ok`: everything clean.

## JSON Notes

- Single-module commands emit one JSON envelope.
- `all --format json` emits one JSON envelope per module, one per line.
- `--format ndjson` emits section-level objects, useful for streaming but less convenient for summaries.
- Exit code `0`: all ok.
- Exit code `1`: command succeeded and found warn/fail diagnostics.
- Exit code `2`: bad input or missing/ambiguous session.
- Exit code `3`: runtime failure.
