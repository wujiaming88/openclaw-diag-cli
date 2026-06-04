# Changelog

## v1.4.11 — bigger timeline middle, drop representative INFO, merge Health Signals into Correlated Logs (2026-06-04)

### Changed
- **Timeline middle sample is now meaningfully bigger.**
  `TIMELINE_RENDER_SAMPLE` rose from 20 to 40, AND the filler picker was
  rewritten to use fractional spacing across the full pool index range
  (`(i * n) // room`). Pre-1.4.11 used `step = n // room` rounded to 1
  once `room ≈ n`, which clustered every filler pick near index 0; on
  long sessions the rendered "middle" sample never reached the run's
  tail. Now picks span first→last evenly and the renderer shows up to
  ~40 representative entries between the anchors. Interesting events
  (errors/warns, state transitions, delivery, model.completed, tool
  calls) are still prioritized first; remains linear-time and bounded.

### Removed
- **Cherry-picked "representative INFO" block in Correlated Logs.** When
  there were no ERROR/WARN entries the section used to emit up to 5
  keyword-matched INFO lines (`_representative_logs`). The selection was
  arbitrary (substring match against `start/end/tool/...`), gave a false
  sense of "we picked the important ones", and frequently buried the
  user under boilerplate like "session.ended". The block — and the
  helper — are gone. On a clean run the section now shows the summary
  line and the positive `ok_*` signals only; the absence of error lines
  IS the affirmative answer.
