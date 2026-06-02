---
name: openclaw-diag
description: "OpenClaw diagnostics CLI. Use when system is slow, stuck, erroring, or for session inspection/tracing."
metadata:
  requires:
    bins: ["openclaw-diag"]
---

# openclaw-diag

Observer-only diagnostic CLI for OpenClaw. Zero dependencies, read-only, never modifies system state.

## Intent Routing

| Symptom | Command |
|---------|---------|
| General health check | `openclaw-diag all --format json` |
| Slow responses | `openclaw-diag performance --format json` |
| Gateway issues / can't connect | `openclaw-diag gateway --format json` |
| Session stuck / hung | `openclaw-diag trace <uuid> --format json` |
| Inspect session content | `openclaw-diag extract <uuid>` |
| Recent errors | `openclaw-diag recent_errors --format json` |
| Cron not firing | `openclaw-diag cron_jobs --format json` |
| Plugin problems | `openclaw-diag plugin_diag --format json` |
| Environment/version info | `openclaw-diag environment --format json` |

## Output

- Default (TTY): human-readable colored output
- `--format json`: structured `{ok, data:{module, verdict, summary, sections}, error}` envelope
- `--format ndjson`: one JSON line per section (for streaming/pipes)
- `--json` is alias for `--format json` (backward compat)

## Verdicts

- `ok`: all checks pass
- `warn`: advisory issues found
- `fail`: critical problems detected

## Key commands

### Full scan
```bash
openclaw-diag all --format json
```

### Trace a message lifecycle
```bash
openclaw-diag trace <session-uuid>                    # last user message
openclaw-diag trace <uuid> --msg-index 0              # first message
openclaw-diag trace <uuid> --msg-match "deploy"       # by content
```

### Extract session content
```bash
openclaw-diag extract <uuid>              # full records dump
openclaw-diag extract <uuid> --summary    # stats only
openclaw-diag extract <uuid> --all        # include backups
```

### Quick jq recipes
```bash
# Get all verdicts
openclaw-diag all --format json | jq -r '.data.verdict'

# Find failing modules
openclaw-diag all --format json | jq 'select(.data.verdict != "ok") | .data.module'

# Model P95 latency
openclaw-diag performance --format json | jq '.data.data.model_p95_max'
```

## Error handling

JSON errors return:
```json
{"ok": false, "error": {"code": "SESSION_NOT_FOUND", "message": "...", "retryable": false, "hint": "..."}}
```

Exit codes: 0=ok, 1=warn/fail, 2=input error, 3=runtime error
