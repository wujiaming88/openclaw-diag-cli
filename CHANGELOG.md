# Changelog

## v1.10.2 — prefilter requests reuse an existing full-scan cache (2026-06-16)

Performance + docs fix for the `all` command. No behavior change to output
contents; pure cache-reuse optimization plus a stale-comment correction.

### `collect_runs` prefilter reuse

Before: in `openclaw-diag all`, `configuration` (and `performance`) populate
the full-scan trajectory cache early, yet `plugin_diag`'s 7d `mtime_prefilter`
request still hit disk for a fresh windowed scan (~540ms observed) because the
superset-reuse path explicitly excluded `mtime_prefilter=True` requests.

Now: any windowed request (`since_ms` set, no per-file limit) — INCLUDING
`mtime_prefilter=True` — is served IN MEMORY from an EXISTING full-scan cache
when one is present. A prefilter request only falls through to the real
prefilter DISK scan when NO full cache exists (the standalone fast path). When
served from the full cache the result is the superset of a prefilter disk scan
(undated runs in old-mtime files are kept); all current windowed consumers
tolerate that (gateway counts dated runs only; plugin_diag takes the top-30
dated runs).

Measured: `plugin_diag` inside `all` dropped from ~540ms to ~100ms. Standalone
`plugin_diag` is unchanged (still does its 7d/30d prefilter disk scan).

Zero-deviation note: the displayed scope count still equals exactly what was
consumed in each run. The count may legitimately differ between modes
(`all` reuses the full-cache superset; standalone uses the prefilter subset),
but the `7d` window token is identical and each displayed count matches its
own scan.

### Fixes
- `ocdiag/core/context.py`: corrected the `collect_runs` docstring — `plugin_diag`
  is no longer listed as a no-window caller (it has been a windowed 7d→30d
  prefilter caller since v1.10.0); documented the new prefilter-reuse path and
  the superset semantics.

### Tests
- New `tests/test_cache_prefilter_reuse.py` (pytest): proves a prefilter request
  reuses an existing full cache (same Run objects, no disk re-scan) and that a
  prefilter request with no full cache does a real disk scan without fabricating
  a full-scan cache entry. Suite: 198 passed, 1 skipped.


## v1.10.1 — data_scope zero-drift: derive window/counts from actual scan (2026-06-16)

A correctness fix on top of v1.10.0. The owner's hard requirement: the
displayed `data_scope` MUST be IDENTICAL to the data the collector
actually consumed — zero deviation. Two failure modes were eliminated:

(A) **DRIFT** — scope window tokens (`24h` / `7d` / `14d` / `30d`) were
hardcoded literals separate from the ms value passed into
`ctx.collect_runs`. They could silently diverge if either side changed.

(B) **MISLABEL** — a displayed count was sometimes NOT the actual scanned
count. Two known cases fixed:

- `plugin_diag` reported the filtered top-30 sample as if it were the
  scope (`trajectory: 7d (30 runs)` even when 200+ runs were scanned to
  produce the sample). Now reports `trajectory: 7d (104 runs scanned, 30
  sampled)` — both numbers are real, both come from the actual winning
  window. The summary JSON gains a new `trajectory_runs_scanned` field.
- `performance` declared `sessions: 7d` but its primary perf sample was
  the latest 20 session files by mtime; 7d was only the daily-trend
  window. The single misleading scope item is now split into two honest
  ones: `sessions: latest-20 (N files)` (perf sample) and
  `sessions: 7d (M files)` (daily trend).

### Single source of truth: `window_token()`

New helper `ocdiag.timeutil.window_token(ms)` maps the SAME ms value used
in the scan to the canonical window token (`24h` / `7d` / `14d` / `30d`,
or `Nd` / `Nh` fallbacks). Every trajectory-window scope now derives its
window token from this helper; literal strings are gone.

### Real scanned counts everywhere

`add_scope` is now called AFTER the scan, so the detail count reflects
the actual size of the returned list (`len(runs)`, `len(files)`). No
collector passes a filtered/capped subset count where the scope口径
expects the scan口径.

### Scan-window vs analysis-threshold distinction

Clarified in code and tests:

- **scan window** = how far back / which files were actually READ from
  disk → goes in `window`.
- **analysis threshold** = a cutoff applied to already-scanned data
  (e.g. `task_health` 24h orphan, `sessions_diag` 7d active,
  `run_health` 24h/7d/30d slices) → goes in `detail`, never in `window`.

### Tests

New `tests/test_scope_consistency.py` (12 tests):

- `window_token()` mapping pinned for known windows + fallbacks.
- For each windowed-trajectory collector (gateway 24h, cron_jobs 7d,
  environment 14d, recent_errors 7d): independently recompute
  `len(ctx.collect_runs(since_ms=ms_ago(W)))` and assert the displayed
  scope detail embeds that exact count.
- `plugin_diag`: assert summary carries both `trajectory_runs_scanned`
  and `samples`, and that scope detail references both.
- `performance`: assert exactly two `sessions` scope items
  (`latest-20`, `7d`) with file counts equal to
  `session_files_analyzed` / `trend_files_analyzed`.
- `sessions_diag` / `task_health`: assert analysis thresholds appear in
  detail text, never as the window token.
- Universal regression guard: no collector emits a raw integer ms as a
  window token.

Total test suite: 196 passed, 1 skipped (was 184 + 12 new).

## v1.10.0 — plugin_diag layered trajectory fallback + unified data_scope (2026-06-16)

### plugin_diag — layered 7d → 30d → full trajectory scan

`plugin_diag` previously did a full trajectory scan and selected the
top-30 most-recent runs that carried plugin metadata. On hosts with
months of accumulated trajectory files this scanned far more than
necessary; on quiet hosts where the latest run had no plugin metadata,
fallback was implicit and invisible. v1.10.0 makes the scan layered
and tells you which window produced the data:

1. Try the last 7 days (`mtime_prefilter=True`).
2. If no top-30 run with plugin metadata, fall back to 30 days.
3. If still empty, fall back to a full scan.
4. Surface the chosen window as `trajectory_scan_scope` in the
   `trajectory_plugins` payload — one of `7d`, `30d`,
   `full_fallback`, or `none` (no trajectory files).

The human evidence body now shows `trajectory 扫描口径: <scope>` on the
first line so operators can see at a glance whether the report came
from a fresh window or an old one.

### Unified `data_scope` / 数据口径 across every collector and inspector

Every diagnostic report now declares — in machine- and human-readable
form — exactly which data window/source it scanned. This was previously
buried per-section ("7d cron run", "today's app log") and missing
entirely from many modules.

- New core type: `core.types.ScopeItem` (`source`, `window`, `detail?`)
  on `Report.data_scope`, populated via `report.add_scope(...)` inside
  each collector's `collect()`.
- JSON renderer: every success envelope now carries a top-level
  `data.data_scope` array of `{source, window, detail?}` objects.
- Human renderer: a `数据口径` line appears in the banner (after `Time`,
  before the first bar) when scope is non-empty, joined by ` · ` —
  e.g. `trajectory:7d(240 runs) · 应用日志:今日(3) · 配置:当前`.
- NDJSON renderer: emits a leading `{"kind":"scope","data_scope":[...]}`
  line before any section line, so streaming consumers see the data
  window first.
- Per-module mapping (verified against current source):
  `configuration` → config:current + trajectory:full;
  `cron_jobs` → cron_store:current + trajectory:7d;
  `doctor` → doctor:current;
  `environment` → system:current + trajectory:14d;
  `gateway` → gateway_status:current + trajectory:24h;
  `performance` → trajectory:full + sessions:7d;
  `plugin_diag` → app_logs:today + config:current + extensions:current
  + trajectory:`<scan_scope>`;
  `recent_errors` → journald:today + app_logs:today + trajectory:7d;
  `run_health` → trajectory:full (windows 24h/7d/30d);
  `sessions_diag` → sessions:full + app_logs:full + trajectory:full;
  `shell_history` → shell_history:full;
  `sys_health` → system:current;
  `task_health` → tasks:current (orphan cutoff 24h);
  `channel` → app_logs:7d;
  inspectors `extract` / `trace` → session:`<id8>`;
  `panorama` → session:`<id8>` + app_logs:session_window.

