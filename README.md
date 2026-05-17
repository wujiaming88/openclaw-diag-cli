# OpenClaw 诊断工具箱

> 排查 OpenClaw 故障的 observer-only CLI。一组诊断、一个入口、零依赖。
> **Observer-only**：不改变被诊断系统的状态；可发只读探测请求、可写诊断输出。

## 安装

```bash
# 一次性运行（无需安装，npm 缓存后离线可用）
npx openclaw-diag-cli

# 装到 PATH
npm install -g openclaw-diag-cli
openclaw-diag
```

依赖：Node 18+ 和 Python 3.8+。

## 五分钟上手

```bash
# 1. 看看能做什么
openclaw-diag

# 2. 检查环境是否就绪
openclaw-diag doctor

# 3. 跑某个诊断
openclaw-diag gateway

# 4. 全部 state collectors 跑一遍（任一崩了不影响其他）
openclaw-diag all

# 5. 输出结构化 JSON
openclaw-diag gateway --json
```

## 诊断列表

诊断按"是否需要参数"分两类。

### State collectors（无需参数，扫一遍系统当前状态）

| 诊断 | 看什么 |
|---|---|
| `sys_health` | DNS / 网络 / CPU / 内存 / 磁盘 / IO / 进程 / 时间同步 |
| `environment` | OpenClaw 版本一致性、Gateway 进程环境变量 |
| `configuration` | `openclaw.json` 展平（敏感字段已脱敏） |
| `gateway` | Gateway 进程、端口、24h 启停、WS 生命周期、错误码 |
| `recent_errors` | 应用日志 / journalctl / session 工具调用错误聚合 |
| `cron_jobs` | 定时任务状态、连续失败、调度漂移、静默检测 |
| `performance` | 模型/工具耗时 P50/P95、慢调用 Top 20、E2E 延迟、Cache 命中率 |
| `sessions` | Session 总览、活跃度、Stuck 探测 |
| `plugin_diag` | 插件状态一致性、ERROR/WARN、Hook 异常、Channel、外部依赖 DNS |
| `shell_history` | 高危命令、openclaw 命令、最近操作 |

### Object inspectors（需要 session uuid，深挖一个具体对象）

| 诊断 | 看什么 |
|---|---|
| `trace <uuid>` | 追踪一条用户消息从进入到响应的完整时间轴 |
| `extract <uuid>` | 导出 session.jsonl 为可读格式（reset / bak / deleted 全状态） |

### Meta

| 命令 | 作用 |
|---|---|
| `openclaw-diag all` | 跑全部 state collectors |
| `openclaw-diag list` | 列出所有诊断 |
| `openclaw-diag doctor` | 检查 Node / Python / ocdiag / OpenClaw 环境 |
| `openclaw-diag bundle <id>` | 打成 self-contained 单文件 .py |

## 常见配方

```bash
# 找出哪个 cron 任务在连续失败
openclaw-diag cron_jobs --json | jq '.data.jobs[] | select(.status!="ok")'

# 看哪个模型的 P95 延迟最高
openclaw-diag performance --json | jq '.data.models | to_entries | sort_by(-.value.p95_s) | .[0:3]'

# 哪些插件今天有 ERROR
openclaw-diag plugin_diag --json | jq '.data.plugin_errors | to_entries[] | select(.value.error_count > 0)'

# 把所有诊断聚合成单个 JSON 报告（含错误行 — 公理 #4）
openclaw-diag all --json 2>/dev/null | jq -s '.' > report.json

# 找出有 stuck session 的事件
openclaw-diag sessions --json | jq '.data.stuck_sessions'

# 追踪用户消息时间轴
openclaw-diag trace <session-uuid> --msg-index 0

# 导出 session 为可读格式（默认脱敏 — 公理 #7）
openclaw-diag extract <session-uuid> --summary

# 同上但保留原文（含潜在 secret）
openclaw-diag extract <session-uuid> --unmask
```

## 离线机器：bundle 出单文件

```bash
# 在有网的机器
openclaw-diag bundle gateway > standalone-gateway.py

# scp 到目标机器（只需要 Python 3.8+，无需安装任何东西）
scp standalone-gateway.py prod-server:/tmp/
ssh prod-server "python3 /tmp/standalone-gateway.py --json"
```

`bundle` 会把脚本和它依赖的共享代码合并成一个 self-contained `.py`，零依赖。

## 配置覆盖

诊断别人机器或容器时，无需改代码：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `OPENCLAW_HOME` | `~/.openclaw` | OpenClaw 主目录 |
| `OPENCLAW_CONFIG` | `$OPENCLAW_HOME/openclaw.json` | 配置文件 |
| `OPENCLAW_LOG_DIR` | `/tmp/openclaw` | 日志目录 |
| `OPENCLAW_SESSIONS` | `$OPENCLAW_HOME/agents` | Session 根 |

也可以用 `--config /path/to/file --log-dir /path/to/logs` 覆盖单次。

## 退出码

| rc | 含义 |
|---|---|
| 0 | 诊断成功 |
| 1 | 诊断运行成功但报告 `status: "error"`（数据源缺失等） |
| 2 | 诊断崩溃（已隔离，不影响 `all`） |

## 设计原则（7 条公理）

| | |
|---|---|
| **#1 Observer-only** | 不改变被诊断系统状态；允许只读探测（HTTP GET / DNS / TCP connect）和写诊断输出 |
| **#2 零运行时依赖** | 仅 Python 3.8+ 标准库；系统工具（curl/dig/free/df/ss/journalctl）属于诊断装备，不算依赖 |
| **#3 仓库内独立** | 每个 diag 能 `python3 diag/X.py` 单独跑；Node 与 Python 入口能力等价 |
| **#4 双视角输出** | 文本 + JSON 双输出，同字段值必须一致；崩溃也输出 NDJSON 错误行 |
| **#5 数据溯源** | 每字段能查到来源；缺失数据分类报告（`{found, reason, checked}`），不允许 None/"" 混用 |
| **#6 失败显式** | 单模块崩溃不带崩 `all`；禁止 silent swallow 无注释 |
| **#7 默认脱敏** | 任何含 secret 的字段必须过 sanitizer；`--unmask` 显式 opt-in |

详细设计 → [docs/DESIGN.md](docs/DESIGN.md)（公理推导、目录结构、扩展指南）

## 反馈

- Issues: https://github.com/wujiaming88/openclaw-diag-cli/issues
- 来源：从 4391 行的 `openclaw-diag.sh` 拆分重写

## License

MIT
