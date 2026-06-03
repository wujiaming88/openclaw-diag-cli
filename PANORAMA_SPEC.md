# panorama inspector — session 360° diagnostic view

## Command

```bash
openclaw-diag panorama <session-id> [--format json|pretty|ndjson] [--mask] [--unmask]
                                     [--run-index N] [--all-runs]
                                     [--include-ambient] [--strict-correlation]
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

### Ambient mode (`--include-ambient`)

Additionally include WARN/ERROR log entries within the session time window that have NO correlation key. These are ambient system events that MIGHT be related but cannot be proven. Excluded by default.

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

## Sections

### 1. session_overview
- sessionId, agentId, sessionKey, trigger (user/cron/subagent)
- Time window (start → end, duration)
- Status (completed/aborted/timeout/active)
- Model, provider, total tokens
- Channel, origin

### 2. timeline
- Unified chronological timeline merging ALL correlated events
- Each entry: `{ts, source, event_type, summary, correlation}`
- Sources: session.jsonl records, trajectory events, app log entries

### 3. runtime_context
- From trajectory: harness, model, config, plugins, skills
- From context.compiled: prompt size, tools count, messages count
- From sessions.json: systemPromptReport, compactionCount, cache stats

### 4. tool_execution
- Tool call waterfall from session.jsonl (toolCall ts → toolResult ts)
- Per-tool: name, start_ts, end_ts, duration_ms
- Stats: total tools, avg/p50/p95 duration, slowest, parallel groups

### 5. correlated_logs
- All app log entries that pass the correlation filter
- Each annotated with correlation path
- Grouped by subsystem

### 6. model_decisions
- From trajectory: trace.metadata (model selection)
- From app logs: model_fallback_decision, harness selection
- Context overflow events

### 7. child_tasks
- From runs.sqlite: all tasks where requester_session_key matches
- Per child: task_id, runtime, agent_id, status, duration, error
- Links to child session files

### 8. delivery
- From cron/runs: delivery status, intended/resolved target
- From app logs: message processed events

### 9. health_signals
- From trajectory: trace.artifacts (abort/timeout/idle flags)
- From app logs: stalled session, long-running session warnings
- Process-level: gateway restart detection (PID changes in log)

## Verdict Logic

| Condition | Verdict |
|---|---|
| trace.artifacts shows abort/timeout OR child task failed OR ERROR-level correlated log | **fail** |
| WARN-level correlated log OR model fallback OR stall detected OR E2E > 5min | **warn** |
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
