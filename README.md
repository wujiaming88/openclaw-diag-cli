# openclaw-diag-cli

> OpenClaw / ArkClaw 故障诊断工具集。零依赖、只读、可组合的纯 Python 脚本。

## 快速开始

无需 git clone，通过 npm 拉一份缓存即可（之后离线可用）：

```bash
# 一次性运行（npm 缓存后离线可用）
npx openclaw-diag-cli list
npx openclaw-diag-cli run gateway
npx openclaw-diag-cli run all --json | jq -s '.'

# 装到 PATH（更短的命令）
npm install -g openclaw-diag-cli
openclaw-diag list
openclaw-diag doctor                       # 检查环境是否就绪
openclaw-diag bundle gateway > gw.py       # 生成单文件诊断脚本
```

依赖：Node 18+（npx）和 Python 3.8+。Node 层是零 npm 依赖的薄壳，只负责定位
`python3` 并把参数透传给现有的 dispatcher，所以 `python3 diag/04_gateway.py`
和 `python3 bin/ocdiag run gateway` 仍然完全可用。

## 为什么存在

排查 OpenClaw 故障时面对的真实痛点：

- **数据散在多个角落**：session.jsonl 在 agents/ 下，配置在 openclaw.json，进程行为在 journalctl，cron 状态在 cron/jobs.json，模型耗时藏在 trajectory 里…… 手敲 jq + grep 组合费时且易漏。
- **`openclaw-diag.sh` 已成为 4391 行单体 bash**，里面塞着 10 段 heredoc 嵌入的 Python，难修改、难单测、难复用。
- **诊断脚本应该是"原子操作"**：每条数据有明确来源，每个模块解决一类问题，可以单独跑、可以组合管道、可以被自动化驱动。

这个仓库就是把那个 4391 行 bash 拆开重写——每个采集动作独立成一个 Python 脚本，按一组公理设计，让"采集 → 分析 → 上报"变成可推理的工程而不是手工活。

---

## 设计公理（First Principles）

下面 6 条是**不可让步**的硬约束。所有目录结构、API、输出格式都从这 6 条推导出来。

### 1. 只读（Read-Only）
诊断脚本**永远不能**修改文件、写配置、重启服务。代价：再难拿的数据也要靠"读"获得；不允许走 `openclaw <subcmd>` 修改类入口。
**收益**：在生产环境、在排查事故现场、在客户机器上跑都安全。

### 2. 零运行时依赖（Zero Runtime Dependencies）
**只用 Python 3.8+ 标准库**。不写 `requirements.txt`，不要 `pip install`。唯一例外：`croniter` 在 `06_cron_jobs.py` 中可选导入（缺失时退化到从历史 runs 推算间隔）。
**收益**：任何能跑 OpenClaw 的节点都能跑诊断（OpenClaw 自己依赖 Node.js，但诊断脚本不依赖 OpenClaw 装在 Python 端的任何包）。`git clone` 完直接 `python3 diag/04_gateway.py`。

### 3. 独立可执行（Independent）
**每个诊断脚本必须能单独跑通**，不依赖 dispatcher、不需要 source 任何 env 文件、不需要先执行别的脚本。
**推论**：脚本顶部用 `sys.path.insert(0, ...)` 把仓库根加进去再 import 共享库；不强制装包。

### 4. 可组合（Composable）
默认输出是人类可读文本（中文，带 emoji 装饰），加 `--json` 输出**结构化 JSON**。
- 单脚本：`{"module": "<id>", "status": "ok|error", "data": {...}}`
- `bin/ocdiag run all --json` 输出 **NDJSON**（每行一个模块的 JSON），可以 `... | jq -s '.'` 聚合，或者 `... | jq 'select(.module=="cron_jobs") | .data'` 抽取。
**推论**：进度信息走 stderr，永远不污染 stdout 的 JSON 流。

### 5. 数据可靠（Data Fidelity）
脚本输出的每个数字、每个状态都必须能溯源：
- 系统数据 → `subprocess.run(["free","-m"])`、`/proc/<pid>/environ`、`journalctl ...`
- OpenClaw 数据 → `~/.openclaw/openclaw.json`、`~/.openclaw/cron/jobs.json`、`~/.openclaw/agents/*/sessions/*.jsonl`
- 日志数据 → `/tmp/openclaw/openclaw-*.log`（按 mtime 取今日）

数据来源在文档里逐模块列清，不允许"看上去合理就行"。同一字段，文本输出和 JSON 输出必须**值一致**。

### 6. 故障隔离（Failure Isolation）
- 单个模块崩溃**不能**带崩 `run all`：dispatcher 在 `runpy.run_path` 外包 try/except。
- 单个数据源缺失（配置不存在、日志没生成、session 文件被删）**不能**抛异常，要明确报告"未找到"。
- 不要 swallow 异常变 silent：失败要在 stderr 留 traceback，rc 非 0。