## v1.9.0 — channel collector reduced to a pure log scanner (2026-06-12)

### Breaking — `channel` no longer interprets config or runs network probes

The `channel` collector has been redesigned around a single,
durable question: *"what does the log actually say went wrong on the
IM-channel side?"* Prior versions detected which IM variant was
installed, walked per-variant config rules, and (with `--probe`)
made outbound HTTP calls to platform token endpoints. That worked
but produced noisy bundled-vs-lark double reports, required
maintaining a per-variant rule table that lagged upstream plugin
changes, and meant operators had to reason about three layers (L1
config / L2-L3 log / L5 probe) before answering a basic "what's in
the logs" question.

This release strips all of that. The collector now:

- Walks `openclaw-*.log` files in the configured `--log-dir`
  over the last 7 days.
- Classifies each JSON line as channel iff its subsystem
  (lowercased) contains one of `feishu`, `lark`, `dingtalk`,
  `wecom`, or its message body starts with a known channel
  prefix (`feishu[`, `[DingTalk]`, `[DingTalk:`, `DingTalk:`,
  `[wecom`, `[WeCom`, `[webhook]`). Note: lark logs through
  subsystem `feishu/<sub>` — detection NEVER requires the literal
  word "lark".
- Collects a channel line as a signal when EITHER its log level is
  WARN or ERROR (`logLevelId>=4`) OR its message matches a phrase
  from the source-mined catalog at
  `ocdiag/channels/signals.py` (silent drops, gating decisions,
  bot-identity recovery, pairing flow, etc., including the dingtalk
  Chinese phrase `群聊被拦截`).
- Sorts signals newest-first, displays at most 20 (with a 倒序 cap
  note when more matched), and preserves the FULL message body
  per line — `日志信息完整` is a hard requirement.
- Reuses the existing console-relay self-pollution guard — lines on
  paths containing `/dist/console-` are dropped before classification.

Severity ⇒ verdict: `error` → FAIL, `warn` → WARN, benign info
(`skipping duplicate`, `drop self-echo`, etc.) is collected but does
not bump the verdict.

#### CLI surface
- **Removed**: `--probe`, `--sender`. The active credential probe and
  the per-sender allowlist gate-check no longer exist on this
  collector.
- **Kept**: `--account <substring>` — now interpreted as a substring
  filter on the message body (matches the channel-prefix portion,
  e.g. `--account default` keeps lines containing `feishu[default]:`).

#### Files
- **Deleted**: `ocdiag/channels/detect.py`,
  `ocdiag/channels/probe.py`, the entire
  `ocdiag/channels/variants/` package
  (`base.py`, `feishu_bundled.py`, `feishu_lark.py`,
  `dingtalk.py`, `wecom.py`).
- **Added**: `ocdiag/channels/signals.py` — phrase catalog +
  `classify(message)` helper, with source-cite comments pointing back
  to the plugin emission sites.
- **Rewritten**: `ocdiag/collectors/channel.py` (pure scanner),
  `tests/test_channel.py` (28 tests for the new shape).
- **Updated**: `ocdiag/main.py` (drop `--probe`/`--sender` plumbing,
  drop `ctx.probe = False` in the `all` aggregator),
  `ocdiag/core/context.py` (drop unused `probe` /
  `sender_open_id` fields), `ocdiag/channels/log_utils.py`
  (added subsystem extraction, channel classification,
  console-relay path helper), `tests/test_cli_help.py`,
  `skill/openclaw-diag/SKILL.md`.

Migration: callers that previously used `--probe` or `--sender` need
to replace those with manual log inspection or live platform-side
checks — credential validity is no longer surfaced through this
collector. Per-account scoping continues to work via
`--account <substring>`.

## v1.8.0 — Channel diagnostic collector (5 IM variants + active probe + self-pollution defense) (2026-06-10)

### Feature — single `channel` collector covering Feishu / Lark / DingTalk / WeCom

**Goal:** diagnose "bot not replying / no response" symptoms across IM
channel plugins. One unified collector per the design principle that
"channel" is one diagnostic dimension regardless of which IM variant
is installed; config / log / probe are layers within it, not separate
commands.

#### Variant detection — `ocdiag/channels/detect.py`
- Identifies which IM channel plugin variants are installed by walking
  `~/.openclaw/npm/projects/*/node_modules/` (strongest evidence) with
  `channels.<provider>` config keys as a fallback. Five variants
  recognized: `feishu-bundled` (`@openclaw/feishu` /
  `@m1heng-clawd/feishu`), `feishu-lark`
  (`@larksuite/openclaw-lark`), `dingtalk`
  (`@dingtalk-real-ai/dingtalk-connector`, accepts both
  `channels.dingtalk` and the canonical `channels.dingtalk-connector`
  config key), `wecom` (`@wecom/wecom-openclaw-plugin`). Multiple
  variants on the same host are diagnosed independently. Unknown
  variant or detection miss surfaces honestly as
  `NO_CHANNEL_DETECTED` rather than guessing.

#### Layered diagnosis — `ocdiag/channels/variants/{feishu_bundled,feishu_lark,dingtalk,wecom}.py`
Each variant module runs three layers per call:

- **L1 config** — credential completeness, connection-mode self-
  consistency, account-policy sanity (per upstream zod schema). Notable
  rules: `CRED_MISSING` (fail), `CONN_MODE_INCONSISTENT` /
  `BOT_WEBHOOK_INCONSISTENT` / `AGENT_AES_KEY_INVALID` (fail),
  `DM_POLICY_OPEN_NO_WILDCARD` (warn),
  `DM_POLICY_PAIRING_UNSUPPORTED` (warn — DingTalk silently degrades
  pairing to open at runtime), `WEBHOOK_PORT_DEFAULT` (warn),
  `GROUP_ALLOWFROM_LEGACY_CHATID` (warn — Lark deprecation),
  `DOMAIN_MISMATCH` (warn — Lark account claiming
  `domain="feishu"`), `GATE_SENDER_NOT_IN_ALLOWLIST` (warn — only
  when `--sender <open_id>` is passed, predicts whether that sender's
  DM will be silently dropped). WeCom dual-mode (Bot WS +
  Agent webhook) handled with strict per-mode rules. Feishu accounts
  inherit top-level fields per the runtime's resolution; checked here
  too.

- **L2/L3 log signature scan** — anchored on literal log strings
  extracted from each plugin's dist tree (NOT paraphrased). Coverage:
  - feishu-bundled: 21 signatures (WS lifecycle / drop reasons /
    pairing / probe timeout / webhook anomaly), with the SDK error
    constants (`reconnect exhausted`, `autoReconnect is disabled`)
    promoted from inside the plugin's outer log lines instead of
    matched standalone — earlier draft regexes anchored those
    constants directly with a `feishu[acct]:` prefix that never
    appears in real logs (false negative). Notable adds: `bot
    identity background retry exhausted` and `requireMention group
    messages stay gated until bot identity recovery succeeds` —
    both signal a silent skip of all requireMention group messages
    until restart.
  - feishu-lark: 17 signatures from `messaging/inbound/{gate,policy,
    permission,mention,dispatch}.ts`, `channel/monitor.ts`. Uses
    Lark-specific drops (different literal strings from bundled).
  - dingtalk: 16 signatures from `core/connection.ts` /
    `core/message-handler.ts` / `channel.ts` (Chinese strings:
    `DM 被拦截:` / `群聊被拦截:` / `重连失败：` / `dmPolicy="pairing"
    暂不支持` etc.).
  - wecom: 22 signatures (`[WeCom] Blocked DM ...`, `[wecom-agent]
    duplicate msgId ... skipped`, `[wecom] Media rejected`, MCP
    doc-auth-error, WS lifecycle including `WSAuthFailureError` /
    `WSReconnectExhaustedError` class names promoted to fail).
  Severity classes: connection-broken signals → fail (e.g.
  `bot_identity_retry_exhausted`, `WSAuthFailureError`); drops &
  policy violations → warn (so silent drops surface during
  diagnosis); informational/expected (pairing requests, dedup,
  empty messages) → ok.

