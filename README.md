# openclaw-diag

零依赖、只读、专为 [OpenClaw](https://github.com/openclaw/openclaw) 设计的诊断 CLI。

## 一、特性与定位

OpenClaw 出问题时，**先跑这条命令再开 ticket**：

```bash
npx openclaw-diag-cli all
```

核心特性：

- **Banner + Verdict + Footer 三段式诊断输出** —— 每个模块顶部给出 `ok / warn / fail` 三态判定与 pass/warn/fail 计数，肉眼可在 30 秒内扫完
- **System prompt 全链路可视化** —— `trace` 还原一条用户消息从进入到响应的时间轴，包含 prompt 大小、模型选型、工具调用、token 消耗
- **Session 级 trace + extract** —— 用 session uuid 或 ≥ 8 位前缀即可定位，无需手动翻 `agents/*/sessions/*.jsonl`
- **模型 / 工具性能分析** —— P50/P95 延迟、慢调用 Top N、E2E 延迟、Cache 命中率、throughput tps
- **默认脱敏** —— API key / token / credential 类字符串在输出前已被 mask，`--unmask` 显式关闭

适用场景：

- 部署后日常监控：`all --json` 接监控管道
- 用户报障应急：5 分钟摸清"哪挂了、什么时候挂的、谁动过它"
- AI agent 运维：trace 单条慢消息、extract 排查 stuck session

## 二、安装与上手

环境要求：**Node 18+**、**Python 3.8+**。

```bash
# 免安装（首次下载到 npm cache，之后离线可用）
npx openclaw-diag-cli all

# 装到 PATH
npm install -g openclaw-diag-cli

# 开发者模式
git clone <repo-url> openclaw-diag-cli
cd openclaw-diag-cli && ./bin/openclaw-diag.js list
```

Quick start：

```bash
openclaw-diag doctor          # 自检 Node / Python / OpenClaw 环境
openclaw-diag all             # 跑全部扫描类诊断
openclaw-diag trace <uuid>    # 追踪一条 session 的处理时间轴
```

## 三、能力详解

### 扫描类（10 个 state collector，无需参数）

| 诊断 | 看什么 | 示例 |
|---|---|---|
| `sys_health` | DNS / 网络 / CPU / 内存 / 磁盘 / IO / 进程 / 时间同步 | `openclaw-diag sys_health` |
| `environment` | OpenClaw 版本一致性、Gateway 进程的环境变量 | `openclaw-diag environment` |
| `configuration` | `openclaw.json` 展平（敏感字段已脱敏） | `openclaw-diag configuration` |
| `gateway` | Gateway 进程、端口、24h 启停、WS 生命周期、错误码 | `openclaw-diag gateway` |
| `recent_errors` | 应用日志 / journalctl / session 工具调用错误聚合 | `openclaw-diag recent_errors` |
| `cron_jobs` | 定时任务状态、连续失败、调度漂移、静默检测 | `openclaw-diag cron_jobs` |
| `performance` | 模型/工具耗时 P50/P95、慢调用 Top 20、E2E 延迟、Cache 命中率 | `openclaw-diag performance` |
| `sessions` | Session 总览、活跃度、Stuck 探测 | `openclaw-diag sessions` |
| `plugin_diag` | 插件状态一致性、ERROR/WARN、Hook、Channel、外部 DNS | `openclaw-diag plugin_diag` |
| `shell_history` | Shell 历史中的高危命令与最近 OpenClaw 操作 | `openclaw-diag shell_history` |

### 对象类（2 个 object inspector，需要 session uuid 或 ≥ 8 位前缀）

**`trace <session_id>`** —— 还原一条用户消息从进入到响应的时间轴，包含 system prompt 大小、模型选型、每个工具调用的耗时、gateway 日志关联。

```bash
openclaw-diag trace 7f3a2c91                    # 默认追踪最后一条 user 消息
openclaw-diag trace 7f3a2c91 --msg-index 0      # 第 0 条 user 消息
openclaw-diag trace 7f3a2c91 --msg-id msg_abc   # 指定 message id
openclaw-diag trace 7f3a2c91 --msg-match "ssh"  # 按文本匹配第一条
openclaw-diag trace 7f3a2c91 --no-trajectory    # 跳过 trajectory 富化
openclaw-diag trace 7f3a2c91 --no-log           # 跳过 gateway 日志关联
```

**`extract <session_id>`** —— 把 `session.jsonl` 导出为人类可读格式，支持 active / reset / deleted / backup 四种状态。

```bash
openclaw-diag extract 7f3a2c91                  # 导出当前 active session
openclaw-diag extract 7f3a2c91 --summary        # 仅输出记录类型计数
openclaw-diag extract 7f3a2c91 --all            # 导出所有版本（含 reset/deleted/backup）
openclaw-diag extract 7f3a2c91 --list           # 仅列出匹配文件，不解析
openclaw-diag extract 7f3a2c91 --types message,toolCall  # 按类型过滤
openclaw-diag extract 7f3a2c91 --unmask         # 关闭脱敏（慎用）
```

### 全局 flag

| flag | 作用 |
|---|---|
| `--json` | 输出结构化 JSON（适合喂给 jq / 监控） |
| `--no-color` | 禁用 ANSI 颜色（CI / 文件重定向） |
| `--unmask` | 关闭默认脱敏（仅 `extract` 与含敏感字段的模块） |

### JSON 输出 schema

每个诊断 `--json` 输出的 envelope 一致：

```json
{
  "module": "sys_health",
  "status": "ok",
  "verdict": "warn",
  "summary": {"pass": 7, "warn": 1, "fail": 0, "total": 8},
  "elapsed_ms": 1862,
  "data": { ... }
}
```

- `status`：legacy 二态，`"ok"` 表示模块正常运行（即使内部有 warn/fail），`"error"` 表示数据源缺失等运行失败
- `verdict`：三态判定，`"ok" | "warn" | "fail"`，由模块内规则汇总
- `summary`：通过/警告/失败/总条目数
- `data`：模块特定结构

### jq 配方

```bash
# 只看 verdict 非 ok 的模块
openclaw-diag all --json | jq 'select(.verdict != "ok") | {module, verdict, summary}'

# 提取每个模型的 P95 与吞吐
openclaw-diag performance --json | jq '.data.models | to_entries[] | {name: .key, p95_s: .value.p95_s, tps: .value.throughput_tps}'

# 列出最大的 5 个 session
openclaw-diag sessions --json | jq '.data.sessions | sort_by(-.size_bytes) | .[0:5] | .[] | {uuid, size_kb: (.size_bytes/1024|floor)}'

# 找连续失败的定时任务
openclaw-diag cron_jobs --json | jq '.data.jobs[] | select(.consecutive_failures > 0) | {name, consecutive_failures}'

# 把所有诊断聚合成 NDJSON 报告（崩溃模块也有错误行，不会丢）
openclaw-diag all --json > report.ndjson
```

## 四、其他

### 设计原则

1. **Observer-only** —— 只读取，绝不修改 OpenClaw 的配置 / session / cron / 服务状态
2. **Zero-deps** —— 只用 Python 标准库 + Node 薄壳，不引入第三方 pip / npm 包
3. **Default-sanitize** —— 默认脱敏 API key / token / credential 类字符串，`--unmask` 显式关闭

### 环境变量覆盖

诊断他人机器或容器时，无需改代码：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `OPENCLAW_HOME` | `~/.openclaw` | OpenClaw 主目录 |
| `OPENCLAW_CONFIG` | `$OPENCLAW_HOME/openclaw.json` | 配置文件 |
| `OPENCLAW_LOG_DIR` | `/tmp/openclaw` | 日志目录 |
| `OPENCLAW_SESSIONS` | `$OPENCLAW_HOME/agents` | Session 根目录 |

也可单次运行时用 flag：`--config /path --log-dir /path --base-dir /path`。

### 退出码

| rc | 含义 |
|---|---|
| 0 | 诊断成功（即使内部 verdict=warn/fail） |
| 1 | 诊断运行成功但报告 `status: "error"`（数据源缺失等） |
| 2 | 诊断崩溃（已隔离，不影响 `all`） |

License: MIT