- **Standalone "Panorama · Health Signals" section.** It always read in
  the same breath as Correlated Logs (signals = "what does this log
  mean?"), so the merge below replaces it. JSON consumers are unaffected:
  `report.data["health_signals"]` and `report.data["positive_health_signals"]`
  still carry every signal, with the same kinds, severities, and shape.

### Merged
- **`Panorama · Correlated Logs & Signals`** (new section name) folds in
  every previous Health Signals render. Order:
  1. Summary line (correlated entries: N ERROR, M WARN, K INFO + window
     filter note).
  2. Positive `ok_*` confirmations (✓ tools / lifecycle / cache /
     outcome / delivery).
  3. Problem signals (⚠/✗) — same kinds and severities as v1.4.10:
     `trajectory_artifact`, `tool_call_leak`, `items_incomplete`,
     `prompt_cache_broke`, `retried_after_failure`, `long_tool_call`,
     `cron_delivery_failed`, `log_stall`, `log_decision`,
     `queue_wait_slow`, `context_precheck_overflow`,
     `state_transition_abnormal`, `config_reload_failed`,
     `gateway_pid_change`.
  4. Raw ERROR log lines (cap 200 + "+N more").
  5. Raw WARN log lines (head 10 + "+N more").
  Verdict logic is **unchanged** — the same `fail()` / `warn()` calls
  drive verdict, just from a different host section. Section count went
  7 → 6.

### Tests
- `test_timeline_render_sample_cap_raised_to_40` — pins the constant.
- `test_timeline_sample_more_than_old_cap_and_spans_run` — 200-event
  fixture: asserts >20 sample lines, ≤40 lines, AND coverage reaches
  the last quarter of the run (the bug fix proof).
- `test_correlated_logs_renders_no_info_lines_when_no_err_warn` —
  clean fixture must render zero `logs.info.*` lines.
- `test_representative_logs_helper_removed` — guards the deletion.
- `test_no_standalone_health_signals_section` — section count = 6,
  no `Panorama · Health Signals` title.
- `test_merged_section_contains_summary_and_positive_signals` —
  ordering: summary before positives.
- `test_merged_section_carries_problem_signals` — long_tool_call lands
  under the merged section, verdict still warns.
- `test_merged_section_verdict_unchanged_for_artifact_failure` —
  trajectory_artifact still renders as ✗ FAIL line under merge.
- Existing tests that referenced "Panorama · Correlated Logs" or
  "Panorama · Health Signals" updated to the merged title; the e2e
  smoke test now asserts presence of the merged section AND absence of
  the old standalone Health Signals section.

### Spec
- `PANORAMA_SPEC.md`: 7-section → 6-section layout, section §5 renamed
  to `correlated_logs_and_signals` with the new render order, INFO log
  lines explicitly noted as "NOT rendered", `TIMELINE_RENDER_SAMPLE`
  documented as 40 with the spacing rationale.

## v1.4.10 — three user-reported panorama fixes (2026-06-04)

### Fixed
- **T1: Correlated Logs no longer empty for older sessions.** The previous
  log discovery (`discover_recent_logs`) selected files by mtime ≥ today
  00:00, so for a session that ran yesterday (or earlier) the relevant
  log file's mtime was already stale and got skipped — leaving Correlated
  Logs at zero entries. Added `discover_logs_for_window(log_dir,
  window_start_ms, window_end_ms)` in `recent_logs.py` that selects log
  files by **filename date** intersecting the session window (with a ±1
  day margin for midnight / timezone boundaries), unioned with the
  existing recent-mtime set so today's live log keeps flowing. The
  inspector now computes the session window FIRST, then discovers logs
  for that window. Window-bound (±5s) filtering still does the precise
  slice. Verified on session e37602da: 0 → 277 correlated entries.

### Added
- **T2a: Enriched `long_tool_call` health signal.** The signal now carries
  `args_summary` and `snippet` (error/result text), and the rendered line
  reads e.g. `long tool call: cron(action=update,
  jobId=0cdb2836-3791-468e-a756-d6b8af97d894) 2.4m → error: patch
  required` instead of the previous `long tool call: cron 2.4m (error)`.
  Args summary respects the existing waterfall masking (already sanitized
  upstream when `--mask` is set). Raw `args` / `result_text` /
  `error_text` remain on the waterfall entry for JSON consumers needing
  the full payload.
- **T2b: Positive (OK) health signals.** A healthy run no longer leaves
  Health Signals as a single "no signals" line. Concise per-aspect
  positive signals are emitted: `ok_tools` ("N tool calls, 0 errors" or
  "M/N tool calls ok"), `ok_lifecycle` ("no leaks (active=0), all N
  items completed"), `ok_cache` ("cache healthy (no breaks)" — only when
  an observation is present), `ok_outcome` ("no aborts/timeouts/stalls"),
  `ok_delivery` ("delivered ok ..." or cron records confirmed). They are
  **additive only** — they never affect the verdict, and problem
  ⚠/✗ lines still render alongside. Surfaced on `report.data
  ["positive_health_signals"]`.
- **T3: Timeline middle-event sample.** The Timeline section previously
  showed only first / last / first error / first warn / first stall,
  hiding the shape of long sessions. Added a `timeline.sample.*` block
  with up to `TIMELINE_RENDER_SAMPLE` (=20) chronological,
  de-duplicated entries between the anchors. Selection prioritizes
  interesting events (log:ERROR/WARN, state transitions, delivery,
  model.completed, tool calls), then pads with evenly-spaced filler so
  the run's overall structure is visible. Linear-time, bounded.

### Tests
- T1: `test_discover_logs_for_window_includes_yesterday`,
  `test_discover_logs_for_window_zero_falls_back`,
  `test_discover_logs_for_window_excludes_far_dates`,
  `test_panorama_correlates_yesterday_log` (integration with synthetic
  yesterday-dated log + backdated mtime).
- T2a: `test_long_tool_call_renders_args_and_error` — asserts new fields
  on the signal and the args+error rendering on the human output.
- T2b: `test_positive_health_signals_clean_run`,
  `test_positive_signals_do_not_change_verdict`.
- T3: `test_timeline_sample_renders_middle_events`,
  `test_timeline_sample_helper_dedup_and_bounds`,
  `test_timeline_sample_small_timeline_returns_all`.
- e2e: `test_e2e_real_session_e37_v1_4_10_improvements` against real
  session e37602da.
- 87 tests total, all green; both 100k-line perf tests still pass.

## v1.4.9 — remove misleading "longest gap" timeline metric (2026-06-04)

### Changed
- **Removed the Timeline `longest gap` key-moment.** It reported the largest
  time span between any two consecutive merged-timeline events across the
  whole session. The arithmetic was correct, but it did not distinguish an
  in-run stall (a real problem, seconds–minutes) from idle time between
  separate conversations (normal, hours). On a long-lived/reused session it
  just surfaced overnight idle (e.g. "23.0h") as a "key moment" — accurate but
  diagnostically meaningless. Consistent with v1.4.6–1.4.8: don't show data
  we can't stand behind. The rest of the Timeline section (event span,
  first/last, first error/warn, first stall) is unchanged.

## v1.4.8 — remove unreliable per-call duration too (2026-06-04)

