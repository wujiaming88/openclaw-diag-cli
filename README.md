# openclaw-diag

OpenClaw 出问题时，**先跑这条命令再开 ticket**：

```bash
npx openclaw-diag-cli all
```

零安装、零依赖、observer-only — 只读探测，绝不改你的状态。

## 这是什么

一个排查 [OpenClaw](https://github.com/openclaw/openclaw) 运行问题的命令行工具箱。

把日常排障要做的事情切成 12 个原子诊断：每个回答一个具体问题（"Gateway 起来了吗？"、"哪个 cron 连续失败？"、"P95 延迟最高的模型是哪个？"），可以单独跑、可以一键全跑、也可以拼成 jq 管道喂给监控。

**适合谁用**

- 用 OpenClaw 的运维 / SRE — 想知道线上某个组件在不在状态
- 应急响应工程师 — 用户报障时要 5 分钟摸清"哪挂了、什么时候挂的、谁动过它"
- 自动化平台 — 想把 OpenClaw 健康指标接到自家监控

**不是什么**

- 不是修复工具 — 它告诉你出了什么问题，但不会去改任何东西
- 不是替代 `openclaw doctor` 的内置检查 — 它做的是更深一层的事故诊断
- 不是性能压测工具 — 它读真实运行数据，不主动施压

## 安装与上手

需要 Node 18+ 和 Python 3.8+。

```bash
# 一次性运行（首次会下载到 npm cache，之后离线可用）
npx openclaw-diag-cli

# 或装到 PATH
npm install -g openclaw-diag-cli
```

```bash
# 检查工具自身环境
openclaw-diag doctor

# 跑某个具体诊断（看 Gateway 状态）
openclaw-diag gateway

# 一次跑完所有 state collectors（任一崩了不影响其他）
openclaw-diag all

# 输出结构化 JSON（适合喂给 jq / 监控）
openclaw-diag gateway --json

# 追踪一条用户消息从进入到响应的完整时间轴
openclaw-diag trace <session-uuid>
```

输出大致长这样（截取 `openclaw-diag gateway`）：

```
── 模块 4：Gateway 状态 ──

  • 进程 / 端口
    PID 12847 (uptime 3d 2h)，监听 :8080，HTTP /healthz → 200
  • 24h 重启
    无重启事件
  • Model API
    amazon-bedrock 可达（DNS+HTTP+认证均通）
  • WS 生命周期
    最近 1h 内 134 次连接，平均存活 47s，无异常关闭
```

加 `--json` 后输出严格结构化（同字段、同值），方便管道处理。

## 诊断列表

```bash
openclaw-diag list   # 看完整列表
```

**扫描类（无需参数，扫一遍系统当前状态）**

| 诊断 | 看什么 |
|---|---|
| `sys_health` | DNS / 网络 / CPU / 内存 / 磁盘 / IO / 进程 / 时间同步 |
| `environment` | OpenClaw 版本一致性、Gateway 进程的环境变量 |
| `configuration` | `openclaw.json` 展平（敏感字段已脱敏） |
| `gateway` | Gateway 进程、端口、24h 启停、WS 生命周期、错误码 |
| `recent_errors` | 应用日志 / journalctl / session 工具调用错误聚合 |
| `cron_jobs` | 定时任务状态、连续失败、调度漂移、静默检测 |
| `performance` | 模型/工具耗时 P50/P95、慢调用 Top 20、E2E 延迟、Cache 命中率 |
| `sessions` | Session 总览、活跃度、Stuck 探测 |
| `plugin_diag` | 插件状态一致性、ERROR/WARN、Hook 异常、Channel、外部依赖 DNS |
| `shell_history` | 高危命令、openclaw 命令、最近操作 |

**对象类（需要 session uuid）**

| 诊断 | 看什么 |
|---|---|
| `trace <uuid>` | 一条用户消息从进入到响应的完整时间轴 |
| `extract <uuid>` | session.jsonl 导出为可读格式（active / reset / deleted / backup 全状态） |

**其它命令**

| 命令 | 作用 |
|---|---|
| `openclaw-diag all` | 跑全部 state collectors |
| `openclaw-diag doctor` | 检查 Node / Python / openclaw-diag / OpenClaw 环境 |
| `openclaw-diag bundle <id>` | 打成单文件 .py，离线机器零依赖运行 |

## 配方（jq 管道）

```bash
# 哪些 cron 任务出问题了
openclaw-diag cron_jobs --json | jq '.data.jobs[] | select(.status!="ok")'

# P95 延迟 top 3 的模型
openclaw-diag performance --json | jq '.data.models | to_entries | sort_by(-.value.p95_s) | .[0:3]'

# 找出有 stuck session 的 agent
openclaw-diag sessions --json | jq '.data.stuck_sessions'

# 把所有诊断聚合成 NDJSON 报告（崩溃模块也有错误行，不会丢）
openclaw-diag all --json 2>/dev/null > report.ndjson
```

## 离线机器与配置覆盖

如果目标机器没法装 npm，先在有网的机器上 `bundle` 出单文件：

```bash
# 在有网的机器上
openclaw-diag bundle gateway > standalone-gateway.py

# 拷到目标机器（只需 Python 3.8+，无需安装任何东西）
scp standalone-gateway.py prod-server:/tmp/
ssh prod-server "python3 /tmp/standalone-gateway.py --json"
```

诊断他人机器或容器时，无需改代码，用环境变量或 flag 覆盖默认路径：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `OPENCLAW_HOME` | `~/.openclaw` | OpenClaw 主目录 |
| `OPENCLAW_CONFIG` | `$OPENCLAW_HOME/openclaw.json` | 配置文件 |
| `OPENCLAW_LOG_DIR` | `/tmp/openclaw` | 日志目录 |
| `OPENCLAW_SESSIONS` | `$OPENCLAW_HOME/agents` | Session 根目录 |

或单次运行时用 flag：`--config /path/to/file --log-dir /path/to/logs`。

## 退出码与设计

| rc | 含义 |
|---|---|
| 0 | 诊断成功 |
| 1 | 诊断运行成功但报告 `status: "error"`（数据源缺失等） |
| 2 | 诊断崩溃（已隔离，不影响 `all`） |

设计上遵循 7 条公理：observer-only、零运行时依赖、仓库内独立、双视角输出（文本/JSON 同字段同值）、数据溯源、失败显式、默认脱敏。详细推导见 [docs/DESIGN.md](docs/DESIGN.md)。

## 反馈

Issues: https://github.com/wujiaming88/openclaw-diag-cli/issues。License: MIT。
