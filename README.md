# openclaw-diag-cli

> OpenClaw / ArkClaw 故障诊断 CLI。零依赖、只读、人和机器都能用。

## 安装

```bash
# 一次性运行（无需安装，npm 缓存后离线可用）
npx openclaw-diag-cli list

# 装到 PATH
npm install -g openclaw-diag-cli
openclaw-diag list
```

依赖：Node 18+ 和 Python 3.8+。

## 五分钟上手

```bash
# 1. 检查环境是否就绪
openclaw-diag doctor

# 2. 列出所有诊断模块
openclaw-diag list

# 3. 跑单个模块
openclaw-diag run gateway

# 4. 全部跑一遍（任一模块崩了不影响其他）
openclaw-diag run all

# 5. 输出结构化 JSON
openclaw-diag run gateway --json
```

## 诊断模块（10 个）

| 模块 | 看什么 |
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

## 单点工具（2 个）

```bash
# 跟踪一条用户消息从进入到响应的完整时间轴
openclaw-diag tools/oc_session_trace.py <session-uuid> --msg-index 0

# 导出 session 为可读格式（支持 reset / bak / deleted 全状态）
openclaw-diag tools/oc_session_extract.py <session-uuid> --summary
```

## 常见配方

```bash
# 找出哪个 cron 任务在连续失败
openclaw-diag run cron_jobs --json | jq '.data.jobs[] | select(.status!="ok")'

# 看哪个模型的 P95 延迟最高
openclaw-diag run performance | grep -A1 "P95"

# 哪些插件今天有 ERROR
openclaw-diag run plugin_diag --json | jq '.data.plugin_errors | to_entries[] | select(.value.error_count > 0)'

# 把所有诊断聚合成单个 JSON 报告
openclaw-diag run all --json 2>/dev/null | jq -s '.' > report.json

# 找出有 stuck session 的事件
openclaw-diag run sessions --json | jq '.data.stuck_sessions'
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
| 0 | 模块成功 |
| 1 | 模块运行成功但报告 `status: "error"`（数据源缺失等） |
| 2 | 模块崩溃（已隔离，不影响其他模块） |

## 设计原则

| | |
|---|---|
| **只读** | 永远不修改文件、不重启服务 |
| **零依赖** | 仅 Python 3.8+ 标准库 |
| **故障隔离** | 单模块崩溃不带崩 `run all` |
| **数据可靠** | 每个字段都能溯源 |
| **可组合** | 文本 + JSON 双输出，stderr 与 stdout 分流 |

详细设计 → [docs/DESIGN.md](docs/DESIGN.md)（公理推导、目录结构、扩展指南）

## 反馈

- Issues: https://github.com/wujiaming88/openclaw-diag-cli/issues
- 来源：从 4391 行的 `openclaw-diag.sh` 拆分重写

## License

MIT