### Changed
- **Removed the per-call duration** from the Model Calls render (the `0s` /
  `20.2m` prefix), the per-model `avg_dur`, and the round-trip wall-clock
  note. Like the throughput removed in v1.4.7, the per-call duration came from
  a session.jsonl message-gap proxy (previous message → assistant message),
  not a real model-timing channel, so it was not trustworthy. Per-call lines
  now show `in=` / `out=` / stop reason / cache only.
- **Kept** the genuinely accurate timings: the authoritative gateway-log
  `run wall time` (a real measurement), the retry per-attempt durations
  (from trajectory `session.started`→`session.ended` spans), the session
  time window, and tool-execution durations (from toolCall→toolResult). The
  raw per-call `duration_ms` is still kept in JSON data for consumers.

### Tests
- `test_model_call_duration_removed_from_render`: per-call line shows no
  duration prefix and no ms-as-seconds artifact; `duration_ms` stays in data.
- Updated `test_model_call_input_and_throughput_fields` accordingly.

## v1.4.7 — remove unreliable throughput entirely (2026-06-04)

### Changed
- **Removed model-call throughput (`tok/s`) entirely** — both the per-call rate
  and the `avg output rate` aggregate. It was derived from a round-trip
  wall-clock gap (previous message → assistant message), not a real API timing
  channel, so it was never trustworthy (v1.4.6 only capped the impossible
  values). Simpler and honest: don't show a number we can't stand behind.
  Per-call lines now show `in=` / `out=` / stop reason; duration (with its
  wall-clock caveat note) and the authoritative gateway-log run wall time stay.
- Dropped the now-unused `MAX_PLAUSIBLE_TOK_PER_S` ceiling.

### Tests
- `test_model_call_throughput_removed`: a 6ms/4096-token call must render NO
  `tok/s` and no bogus value; `out=`/stop reason still present.
- Updated `test_model_call_input_and_throughput_fields` to assert throughput
  is absent while input/output tokens remain.

## v1.4.6 — suppress impossible per-call/aggregate throughput (2026-06-04)

### Fixed
- Model Calls throughput (`tok/s`) is derived from a round-trip wall-clock gap
  (previous message → assistant message), not a real API timing channel. In
  multi-step / cron runs consecutive messages are written milliseconds apart,
  so a large output over a ~6ms gap produced physically impossible rates
  (e.g. `4096 tok / 0.006s = 682,666 tok/s`). Both the **per-call** rate and the
  **`avg output rate`** aggregate now suppress any value above
  `MAX_PLAUSIBLE_TOK_PER_S` (1000 tok/s — well above any real model's sustained
  output) and render `n/a` instead. Calls with a genuine measurable gap still
  show a real rate (e.g. a 7s tool turn → 130 tok/s).
- The Model Calls section note now states per-call tok/s is a rough estimate,
  shown `n/a` when the gap is too small to be real generation time.

### Added
- Regression test `test_model_call_throughput_suppresses_impossible_rate`
  (6ms gap, 4096 out → must render `n/a`, never a bogus number; asserts no
  rendered rate exceeds the ceiling).

## v1.4.5 — surface hidden failed attempts behind a recovered runId (2026-06-04)

A single `runId` can carry MULTIPLE attempt-cycles when the run retries
internally — each cycle is a full event sequence (`session.started` →
`trace.metadata` → `context.compiled` → `prompt.submitted` →
`model.completed` → `trace.artifacts` → `session.ended`) with `seq`
resetting to 1 each cycle, all sharing the same `runId`. Before v1.4.5
the trajectory grouper overwrote `run["artifacts"]` on each occurrence,
keeping only the LAST cycle. So a failed attempt-1 (e.g. a 5-minute idle
timeout) followed by a successful attempt-2 retry rendered as a clean
"success" — the failure was completely erased. Of 120 recently
inspected sessions, 13 carried multi-artifact-per-runId and 12 of those
hid an early failed attempt behind the eventual success (~10%). This
also defeated the v1.4.4 timeout classification: a real attempt-1
timeout never surfaced if attempt-2 succeeded.

### Added

