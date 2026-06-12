---
name: openclaw-diag
description: "Diagnostics CLI for OpenClaw systems ONLY. Use when troubleshooting an OpenClaw deployment: health, gateway, cron, sessions, panorama/trace/extract, plugins, tasks/subagents, recent errors, or IM channel issues (Feishu / Lark / DingTalk / WeCom bot not replying). 中文触发（仅限 OpenClaw）：OpenClaw 健康检查、网关、定时任务、session、panorama、channel、任务、插件。"
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

Pick the entry point by answering two questions, then look up the exact
command in the **Routing** table below (and the per-command sections for
flags).

- **No session UUID?** Broad / unknown symptom → start with `all`. A
  specific subsystem symptom (gateway / performance / cron / plugin /
  task / channel) → run that one collector directly.
- **Have a session UUID?** Open-ended ("is it healthy / why stuck / pull
  everything") → `panorama`; one specific user message/turn → `trace`;
  need raw transcript or record counts → `extract` first.
- A collector comes back warn/fail → drill into that collector alone only
  if you need more detail (see Workflow §3).
- IM channel symptom (飞书 / 钉钉 / 企微 bot not replying, message dropped) →
  `channel`; see the **Channel** section. The collector is a pure log
  scanner (no config interpretation, no active probe); use
  `--account <substring>` to filter by a per-account log prefix on
  multi-account hosts.

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
| IM channel: bot not replying / silent drops | `openclaw-diag channel --format json` (multi-account: `--account <substring>`) |

## Workflow

1. For broad or unclear symptoms, run:

```bash
openclaw-diag all --format json
```

2. Parse each JSON line independently. For each envelope:
   - `ok=false`: report the `error.code`, `message`, and hint.
   - `ok=true`: inspect `data.module`, `data.verdict`, `data.summary`, and non-ok checks in `data.sections`.

3. Drill down only where needed. For any module whose verdict is
   warn/fail, re-run that single module with `--format json` for full
   detail (exact commands in the **Routing** table). A few have specific
   follow-ups:
   - `run_health` warn/fail → after re-running, use `trace <uuid>` if a session UUID is involved.
   - `task_health` warn/fail → check failure samples, `top_error_patterns`, `timeout_analysis`, `stuck_analysis`, and the runtime breakdown.
   - `sessions_diag` warn/fail → use `extract <uuid> --summary --format json` when a session UUID is known.

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

## Channel

Use `channel` when the user reports an IM channel symptom: bot not
replying, message arrived but no response, channel 卡住, suspected
silent drop. The collector is a **pure log scanner** — it surfaces
ERROR/WARNING entries from IM-channel subsystems plus a curated
catalog of INFO-level "silent drop / gating" phrases (blocked
sender, group disabled, did-not-mention-bot, bot-identity gating,
pairing requests, etc.). It does NOT interpret `channels.*` config,
does NOT detect installed packages, and does NOT make any outbound
network call.

```bash
openclaw-diag channel --format json                       # default: scan last 7 days of logs
openclaw-diag channel --log-dir /tmp/openclaw             # explicit log dir
openclaw-diag channel --account default --format json     # filter to lines containing "default"
                                                          # (matches feishu[default]: ..., [DingTalk:default] ..., etc.)
```

What gets collected:
- A line is classified as **channel** when its subsystem (lowercased)
  contains one of `feishu`, `lark`, `dingtalk`, `wecom`, OR its
  message body starts with a known channel prefix (`feishu[`,
  `[DingTalk]`, `[DingTalk:`, `DingTalk:`, `[wecom`, `[WeCom`,
  `[webhook]`). Note: the lark plugin logs through subsystem
  `feishu/<sub>` — we never key on the literal word "lark".
- A channel line becomes a **signal** if EITHER its log level is
  WARN or ERROR (logLevelId>=4) OR its message matches a phrase from
  the source-mined catalog (`ocdiag/channels/signals.py`).

Severity → verdict mapping:
- `error` (logLevelId>=5) → FAIL
- `warn` (logLevelId==4 or attention phrase) → WARN
- `info` (benign drop phrase, e.g. duplicate / empty / self-echo) → OK
  (still displayed)

Display: signals are sorted newest-first and capped at 20 per render
(JSON output keeps the full list under `data.signals`). When more
than 20 matched, a 倒序 cap note is appended.

Self-pollution defense: lines whose `_meta.path.fullFilePath` matches
the gateway console-relay sink (`/dist/console-`) are dropped before
classification — chat output captured by the relay can't pollute the
catalog.

Reading discipline: a `warn` signal whose body says "blocked
unauthorized sender" or "did not mention bot" is usually the direct
answer to "my message got no reply" — the channel plugin silently
dropped it per policy. Surface those literally before speculating
about Agent or model issues. If the user needs to know whether their
credentials are valid (was the symptom "auth failure"?), inspect the
ERROR-level lines that surfaced; the collector no longer probes
remote endpoints.

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
