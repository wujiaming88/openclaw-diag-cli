# openclaw-diag-cli

ArkClaw / OpenClaw 只读诊断工具集（纯 Python，零运行时依赖）。
从 `openclaw-diag.sh`（4391 行 bash + inline Python）拆分而来，每个诊断脚本独立可跑。

## 特点

- **零依赖**：仅使用 Python 3.8+ 标准库
- **只读**：永不修改文件、不重启服务
- **模块化**：10 个 diag 模块 + 2 个 session 工具，全部独立可执行
- **结构化输出**：默认人类可读，`--json` 切换为 JSON

## 目录结构

```
openclaw-diag-cli/
├── ocdiag/                  共享库
├── diag/                    10 个诊断模块
│   ├── 01_sys_health.py     系统健康（DNS/网络/CPU/内存/磁盘/IO/进程/时间同步）
│   ├── 02_environment.py    基础环境（版本一致性、Gateway 进程环境变量）
│   ├── 03_configuration.py  配置（含敏感字段脱敏）
│   ├── 04_gateway.py        Gateway 状态（WS 生命周期 + 错误码统一视图）
│   ├── 05_recent_errors.py  近期错误（多日志聚合 + journalctl + tool 错误）
│   ├── 06_cron_jobs.py      定时任务（jobs.json + state + runs/ 三源合并）
│   ├── 07_performance.py    模型/工具性能（慢调用 Top 20 / E2E 延迟 / Cache）
│   ├── 08_sessions.py       Session 数据（六维分析 + Stuck 探测）
│   ├── 09_plugin_diag.py    插件诊断（一致性 + ERROR/WARN + Hook + Channel + DNS）
│   └── 10_shell_history.py  Shell 历史（高危命令 + openclaw 命令）
├── tools/
│   ├── oc_session_trace.py    跟踪用户消息处理时间轴
│   └── oc_session_extract.py  导出 session JSONL 为可读格式
└── bin/
    └── ocdiag                总入口 dispatcher
```

## 快速使用

```bash
# 列出所有模块
./bin/ocdiag list

# 运行单个模块
python3 diag/04_gateway.py

# 运行所有模块
./bin/ocdiag run all

# 跳过指定模块
./bin/ocdiag run all --skip performance,sessions

# JSON 输出
python3 diag/03_configuration.py --json

# Session 工具
python3 tools/oc_session_trace.py <session-uuid>
python3 tools/oc_session_extract.py <session-uuid> --summary
```

## 环境变量覆盖

- `OPENCLAW_HOME`（默认 `~/.openclaw`）
- `OPENCLAW_CONFIG`（默认 `$OPENCLAW_HOME/openclaw.json`）
- `OPENCLAW_LOG_DIR`（默认 `/tmp/openclaw`）
- `OPENCLAW_SESSIONS`（默认 `$OPENCLAW_HOME/agents`）

## License

MIT
