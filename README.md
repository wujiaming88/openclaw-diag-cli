# openclaw-diag

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![npm version](https://img.shields.io/npm/v/openclaw-diag-cli.svg)](https://www.npmjs.com/package/openclaw-diag-cli)
[![npm downloads](https://img.shields.io/npm/dm/openclaw-diag-cli.svg)](https://www.npmjs.com/package/openclaw-diag-cli)
[![Node.js](https://img.shields.io/badge/node-%3E%3D18-blue.svg)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/python-%3E%3D3.8-blue.svg)](https://www.python.org/)

Observer-only diagnostic CLI for [OpenClaw](https://github.com/openclaw/openclaw) — built for humans and AI Agents. 13 diagnostic modules, 3 inspectors, structured output, zero dependencies.

[Install](#installation) · [Quick Start](#quick-start) · [Commands](#commands) · [AI Agent Skill](#ai-agent-skill) · [Output Formats](#output-formats) · [Examples](#examples) · [Contributing](#contributing)

## About & Scope

- **Relationship to OpenClaw** — Independent, community-maintained companion tool for [OpenClaw](https://github.com/openclaw/openclaw). NOT an official OpenClaw product and not affiliated with the OpenClaw maintainers. It reads OpenClaw's on-disk artifacts (`openclaw.json`, `/tmp/openclaw/*.log`, `agents/*/sessions/*.jsonl`, `cron/`, `tasks/runs.sqlite`) — it does not embed or modify OpenClaw itself.
- **Maintenance scope** — Covers 13 diagnostic modules + 3 session inspectors (trace / extract / panorama), tracking current OpenClaw releases. Zero runtime dependencies (Python stdlib + Node thin-shell). Out of scope by design: no remediation, no config writes, no service restarts, no telemetry — diagnosis only.
- **Security & masking** — Observer-only: read-only file reads plus read-only DNS/TCP/HTTP probes (no POST/PUT/DELETE, no state mutation). Masking is per-command: `extract` is masked by default; `trace` and `panorama` are unmasked by default (pass `--mask` before sharing externally); config/log collectors always redact API keys / tokens / secrets. No data leaves the host. See [Security](#security) for details.

## Why openclaw-diag?

- **Agent-Native Design** — Structured JSON output, error codes, explicit verdicts. AI Agents can diagnose OpenClaw systems with zero extra parsing
- **Zero Dependencies** — Pure Python stdlib + Node thin-shell. No pip packages, no build steps
- **Observer-Only** — Never modifies system state. Safe to run anytime, anywhere
- **Explicit Verdicts** — Every check produces `ok` / `warn` / `fail` with clear thresholds, not regex-guessed
- **13 Diagnostic Modules** — From system health to model performance to cron jobs, comprehensive coverage
- **Session Forensics** — Trace a single message's full lifecycle, or extract entire session history
- **Default Sanitization** — API keys, tokens, secrets masked in config/log output. Trajectory-sourced fields are plaintext by default; use `trace --mask` when sharing output

## Features

| Module | What it checks |
|--------|----------------|
| 🖥️ sys_health | Disk, memory, CPU, DNS, NTP, network, IO wait |
| ⚙️ environment | OpenClaw version, Node/Python, env vars, version drift (14d) |
| 📋 configuration | openclaw.json validation, sensitive field sanitization |
| 🌐 gateway | Process status, port, restarts, WebSocket lifecycle, error codes, model API endpoints |
| ❗ recent_errors | Log aggregation by level, journalctl, session tool-call errors |
| ⏰ cron_jobs | Schedule parsing, consecutive failures, drift, silent detection |
| 📊 performance | Model/tool P50/P95, slow calls Top N, cache hit rate, throughput |
| 💬 sessions_diag | Session count, sizes, stuck detection |
| 🔌 plugin_diag | Plugin status, hook errors, DNS, trajectory drift |
| 🏃 run_health | 24h/7d/30d run health, abort rate, active leak detection |
| 📋 task_health | Task/subagent success rate, failures, timeouts, stuck detection |
| 🐚 shell_history | Dangerous command detection in shell history |
| 🩺 doctor | Environment self-check (Node, Python, OpenClaw, paths) |

| Inspector | Purpose |
|-----------|---------|
| 🔍 trace | Trace one user message: receive → model → tools → response timeline |
| 📤 extract | Export session.jsonl to readable format with stats |
| 🌐 panorama | Session 360° diagnosis: all correlated data from every source |

## Installation

**Requirements:** Node.js 18+, Python 3.8+

```bash
# Option 1: Run directly with npx (recommended)
npx openclaw-diag-cli all

# Option 2: Install globally
npm install -g openclaw-diag-cli
openclaw-diag all

# Option 3: Install AI Agent skill
npx openclaw-diag-cli skill-install
```

## Quick Start

```bash
# Full health check
openclaw-diag all

# Something slow? Check model performance
openclaw-diag performance

# Session stuck? Trace the last message
openclaw-diag trace <session-uuid>

# Full session 360° diagnosis
openclaw-diag panorama <session-uuid>

# See all session content
openclaw-diag extract <session-uuid>

# JSON output for scripts / AI Agents
openclaw-diag all --format json
```

## Quick Start (AI Agent)

If you are an AI Agent helping a user diagnose OpenClaw:

```bash
# 1. Install (if not already)
npx openclaw-diag-cli skill-install

# 2. Run full diagnostic
openclaw-diag all --format json

# 3. Drill into specific issues based on verdict
openclaw-diag gateway --format json      # if gateway verdict != ok
openclaw-diag performance --format json  # if performance verdict != ok
```

## Commands

### State Collectors (no arguments needed)

```bash
openclaw-diag all                    # Run all collectors
openclaw-diag all --skip gateway,cron_jobs  # Skip specific modules
openclaw-diag <module-id>            # Run single module
openclaw-diag list                   # List all available modules
openclaw-diag doctor                 # Environment self-check
openclaw-diag examples               # Show usage examples
```

### Inspectors (require session UUID)

```bash
# Trace: follow one message's lifecycle
openclaw-diag trace <uuid>                    # Last user message
openclaw-diag trace <uuid> --msg-index 0      # First message
openclaw-diag trace <uuid> --msg-match "deploy"  # Match by content
openclaw-diag trace <uuid> --no-trajectory    # Skip trajectory enrichment

# Extract: dump session content
openclaw-diag extract <uuid>                  # Full records
openclaw-diag extract <uuid> --summary        # Stats only
openclaw-diag extract <uuid> --all            # Include backups/deleted
openclaw-diag extract <uuid> --types message  # Filter by record type

# Panorama: 360° session diagnosis
openclaw-diag panorama <uuid>                 # Latest run (default)
openclaw-diag panorama <uuid> --all-runs      # All runs in session
openclaw-diag panorama <uuid> --strict-correlation  # Only sessionId/runId matches
openclaw-diag panorama <uuid> --unmask        # Show full tool args/results
openclaw-diag panorama <uuid> --format json   # JSON output for programmatic use
```

### Utility

```bash
openclaw-diag skill-install          # Deploy skill to AI Agent frameworks
openclaw-diag --version              # Show version
```

## Output Formats

```bash
--format pretty    # Colored human output (default when TTY)
--format json      # Structured JSON envelope
--format ndjson    # One JSON line per section (streaming/pipes)
--json             # Alias for --format json (backward compat)
```

### JSON Envelope

Success:
```json
{
  "ok": true,
  "data": {
    "module": "gateway",
    "verdict": "warn",
    "summary": {"pass": 5, "warn": 1, "fail": 0, "total": 6},
    "elapsed_ms": 1234,
    "sections": [...],
    "data": {...},
    "status": "ok"
  },
  "error": null
}
```

Error:
```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "SESSION_NOT_FOUND",
    "message": "找不到 session 'abc123'",
    "retryable": false,
    "hint": "recent sessions: 7e9f3b31, b371118e"
  }
}
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success, verdict = ok |
| 1 | Success, verdict = warn or fail |
| 2 | User input error (bad command, missing arg) |
| 3 | Runtime error (file unreadable, crash) |

## AI Agent Skill

openclaw-diag ships with a [SKILL.md](./skill/openclaw-diag/SKILL.md) compatible with multiple AI Agent frameworks.

### Install

```bash
openclaw-diag skill-install
```

This deploys the skill to all detected frameworks:

| Framework | Path |
|-----------|------|
| OpenClaw | `~/.openclaw/skills/openclaw-diag/SKILL.md` |
| Claude Code | `~/.claude/commands/openclaw-diag.md` |
| Codex | `~/.codex/instructions/openclaw-diag.md` |
| Cursor | `~/.cursor/rules/openclaw-diag.mdc` |

### Intent Routing (for Agents)

| Symptom | Command |
|---------|---------|
| General health check | `openclaw-diag all --format json` |
| Slow responses | `openclaw-diag performance --format json` |
| Can't connect / Gateway down | `openclaw-diag gateway --format json` |
| Session stuck | `openclaw-diag trace <uuid> --format json` |
| Full session health check | `openclaw-diag panorama <uuid> --format json` |
| Recent errors | `openclaw-diag recent_errors --format json` |
| Cron not firing | `openclaw-diag cron_jobs --format json` |
| Plugin issues | `openclaw-diag plugin_diag --format json` |

## Examples

```bash
# Quick health summary with jq
openclaw-diag all --format json | jq -r '.data | "\(.module): \(.verdict)"'

# Find modules with problems
openclaw-diag all --format json | jq 'select(.data.verdict != "ok") | .data.module'

# Model P95 latency
openclaw-diag performance --format json | jq '.data.data.model_p95_max'

# Trace a slow message
openclaw-diag trace abc12345 --msg-index 0

# Export session and pipe to file
openclaw-diag extract abc12345 > session-dump.txt

# Full session panorama — is it slow? tools ok? model perf? stuck?
openclaw-diag panorama abc12345
openclaw-diag panorama abc12345 --all-runs --format json

# NDJSON for monitoring pipeline
openclaw-diag all --format ndjson | while read line; do
  echo "$line" | jq -r 'select(.verdict != "ok") | "\(.module)/\(.section): \(.verdict)"'
done
```

## Architecture

```
bin/
  openclaw-diag.js     Node thin-shell (npx entry, spawns Python)
  ocdiag               Python direct entry
ocdiag/
  main.py              CLI dispatch + argument parsing
  core/                Types (Check/Section/Report/Verdict), registry, context
  collectors/          13 state collectors (one file each, @register decorator)
  inspectors/          trace + extract + panorama
  render/              human / json / ndjson renderers
  (shared utilities)   sessions, trajectory, sensitive, paths, ...
skill/
  openclaw-diag/       Agent skill (SKILL.md only)
scripts/
  install-skill.py     Deploys skill to agent frameworks
```

Adding a new collector: create one file in `ocdiag/collectors/`, add `@register` — done. No other files to edit.

## Global Flags

| Flag | Effect |
|------|--------|
| `--format pretty\|json\|ndjson` | Output format |
| `--json` | Alias for `--format json` |
| `--no-color` | Disable ANSI colors |
| `--mask` | Redact secrets/args/log bodies (opt-in; default varies by command) |
| `--unmask` | Show full plaintext (opt-in; default varies by command) |
| `--config PATH` | Custom openclaw.json path |
| `--log-dir PATH` | Custom log directory |
| `--sessions-base PATH` | Custom sessions base directory |
| `--openclaw-home PATH` | Custom OpenClaw home directory |

Masking default is per-command: `extract` is masked by default (`--unmask` for trusted local analysis); `trace` and `panorama` are unmasked by default (pass `--mask` before sharing output externally). Config/log state collectors always redact API keys/tokens/secrets regardless of flag.

## Security

- **Observer-only diagnostics**: diagnostic commands never write, delete, or modify OpenClaw state. `skill-install` writes skill files to agent framework paths by explicit request
- **Default sanitization**: API keys, tokens, secrets are masked in config/log collectors. Trajectory-sourced free-form fields (message content, tool output) are plaintext by default — use `trace --mask` when sharing output externally; `extract` is masked by default, use `--unmask` only for trusted local analysis
- **Read-only probes**: some collectors perform DNS lookups, TCP connects, or HTTP GET/HEAD probes (sys_health, gateway, plugin_diag) for connectivity checks. No POST/PUT/DELETE, no service restarts, no runtime state mutation
- **No dependencies**: no supply-chain attack surface beyond Node.js + Python stdlib

## Contributing

Issues and PRs welcome. For new collectors:

1. Create `ocdiag/collectors/your_module.py`
2. Use `@register` decorator
3. Return `Report` with explicit `Verdict` on every `Check`
4. Run `python3 -m ocdiag.main your_module` to test
5. Add tests in `tests/`

### Running tests

The runtime `ocdiag` package is zero-dep. Tests fall into two groups:

- **Stdlib-only** (no install needed) —
  ```bash
  python3 tests/run_collector_tests.py
  python3 tests/run_sessions_tests.py
  python3 tests/run_trajectory_tests.py
  ```
- **pytest** (`tests/test_panorama.py`, `tests/test_v2_*.py`) — install the
  optional `dev` extras first, then run pytest:
  ```bash
  pip install -e ".[dev]"
  pytest tests/
  ```

`pytest` is declared under `[project.optional-dependencies].dev` in
`pyproject.toml`; it is **not** a runtime dependency.

## License

[MIT](./LICENSE)
