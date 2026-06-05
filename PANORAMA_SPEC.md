# panorama inspector — session 360° diagnostic view

## Command

```bash
openclaw-diag panorama <session-id> [--format json|pretty|ndjson] [--mask] [--unmask]
                                     [--run-index N] [--all-runs]
                                     [--strict-correlation]
```

## Purpose

Given a session UUID, collect ALL correlated data from standard OpenClaw data sources to provide a complete picture of the session's execution health. Unlike `trace` (single message) or `extract` (conversation dump), `panorama` shows the full system-level context: lifecycle, tool timing, errors, model decisions, child tasks, delivery — everything needed to diagnose issues.

## Data Sources (standard only, always available)

| # | Source | Path | Content |
|---|---|---|---|
| ① | session.jsonl | `~/.openclaw/agents/<agent>/sessions/<uuid>.jsonl` | Conversation records |
| ② | trajectory.jsonl | `<uuid>.trajectory.jsonl` | Runtime trace (7 event types) |
| ③ | sessions.json store | `sessions.json` in same directory | Session metadata entry |
| ④ | OpenClaw app log | `/tmp/openclaw/openclaw-<date>.log` | Structured JSON logs |
| ⑤ | runs.sqlite | `~/.openclaw/tasks/runs.sqlite` | Task/subagent execution records |
| ⑥ | cron/runs/*.jsonl | `~/.openclaw/cron/runs/<jobId>.jsonl` | Cron job run records |

## Correlation Logic (zero subjective filtering)

### ID Expansion

From `sessionId`, expand the full correlation graph:

1. `sessionId` → direct file lookup (①②)
2. `sessionId` → `sessions.json` → `sessionKey` (③)
3. `sessionId` → trajectory → `runIds` (②)
4. `sessionId` → app logs → extract additional `runIds` (④)
5. `sessionKey` → `runs.sqlite` (`requester_session_key`) → child tasks (⑤)
6. `sessionKey` contains `cron:<jobId>` → parse → cron runs (⑥)
7. `session.jsonl` content → `toolCallIds` (①)

### Log Filtering (objective, graph-based)

A log entry is included if and only if it contains ANY of:
- `sessionId`
- Any `runId` from the expanded set
- Any `toolCallId` from session.jsonl
- `sessionKey` (for persistent sessions)
- Any `childSessionId` (from runs.sqlite)
- `cronJobId` (if cron-triggered session)

Each included entry is annotated with its `correlation.path` explaining WHY it was included.

No whitelist/blacklist. No subjective value judgment. Correlation IS the proof of relevance.

Log entries with NO correlation key are always excluded — relevance must be provable via the correlation graph, never inferred from a time window alone.

### OTel traceId expansion (v1.4.4)

OpenClaw emits an OTel `traceId` (32-hex W3C) on every gateway log line. Lines that text-mention our sessionId or a runId carry the same traceId as deep-stack provider/plugin/harness lines that don't mention any session text. v1.4.4 runs a second pass:

1. **Pass 1**: existing graph-id substring filter.
2. **Harvest**: collect every non-zero `traceId` that appears on a pass-1 record.
3. **Pass 2**: re-scan log files; admit any line whose `traceId` is in the harvested set, ignoring lines already admitted by pass 1.

Pass-2 entries carry `correlation.path = "otel-trace:<traceId>"`. The harvested ids are emitted on the JSON envelope as `otel_trace_ids`. The two-pass scan is window-bounded (same `[start − 5s, end + 5s]` filter) and runs in linear time — verified at 100k lines under 6s.

### Strict mode (`--strict-correlation`)

Only include entries matching `sessionId` or `runIds`. Exclude sessionKey-only and toolCallId-only matches. **In strict mode, OTel traceId expansion is also gated**: only traceIds discovered on a sessionId/runId-seeded line are trusted; sessionKey-seeded lines (which can survive run reuse) cannot expand the trace.

## Output Structure (JSON envelope)

```json
{
  "ok": true,
  "data": {
    "module": "panorama",
    "sessionId": "...",
    "agent": "main",
    "verdict": "ok|warn|fail",
    "summary": {...},
    "correlation_graph": {
      "sessionId": "...",
      "sessionKey": "...",
      "runIds": ["..."],
      "toolCallIds": ["..."],
      "childSessionIds": ["..."],
      "cronJobId": "..."
    },
    "sections": [...]
  }
}
```

## Sections (v1.4.13 layout — 7 sections)

The standalone "Runtime Context", "Model Decisions", and "Delivery" sections
were merged into adjacent sections in v1.4.3. JSON consumers keep the
`runtime_context` data key unchanged for backward-compat; `model_decisions`
and `delivery` data keys were removed (their content lives under
`health_signals` and `timeline` respectively).

**v1.4.11 merge:** the standalone "Panorama · Health Signals" section was
folded into "Panorama · Correlated Logs", and the merged section was
renamed to "Panorama · Correlated Logs & Signals". The two were always
read together — signals are the human explanation of what the logs say —
so a single section reduces scrolling without changing what is rendered
or how the verdict is computed. Section count went from 7 to 6.

**v1.4.13 add:** a new "Panorama · Findings" section is rendered FIRST
(before Session Overview) — an objective, deterministic triage summary
that re-surfaces the worst already-computed problem signals at the top
so the reader sees the punch list without scrolling. It does NOT invent
anything: every line is a pure restatement of fields the source signal
already carries (ts, runId, finalStatus, raw flags, tool args, error
text quoted verbatim). The same deterministic severity key is now also
applied to the problem-signals list inside Correlated Logs & Signals
(fail-class before warn-class, higher rank first, ts tie-break) so
both sections agree on order. Verdict logic is unchanged. Section
count went from 6 to 7.

JSON consumers are unaffected: `report.data["health_signals"]` and
`report.data["positive_health_signals"]` are still populated.
v1.4.13 adds `report.data["findings"]` (ordered list of objective
`{severity, kind, ts_ms, summary, ref}` dicts capped at
`FINDINGS_TOP_N`) and `report.data["findings_more_count"]` (integer
tail count when more than the cap exist).

### ⛔ Findings — objective-only contract

Findings is the ONLY summary surface in the report. To stay useful as a
human triage tool, it MUST present nothing but objective, machine-derivable
facts. Forbidden ABSOLUTELY in any rendered Findings line:

- root-cause claims ("root cause: X", "the problem is X")
- causal chains ("caused", "led to", "because", "due to", "→ triggered")
- probabilistic / subjective language ("likely", "probably", "seems",
  "appears", "most significant")
- recommendations / next steps ("suggest", "should", "recommend",
  "investigate")

Allowed (objective): reading field values verbatim, counting signal
occurrences, deterministic severity classification from the fixed
`SIGNAL_SEVERITY` table, timestamps, run/tool ids, error text quoted
verbatim, evidence pointers ("see Correlated Logs & Signals"). State
WHAT IS, never WHY or WHAT-TO-DO. A test (`test_findings_summary_lines_
have_no_forbidden_words`) scans the rendered Findings text for the
blocklist and asserts none of these tokens appear.

### Severity classification — `SIGNAL_SEVERITY` table

Severity is a fixed module-level mapping `kind → (class, rank)`,
documented in code so the ordering is traceable. `class ∈ {fail, warn}`
mirrors the verdict-driving call (`.fail()` vs `.warn()`) the renderer
already makes for that kind under Correlated Logs & Signals. `rank` is
an int; higher = more severe within its class. Ordering key (used in
both the new Findings section and the existing problem-signals list):

```
(class_order desc, rank desc, ts asc, kind asc)
```

Class order: `fail` > `warn` > unknown. Stable, total, deterministic.
Unknown signal kinds default to `("warn", 0)` so future additions still
surface even before the table is updated. The single per-kind promotion
is `retried_after_failure` upgrading `warn → fail` when its
`final_failed` field is true (matches the render-time `.fail()` call).

### 0. findings (v1.4.13)
- Renders FIRST, before Session Overview. Built last in code so the verdict
  is fully resolved, then moved to position 0 in the section list.
- First line: `verdict: <VERDICT> — <F> fail, <W> warn signals` (or
  `verdict: <VERDICT> — 0 problem signals` when there are none). The
  verdict is read from `report.verdict` — never recomputed.
- Up to `FINDINGS_TOP_N` (=10) ranked findings, each on its own line with
  the deterministic severity marker (`✗` for fail, `⚠` for warn). Lines
  are objective restatements of the signal's fields plus a `(see
  Correlated Logs & Signals)` evidence pointer. Examples:
  - `✗ artifact run 7b06f3d9: finalStatus=error, idleTimedOut=true,
    promptErrorSource=prompt @<ts> (see Correlated Logs & Signals)`
  - `⚠ long_tool_call cron(action=update, jobId=0cdb2836-...) duration=2.4m
    is_error=true result="patch required" @<ts>
    (see Correlated Logs & Signals)`
- When more than `FINDINGS_TOP_N` problem signals exist, a single
  `+N more (see Correlated Logs & Signals)` line is appended.
- When zero problem signals exist, a single objective line `no problem
  signals` is rendered. No fabricated praise.

### 1. session_overview (incl. runtime context)
- sessionId, agentId, sessionKey, trigger (user/cron/subagent)
- Time window (start → end, duration)
- Status (completed/aborted/timeout/active)
- Model, provider, total tokens, cost
- Channel, origin
- Activity / token / child-task summary stats
- **v1.4.4 prompt cache observation** (when `trace.artifacts.promptCache.observation` is present):
  - `broke==true` → WARN line "cache broke: lost ~N cached tokens (prev cacheRead M → N)" with `lost = max(0, prev−cur)`
  - `broke==false` and `cacheRead>0` → OK "cache hit: cacheRead=N"
  - missing observation → no line
  Data keys on `runtime_context`: `cache_broke`, `cache_read_observed`, `cache_read_previous`, `cache_read_lost`.
- **v1.4.4 itemLifecycle** — always rendered when any count is non-zero:
  `items: started=S completed=C active=A`
- **v1.4.4 queue/concurrency** (parsed from app log) — one line:
  `queue: max wait=Wms, max queueSize=Q, max concurrentRuns=R`
- **v1.4.4 context precheck** (parsed from app log) — `context precheck: route=<r> estPromptTokens=N`; WARN when route is `compact`/`overflow`.
- **Runtime context fields (folded in v1.4.3):**
  - harness version + node runtime
  - plugins activated + plugin load errors (errors as WARN)
  - skill count + skill names
  - system prompt budget: `system_prompt_chars` / `project_context_chars` /
    `non_project_context_chars` / `tools_schema_chars`
  - bootstrap truncation (WARN if any)
  - compiled tool / message counts, stream strategy, transport

### 2. timeline (incl. delivery)
- Unified chronological timeline merging ALL correlated events
- Each entry: `{ts_ms, ts_local, source, event_type, summary, correlation?}`
- Sources: `session.jsonl`, `trajectory.jsonl`, `app_log`, `delivery`,
  `state` (v1.4.4 — parsed `session state` log lines), `config`
  (v1.4.4 — parsed `config hot reload` log lines)
- **Delivery events (folded in v1.4.3):** cron run records and
  messaging-tool sends are emitted with `source="delivery"` /
  `event_type="delivery"`
- **v1.4.4 state transitions:** `event_type="state"` entries with
  `state: prev → new reason="..." qd=N`
- **v1.4.4 config reloads:** `event_type="config_reload"`. Applied reloads
  list affected keys; skipped reloads carry the parser's reason
- Truncation honesty: when the cap drops events, `dropped_middle` and
  `truncated:true` are reported on the JSON envelope and surfaced as a WARN
  line in the section
- Records with no parseable timestamp are counted under `skipped_no_ts`
- **v1.4.10 middle-event sample** — the rendered Timeline section now
  includes a `timeline.sample.*` block (up to `TIMELINE_RENDER_SAMPLE`
  entries, chronological, deduped against first/last/key-moment anchors).
  Selection prioritizes interesting events (log:ERROR, log:WARN, state
  transitions, delivery, model.completed, tool calls), then pads with
  evenly-spaced filler so the run's overall shape is visible. Linear
  time, bounded memory.
  **v1.4.11:** `TIMELINE_RENDER_SAMPLE` was raised from 20 to 40, and the
  filler picker was rewritten to use fractional spacing across the full
  pool index range (`(i * n) // room`). Pre-1.4.11 code used `step =
  n // room` rounded to 1 once `room ≈ n`, so the picks all clustered
  near index 0; long runs now get representative coverage across the
  whole window.

### 3. model_calls
- Per-call data: `ts_ms`, `duration_ms` (round-trip wall-clock proxy — kept
  in JSON only, not rendered), `provider`, `model`, `stopReason`, `input`,
  `output`, `cacheRead`, `cacheWrite`, `tools[]`, `cost?`
- Per-call render carries `in=`, `out=`, and stop reason only. Removed:
  per-call throughput `tok/s` (v1.4.6/1.4.7) and per-call duration prefix +
  the wall-clock note + per-model `avg_dur` (v1.4.8) — all derived from the
  unreliable message-gap proxy, not real model timing.
- **Authoritative run wall time** (from gateway log
  `embedded run prompt end ... durationMs=N`): a `run wall time: <duration>
  (<N>ms, from gateway log)` line — this IS a real measurement and is kept.
  Stored on `runtime_context.log_run_duration_ms`.
- Per-model breakdown (avg_output, stop reasons)
- **Model-call errors:** when trace.artifacts show `aborted`,
  `externalAbort`, `timedOut`, `idleTimedOut`,
  `timedOutDuringCompaction`, `timedOutDuringToolExecution`, or a non-null
  `promptErrorSource`, a FAIL line surfaces the run's error flags. If
  `model_calls` is empty in that case, an extra WARN line states "model
  call failed with no usage record (see Health Signals)".

### 4. tool_execution
- Tool call waterfall from session.jsonl (toolCall ts → toolResult ts)
- Per-tool: name, start_ts, end_ts, duration_ms, args, result_text/error
- Stats: total tools, avg/p50/p95/max duration, error count, slowest

### 5. correlated_logs_and_signals (v1.4.11 merged)

This section bundles correlated log lines AND health signals — they are
the same diagnostic story, so the merge eliminates a back-and-forth read
between two adjacent sections. Render order:

1. **Summary line** — `<N> correlated entries: <E> ERROR, <W> WARN, <I> INFO`
   plus a window-filter note when entries were dropped or kept ts-less.
2. **Positive (✓) signals** — `health.ok_*` lines (tools / lifecycle /
   cache / outcome / delivery). Always rendered when present; never
   change the verdict.
3. **Problem (⚠/✗) signals** — every `_health_signals` kind documented
   in §7 below renders here, with the same `fail()`/`warn()` severity
   as the standalone v1.4.10 section. Verdict logic is unchanged.
4. **Raw ERROR log lines** — head-200 (safety cap) with `+N more`
   trailer.
5. **Raw WARN log lines** — head-10 with `+N more` trailer.

Other behavior:

- App log entries pass the correlation filter, **bounded to the session
  window** (`[window_start − 5s, window_end + 5s]`) so reused
  sessionKey/toolCallId noise is dropped.
- **v1.4.10 window-aware log discovery.** Log files are selected by
  filename date (`openclaw-YYYY-MM-DD.log`) intersecting the session
  window (with a ±1 day margin for midnight / tz boundaries), unioned
  with today's mtime ≥ today-00:00 set. This lets a session that ran on
  a previous day still pull in the right log file (the pre-1.4.10
  mtime-only discovery dropped older files, leaving Correlated Logs
  empty). When the window is unknown (0/0), discovery falls back to
  `discover_recent_logs`. The downstream window-bound filter still does
  the precise ±5s slice — broadening file selection does not leak
  unrelated entries.
- INFO log lines: shown **only when the window has no ERROR/WARN** — up to
  `REPRESENTATIVE_INFO_LINES` (=20) representative entries (lifecycle/tool/
  model boundaries, spanning head→middle→tail) as OK-level (✓) lines, so a
  quiet window still shows concrete evidence of what happened instead of
  just a count. (History: v1.4.11 removed this block; v1.4.12 restored it and
  raised the cap from 5 to 20 — 5 was too few to show the run's shape.) When
  ERROR/WARN exist, raw INFO lines are suppressed (the errors/warns + signals
  carry the diagnosis).
- `logs.summary.data` still carries `out_of_window_dropped`,
  `ts_less_kept`.

### 6. child_tasks
- From runs.sqlite: all tasks where requester_session_key matches
- Per child: task_id, runtime, agent_id, status, duration, error

### (Health Signals — folded into §5 in v1.4.11)

The standalone "Panorama · Health Signals" section was merged into
"Panorama · Correlated Logs & Signals" in v1.4.11. Every signal kind
listed below is still computed and surfaced under `report.data
["health_signals"]`; positives are still on `report.data
["positive_health_signals"]`. The renderer routes them to the merged
section above, with severity (`fail`/`warn`) preserved.
- `trajectory_artifact` — abort/timeout flags. v1.4.4 attaches a
  `human_summary` array translating the flag combo into messages like
  "went idle", "hung during tool execution", "hung during context
  compaction", "exceeded turn timeout", "cancelled externally", "aborted
  (internal)". Specific flags subsume the bare `timed_out` / `aborted`
  messages so the same event is reported once. Raw flag list still on
  `flags`.
- `tool_call_leak` — `itemLifecycle.activeCount > 0` at run end
- `items_incomplete` (v1.4.4) — `startedCount > completedCount` and
  `activeCount == 0`: items dropped or errored silently
- `prompt_cache_broke` (v1.4.4) — `promptCache.observation.broke == true`;
  carries `cache_read`, `previous_cache_read`, `lost_tokens`
- `last_tool_error`
- `log_stall` (long-running / stalled session log markers)
- `gateway_pid_change` (multiple gateway PIDs in the correlated logs)
- `long_tool_call` (any tool > 60s). **v1.4.10:** the signal now carries
  `args_summary` (compact `{k=v, ...}` rendering of the tool call's
  arguments — sanitized when `--mask` is set), `snippet` (a 120-char
  excerpt of the error or result text), and `callId`. The rendered line
  reads e.g. `long tool call: cron(action=update,
  jobId=0cdb2836-...) 2.4m → error: patch required` instead of the
  previous `long tool call: cron 2.4m (error)`.
- `child_task_failed`
- **cron_delivery_failed (folded in v1.4.3):** failed cron deliveries
  (`deliveryStatus` ∈ failed/error/errored/undelivered) influence verdict
  via this signal, not via a separate Delivery section
- **log_decision (folded in v1.4.3):** correlated log lines mentioning
  `model_fallback_decision`, `harness_select`, `context_overflow`, or
  `compaction_triggered`. The trajectory `model_select` entries were
  dropped because the model identity is already in Session Overview.
- **v1.4.4 log-derived signals** (parsed from correlated, window-bounded logs):
  - `queue_wait_slow` — any single dequeue with `waitMs > 2000`
  - `context_precheck_overflow` — precheck route in `{compact, compacting,
    overflow, overflowing}`
  - `state_transition_abnormal` — `session state` line whose `new` is
    `aborted`/`error`/`errored`/`failed`
  - `config_reload_failed` — `config reload skipped (invalid config): ...`
- **v1.4.5 retried_after_failure** — a single `runId` carried multiple
  attempt-cycles AND at least one earlier attempt failed. WARN when the
  final attempt succeeded (the run recovered), FAIL when the final
  attempt also failed. Carries `attempt_count`, `failed_count`,
  `final_status`, `final_failed`, and a `per_attempt[]` list of human
  reasons (e.g. `"#1 went idle (no progress within idle timeout) (5.1m)"`,
  `"#2 success (52s)"`). The per-attempt classification reuses the same
  vocabulary as `_classify_timeout_flags`.
- **v1.4.10 positive (OK) signals** — a parallel list emitted on
  `report.data["positive_health_signals"]` so a healthy run shows
  per-aspect confirmations instead of a single "no signals" line.
  Kinds: `ok_tools` ("N tool calls, 0 errors" / "M/N tool calls ok"),
  `ok_lifecycle` ("no leaks (active=0), all N items completed"),
  `ok_cache` ("cache healthy (no breaks)" — only when an observation is
  present), `ok_outcome` ("no aborts/timeouts/stalls"), `ok_delivery`
  ("delivered ok ..." or cron-records confirmed). v1.4.11 renders them
  in the merged "Correlated Logs & Signals" section, just below the
  summary line. They are **additive only** — problem ⚠/✗ lines still
  drive the verdict.

## Verdict Logic

| Condition | Verdict |
|---|---|
| trace.artifacts shows abort/timeout OR child task failed OR ERROR-level correlated log OR cron delivery failure OR model-call error | **fail** |
| WARN-level correlated log OR log-marker decision (fallback/overflow/compaction) OR stall detected OR E2E > 5min OR plugin load error OR bootstrap truncation OR (v1.4.4) prompt cache broke / items incomplete / queue wait > 2s / context precheck overflow / abnormal state transition / config reload failed OR (v1.4.5) retried_after_failure with the FINAL attempt succeeding | **warn** |
| All clean | **ok** |

## v1.4.4 JSON envelope additions

- `runtime_context[i]`: `cache_broke`, `cache_read_observed`,
  `cache_read_previous`, `cache_read_lost`, `log_run_duration_ms`
- `log_parsed`: `{queue_events[], queue_summary, run_registered[],
  state_transitions[], run_durations[], context_prechecks[],
  config_reloads[]}` — parsed once from correlated, window-bounded logs
- `otel_trace_ids`: list of OTel traceIds harvested for second-pass
  expansion
- `health_signals[]` gains kinds: `prompt_cache_broke`, `items_incomplete`,
  `queue_wait_slow`, `context_precheck_overflow`,
  `state_transition_abnormal`, `config_reload_failed`

## v1.4.13 JSON envelope additions

- `findings`: ordered list of objective triage entries, each
  `{severity, kind, ts_ms, summary, ref}`. `severity ∈ {"fail", "warn"}`,
  `summary` is a pure restatement of the source signal (no inference),
  `ref` always points at `"Correlated Logs & Signals"`. Capped at
  `FINDINGS_TOP_N` (= 10).
- `findings_more_count`: integer count of problem signals that did not
  fit under the cap (0 when ≤ TOP_N).
- `health_signals[]`: now sorted by the same deterministic severity key
  used by Findings (fail > warn, rank desc, ts asc, kind asc) — the set
  of signals is unchanged; only the order is.

## Multi-run handling (persistent sessions)

- Persistent sessions have multiple runIds (one per user interaction)
- `--run-index -1` (default): only the latest run
- `--run-index 0`: first run
- `--all-runs`: all runs in the session
- Run boundaries detected from trajectory `session.started` events or log `embedded run start` timestamps

## Multi-attempt-per-runId handling (v1.4.5)

A single `runId` can carry MULTIPLE attempt-cycles when the run retries
internally. Each cycle is a full event sequence (`session.started` →
`trace.metadata` → `context.compiled` → `prompt.submitted` →
`model.completed` → `trace.artifacts` → `session.ended`) with `seq`
resetting to 1 each cycle, all sharing the same `runId`. The pre-1.4.5
grouper kept only the LAST `trace.artifacts`/`session.started`/
`trace.metadata` for a runId, so a failed attempt-1 was overwritten by a
successful attempt-2 and never surfaced.

The grouper now collects every cycle into `run["attempts"]`. The
top-level `run["artifacts"]` / `session_started` / `trace_metadata` keys
still point at the LAST cycle (preserves behavior for everything that
read those keys), and the per-attempt detail enables multi-attempt
health reporting:

- `report.data["run_attempts"][i]`: one entry per selected run,
  `{runId, attempt_count, had_failed_attempt, attempts[]}`. Each
  `attempts[j]` is `{index, started_ms, ended_ms, duration_ms,
  final_status, failure_flags[], prompt_error_source, failed}`.
- Session Overview adds a one-line `attempts: N (M failed,
  final=<status>) [run <rid8>]` for each multi-attempt run (WARN when
  any attempt failed, OK otherwise).
- Health Signals adds a `retried_after_failure` entry per multi-attempt
  run with at least one failed attempt — see entry above. WARN when the
  final attempt recovered, FAIL when it also failed.

A new attempt is signaled by either a repeated `session.started` for the
same runId, OR (defensive fallback) by a `seq` value resetting on any
event when no attempt is currently open. An attempt that produces no
`trace.artifacts` (run died mid-flight) is recorded with
`failure_flags=["noArtifacts"]` and `failed=true`.

Single-cycle runs (the common case) are unchanged: `attempt_count == 1`,
no `attempts` line in Overview, no `retried_after_failure` signal.

## Masking

`panorama` defaults to **unmasked** output. The two flags are opt-in:

- `--mask` (opt-in, must be explicitly passed): redact tool arguments,
  tool results / message content, API keys / bearer tokens / sensitive
  KEY=VALUE patterns, AND the body of correlated log entries (raw
  ERROR/WARN/INFO render plus the `correlated_logs` JSON envelope copy).
  Use this when sharing output externally.
- `--unmask` (opt-in, also default behavior when `--mask` is not passed):
  show full plaintext. Intended for local analysis only.

When both `--mask` and `--unmask` are passed, `--unmask` wins.

## Exit codes

- 0: all ok
- 1: command succeeded, found warn/fail signals
- 2: bad input / session not found
- 3: runtime failure
