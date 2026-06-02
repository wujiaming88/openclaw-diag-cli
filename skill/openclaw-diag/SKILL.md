---
name: openclaw-diag
description: "OpenClaw diagnostics CLI. Triggers: system slow, stuck, errors, session trace, health check, performance, cron not firing, plugin issues. 中文触发：系统慢、卡住、报错、定时任务没触发、插件异常、网关连不上、session 卡死、性能差。"
metadata:
  requires:
    bins: ["openclaw-diag"]
---

# openclaw-diag

Read-only OpenClaw diagnostic CLI. Diagnostic commands are observer-only and never modify system state.

## Safety

- Trajectory-sourced fields (message content, tool output) are **plaintext by default**. When sharing output externally or posting to chat, always use `--mask`
- `extract` is masked by default; use `--unmask` only for trusted local analysis
- Some collectors perform read-only DNS/TCP/HTTP probes for connectivity checks; no POST/PUT/DELETE

## Routing

| Symptom | Command |
|---------|---------|
| 全面检查 | `openclaw-diag all --format json` |
| 慢 | `openclaw-diag performance --format json` |
| Gateway / 连不上 | `openclaw-diag gateway --format json` |
| Session 卡死 | `openclaw-diag trace <uuid> --format json --mask` |
| 看 session 内容 | `openclaw-diag extract <uuid> --summary --format json` |
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
openclaw-diag trace <uuid> --format json --mask       # safe for sharing
openclaw-diag trace <uuid> --msg-index 0 --format json --mask
openclaw-diag trace <uuid> --msg-match X --format json --mask
```

## Extract

```bash
openclaw-diag extract <uuid> --format json            # masked by default
openclaw-diag extract <uuid> --summary --format json  # stats only
```

## Errors

`{ok:false, error:{code, message, retryable, hint}}`

Exit: 0=ok, 1=warn/fail, 2=input error, 3=runtime error