---

## 推导出的架构

```
openclaw-diag-cli/
├── ocdiag/         共享原语（公理 #2 推论：库小而稳）
│   ├── paths.py        路径常量 + 环境变量覆盖
│   ├── jsonlog.py      OpenClaw JSON 日志解析（公理 #5）
│   ├── timeutil.py     ISO/epoch 时间转换 + 人类友好格式化
│   ├── tokens.py       fmt_tokens / percentile / human_size
│   ├── sensitive.py    密钥/Token 脱敏（公理 #1 的延伸：输出也要安全）
│   ├── output.py       双模式输出（人类可读 + JSON）— 公理 #4 实现
│   ├── recent_logs.py  发现今日更新日志
│   ├── cli.py          公共 argparse（--config / --log-dir / --json）
│   └── dispatcher.py   bin/ocdiag 复用的入口
│
├── diag/           诊断模块（公理 #3：每个能独立跑）
│   ├── 01_sys_health.py        系统健康（DNS/网络/CPU/内存/磁盘/IO/进程/时间同步）
│   ├── 02_environment.py       OpenClaw 基础环境（版本一致性、Gateway 进程 env）
│   ├── 03_configuration.py     openclaw.json 展平（脱敏后）
│   ├── 04_gateway.py           Gateway 状态（WS 生命周期 + 错误码统一视图）
│   ├── 05_recent_errors.py     近期错误（多日志聚合 + journalctl + tool 错误）
│   ├── 06_cron_jobs.py         定时任务（jobs.json + state + runs/ 三源合并）
│   ├── 07_performance.py       模型/工具性能（慢调用 Top 20 / E2E 延迟 / Cache）
│   ├── 08_sessions.py          Session 数据（六维分析 + Stuck 探测）
│   ├── 09_plugin_diag.py       插件诊断（一致性 + ERROR/WARN + Hook + Channel + DNS）
│   └── 10_shell_history.py     Shell 历史（高危命令 + openclaw 命令）
│
├── tools/          单点深挖工具（不是采集，是分析特定对象）
│   ├── oc_session_trace.py     跟踪一条 user 消息从进入到响应的完整时间轴
│   └── oc_session_extract.py   把 session jsonl 导出为可读格式（含 reset/bak/deleted 全状态）
│
└── bin/
    └── ocdiag      可选的总入口（list / run <id> / run all）
```

---

## 数据来源（每条数据从哪里读）

公理 #5 的具体落地——下游用任何字段都能查到它从哪来：

| 模块 | 数据来源 |
|---|---|
| 01_sys_health | `dig`/`getent`、`free -m`、`df -m`、`/proc/<pid>/limits`、`timedatectl` |
| 02_environment | `openclaw --version`、`/proc/<gw-pid>/environ`、`~/.config/systemd/user/openclaw-gateway.service.d/env.conf` |
| 03_configuration | `~/.openclaw/openclaw.json`（脱敏：key/secret/token/password 等关键词命中后 mask） |
| 04_gateway | `systemctl status` + `journalctl --since 24h` + `~/.openclaw/openclaw.json:gateway.port` + `/tmp/openclaw/openclaw-*.log`（subsystem 白名单过滤） |
| 05_recent_errors | 今日 `openclaw-*.log` 的 ERROR/FATAL + `journalctl --priority err` + 最近 session.jsonl 的 toolResult.isError |
| 06_cron_jobs | `~/.openclaw/cron/jobs.json` + `jobs-state.json` + `runs/<jobId>.jsonl`（合并三源） |
| 07_performance | 最近 20 个 `agents/*/sessions/*.jsonl`（含 reset 文件，按 mtime） |
| 08_sessions | 同上 + `/tmp/openclaw/openclaw-*.log` 中 subsystem=diagnostic 的 stuck-session 行 |
| 09_plugin_diag | 今日日志 `_meta.name` 解析 + `~/.openclaw/openclaw.json:plugins.entries` + `~/.openclaw/extensions/` + DNS 探测 |
| 10_shell_history | `~/.bash_history` + `~/.zsh_history` |
| oc_session_trace | session.jsonl + 同目录 `*.trajectory.jsonl`（可选） + Gateway 日志（可选） |
| oc_session_extract | session.jsonl + 兄弟文件 `.deleted` / `.reset.N` / `.bak-*` |

---

## 用法

### 最小用法（独立脚本）

```bash
git clone https://github.com/wujiaming88/openclaw-diag-cli.git
cd openclaw-diag-cli
python3 diag/04_gateway.py            # 直接跑，零配置
python3 diag/04_gateway.py --json     # 同样的数据，JSON 格式
```

### 总入口（可选）

