# Trajectory Integration Plan (v0.6.0)

> Goal: maximize value extraction from `<uuid>.trajectory.jsonl` and `<uuid>.trajectory-path.json` files emitted by OpenClaw 2026.5.x runtime. These files are a structured per-run observability layer — much richer than session.jsonl.

## Owner Decisions (locked-in 2026-05-26)

1. **Single release**: Phase 1 + Phase 2 + Phase 3 ship together in v0.6.0. No staged rollout.
2. **Module placement**:
   - `prompt_budget` analyzer → folded into existing `07_performance.py` (NOT a new module)
   - `delivery_audit` analyzer → folded into existing `06_cron_jobs.py` (NOT a new module)
   - `run_health` → keep as new standalone module `diag/11_run_health.py`
3. **Sanitization default for trajectory fields**: **OFF by default**. Fields like `assistantTexts`, `messagingToolSentTexts`, `prompt`, `finalPromptText`, `toolMetas[].meta` display plaintext in default output. Add explicit `--mask` flag to opt INTO sanitization. This is a deliberate departure from DESIGN.md axiom #7 for trajectory-sourced fields, justified by the diagnostic value of seeing real content. README and CHANGELOG MUST call this out clearly.
   - Non-trajectory sanitization (shell history, plugin error samples, systemd units, session content) is UNCHANGED — still default-mask.
   - Sanitization toggling lives in a single shared helper `ocdiag.trajectory.sanitize_field(value, mask: bool)` that all consumers route through.
