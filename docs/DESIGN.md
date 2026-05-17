# 设计原理（Design Notes）

面向想理解或扩展这个工具的人。普通用户看 [README](../README.md) 即可。

## 为什么存在

排查 OpenClaw 故障时面对的真实痛点：

- **数据散在多个角落**：session.jsonl 在 agents/ 下，配置在 openclaw.json，进程行为在 journalctl，cron 状态在 cron/jobs.json，模型耗时藏在 trajectory 里。手敲 jq + grep 组合费时且易漏。
- **`openclaw-diag.sh` 已成 4391 行单体 bash**，里面塞着 10 段 heredoc 嵌入的 Python，难修改、难单测、难复用。
- **诊断脚本应该是"原子操作"**：每条数据有明确来源，每个模块解决一类问题，可以单独跑、组合管道、被自动化驱动。

## 6 条设计公理

不可让步的硬约束。所有目录结构、API、输出格式都从这 6 条推导。

### 1. 只读
诊断脚本永远不修改文件、不写配置、不重启服务。
**收益**：生产环境、事故现场、客户机器都能放心跑。

### 2. 零运行时依赖
只用 Python 3.8+ 标准库。不写 `requirements.txt`，不要 `pip install`。
**唯一例外**：`croniter` 在 `06_cron_jobs.py` 中可选导入（缺失时退化到从历史 runs 推算间隔）。
**收益**：任何能跑 OpenClaw 的节点都能跑诊断。

### 3. 独立可执行
每个诊断脚本能单独跑通，不依赖 dispatcher、不需要先 source env、不需要先执行别的脚本。

### 4. 可组合
默认人类可读文本，加 `--json` 输出结构化 JSON；`run all --json` 输出 NDJSON（每行一个模块）。进度信息走 stderr，不污染 stdout。

### 5. 数据可靠
脚本输出的每个数字、每个状态都必须能溯源——见 README 的「数据来源表」。同一字段，文本输出和 JSON 输出值必须一致。

### 6. 故障隔离
- 单模块崩溃**不能**带崩 `run all`：dispatcher 在 `runpy.run_path` 外包 try/except。
- 单数据源缺失（配置不存在、日志没生成）**不能**抛异常，要明确报告"未找到"。
- 失败要在 stderr 留 traceback，rc 非 0；不静默吞异常。

## 目录结构（来自公理）

```
openclaw-diag-cli/
├── ocdiag/         共享原语（公理 #2）
│   ├── paths.py        路径常量 + 环境变量覆盖
│   ├── jsonlog.py      OpenClaw JSON 日志解析
│   ├── timeutil.py     时间转换 + 格式化
│   ├── tokens.py       token 计数 / 百分位 / 大小格式化
│   ├── sensitive.py    密钥脱敏（公理 #1 延伸）
│   ├── output.py       双模式输出 — 公理 #4 实现
│   ├── recent_logs.py  发现今日日志
│   ├── cli.py          公共 argparse
│   └── dispatcher.py   bin/ocdiag 复用入口
│
├── diag/           诊断模块（公理 #3：每个独立可跑）
│   ├── 01_sys_health.py        系统健康
│   ├── 02_environment.py       OpenClaw 基础环境
│   ├── 03_configuration.py     配置展平（脱敏）
│   ├── 04_gateway.py           Gateway 状态
│   ├── 05_recent_errors.py     近期错误
│   ├── 06_cron_jobs.py         定时任务
│   ├── 07_performance.py       模型/工具性能
│   ├── 08_sessions.py          Session 数据
│   ├── 09_plugin_diag.py       插件诊断
│   └── 10_shell_history.py     Shell 历史
│
├── tools/
│   ├── oc_session_trace.py     单消息时间轴追踪
│   └── oc_session_extract.py   session 导出（含 reset/bak/deleted 全状态）
│
├── lib/
│   └── bundle.py               bundle 子命令实现
│
└── bin/
    ├── ocdiag                  Python dispatcher
    └── openclaw-diag.js        npx 入口（Node 薄壳）
```

## 加一个诊断模块

```python
#!/usr/bin/env python3
"""模块 11：xxx — 采集什么 + 数据来源 + 输出含义。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output


def main():
    parser = cli.build_common_parser(description="...", prog="11_my_check")
    args = parser.parse_args()
    out = output.init("my_check", json_mode=args.json, no_color=args.no_color)
    out.section("模块 11：xxx")
    # ...
    out.item("某个发现")
    out.set_data("key", value)  # JSON 模式时有效
    return out.done()


if __name__ == "__main__":
    sys.exit(main())
```

注册到 dispatcher：在 `ocdiag/dispatcher.py:MODULES` 加一行。

**强约束**：
- 流式读 JSONL（`for line in open(...)`），不能 `.read().split('\n')`
- 子进程调用必须带 `timeout`
- 数据源缺失明确报告"未找到"，不抛异常

## 不做的事（反模式）

| 不做 | 原因 |
|---|---|
| 不写测试框架 | 优先靠 ground truth 对齐验证 |
| 不加 web UI / TUI / Rich | 与公理 #2、#4 冲突 |
| 不需要 `pip install` | 公理 #2、#3 |
| 不重启 / 不修改 / 不发请求 | 公理 #1 |
| 不强制配置 / 不强制 token | 任何节点 clone 即跑 |
| 不引入 jq 子进程 | Python 自带 json |
| 不内嵌 Python 在 bash heredoc | 这是我们要替代的旧形态 |

## 来历

由 4391 行的 `openclaw-diag.sh`（10 个 bash 模块 + 10 段 heredoc Python）拆分重写。原 bash 脚本仍在维护，面向"打包采集 + 远程发送"的 all-in-one 用例；本仓库面向"模块化、自动化、可推理"的诊断场景。

## License

MIT