- **L5 active credential probe** — only when `--probe` is passed.
  Hits the canonical token-introspection endpoint per platform
  (read-only, no message side-effects, no quota spend beyond a
  single auth call):
  - Feishu: `POST {domain}/open-apis/auth/v3/tenant_access_token/internal`
    (`open.feishu.cn` / `open.larksuite.com` / explicit https URL).
  - DingTalk: `POST https://api.dingtalk.com/v1.0/oauth2/accessToken`.
  - WeCom Agent: `GET https://qyapi.weixin.qq.com/cgi-bin/gettoken`.
  - WeCom Bot: NOT probed — no HTTP token endpoint exists; reports
    `WECOM_BOT_NO_PROBE_ENDPOINT` info honestly.
  Three result states: `valid` (live credential) / `invalid`
  (platform rejected, with the platform's own error code) /
  `unreachable` (network/timeout/DNS — explicitly NOT classified as
  invalid so a transient outage doesn't false-positive a credential
  rejection). 10s hard timeout per call.

#### Secret resolution — `ocdiag/channels/probe.py`
- OpenClaw stores credentials as `SecretRef` (`{source, provider,
  id}`). The public `openclaw config get` redacts these on purpose,
  so the probe path resolves them itself by reading
  `secrets.providers.<name>` and following the underlying store:
  - `source: env` → `os.environ[id]`
  - `source: file`, `mode: json` → JSON pointer (`id`) into the
    JSON file
  - `source: file`, `mode: singleValue` → entire file body trimmed
- `source: exec` is intentionally NOT supported (would spawn external
  process whose policy varies per host); reports
  `SECRET_UNRESOLVED` and skips the network call.
- Plaintext secrets are held in memory only for the urllib request
  itself — never copied into `Report.data`, `evidence`, or any
  output field. The variant report exposes only
  `ref:<source>:<provider>` metadata so a reader sees *where* a
  credential comes from, never *what* it is. Even secret length is
  intentionally not surfaced (length is a brute-force oracle for
  some token formats).

#### `--sender <open_id>` flag — `ocdiag/main.py` + `ocdiag/core/context.py`
Predicts whether a specific user's DM would be silently dropped by
the dmPolicy/allowFrom configuration. When absent the rule is
skipped (no false-positive on a hypothetical sender).

#### Self-pollution defense — `ocdiag/channels/log_utils.py`
Critical safety: OpenClaw gateway has a console-relay sink
(`/dist/console-*.js`) that captures the *assistant's own outbound
chat* into the same log stream. Without filtering, a chat message
that quotes a signature literal (e.g. discussing the channel
collector itself) would re-trigger the regex, including a recursive
"diagnostic caught a real failure" → "diagnostic caught the diagnostic's
report" loop. Defense: each scanner reads `_meta.path.fullFilePath`
and rejects entries from the gateway relay (`/dist/console-`); only
lines emitted from the plugin's own dist tree count.

#### Output schema
- Single envelope with sections per variant: `0. 检测概览`,
  `<variant> · L1 配置 (<detect_basis>)`,
  `<variant> · L2/L3 日志签名`, and (when `--probe`)
  `<variant> · L5 主动凭证探测`. Each account renders a one-line
  summary (`connectionMode`/`dmPolicy`/`groupPolicy`/`requireMention`/
  `domain`/`appId`/`appSecret` labels) followed by per-rule findings.
- Verdict mapping respects the existing legacy `status: ok|error`
  contract (warn folds to ok), with per-finding `verdict: ok|warn|fail`
  surfaced for finer-grained Agent consumption. Exit code rules
  unchanged.

#### Tests — `tests/test_channel.py`
103 collector tests (variant detection × 4, secret resolution × 7,
L1 rules × 17 across all variants, log signature regex × 32, probe
paths × 9, secret-leak sentinel × 1, end-to-end fixture flows ×
4 + 5 self-pollution reverse fixtures). Total suite: **242 passed,
1 skipped** (was 139 + 1).

### Skill — `skill/openclaw-diag/SKILL.md`
- Trigger description gains channel-relevant Chinese phrases ("飞书/
  钉钉/企微", "机器人不回", "消息没有响应", "channel 卡住").
- One Routing row added linking the symptom to `channel`.
- A new `## Channel` section documents the variant concept,
  `--probe`, `--sender`, and the silent-drop reading discipline.
  No structural reshuffle of Decision Ladder / Routing / Workflow
  per the established maintenance discipline.

## v1.7.0 — Full cron config surfacing + SQLite run-order fix (2026-06-09)

### Feature — full cron config in `cron_jobs` output
- **`ocdiag/collectors/cron_jobs.py`** — after the SQLite migration in
  OpenClaw 2026.6.x, `~/.openclaw/cron/jobs.json` is no longer the
  authoritative store, so users lost the ability to `cat` the file and
  see the full job configuration. The collector now surfaces every
  configured field for each job, regardless of data source (SQLite
  `cron_jobs.job_json` or legacy `cron/jobs.json`):
  - **JSON envelope** — each entry under `data.jobs[]` gains a new
    `config` sub-object with `enabled`, `agent_id`, `session_key`,
    `session_target`, `wake_mode`, `description`, `delete_after_run`,
    `created_at_ms`, the full `schedule` block, a `payload` block
    (`kind` / `model` / `fallbacks` / `thinking` / `timeout_seconds` /
    `tools_allow` / `allow_unsafe_external_content` / `light_context` /
    `message` / `message_len` / `text`), and a `delivery` block
    (`mode` / `channel` / `to` / `thread_id` / `account_id` /
    `best_effort` / `completion_mode` / `completion_to`). Empty / null
    fields are dropped to keep the JSON tidy. Existing fields
    (`id`, `name`, `status`, `schedule`, `success_rate`, `p50_ms`,
    `p95_ms`, `last_run_ts`, `next_run_ts`, `consecutive_errors`,
    `flags`) are preserved unchanged — `config` is purely additive.
  - **Pretty / detail block** — each per-job detail entry gains a
    compact `配置:` block under 调度 / 上次执行 / 下次执行 / 成功率 /
    耗时, with one line per group (`启用 / sessionTarget / wakeMode /
    agentId / sessionKey`, `payload: kind | model=… | timeout=…s |
    tools=[…]`, `delivery: mode → channel/to | thread=… | account=…`).
    Empty fields are skipped per line to keep output tight.
  - **Sensitive-text masking** — `payload.message` and `payload.text`
    pass through `ocdiag.sensitive.sanitize_text` before output, so
    any tokens / API keys embedded in a prompt body (`sk-ant-…`,
    `ghp_…`, `Bearer …`, `KEY=value` env-var shapes) are masked. The
    JSON envelope keeps the full sanitized text for `jq`-driven
    auditing; `message_len` records the *original* character count so
    truncation detection still works. The pretty block deliberately
    shows only a one-line ~200-char preview prefixed with
    `message(<n> 字):` — splatting a 14KB message into terminal output
    would be unreadable. `delivery.to` / `sessionKey` are
    open_id-style identifiers, not secrets, so they remain plain
    (consistent with the existing `delivery_to` collector behavior).
- No new flags or masking knobs — all surfacing reuses the existing
  `sanitize_text` helper. Both data sources share the same code path,
  since the SQLite `job_json` blob round-trips to the legacy job dict
  shape verbatim.

### Fix — SQLite cron run loader keeps most-recent 200 (not oldest)
- **`ocdiag/collectors/cron_jobs.py`** — `_fetch_sqlite_runs` was issuing
  `ORDER BY COALESCE(ts, 0) ASC, seq ASC LIMIT 200`, which kept the
  *oldest* 200 entries from `cron_run_logs` for a job. The downstream
  `_analyze()` consumer slices `finished[-20:]` to compute the recent
  success rate / P50 / P95 / drift / silent / delivery-failure metrics,
  and the legacy JSON loader (`_load_runs`) uses
  `deque(maxlen=200)` (i.e. *most recent* 200). Once a job's run log
  exceeded 200 entries, the SQLite path therefore analyzed "the tail of
  the oldest 200" — silently stale data divergent from the legacy
  semantics, with every aggregate reported wrong.
  Both query branches (with and without `store_key` filter) now
  `ORDER BY … DESC … LIMIT 200` (most recent N), then reverse the list
  in Python so the returned shape stays ts-ascending — `_analyze()` is
  unchanged. (v1.6.1 was prepared with this fix in the working tree but
  never published to npm; it's folded into 1.7.0.)

### Tests
- **`tests/run_cron_sqlite_tests.py`** — now runs 6 cases:
  - `[1/6]` SQLite-only path
  - `[2/6]` Legacy JSON fallback
  - `[3/6]` SQLite + legacy coexistence → `cron.source_inconsistency` warn
  - `[4/6]` >200 runs → keep most recent 200, ts-ascending (regression
    canary for the run-order fix: asserts loader truncation, recency,
    ordering, and that `_analyze()`'s `success_rate` reads `0%` for a
    failing tail rather than `100%` from a stale-ok prefix)
  - `[5/6]` Config surfacing via SQLite source — asserts the new
    `data.jobs[].config` block exists with all expected keys, that
    `payload.message_len` matches the original ~14KB length, that the
    full sanitized message survives in JSON, that an embedded fake
    `sk-ant-…` token is masked from both JSON and detail, that the
    pretty `配置:` block renders payload + delivery summary lines and
    a `message(<n> 字):` preview, and that `len(detail) < message_len`
    (full text never leaks)
  - `[6/6]` Same surfacing assertions for the legacy JSON source
- All other suites still pass: `run_collector_tests.py`,
  `run_sessions_tests.py`, `run_trajectory_tests.py`,
  `test_v2_collectors.py`, `test_cache_superset.py`,
  `test_performance_verdict.py`, `test_panorama.py`. Zero regressions.

## v1.6.0 — SQLite-aware cron_jobs collector + legacy fallback + #90072 inconsistency detection (2026-06-09)

### Feature
- **`ocdiag/collectors/cron_jobs.py`** — `_section_jobs` is now SQLite-aware.
  OpenClaw 2026.6.x moved the cron primary store from
  `~/.openclaw/cron/jobs.json` (+ sibling `jobs-state.json` and
  `runs/<id>.jsonl`) to a shared SQLite database at
  `~/.openclaw/state/openclaw.sqlite` (tables `cron_jobs` and
  `cron_run_logs`). The collector now opens that DB read-only
  (`file:…?mode=ro`, mirroring `task_health._open_db`), reads job
  definitions from `cron_jobs.job_json` and re-hydrates runtime state
  from the sibling `state_json` column (job_json's embedded `state` is
  always emptied by the migrator), and feeds per-job runs to the
  existing `_analyze()` from `cron_run_logs.entry_json` (which
  round-trips to the legacy per-line shape).
- **Legacy fallback** — when the SQLite store is missing, has no
  `cron_jobs` table, has no rows, or fails to open / parse for any
  reason, the collector falls back to the original
  `cron/jobs.json` + `jobs-state.json` + `runs/*.jsonl` flow with no
  behavioural change. Existing JSON envelope fields
  (`total_jobs`, `jobs[]`, `status_overview`, `max_consecutive_errors`,
  …) are preserved; the new `data.source` field reports `"sqlite"` |
  `"legacy-json"` | `"both"` | `"none"`.
- **#90072 inconsistency detection** — when the SQLite store has cron
  rows AND `cron/jobs.json` still parses with jobs, the collector
  reads from SQLite (authoritative) but emits a new
  `cron.source_inconsistency` warn surfacing
  `sqlite_job_count` vs `legacy_job_count` for the operator to
  reconcile (silent migration loss is the symptom of OpenClaw issue
  #90072). A separate `cron.store_key_mismatch` warn fires when the
  `cron_jobs.store_key` rows resolve to a different absolute path than
  the configured `OPENCLAW_CRON_JOBS` — the rows are still read, but
  the mismatch is logged.

### Robustness
- All SQLite operations are read-only and try/except-wrapped: a missing
  DB file, missing table, corrupt `job_json`/`state_json` blob, or
  unreadable `entry_json` row degrades gracefully — never crashes the
  collector. The dispatcher closes the DB before delegating to the
  shared analyzer, and the per-job run loader re-opens the read-only
  URI (cheap, keeps the lifetime short).
- Per-job run cap preserved at `_RUN_LIMIT_PER_JOB = 200`, matching
  the legacy `_load_runs()` `maxlen=200` budget — bounded memory on
  hot jobs.

### Paths
- **`ocdiag/paths.py`** — added `STATE_DB` constant
  (`OPENCLAW_STATE_DB` env override, default
  `$OPENCLAW_HOME/state/openclaw.sqlite`). The legacy `CRON_JOBS` /
  `CRON_STATE` / `CRON_RUNS_DIR` constants are unchanged because the
  fallback path still needs them.

### Tests
- New `tests/run_cron_sqlite_tests.py` (stdlib only): seeds a temp
  `openclaw.sqlite` with a minimal `cron_jobs` + `cron_run_logs`
  schema and asserts (a) pure-SQLite read produces `source="sqlite"`,
  correct job + run analysis, no inconsistency check; (b) SQLite
  missing falls back to `source="legacy-json"`; (c) SQLite + legacy
  `jobs.json` coexist → `source="both"`,
  `cron.source_inconsistency` fires, verdict ≥ warn, the stranded
  legacy-only job does NOT appear in `data.jobs`. Real OpenClaw state
  is never touched (each test stages a fresh `tempfile.mkdtemp` HOME).
- All existing suites still pass: `run_collector_tests.py` (20/20),
  `run_sessions_tests.py` (20/20), `run_trajectory_tests.py`,
  `test_v2_collectors.py`, `test_cache_superset.py`,
  `test_performance_verdict.py`, `test_panorama.py` (109 passed,
  1 skipped). Zero regressions.

## v1.5.2 — superset cache filter for windowed queries + gateway mtime prefilter & undated-run count fix (2026-06-08)

### Performance
- **`ocdiag/core/context.py`** — `DiagContext.collect_runs` now reuses the
  in-process full-scan cache to serve windowed queries instead of
  re-scanning disk. When a no-window full scan is already cached (the
  typical case in `all`, where `configuration` / `performance` populate it
  first), the gateway 24h / cron_jobs 7d / recent_errors 7d / environment
  14d windows are filtered IN MEMORY with the exact predicate from
  `trajectory.collect_runs` (`(not r.started_ts_ms) or (r.started_ts_ms >=
  since_ms)`). On a 227-file / 1847-run dataset this drops each windowed
  call from ~5s to <1ms — ≈5000× speedup, output-identical to a fresh
  scan (proved by `tests/test_cache_superset.py`).
- **`ocdiag/collectors/gateway.py`, `ocdiag/trajectory.py`,
  `ocdiag/core/context.py`** — standalone `gateway` (no full-scan cache to
  superset-filter) now opts into a file mtime prefilter for its 24h
  windowed scan. `discover_trajectory_files` accepts an optional
  `mtime_floor_ms` (backward-compatible, default = current behavior); the
  context computes the floor as `since_ms - 2h grace` to absorb clock
  skew. Cache key gains a `mtime_prefilter` dimension so prefiltered
  (subset) results never pollute the complete-set keys. In the `all`
  command the superset path takes precedence and the prefilter flag is a
  no-op — gateway's reported numbers are identical either way.

### Fix
- **`ocdiag/collectors/gateway.py`** — `_section_run_frequency`'s reported
  `runs_24h` count now excludes undated/incomplete runs (those without a
  `started_ts_ms`), matching its own hourly histogram which already skips
  them. Previously the count line and `data.runs_24h` inflated by the
  number of undated runs in the cached set; the histogram never agreed
  with the count line. On the reference dataset this drops `runs_24h`
  from 24 → 20 (4 undated runs no longer counted); the histogram and
  `data.run_frequency_24h` buckets are unchanged.

### Tests
- New `tests/test_cache_superset.py` (stdlib only): proves the superset
  reuse path is set-equal to a fresh `trajectory.collect_runs(files,
  since_ms=...)` for 24h / 7d / 14d windows including undated-run
  preservation, that windowed calls reuse Run objects from the full-scan
  cache (no re-parse), and that `mtime_prefilter` results never pollute
  the non-prefilter cache key.
- All existing suites pass: `run_trajectory_tests.py`,
  `run_collector_tests.py`, `run_sessions_tests.py`, `test_panorama.py`.
- `cron_jobs` / `recent_errors` `--json` output is byte-identical vs
  v1.5.1 (verified by capturing pre/post and diffing).

## v1.5.1 — route all human output through pager, not just `all` (2026-06-08)

### Fix
- **`ocdiag/main.py`** — `_render()` and `_dump_extract_records()` now route
  pretty-format output through `_paged_print()`. Previously the pager was
  wired into `cmd_all` only, so single collectors (e.g. `performance`) and
  inspectors (`panorama` / `trace` / `extract`) wrote directly to stdout —
  long human output blew past terminal scrollback. Both call sites now
  page identically to `all`.
- `extract` pretty mode pages the Report summary first and the per-file
  records dump second (two sequential pager sessions, by design — keeps the
  diff minimal and preserves the existing output ordering).

### Behavior preserved
- JSON / NDJSON modes still write directly — `--json | jq` and CI redirects
  are byte-identical to v1.5.0 (verified on `performance` against a
  pre-change baseline; only timestamps differ).
- `_paged_print()` guards are unchanged: non-TTY stdout (pipes, redirects,
  CI) and short output (≤ terminal height) still write directly. Set
  `PAGER=cat` to disable paging interactively.

## v1.5.0 — DiagContext trajectory cache + single-scan performance + skill-install --help/--dry-run safety (2026-06-08)

### Performance
- **`ocdiag/core/context.py`** — added per-invocation trajectory cache to
  `DiagContext`. New methods `trajectory_files()` and
  `collect_runs(*, since_ms=, limit_per_file=, populate_raw=)` memoize the
  expensive disk walk and JSONL parse on the ctx instance. Cache key is
  `(since_ms, limit_per_file, populate_raw)`; the file list is memoized
  separately. Returned lists are shallow copies so callers can `sort()`
  in place without mutating the cache.
- **`ocdiag/collectors/performance.py`** — `_section_trajectory_perf` and
  `_section_prompt_budget` now take `ctx` and route through `ctx.collect_runs()`.
  This collapses the previous **two full trajectory scans** in one
  `performance` run into a single shared scan.
- **`configuration.py`, `plugin_diag.py`, `run_health.py`** — migrated to
  `ctx.collect_runs()` / `ctx.trajectory_files()`. Together with `performance`,
  these five no-window callers now share one cached scan during
  `openclaw-diag all`.
- **`gateway.py` (24h), `cron_jobs.py` (7d), `recent_errors.py` (7d),
  `environment.py` (14d)** — migrated to `ctx.collect_runs(since_ms=...)`.
  Each window keeps its own cache slot (correct: distinct windowed callers
  must not share parsed lists).
- **`sessions_diag.py`** — uses the separate `collect_summaries` API and is
  intentionally untouched in this pass; a code comment notes it as a future
  cache dimension.

  Net effect on the `all` command: trajectory parse cost drops from ~9 scans
  to ~5 scans (1 shared no-window scan + 4 distinct windowed scans + 1 for
  `sessions_diag` summaries). On a perf-machine dataset of ~551MB, this
  removes roughly half of the trajectory-parse wall-clock; absolute speedup
  must be measured on real data, not on this dev host.

### Behavior fix
- **`bin/openclaw-diag.js`** — `skill-install --help` / `-h` now prints a
  short usage page and exits 0 **without** spawning the installer. Previously
  the wrapper passed argv straight to `scripts/install-skill.py`, which has
  no flag parsing and so `--help` actually performed an install.
- **`scripts/install-skill.py`** — added `--dry-run` support. When set, every
  installer prints the target path it WOULD write to and returns without
  creating directories or copying files. Detection is the same as the real
  installer (silently skips frameworks whose root dir is missing).

### Safety
- All write operations preserved their existing behavior — no collector
  output, verdict, or `data.*` field changes (verified by JSON diff against
  pre-change baseline; only timing/clock/disk drift remained).

## v1.4.21 — README: surface npm downloads, OpenClaw relationship, scope & masking at top (2026-06-05)

### Changed
- **`README.md`** — Front-of-readme upgrade so npm visitors see the four
  things that matter in the first screen:
  - Added a dynamic **npm downloads** badge (`img.shields.io/npm/dm/...`) next
    to the existing version badge — auto-updates from npm, no hardcoded
    numbers.
  - New **About & Scope** section, three compact bullets, placed between the
    nav links and `## Why openclaw-diag?`:
    - **Relationship to OpenClaw** — explicit "independent, community-maintained,
      NOT an official OpenClaw product, not affiliated" wording. Lists the
      on-disk artifacts read (`openclaw.json`, `/tmp/openclaw/*.log`,
      `agents/*/sessions/*.jsonl`, `cron/`, `tasks/runs.sqlite`) so the
      reader-only contract is concrete, not vague.
    - **Maintenance scope** — 13 modules + 3 inspectors, tracking current
      OpenClaw releases, zero runtime deps, plus an explicit out-of-scope
      list (no remediation, no config writes, no service restarts, no
      telemetry).
    - **Security & masking** — observer-only summary that mirrors (does not
      duplicate) the existing `## Security` section's masking policy:
      `extract` masked by default; `trace` / `panorama` unmasked by default,
      use `--mask` before sharing externally; config/log collectors always
      redact secrets. Links down to `## Security` for the full statement.
  No other README sections changed — `## Why`, `## Features`, `## Security`,
  flag tables, and architecture all stay intact.

## v1.4.20 — performance: decouple latency from availability verdict + 7-day mtime window for daily trend (2026-06-05)

### Fixed
- **`ocdiag/collectors/performance.py` · `_section_models`** — model-performance
  verdict no longer flags slow-but-healthy heavy models as `fail`. Latency and
  availability are now separate signals:
  - `fail` → only when `min_success_rate < 90%` across models with `calls >= 10`
    (real availability problem; `verdict_trigger="availability_critical"`).
  - `warn` → `min_success_rate < 95%` (`verdict_trigger="availability"`) **or**
    `max_p95 > 60s` while availability is fine (`verdict_trigger="latency"`).
  - `ok` → otherwise (`verdict_trigger="ok"`).
  Models with `calls < 10` still appear in detail/data but are excluded from
  the verdict-driving min computation (sample too small to trust). When no
  model qualifies, verdict falls back to latency-only and a high P95 is at
  most a `warn`. Output `data` adds `min_success_rate_pct`,
  `min_success_rate_model`, and `verdict_trigger`. Existing `models` /
  `model_p95_max` payload fields are unchanged.

  Real-world driver: `amazon-bedrock/claude-opus-4-6` on long agentic turns
  routinely runs P95=60–70s with 97–100% success rate. The previous rule
  (`max_p95 > 60 → fail`) misclassified that as a failure. Now the same
  trace warns on latency without crying availability fault.

- **`ocdiag/collectors/performance.py` · daily trend sampling** — `_section_daily_trend`
  now reads from an independent **7-day mtime window** of session files, not
  from the latest-20-files perf sample. Previously, a host with many sessions
  per day (e.g. 68 files total, ~30/day) had its 20-file perf sample skew
  toward today/yesterday; older days inside the rendered 7-day trend reported
  `0 calls` despite having tens of real model calls (observed: 06-03 reported
  `0` while the actual count was 48 cross-agent calls).

  New helper `_collect_session_files_by_window(sessions_base, days=7)` filters
  by `mtime >= now - days*86400` with no count cap. New helper
  `_parse_daily_stats(files)` does a lightweight per-line parse (timestamp +
  assistant marker + duration + output tokens only) so the 7-day scan stays
  cheap. The latest-20 perf window for `_section_models` /
  `_section_tools` / etc. is unchanged. Section detail/data now exposes
  `trend_file_count` ("数据来源: 7 天 mtime 窗口内 N 个 session 文件").

### Tests
- `tests/test_performance_verdict.py` — 11 new tests:
  - **Verdict matrix (8)**: high-P95 + 100% → warn-latency; high-P95 + 85% →
    fail-critical; low-P95 + 85% → fail-critical; low-P95 + 98% → ok; 92% +
    20s → warn-availability; calls<10 + 0% → ok (excluded); calls<10 + P95=70s
    → warn-latency only; multi-model → min picks worst.
  - **daily_trend window (3)**: 8-day-old files excluded; 1-day-old included;
    days outside latest-20 perf sample but inside 7-day window report real
    call counts (P50 derived from constructed durations); files outside the
    7-day window do not pollute daily_stats.

  Total suite: 139 passed / 1 skipped (up from 128 / 1; panorama suite
  unchanged at 109 / 1 within the total).

## v1.4.19 — SKILL.md upgraded to AI-agent diagnostic playbook (2026-06-05)

### Changed
- **`skill/openclaw-diag/SKILL.md`** rewritten from a command-reference + panorama
  description into an operating procedure for AI agents diagnosing OpenClaw.
  Frontmatter (`name`, `description` triggers, `metadata.requires`) is unchanged
  so existing skill-discovery hits keep working.
  - **Preflight** section added — tells the agent to confirm execution context
    (`--version`, `npx` fallback, ask for SSH/container access or explicit
    `--openclaw-home`/`--log-dir`/`--sessions-base`/`--config` when running
    off-host, run `doctor --format json` when uncertain) before diagnosing.
  - **Decision Ladder** added as the primary routing rule (no UUID + symptom →
    `all`; UUID + open-ended → `panorama`; UUID + specific message → `trace`;
    raw records → `extract`). The existing **Routing** table is kept as a
    quick lookup, with `Session stuck` reworded to `Specific message
    stuck/slow → trace` plus a new `Session health / why / 全貌 → panorama`
    row so the table no longer conflicts with the ladder.
  - **Panorama · empty correlated logs** — four-state interpretation guide
    aligned to the real check names emitted by `_render_section` in
    `panorama.py` (`logs.not_retained`, `logs.uncorrelated`, `logs.missing`,
    `logs.none`). Crucially `logs.none` is now described as the
    unknown-window degenerate fallback, not a generic "no correlated entries"
    catch-all.
  - **Masking policy** consolidated into a single global rule under Safety
    Rules (`trace`/`panorama` unmasked by default → pass `--mask` when
    sharing; `extract` masked by default → `--unmask` only for trusted local
    use; config/log collectors redact secrets). The per-command sections now
    reference the global rule instead of repeating it.
  - **Final answer contract** strengthened — added "do not present a root
    cause unless directly supported by checks/logs", "Evidence observed →
    Interpretation → Confidence / limits → Safe next checks" structure, and
    "do not recommend restart/config changes unless the user asks".
  - **De-versioned section descriptions** — Panorama section heading is now
    `Panorama sections currently include:` instead of pinning a specific
    version, and changelog-style historical notes ("Per-call duration was
    removed in v1.4.5", "v1.4.13 — 7 sections") are removed. Behavioral
    information the agent needs (Findings-first ordering, merged Correlated
    Logs & Signals, `--mask`, `--all-runs`, four empty-log states) is
    preserved.
  - **Common Pitfalls** section added at the end — exit code `1` is not a
    CLI failure; `ok=true` with `verdict=fail` is a real finding;
    `logs.not_retained` is a retention artefact, not absence of errors;
    `panorama` defaults to latest run only.
- Version bumped to **1.4.19** across `package.json`, `pyproject.toml`, and
  `ocdiag/__init__.py`. SKILL.md is shipped in the npm package, so the bump
  is required for the new playbook to reach installed Agent harnesses.

### Validation
- `python3 -m pytest tests/test_panorama.py -q` — 109 passed, 1 skipped, 0
  failed. SKILL.md is documentation only; the bump exercises
  `doctor.ocdiag` reading `__version__` cleanly.

## v1.4.18 — honest empty-correlation states (2026-06-05)

### Changed
- **Panorama · Correlated Logs & Signals** distinguishes three reasons
  the section can be empty instead of collapsing them into one
  ambiguous line. Before, a session whose log was already rotated out
  of `/tmp/openclaw/` rendered the same `no correlated log entries
  found` as a session whose log files were intact but never carried
  the `sessionId` — which made it impossible to tell "we lost the
  evidence" from "the evidence is here but the link is broken (likely
  bug)". The three states are now:
  1. `logs.missing` (warn) — `no app log files found in log_dir`. No
     `openclaw-*.log` at all. Unchanged from v1.4.17.
  2. `logs.not_retained` (ok) — every date the session window spans is
     missing from `log_dir`, but adjacent days exist (so `log_files`
     was non-empty via the ±1 day discovery margin). Renders as
     `session-window log not retained: 2026-06-03 (log_dir has:
     2026-06-04, 2026-06-05) — app_log correlation/timeline
     unavailable for this window`. Environmental, not a session
     fault — emitted as **ok** so it doesn't pollute the verdict's
     warn count.
  3. `logs.uncorrelated` (ok) — at least one window-date log file is
     present but no lines carry this sessionId/runId. Renders as `no
     correlated entries — session-date log(s) present (2026-06-05)
     but no lines carry this sessionId/runId`. This is the state worth
     investigating (logger version mismatch, strict-correlation miss).
  4. `logs.none` (ok) — the original "unknown window" fallback (when
     `window_start` and `window_end` are both 0). Preserved for the
     degenerate path so we never raise.
- Each new check carries structured `data`:
  `{window_dates_missing, window_dates_present, available_log_dates}`
  so JSON consumers can branch programmatically.

### Added
- `ocdiag.recent_logs.window_log_dates(log_dir, start_ms, end_ms)` —
  helper that classifies the dates the session window spans into
  present/missing relative to `log_dir`, and lists every available
  `openclaw-*.log` date. Reuses the existing `_filename_date` regex.

### Tests
- Five new regression cases in `tests/test_panorama.py`:
  - `test_logs_not_retained_state` — window day's file rotated away;
    adjacent days drag in a non-empty `log_files` set.
  - `test_logs_uncorrelated_state` — window day's file present, lines
    don't mention the sessionId.
  - `test_logs_missing_state_preserved` — empty `log_dir` keeps
    emitting `logs.missing` (warn).
  - `test_logs_unknown_window_falls_back_to_none` — `window=0` path
    doesn't raise.
  - `test_window_log_dates_helper` — direct helper unit test across
    multi-day windows + edge cases.

## v1.4.17 — readable Tool Execution layout (2026-06-05)

### Changed
- **Panorama · Tool Execution** is now scannable instead of dense. The
  v1.4.16 line `⚠ #4 edit 32889ms ✗ args={…} ⇒ ERR Validation failed for
  tool "edit":   - edits.0: must not have additional proper…` had four
  readability problems: (1) **double status glyph** — the human renderer
  already prepends ✓/⚠/✗ from `Check.verdict`, but the inspector also
  baked one into the message string, so each line shouted twice; (2) raw
  `NNNNNms` durations while every other section humanized; (3) result
  bodies were JSON pretty-printed indentation collapsed by `\n→space` and
  ended up as runs of three+ spaces; (4) `#idx`/name/dur jittered between
  rows, so the eye couldn't stripe down the duration column. The new
  rendering:
  ```
  ✓ 5 calls · 2 err · avg 44.9s · p50 32.9s · p95 32.9s · max 2.4m · slowest cron(2.4m)
  ✓ #1   Bash              199ms  {cmd=ls}  → ok
  ✓ #2   memory_search      4.8s  {query=lookup}
      → { "results": [], "disabled": true, "unavailable": true, "error": "No API key co…
  ⚠ #3   cron               2.4m  {action=update, jobId=abc}  ⇒ ERR patch required
  ⚠ #4   edit              32.9s  {path=/x}
      ⇒ ERR Validation failed for tool "edit": - edits.0: must not have additional pr…
  ✓ #5   sessions_history      ?  {limit=10}
  ```
  Specifically:
  - Per-call message no longer carries a status char — the section's
    glyph alone signals success/failure.
  - New `_fmt_tool_dur(ms)` renders <1s as `NNNms` (signal preserved),
    <60s as `X.Xs`, ≥1m as `X.Xm`. Used by both the per-call duration
    column and the timing summary's avg/p50/p95/max.
  - `#idx` (4-wide), tool name (16-wide ljust), duration (6-wide rjust).
    Names longer than the pad render flush rather than truncate.
  - Result/error <=48 chars and a header that still fits in 110 chars
    rides inline as `→ snippet` / `⇒ ERR snippet`. Anything longer drops
    to `Check.detail`, which the human renderer indents one continuation
    line — short calls stay on one row, long ones don't run off the right
    edge.
  - `_format_args_inline` and `_format_result_inline` now collapse
    runs of whitespace (the indent leftovers from `json.dumps(indent=2)`
    after newline→space substitution) to single spaces.
  - Timing summary uses `·` separators and tacks on `slowest NAME(DUR)`
    (pulled from `tool_stats.slowest`, which already existed in the
    JSON envelope).
  - Pending calls (no `toolResult` paired) render `?` as the duration
    and omit the result arrow entirely.

### Unchanged
- `report.data["tool_waterfall"]` and `report.data["tool_stats"]` JSON
  fields are byte-identical to v1.4.16 — only the human-readable
  `Check.message`/`Check.detail` strings changed. Consumers depending on
  the JSON envelope (e.g. masking tests, panorama-spec validators) keep
  working.
- Masking is unaffected: secret scrubbing happens upstream in
  `_build_tool_waterfall`; the renderer only re-formats whitespace.

### Tests
- Eight new regressions in `tests/test_panorama.py`:
  `test_fmt_tool_dur_helper`, `test_collapse_ws_helper`,
  `test_tool_execution_renders_humanized_durations`,
  `test_tool_execution_no_double_status_glyph`,
  `test_tool_execution_short_error_inline_long_in_detail`,
  `test_tool_execution_pending_call_renders_question_mark`,
  `test_tool_execution_summary_includes_slowest`,
  `test_tool_execution_compresses_json_whitespace`,
  `test_tool_execution_json_envelope_unchanged`,
  `test_tool_execution_columns_align_in_human_render`.
  Shared fixture `_build_tool_render_fixture` covers the full matrix
  (short success, long-JSON success, short error, long error, pending).

## v1.4.16 — exclude OpenClaw transcript-only injected turns from Model Calls (2026-06-05)

### Fixed
- **[P1] Panorama · Model Calls no longer counts OpenClaw transcript-only
  injected assistant turns as real model calls.** The previous release
  surfaced bogus rows like `delivery-mirror: 2 calls | avg_out=0.0 tok |
  stop[stop=2]` and `gateway-injected: …` because `_extract_model_calls`
  did not filter the two synthetic assistant kinds OpenClaw injects into
  the transcript: `delivery-mirror` (delivery mirror) and
  `gateway-injected` (gateway-side injection). Both are not LLM
  inferences and OpenClaw itself filters them at replay time. The
  inspector now drops any assistant turn where
  `provider == "openclaw"` and `model ∈ {delivery-mirror,
  gateway-injected}` before counting, so by-model breakdown, total
  tokens, `session_stats.model_calls`, and the rendered Model Calls
  section all stop double-counting injected transcript turns. The
  filter is applied early in extraction without updating
  `last_ts`, so the next real call's `duration_ms` is measured against
  the previous *real* boundary instead of being skewed by the
  intervening synthetic turn.
- Reference (OpenClaw 2026.6.1 dist):
  `dist/selection-DrXxngyT.js` (`TRANSCRIPT_ONLY_OPENCLAW_ASSISTANT_MODELS`),
  `dist/compaction-successor-transcript-CUmEvaGX.js`
  (`TRANSCRIPT_ONLY_OPENCLAW_MODELS`), and
  `docs/reference/transcript-hygiene.md` ("Replay filters OpenClaw
  delivery-mirror and gateway-injected assistant turns.").

### Tests
- New regression test
  `test_model_calls_exclude_openclaw_transcript_only_injections` in
  `tests/test_panorama.py`. The fixture mixes two real
  `amazon-bedrock` calls, one `provider=openclaw,model=delivery-mirror`
  injection, one `provider=openclaw,model=gateway-injected` injection,
  and a negative-case `provider=anthropic,model=delivery-mirror` row
  (must NOT be filtered, since the filter requires both fields).
  Asserts `model_calls`, `model_aggregate.models`,
  `session_stats.model_calls`, total token sums, the second real
  call's `duration_ms` (measured past the skipped injections), and
  the rendered text are all transcript-only-free.

## v1.4.15 — clarify per-command masking in README + --version on python entry (2026-06-05)

### Fixed
- **[P2] README Global Flags table no longer claims `default: sanitized` as a
  global rule.** That phrasing was misleading because masking defaults are
  per-command: `extract` is masked by default, `trace` and `panorama` are
  unmasked by default, and config/log state collectors always redact
  secrets. The table now lists both `--mask` and `--unmask` as opt-in flags
  whose default varies per command, with a one-line clarifier directly
  under the table — matching the existing language in the Security section,
  PANORAMA_SPEC.md, and SKILL.md.
- **[P3] `python3 bin/ocdiag --version` and the `ocdiag` console script now
  print the version.** Previously only the Node entry (`openclaw-diag
  --version`) handled the flag; the Python entry returned
  `Error: 未知命令 '--version'`. `main()` now intercepts `--version` /
  `-V` / `-v` / `version` before dispatch and prints the bare version
  number (byte-for-byte parity with the Node entry).

### Tests
- Regression tests in `tests/test_panorama.py` cover `main(["--version"])`
  and the `-V` / `-v` / `version` aliases via `capsys`, asserting `rc=0`
  and stdout equal to `__version__`.

## v1.4.14 — mask correlated logs + honor --openclaw-home for runs.sqlite/cron (2026-06-05)

### Fixed
- **[P1] `panorama --mask` now scrubs correlated log entries.** The previous
  release sanitized tool args/result text but left raw ERROR/WARN/INFO log
  bodies — and the `correlated_logs` JSON envelope copy — completely
  unmodified, so a `Bearer <secret>` or `sk-<live-key>` in a correlated
  application log line leaked verbatim under `--mask`. Both the rendered
  Section text (Raw ERROR / Raw WARN / representative INFO blocks) and the
  envelope list now go through `_maybe_sanitize` when `--mask` is active.
- **[P2] `--openclaw-home` is now honoured by `runs.sqlite` and
  `cron/runs/<job>.jsonl` lookup.** `_runs_sqlite_path()` and
  `_cron_run_path()` previously read the import-time `paths_mod.OPENCLAW_HOME`
  / `paths_mod.CRON_RUNS_DIR` constants, which only reflect the
  `OPENCLAW_HOME` env var, so a CLI-only `--openclaw-home <dir>` invocation
  silently missed both sources. They now derive the path from
  `ctx.openclaw_home` (env + CLI). The standalone `OPENCLAW_CRON_RUNS` env
  override still wins when set, preserving existing behaviour.

### Tooling / Docs
- **[P2] `pytest` declared as `[project.optional-dependencies].dev`.** The
  runtime `ocdiag` package remains zero-dep; contributors who run the
  pytest suites (`tests/test_panorama.py`, `tests/test_v2_*.py`) install
  with `pip install -e ".[dev]"`. README documents the test entry points.
- **[P3]** README architecture tree now lists `panorama` under
  `inspectors/`. SKILL.md Panorama section synced with v1.4.13 reality
  (Findings-first ordering, merged Correlated Logs & Signals, removed
  standalone Runtime Context / Model Decisions, removed per-call duration
  / longest gap). PANORAMA_SPEC.md masking section now correctly states
  `--mask` is opt-in (default is unmasked).

### Tests
- New regression tests covering the four mask render paths
  (envelope `correlated_logs`, raw ERROR, raw WARN, unmask sanity check)
  plus `--openclaw-home` routing for `runs.sqlite` and `cron/runs/<job>.jsonl`,
  including a guard that `OPENCLAW_CRON_RUNS` still wins when explicitly set.

## v1.4.13 — objective Findings summary + deterministic severity ranking (2026-06-04)

### Added
- **`Panorama · Findings`** — a new section rendered FIRST (before Session
  Overview). It is an objective, deterministic triage summary that
  re-surfaces the worst already-computed problem signals so the reader
  sees the punch list without scrolling. It does NOT invent anything:
  every line is a pure restatement of fields already on the source signal
  (timestamp, runId, finalStatus, raw flags, tool args, error text quoted
  verbatim) plus a `(see Correlated Logs & Signals)` evidence pointer.
  - First line: `verdict: <V> — <F> fail, <W> warn signals` (or
    `verdict: <V> — 0 problem signals`). The verdict is read from
    `report.verdict`, never recomputed.
  - Up to `FINDINGS_TOP_N` (= 10) ranked findings ordered by a strictly
    deterministic key: `(severity_class desc, kind_rank desc, ts asc,
    kind asc)` — fail-class before warn-class, higher rank first, ts
    tie-break, kind name as final tie-break.
  - `+N more (see Correlated Logs & Signals)` trailer when more than
    `FINDINGS_TOP_N` problem signals exist.
  - Verdict logic is unchanged — Findings is purely a re-surface.

### Changed
- **`SIGNAL_SEVERITY`** — a fixed module-level mapping
  `kind → (class, rank)` covering every signal kind `_health_signals`
  emits. Documents the severity contract in one place so the ordering is
  traceable. Unknown kinds default to `("warn", 0)` so a new kind added
  later still surfaces (just below catalogued entries) — no silent drop.
  The single per-kind promotion is `retried_after_failure` upgrading
  `warn → fail` when its `final_failed` is true, matching the
  render-time `.fail()` call.
- **Problem signals under Correlated Logs & Signals are now sorted by
  the same key.** Previously they rendered in insertion order; now the
  detail list reads in severity order so it matches the Findings summary
  at the top. Positive `✓` signals and raw ERROR/WARN/INFO log rendering
  are untouched (positives first, then problems, then raw logs).
- **JSON envelope** — `report.data["findings"]` (ordered list of
  `{severity, kind, ts_ms, summary, ref}` dicts) and
  `report.data["findings_more_count"]` (tail count). `health_signals[]`
  is unchanged in content but is now ordered by the deterministic key.

### Spec
- `PANORAMA_SPEC.md` updated: section count 6 → 7, new §0 documenting
  Findings, new "Findings — objective-only contract" sub-section listing
  the forbidden subjective vocabulary, new "Severity classification"
  sub-section pinning the `SIGNAL_SEVERITY` table.

### Tests
- `test_findings_section_first_and_verdict_line` — section is at index 0
  and its verdict counts equal the underlying signal totals exactly.
- `test_findings_ordered_by_deterministic_severity_key` — direct
  exercise of `_signal_sort_key` against a mixed fail/warn/ts fixture;
  pins the expected order.
- `test_findings_cap_and_more_line` — cap respected; `+N more` carries
  the right tail count.
- `test_findings_summary_lines_have_no_forbidden_words` — the
  objective-only contract is mechanically enforced. Scans every rendered
  Findings line for `root cause`, `caused`, `because`, `likely`,
  `probably`, `suggest`, `should`, `recommend`, `investigate`,
  `most significant`, `due to`, `appears`, `seems` (case-insensitive)
  and asserts none appear.
- `test_findings_ok_fixture_says_no_problem_signals` — zero problem
  signals → exactly the line `no problem signals`, no fabricated praise.
- `test_correlated_logs_problem_signals_severity_ordered` — fail-class
  signal must precede warn-class signal in the rendered details.
- `test_findings_in_json_envelope` — shape check of `findings` and
  `findings_more_count`.
- `test_findings_top_n_constant_is_module_level`,
  `test_signal_severity_table_documented` — guard against silent
  catalogue regressions.
- Existing `test_no_standalone_health_signals_section` updated for the
  new section count (6 → 7).

### Validation
- 104 tests green. 100k-line perf test still passes. Real e2e against
  `e37602da-ce25-45c6-97d9-2cffa237d1ba --all-runs` produces a
  Findings section with verdict line `verdict: WARN — 0 fail, 27 warn
  signals`, ordered top-10 problem signals, and `+17 more`. Forbidden
  vocabulary scan over the rendered Findings text returns zero hits.

## v1.4.12 — restore representative INFO, raise cap 5 → 20 (2026-06-04)

### Changed
- **Restored the representative-INFO block** in the merged Correlated Logs &
  Signals section, and raised the cap from 5 to `REPRESENTATIVE_INFO_LINES`
  (=20). v1.4.11 had removed it; that was wrong — on a window with no
  ERROR/WARN, a few representative INFO lines (lifecycle/tool/model
  boundaries, spanning head→middle→tail) are exactly what shows "what
  happened" on an otherwise-quiet run. Renders only when there are no
  ERROR/WARN entries; OK-level (✓), no verdict effect.
- Fixed an indentation bug in the restored block (it was nested under
  `if warn_entries:`, so it never fired when there were zero WARN entries
  — the common clean case). It now sits at else-body scope, firing exactly
  when both ERROR and WARN are empty. Verified on a real session: a window
  with 277 INFO / 0 ERROR / 0 WARN now renders 20 INFO lines.

### Tests
- Replaced the v1.4.11 "INFO removed"/"helper removed" tests with
  `test_correlated_logs_renders_representative_info_when_no_err_warn` and
  `test_representative_logs_present_and_capped` (asserts cap 20, non-empty,
  never exceeds cap).

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