4. **No caching, performance-hardened**: trajectory loader streams from disk every invocation. No `/tmp/openclaw-diag-cache/` or any persistent cache. Observer-only (axiom #1) means no side effects of any kind. **However**, performance must be excellent:
   - `summarize_trajectory()` over 100MB / 78 files / 329 runs MUST complete in <2s (cold cache).
   - `iter_runs()` per-file is O(file_size); peak memory bounded by largest single record (~1MB).
   - Use line-by-line streaming (`for line in f`), NOT `f.read()` or `json.loads(f.read())`.
   - For multi-file scans, parallelize across files using `concurrent.futures.ThreadPoolExecutor` (I/O-bound). Default 4 workers, configurable via env `OCDIAG_TRAJECTORY_WORKERS`.
   - Each collector that scans trajectories has a default time-window cap (e.g. last 7d / last 14d / last 100 runs) to bound work.
   - Add `--trajectory-limit N` flag to per-collector commands (default sane per collector).
   - Performance test in Phase 5 MUST measure wall time; fail test if >5s on the reference dataset (78 files / 100MB / 329 runs).

## Background

Each user/cron/heartbeat-triggered run produces 7 events:
1. `session.started` — trigger, agentId, messageProvider, toolCount
2. `trace.metadata` — harness version, model, plugins[], skills[], systemPromptReport, redaction state
3. `context.compiled` — system prompt size, full prompt text, tools snapshot
4. `prompt.submitted` — actual submission ts
5. `model.completed` — usage, promptCache observation, compactionCount, assistantTexts, messagesSnapshot, 6 abort flags
6. `trace.artifacts` — finalStatus, itemLifecycle, toolMetas, didSendViaMessagingTool, messagingToolSentTexts/Targets, successfulCronAdds
7. `session.ended` — final status flags

Pointer file `<uuid>.trajectory-path.json` contains `{traceSchema, schemaVersion, sessionId, runtimeFile}`.

Schema: `traceSchema=openclaw-trajectory`, `schemaVersion=1`.

## Acceptance Criteria

- All existing tests pass; no regression in current trace/all output for sessions WITHOUT trajectory files.
- New text and JSON outputs MUST agree (axiom #4).
- All new fields are sourced from trajectory; each surfaced number is traceable to a specific event type.
- `--unmask` properly bypasses sanitization; default sanitizes user-content fields.
- No new runtime dependencies (axiom #2).
- Single-module crash MUST NOT crash `run all` (axiom #6).
- Verdict logic must follow existing pattern: ok / warn / fail with pass/warn/fail counts.

## Phase 0 — Shared Trajectory Loader (foundation)

Add `ocdiag/trajectory.py` (single source of truth, no other module reads trajectory directly).

### Public API

```python
# ocdiag/trajectory.py

TRAJECTORY_SCHEMA = "openclaw-trajectory"
SUPPORTED_SCHEMA_VERSIONS = (1,)

def discover_trajectory_files(sessions_base: str) -> list[str]:
    """Return absolute paths of all *.trajectory.jsonl files under any agent."""

def trajectory_file_for_session(session_file: str) -> Optional[str]:
    """Given /.../<uuid>.jsonl return /.../<uuid>.trajectory.jsonl if it exists."""

def iter_runs(traj_path: str, *, max_size_mb: int = 50) -> Iterator["Run"]:
    """Stream runs from one trajectory file. Each Run groups 7 events by runId.
    Skips files larger than max_size_mb with a warning record. Tolerates
    malformed lines (skip + count). Returns at most one Run per runId; events
    with same runId are merged in seq order.
    """

@dataclass
class Run:
    session_id: str
    run_id: str
    session_key: str
    workspace_dir: str
    provider: str
    model_id: str
    model_api: str
    started_ts_ms: int           # from session.started
    ended_ts_ms: int | None      # from session.ended (may be None for in-flight)
    trigger: str                 # 'user' | 'cron' | 'heartbeat' | 'acp' | other
    agent_id: str
    message_provider: str
    tool_count: int
    client_tool_count: int
    # Final outcome (from session.ended + trace.artifacts; None if missing)
    final_status: str | None     # 'success' | 'error' | None
    aborted: bool
    external_abort: bool
    timed_out: bool
    idle_timed_out: bool
    timed_out_during_compaction: bool
    timed_out_during_tool_execution: bool
    prompt_error_source: str | None  # 'prompt' | 'tool' | None
    # Usage (from trace.artifacts.usage)
    usage_input: int
    usage_output: int
    usage_cache_read: int
    usage_cache_write: int
    usage_total: int
    # Cache observation
    cache_broke: bool | None
    compaction_count: int
    # Run health
    started_count: int
    completed_count: int
    active_count: int            # >0 means tool-call leak
    # Messaging audit
    did_send_via_messaging_tool: bool
    messaging_targets: list[str]
    messaging_text_count: int
    successful_cron_adds: int
    tool_metas: list[dict]       # [{toolName, meta}]
    # System prompt budget (from trace.metadata.prompting.systemPromptReport)
    system_prompt_chars: int
    system_prompt_project_chars: int
    system_prompt_non_project_chars: int
    skills_prompt_chars: int
    tools_schema_chars: int
    bootstrap_truncated_files: int
    bootstrap_near_limit_files: int
    injected_workspace_files: list[dict]  # [{name, rawChars, injectedChars, truncated}]
    # Plugin snapshot (from trace.metadata.plugins.entries)
    plugin_entries: list[dict]   # passthrough; minimal copy
    # Skill snapshot — count + names only by default (memory)
    skill_count: int
    skill_ids: list[str]
    # Harness for env diagnostics
    harness_version: str
    harness_node: str
    invocation: list[str]
    redaction_modes: dict        # {config, payloads, harness}
    # Raw event refs (for tools that need full data, e.g. trace command)
    raw_events: dict             # {event_type: full_record}; lazy populated by load_full_run

def load_run_full(traj_path: str, run_id: str) -> Optional[Run]:
    """Load one specific run with raw_events populated."""

def summarize_trajectory(traj_path: str) -> dict:
    """Return counts: total_runs, by_trigger, by_final_status, by_abort_flag.
    Used by sessions / recent_errors / cron_jobs collectors."""

def detect_schema_drift(traj_path: str) -> Optional[str]:
    """If schemaVersion not in SUPPORTED_SCHEMA_VERSIONS, return version string.
    Allows graceful warning rather than crash."""
```

### Implementation rules

- Stream line-by-line; do NOT read full file into memory (some files exceed 3MB).
- Tolerate truncated final line (last write may be incomplete).
- Group events by runId; do not assume strict ordering across event types within file.
- A Run is considered "complete" only when both session.started and trace.artifacts are present. Incomplete runs (in-flight or crashed mid-run) MUST be returned with `final_status=None` and explicit `incomplete=True` flag — see axiom #5 (data missing classification).
- All numeric fields default to 0 if missing; all bool flags default to False; string fields use None for missing (never empty string).
- Schema drift: if `schemaVersion > 1`, emit warning record and best-effort parse known fields.

### Performance budget

- `summarize_trajectory` over 100MB / 78 files / 329 runs MUST complete in <2s on typical hardware.
- `iter_runs` for one file is O(file_size) memory-bound by largest single record (~1MB).

## Phase 1 — Existing Collector Upgrades

Apply in order; each phase is independently shippable.

### 1.1 `08_sessions.py` (high priority)

Current: counts files by mtime, no insight into actual run state.

Add:
- **Stuck-tool detection**: count runs where `active_count > 0`. Each such run is a tool-call leak. Show top 5 (sessionId, runId, ts, count, sample toolMetas).
- **Run trigger distribution**: user / cron / heartbeat / acp counts.
- **Per-trajectory run density**: runs/file histogram; flag files with >50 runs (potentially long-lived session worth investigating).
- **Incomplete-run rate**: % of runs lacking session.ended (likely crashed/killed).

Verdict rules:
- fail: any run with `active_count > 0` AND `final_status='success'` (silent leak)
- warn: incomplete-run rate > 5% OR any single trajectory >50MB
- ok: otherwise

JSON additions: `runs_total`, `runs_by_trigger`, `runs_with_active_leaks`, `incomplete_runs`, `largest_trajectory_mb`, `top_leak_runs`.

### 1.2 `05_recent_errors.py` (high priority)

Current: greps logs for ERROR/WARN.

Add from trajectory (last 7d window):
- **Aborted runs**: count by category (`aborted`, `externalAbort`, `timedOut`, `idleTimedOut`, `timedOutDuringCompaction`, `timedOutDuringToolExecution`).
- **promptErrorSource non-null**: distribution of `prompt` vs `tool` vs other.
- **Tool-call leaks**: same as 1.1 but framed as errors.
- **Top failing toolNames**: aggregate `toolMetas` for runs with `final_status='error'`; show top 10 by occurrence.
- **Sample evidence**: for each error category, show 2 most recent (sessionId#runId@ts — toolMetas snippet, sanitized).

Verdict rules:
- fail: >10 abort/timeout events in 24h, OR any tool-call leak
- warn: 1–10 abort events in 24h, OR `compactionCount` >0 in >20% of runs (last 24h)
- ok: otherwise

JSON additions: `abort_breakdown`, `prompt_error_sources`, `tool_leak_count`, `top_failing_tools`, `compaction_rate_24h`.

### 1.3 `06_cron_jobs.py` (CRITICAL — ArkClaw 5.7 cron-not-delivered scenario; ALSO absorbs delivery_audit per Decision #2)

Current: parses jobs.json + log scrapes for execution traces.

Add (cron-specific):
- **Cron delivery audit**: filter trajectory runs where `trigger='cron'` (last 7d). For each:
  - `final_status` (success/error)
  - `did_send_via_messaging_tool` — if False, this is a delivery failure
  - `messaging_targets` count, `messaging_text_count`
  - `successful_cron_adds` (this run added new crons)
- **Cross-correlation**: match each cron run with `cron_runs.jsonl` (existing) by ts/runId; flag mismatches (e.g., cron module says ok but agent didn't send).
- **Silent-cron detection**: cron runs where `final_status=success` but `did_send_via_messaging_tool=false` AND `successful_cron_adds=0` AND `assistantTexts` is empty/short — strong signal of broken delivery (this is the 5.7 bug pattern from MEMORY.md).

Absorbed delivery_audit content (cross-trigger, not just cron):
- For last 7d, group by trigger:
  - `user` triggered runs: count where `did_send_via_messaging_tool=true`; vs runs where `assistantTexts[0]` is non-empty (channel responded directly without explicit messaging tool — normal pattern)
  - `cron` triggered runs: count where `did_send_via_messaging_tool=true`; flag silent crons (above)
  - `heartbeat` triggered: usually no send; warn if `did_send_via_messaging_tool=true` (unexpected)
- Surface as a sub-section `Delivery Audit` within the cron_jobs collector output, AFTER the cron-specific content.

Verdict rules:
- fail: any silent-cron pattern detected
- warn: cron success rate < 95% in 7d, OR heartbeat-triggered did_send rate > 0
- ok: otherwise

JSON additions: `cron_runs_7d`, `cron_send_rate`, `silent_cron_runs[]` (with sessionId#runId), `delivery_audit: { user: {...}, cron: {...}, heartbeat: {...} }`.

### 1.4 `07_performance.py` (high priority; ALSO absorbs prompt_budget per Decision #2)

Current: P50/P95 latency from session.jsonl tool/model timing.

Add (performance-specific):
- **Cache health**: across recent 100 runs, % with `cache_broke=True`; avg `cacheRead/total` ratio.
- **Compaction rate**: % of runs with `compactionCount > 0`; max compactionCount.
- **Per-trigger latency**: split P50/P95 by trigger (user vs cron vs heartbeat).
- **Token velocity**: aggregate output tokens / total wall time per provider+model.

Absorbed prompt_budget content (sub-section `System Prompt Budget` within performance output):
- Average across last 50 runs:
  - total `systemPrompt.chars`
  - `projectContextChars` vs `nonProjectContextChars`
  - `skills.promptChars` (with top 10 largest skills by blockChars)
  - `tools.schemaChars` (with top 10 largest tools by schemaChars + propertiesCount)
- Workspace files: name + injectedChars + truncated flag (most recent run)
- Bootstrap truncation incidents (any `truncatedFiles>0` runs in window)
- Per-skill / per-tool char-budget tables sorted desc, top 20 each

Verdict rules (combined):
- fail: cache hit ratio (cacheRead/total) < 30% averaged over last 50 runs
- fail: any single skill > 10000 blockChars OR any tool > 15000 schemaChars
- warn: any single skill > 5000 blockChars OR any tool > 8000 schemaChars
- warn: compaction rate > 20%
- warn: total system prompt > 80% of model context window AND truncatedFiles > 0
- ok: otherwise

JSON additions: `cache_health`, `compaction_stats`, `per_trigger_latency`, `prompt_budget: { avg_chars, project_chars, non_project_chars, skills: {top: [...]}, tools: {top: [...]}, workspace_files: [...], bootstrap_truncation: {...} }`.

### 1.5 `09_plugin_diag.py` (high priority)

Current: parses `openclaw plugins list/inspect`, DNS checks.

Add:
- **Plugin status timeline**: from latest 10 trajectories (one per ~run), extract `plugins.entries[]`. Build per-plugin history: when did it last appear with `activated=true` / `error != null`.
- **Drift detection**: compare current plugin status (from `plugins inspect`) with most-recent trajectory snapshot; flag mismatches (e.g., plugin currently disabled but was activated 1h ago without explicit user action).
- **Hidden activation reasons**: surface `activationReason` for any disabled plugin (e.g., "not in allowlist", "hooks.allowConversationAccess required").
- **Imported runtime plugin IDs**: `importedRuntimePluginIds` (currently 64 entries) — cross-check against entries[] to find imported-but-not-loaded plugins.

Verdict rules:
- fail: any plugin with `error != null` in latest run
- warn: drift detected (current vs trajectory) OR imported-but-not-loaded > 5
- ok: otherwise

JSON additions: `plugin_drift[]`, `plugin_errors_recent[]`, `imported_unused[]`.

### 1.6 `02_environment.py` (medium priority)

Current: `openclaw --version`, current process env grep.

Add:
- **Historical version drift**: extract distinct `harness.version` values from last 14d of trajectories. If multiple, list each version with run count and last-seen ts.
- **Node version drift**: same for `runtime.node`.
- **Invocation drift**: distinct `invocation[]` values; flag if --port or other args changed.

Verdict rules:
- warn: >1 distinct OpenClaw version in 14d (recent upgrade or rollback)
- ok: otherwise

JSON additions: `version_history[]`, `node_version_history[]`, `invocation_changes[]`.

### 1.7 `03_configuration.py` (low priority)

Current: flatten openclaw.json.

Add:
- **Effective runtime config**: from latest trajectory's `trace.metadata.config.runtime` (`timeoutMs`, `disableTools`, `toolResultFormat`, `trigger`).
- **Skill snapshot count drift**: track `skills.snapshotVersion` over time; flag rapid changes.

No verdict change; this is informational.

### 1.8 `04_gateway.py` (low priority)

Add:
- **Run frequency window**: histogram of run starts in 24h (helps spot "why is gateway hot?").
- **Invocation extraction**: parse `harness.invocation` to detect `--port`, runtime config drift.

No verdict change; informational.

## Phase 2 — trace command (oc_session_trace.py) Enhancements

Current: uses trajectory only for context.compiled prompt size, per-run window timing.

Add to per-run report:

- **Outcome line**: `final_status, prompt_error_source, all 6 abort flags`. If any flag true, print as warning marker.
- **Tool lifecycle**: print `started/completed/active`. If active>0, print warning with `toolMetas` of incomplete calls (best-effort: cross-reference toolCall items in messagesSnapshot vs completed toolResults).
- **Cache observation**: print `compactionCount` + `cache.broke` + `cacheRead/total` ratio.
- **Messaging audit**: if `trigger='cron'` or messaging used, show `did_send_via_messaging_tool` + targets + text count.
- **System prompt budget**: condensed view (`38950 chars: project=18140 / non-project=20810; skills=9644; tools=54481`).
- **Plugin snapshot**: list plugins with `error != null` from this run's trace.metadata.

### CLI flags
- `--include-trajectory-detail` (default on): include trajectory enrichments.
- `--no-trajectory` (existing): unchanged behavior.
- `--show-tool-metas`: include full toolMetas list (sanitized).
- `--show-plugin-snapshot`: print full plugin list state.

JSON additions: `trajectory.outcome`, `trajectory.lifecycle`, `trajectory.cache`, `trajectory.delivery`, `trajectory.prompt_budget`, `trajectory.plugin_snapshot`.

## Phase 3 — New Collectors (additive to dispatcher)

Per Decision #2: only `run_health` is a new standalone module. `prompt_budget` analysis is absorbed by `07_performance.py` (Phase 1.4) and `delivery_audit` is absorbed by `06_cron_jobs.py` (Phase 1.3). The 1.3 / 1.4 sections above already enumerate the absorbed fields.

Add to `ocdiag/dispatcher.py` module list. New file under `diag/`.

### 3.1 `diag/11_run_health.py` (new — only new collector in v0.6.0)

Global run-health overview, complementary to `recent_errors`:
- Total runs by trigger (last 24h / 7d / 30d windows)
- Final-status distribution (success / error / incomplete)
- Abort flag distribution per category (aborted / externalAbort / timedOut / idleTimedOut / timedOutDuringCompaction / timedOutDuringToolExecution)
- Active leak count (runs with `active_count > 0`)
- Avg + P95 wall duration per trigger (session.started → session.ended)
- compaction rate per trigger
- Top 10 longest-running runs in last 7d (sessionId#runId, trigger, duration, finalStatus)

Verdict rules:
- fail: any active leak in last 24h
- warn: error rate > 5% in last 24h, OR compaction rate > 30% in last 24h
- ok: otherwise

JSON: `windows: { '24h': {...}, '7d': {...}, '30d': {...} }`, `top_long_runs: [...]`, `verdict`.

Default flag: `--window 7d` (also accept `24h` / `30d` / `all`).

## Phase 4 — Documentation

- Update `README.md` to describe trajectory integration; add to data-source table.
- Update `docs/DESIGN.md` with new "trajectory observability layer" section.
- Document field provenance: every new metric MUST list its source event type.
- Add CHANGELOG entry for v0.6.0.

## Phase 5 — Tests / Verification

### Synthetic fixtures

Create `tests/fixtures/trajectory/` with hand-crafted JSONL covering:
- `complete_user_run.trajectory.jsonl` — normal happy path
- `aborted_run.trajectory.jsonl` — user Ctrl-C scenario
- `idle_timeout_run.trajectory.jsonl` — idleTimedOut=true
- `tool_leak_run.trajectory.jsonl` — active_count > 0
- `silent_cron_run.trajectory.jsonl` — cron trigger + did_send=false (the 5.7 bug pattern)
- `cache_break_run.trajectory.jsonl` — promptCache.observation.broke=true
- `incomplete_run.trajectory.jsonl` — only session.started + trace.metadata (mid-flight)
- `multiline_run.trajectory.jsonl` — 7 events for 5 separate runIds in one file
- `bad_schema_run.trajectory.jsonl` — schemaVersion=99 (drift test)
- `truncated_run.trajectory.jsonl` — last line truncated mid-record

Each fixture has expected output under `tests/expected/trajectory/<name>.json`.

### Test runner

- Single Python script `tests/run_trajectory_tests.sh` that pipes each fixture into the relevant collector with `--json` and diffs against expected.
- Add fixture-driven tests to existing test infrastructure (if any) or create minimal one.
- All Phase 1–3 collectors run successfully on `~/.openclaw/agents/main/sessions/` (real data, 78 files / 329 runs); verify each verdict transitions correctly.

### Smoke verification

```bash
# Full smoke test on real data (78 trajectories, ~329 runs, 100MB)
time openclaw-diag all --json | jq '.module, .verdict, .pass, .warn, .fail'
time openclaw-diag run_health
time openclaw-diag run_health --window 24h
time openclaw-diag cron_jobs              # absorbed delivery_audit
time openclaw-diag performance            # absorbed prompt_budget
time openclaw-diag trace 80e72ac6 --include-trajectory-detail
# Mask test
openclaw-diag trace 80e72ac6 --include-trajectory-detail --mask
# Workers control
OCDIAG_TRAJECTORY_WORKERS=1 time openclaw-diag run_health
OCDIAG_TRAJECTORY_WORKERS=8 time openclaw-diag run_health
```

### Performance gates (Phase 5 acceptance)

- `openclaw-diag run_health` on 78 files / 329 runs / 100MB: <2s wall.
- `openclaw-diag all --json`: <8s wall (full sweep across all collectors).
- Memory peak: <500MB across all collectors.

## Phase 6 — Release v0.6.0

- All Phase 1–3 collectors GA.
- Bump package.json + commit/tag/push.
- `codex exec review --commit <sha>` MUST be run, P2+ findings addressed.
- Multiple review rounds OK if findings persist (per MEMORY.md 0.4.0 lesson).
- npm publish + tarball spot-check.

## Out of Scope

- Modifying OpenClaw runtime or trajectory schema.
- Real-time tailing of trajectory files (this is offline analysis).
- Cross-machine aggregation.
- Web UI / dashboard.
- ACP-stream parsing (`<uuid>.acp-stream.jsonl`) — separate future work.

## Risks / Notes

- **Schema may evolve**: Phase 0 loader MUST handle schemaVersion drift gracefully (warn-not-crash).
- **Performance**: 100MB+ trajectory data on busy systems; loader must stream, parallelize, AND respect time-window caps. See Decision #4 perf gates.
- **Sanitization changed for trajectory**: Per Decision #3, `messagingToolSentTexts`, `assistantTexts`, `prompt`, `finalPromptText`, `toolMetas[].meta` are NOT masked by default. README and CHANGELOG must call this out. `--mask` flag opts in.
- **Memory**: `messagesSnapshot` and `tools` arrays inside trace.metadata can be large. Loader copies only minimum needed; raw_events lazy-load only when explicitly requested.
- **Truncated final line**: trajectory writer may not flush on crash. Loader must tolerate.
- **No cache, perf-hardened**: Decision #4 — no persistent cache, but loader uses streaming + ThreadPoolExecutor + window caps to hit perf gates.
