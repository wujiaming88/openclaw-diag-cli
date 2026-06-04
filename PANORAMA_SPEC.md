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

### Strict mode (`--strict-correlation`)

Only include entries matching `sessionId` or `runIds`. Exclude sessionKey-only and toolCallId-only matches.

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

## Sections (v1.4.3 layout — 7 sections)

The standalone "Runtime Context", "Model Decisions", and "Delivery" sections
were merged into adjacent sections in v1.4.3. JSON consumers keep the
`runtime_context` data key unchanged for backward-compat; `model_decisions`
and `delivery` data keys were removed (their content lives under
`health_signals` and `timeline` respectively).

### 1. session_overview (incl. runtime context)
- sessionId, agentId, sessionKey, trigger (user/cron/subagent)
- Time window (start → end, duration)
- Status (completed/aborted/timeout/active)
- Model, provider, total tokens, cost
- Channel, origin
- Activity / token / child-task summary stats
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
- Sources: `session.jsonl`, `trajectory.jsonl`, `app_log`, `delivery`
- **Delivery events (folded in v1.4.3):** cron run records and
  messaging-tool sends are emitted with `source="delivery"` /
  `event_type="delivery"`
- Truncation honesty: when the cap drops events, `dropped_middle` and
  `truncated:true` are reported on the JSON envelope and surfaced as a WARN
  line in the section
- Records with no parseable timestamp are counted under `skipped_no_ts`

### 3. model_calls
- Per-call: `ts_ms`, `duration_ms` (round-trip wall-clock — see note),
  `provider`, `model`, `stopReason`, `input`, `output`, `cacheRead`,
  `cacheWrite`, `tools[]`, `cost?`
- Per-call render carries `in=`, `out=`, throughput `tok/s` (or `n/a` when
  duration is zero), and stop reason
- Note line: durations are round-trip wall-clock (last input msg →
  assistant msg), NOT pure model API latency — the trajectory has no
  native durationMs/TTFT
- Per-model breakdown (avg_output, avg_duration, stop reasons)
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

### 5. correlated_logs
- App log entries passing the correlation filter, **bounded to the session
  window** (`[window_start − 5s, window_end + 5s]`) so reused
  sessionKey/toolCallId noise is dropped
- ERROR entries: ALL in-window errors are rendered (safety cap 200 with
  `+N more` line)
- WARN entries: head-10 plus `+N more`
- INFO: representative sampling when no errors/warns
- `logs.summary.data` carries `out_of_window_dropped`, `ts_less_kept`

### 6. child_tasks
- From runs.sqlite: all tasks where requester_session_key matches
- Per child: task_id, runtime, agent_id, status, duration, error

### 7. health_signals (incl. model decisions)
- trace.artifacts: aborted, externalAbort, timedOut, idleTimedOut,
  timedOutDuringCompaction, timedOutDuringToolExecution
- itemLifecycle leak (active > 0 at run end)
- last_tool_error
- log_stall (long-running / stalled session log markers)
- gateway_pid_change (multiple gateway PIDs in the correlated logs)
- long_tool_call (any tool > 60s)
- child_task_failed
- **cron_delivery_failed (folded in v1.4.3):** failed cron deliveries
  (`deliveryStatus` ∈ failed/error/errored/undelivered) influence verdict
  via this signal, not via a separate Delivery section
- **log_decision (folded in v1.4.3):** correlated log lines mentioning
  `model_fallback_decision`, `harness_select`, `context_overflow`, or
  `compaction_triggered`. The trajectory `model_select` entries were
  dropped because the model identity is already in Session Overview.

## Verdict Logic

| Condition | Verdict |
|---|---|
| trace.artifacts shows abort/timeout OR child task failed OR ERROR-level correlated log OR cron delivery failure OR model-call error | **fail** |
| WARN-level correlated log OR log-marker decision (fallback/overflow/compaction) OR stall detected OR E2E > 5min OR plugin load error OR bootstrap truncation | **warn** |
| All clean | **ok** |

## Multi-run handling (persistent sessions)

- Persistent sessions have multiple runIds (one per user interaction)
- `--run-index -1` (default): only the latest run
- `--run-index 0`: first run
- `--all-runs`: all runs in the session
- Run boundaries detected from trajectory `session.started` events or log `embedded run start` timestamps

## Masking

- `--mask` (default): redact tool arguments, message content, API keys
- `--unmask`: show everything (local analysis only)

## Exit codes

- 0: all ok
- 1: command succeeded, found warn/fail signals
- 2: bad input / session not found
- 3: runtime failure
