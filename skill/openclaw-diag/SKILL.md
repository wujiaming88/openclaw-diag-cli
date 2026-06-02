---
name: openclaw-diag
description: "OpenClaw diagnostics. Triggers: system slow, stuck, errors, session trace, health check, performance, cron not firing, plugin issues."
metadata:
  requires:
    bins: ["openclaw-diag"]
---

# openclaw-diag

Read-only OpenClaw diagnostic CLI. Never modifies system state.

## Routing

| Symptom | Command |
|---------|---------|
| 全面检查 | `openclaw-diag all --format json` |
| 慢 | `openclaw-diag performance --format json` |
| Gateway / 连不上 | `openclaw-diag gateway --format json` |
| Session 卡死 | `openclaw-diag trace <uuid>` |
| 看 session 内容 | `openclaw-diag extract <uuid>` |
| 最近报错 | `openclaw-diag recent_errors --format json` |
| Cron 没触发 | `openclaw-diag cron_jobs --format json` |
| 插件异常 | `openclaw-diag plugin_diag --format json` |

## Output format

- `--format json` → `{ok, data:{module, verdict, summary}, error}`
- `--format ndjson` → one JSON line per section
- Default (TTY) → colored human output
- `--json` = `--format json`

## Trace

```bash
openclaw-diag trace <uuid>               # last user msg
openclaw-diag trace <uuid> --msg-index 0 # first
openclaw-diag trace <uuid> --msg-match X # by content
```

## Extract

```bash
openclaw-diag extract <uuid>             # full dump
openclaw-diag extract <uuid> --summary   # stats only
```

## Errors

`{ok:false, error:{code, message, retryable, hint}}`

Exit: 0=ok, 1=warn/fail, 2=input error, 3=runtime error

## Install skill

```bash
openclaw-diag skill-install
```