- **Multi-attempt grouping.** `_group_trajectory_runs` now collects
  every attempt-cycle of a runId into `run["attempts"]`. A new attempt
  is signaled by a repeated `session.started` for the same runId (or,
  defensively, by a `seq` reset when no cycle is open). The top-level
  `run["artifacts"]`, `session_started`, and `trace_metadata` keys
  still point at the LAST cycle, preserving every consumer that reads
  them. Per-attempt entries carry `index`, `started_ms`, `ended_ms`,
  `duration_ms`, `final_status`, `failure_flags[]`,
  `prompt_error_source`, and a `failed` boolean. `run["attempt_count"]`
  and `run["had_failed_attempt"]` aggregate over the list. An attempt
  that produces no `trace.artifacts` is flagged `failed=true` with
  `failure_flags=["noArtifacts"]` (the run died mid-flight).
- **`retried_after_failure` health signal.** Fires per multi-attempt
  run when any earlier attempt failed. WARN-level when the final
  attempt succeeded (the run recovered), FAIL-level when the final
  attempt also failed. The signal carries `attempt_count`,
  `failed_count`, `final_status`, `final_failed`, and a `per_attempt[]`
  list of human reasons per cycle. The classification reuses the v1.4.4
  vocabulary — `idleTimedOut`→"went idle (no progress within idle
  timeout)", `timedOutDuringToolExecution`→"hung during tool
  execution", etc. Verdict degrades to WARN when the run recovered;
  stays FAIL when the final attempt also failed (the existing
  `trajectory_artifact` signal handles that case independently).
- **Session Overview "attempts" line.** When any selected run has
  `attempt_count > 1`, Overview adds a one-line summary
  `attempts: N (M failed, final=<status>) [run <rid8>]` per such run
  (WARN when any attempt failed, OK otherwise) so the hidden retry is
  visible without scrolling to Health Signals.
- **`report.data["run_attempts"]`.** Per-run attempt details for JSON
  consumers — list of
  `{runId, attempt_count, had_failed_attempt, attempts[]}` so
  downstream tools can do their own per-attempt analysis.

### Verified