```bash
python3 bin/ocdiag list                # 列出 10 个模块
python3 bin/ocdiag run gateway         # 跑 04_gateway
python3 bin/ocdiag run all             # 全部跑一遍（任一模块崩了不影响其他）
python3 bin/ocdiag run all --skip performance,sessions  # 跳过重模块
```

### npm / npx 入口（同样支持上述全部参数）

```bash
npx openclaw-diag-cli list
npx openclaw-diag-cli run gateway --json
npx openclaw-diag-cli run all --skip performance,sessions
npx openclaw-diag-cli doctor                # 检查 Node/Python/ocdiag/OpenClaw
npx openclaw-diag-cli bundle 04_gateway > standalone-gateway.py
```

### JSON 管道（公理 #4 的真正用法）

```bash
# 1) 单模块 JSON → jq 抽取关键字段
python3 diag/06_cron_jobs.py --json | jq '.data.jobs | length'

# 2) run all NDJSON → 聚合为单文档
python3 bin/ocdiag run all --json 2>/dev/null | jq -s '.' > report.json

# 3) 找出有错误的模块
python3 bin/ocdiag run all --json 2>/dev/null | jq 'select(.status=="error")'

# 4) 提取所有 cron 任务的成功率
python3 bin/ocdiag run all --json 2>/dev/null \
  | jq 'select(.module=="cron_jobs") | .data.jobs[] | {name, success_rate}'
```

### 工具：单点深挖

```bash
# 跟踪一条 user 消息的处理时间轴
python3 tools/oc_session_trace.py <session-uuid> --msg-index 0

# 导出 session 为可读格式
python3 tools/oc_session_extract.py <session-uuid> --summary
python3 tools/oc_session_extract.py <session-uuid> --types message --no-pretty
```

### 环境变量覆盖

跑别人机器/容器时不用改代码，覆盖路径即可：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENCLAW_HOME` | `~/.openclaw` | OpenClaw 主目录 |
| `OPENCLAW_CONFIG` | `$OPENCLAW_HOME/openclaw.json` | 配置文件 |
| `OPENCLAW_LOG_DIR` | `/tmp/openclaw` | 日志目录 |
| `OPENCLAW_SESSIONS` | `$OPENCLAW_HOME/agents` | Session 根 |
| `OPENCLAW_SERVICE_FILE` | `~/.config/systemd/user/openclaw-gateway.service` | systemd 服务单元 |

也可以用 `--config /path/to/openclaw.json --log-dir /path/to/logs` 覆盖单个参数。

---

## 退出码与错误隔离

| rc | 含义 |
|---|---|
| 0 | 模块成功，data 字段已填 |
| 1 | 模块运行成功但报告 `status: "error"`（数据源缺失等业务错误） |
| 2 | 单模块崩溃（dispatcher 已隔离，不影响其他模块） |

`bin/ocdiag run all` 的总 rc 取最大值；任一模块崩溃 stderr 留 traceback，但 stdout 流仍完整。

---

## 扩展：加一个新诊断模块

遵循公理即可：

1. 新建 `diag/11_my_check.py`，shebang `#!/usr/bin/env python3`
2. 顶部 docstring 说明：**采集什么 + 数据来源 + 输出含义**
3. `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` 接入共享库
4. `from ocdiag import cli, output, paths` 拿到统一基础设施
5. `parser = cli.build_common_parser(...); args = parser.parse_args()`
6. `out = output.init("my_check", json_mode=args.json, ...)`
7. 业务逻辑——文本输出用 `out.item / out.evidence / out.section`，JSON 数据用 `out.set_data("key", value)`
8. 流式读 JSONL（`for line in open(...)`），不能 `.read().split('\n')`
9. 子进程调用必须带 `timeout`
10. 数据源缺失要明确报告"未找到"，不抛异常

注册到 `bin/ocdiag` 只需在 `ocdiag/dispatcher.py:MODULES` 列表加一行。

---

## 不做的事（反模式）

| 不做 | 原因 |
|---|---|
| 不写测试框架 | 优先靠 ground truth 对齐验证；测试以后补 |
| 不加 web UI / TUI / Rich | 公理 #2（零依赖）+ 公理 #4（管道友好）冲突 |
| 不需要 `pip install` | 公理 #2 + #3 |
| 不重启 / 不修改 / 不发请求 | 公理 #1 |
| 不强制配置 / 不强制 token | 任何节点 clone 即跑 |
| 不引入 jq 子进程 | Python 自带 json，更可控 |
| 不内嵌 Python 在 bash heredoc 里 | 这就是我们要替代的旧形态 |

---

## 来历

由 4391 行的 `openclaw-diag.sh`（10 个 bash 模块 + 10 段 heredoc Python）拆分重写。原脚本仍在维护，作为"打包采集 + 远程发送报告"的 all-in-one 用例存在；本仓库面向"模块化、自动化、可推理"的诊断场景。

---

## License

MIT