- All 70 prior tests stay green; 6 new tests added (5 unit + 1 e2e
  against the real e37602da session that exhibits the bug, +1 e2e
  against single-cycle 63d70b29 confirming no regression). 75 tests
  collected total (the e37602da/63d70b29 e2e tests skip if the real
  fixtures aren't on the host).
- End-to-end against session `e37602da-ce25-45c6-97d9-2cffa237d1ba` with
  `--all-runs`: TWO previously-hidden retries surface — runId 7b06f3d9
  (#1 went idle 5.1m → #2 success 52s) and runId 2e4f50df (#1 went idle
  20.3m → #2 success 5.9m). Both rendered as `retried_after_failure`
  WARN signals carrying the classified per-attempt chain.
- Single-cycle 63d70b29 confirmed clean: no `attempts` line, no
  `retried_after_failure` signal.
- Perf: 100k-line correlation scan stays linear — 0.35s and 0.44s on
  the two perf tests (well under their 5s/6s caps). The grouper change
  is also linear in the number of trajectory events.

## v1.4.4 — lifecycle log mining + cache/lifecycle/timeout fidelity (2026-06-04)

`panorama` now mines the gateway log for the data the trajectory leaves on
the floor: per-run wall time, queue wait, prompt-cache breakage, context
overflow, state transitions, and config hot-reloads. Correlation expands
through OTel `traceId` so deep-stack provider/plugin lines that don't
text-mention the sessionId are still pulled into the picture. Existing
flag-based health signals get a human "where it hung" summary instead of
a camelCase boolean dump.

### Added

- **Prompt cache breakage detection (task A).** `trace.artifacts.data.
  promptCache.observation` is now fully extracted: when `broke==true`,
  Session Overview emits a WARN line with the lost-token magnitude
  (`lost = max(0, previousCacheRead − cacheRead)`) and a
  `prompt_cache_broke` health signal carries the numbers. When
  `broke==false` and `cacheRead>0`, an OK "cache hit" line shows up.
  Older runs without the `observation` block render no cache line.
  Data keys: `runtime_context.cache_broke`, `cache_read_observed`,
  `cache_read_previous`, `cache_read_lost`.
- **Item-lifecycle render + incomplete detection (task B).** Always
  rendered: `items: started=S completed=C active=A`. The existing
  `tool_call_leak` signal still fires on `activeCount>0`. New
  `items_incomplete` signal fires when `startedCount > completedCount`
  AND `activeCount == 0` — items dropped or errored silently.
- **Granular timeout/abort classification (task C).** Health signal
  `trajectory_artifact` gains a `human_summary` array translating the
  flag combo into messages like "went idle (no progress within idle
  timeout)", "hung during tool execution", "hung during context
  compaction", "exceeded turn timeout", "cancelled externally", "aborted
  (internal)", and "prompt error source: <value>". More-specific flags
  subsume the bare `timed_out` / `aborted` text. Raw flags stay on the
  `flags` array for JSON consumers.
- **OTel traceId correlation (task D).** Log filtering is now two-pass:
  pass 1 runs the existing graph-id substring match; we then harvest
  every non-zero 32-hex `traceId` field from those records and a second
  pass admits any line whose `traceId` is in that set. Pass-2 entries
  carry `correlation.path = "otel-trace:<traceId>"`. Linear time
  (single extra scan with a precomputed set), window-bounded by the
  same `[start − 5s, end + 5s]` filter as pass 1. In `--strict-correlation`
  mode the harvest only trusts traceIds discovered on a sessionId/runId-
  seeded line — sessionKey-seeded lines (which can survive run reuse)
  cannot expand the trace. Harvested ids surface as
  `report.data["otel_trace_ids"]`.
- **Lane queue latency / concurrency (task E).** Parses `lane dequeue
  ... waitMs=N queueSize=N`, `lane enqueue`, `run registered: ...
  totalActive=N` from correlated logs. Session Overview emits
  `queue: max wait=Wms, max queueSize=Q, max concurrentRuns=R`. Any
  single dequeue with `waitMs > 2000` becomes a `queue_wait_slow` WARN
  signal. Distinguishes queue wait from model/compute latency.
- **Authoritative run wall time (task F).** Parses
  `embedded run prompt end: runId=... durationMs=N` from the gateway log
  and renders `run wall time: <duration> (<N>ms, from gateway log)` in
  the Model Calls section. Preserved on
  `runtime_context.log_run_duration_ms`. Coexists with the v1.4.2
  per-call round-trip duration note.
- **Context-overflow precheck (task G).** Parses
  `[context-overflow-precheck] pre-prompt check ... route=<r>
  estimatedPromptTokens=N`. Renders an Overview line; routes in
  `{compact, compacting, overflow, overflowing}` add a
  `context_precheck_overflow` WARN signal.
- **Session state transitions (task H).** Parses `session state:
  sessionId=... prev=X new=Y reason="..." queueDepth=N` from correlated
  logs. Each transition becomes a timeline entry with `source="state"`.
  Transitions whose `new` is `aborted`/`error`/`errored`/`failed` add a
  `state_transition_abnormal` WARN signal.
- **Config hot-reload events (task I).** Parses
  `config hot reload applied (<keys>)` and
  `config reload skipped (invalid config): <reason>` from correlated
  logs. Both go into the timeline as `event_type="config_reload"`.
  Skipped reloads add a `config_reload_failed` WARN signal carrying
  the parser's reason string.
- **`log_parsed` JSON envelope key.** Single dict carrying every parsed
  log bucket: `queue_events`, `queue_summary`, `run_registered`,
  `state_transitions`, `run_durations`, `context_prechecks`,
  `config_reloads`. Parsed once per run, in linear time over the
  already-correlated, window-bounded log.

### Changed

- All log-derived signals from the new parsers compose with the existing
  v1.4.3 window bound (`[start − 5s, end + 5s]`) automatically — they run
  on the post-window list. Strict mode flows through to OTel expansion
  (sessionId/runId-seeded only). Mask handling is unchanged: tool args
  / results still respect `--mask`; correlated log content is preserved
  as-is, consistent with v1.4.3.

### Verified

- `python3 -m pytest tests/` — 70 passed (was 48 in v1.4.3; 22 new tests
  cover tasks A–J plus the perf and e2e cases).
- 100k-line synthetic log perf test (`test_perf_100k_lines_with_otel_under_6s`)
  completes in ~0.4s — the two-pass scan is single-pass-equivalent thanks
  to the harvested-id set lookup.
- End-to-end smoke against real session
  `63d70b29-1a14-4a2b-83c5-9432f9987f40` (clean run) and the longer
  `7d31725b-da60-4dd6-ac33-bfec21222f46` (cache breaks + queue waits)
  produces all 7 sections, no error, and renders cache/queue/precheck/
  state lines as expected.

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
