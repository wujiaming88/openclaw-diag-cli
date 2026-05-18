# 设计原理（Design Notes）

面向想理解或扩展这个工具的人。普通用户看 [README](../README.md) 即可。

## 为什么存在

排查 OpenClaw 故障时面对的真实痛点：

- **数据散在多个角落**：session.jsonl 在 agents/ 下，配置在 openclaw.json，进程行为在 journalctl，cron 状态在 cron/jobs.json，模型耗时藏在 trajectory 里。手敲 jq + grep 组合费时且易漏。
- **`openclaw-diag.sh` 已成 4391 行单体 bash**，里面塞着 10 段 heredoc 嵌入的 Python，难修改、难单测、难复用。
- **诊断脚本应该是"原子操作"**：每条数据有明确来源，每个模块解决一类问题，可以单独跑、组合管道、被自动化驱动。

## 7 条设计公理

不可让步的硬约束。所有目录结构、API、输出格式都从这 7 条推导。

### 1. Observer-only（不是 read-only）
**不改变被诊断系统的状态**：不写 OpenClaw 配置 / session / cron；不发 POST/PUT/DELETE；不重启服务。
**允许做的事**：HTTP GET 探测、DNS 查询、TCP connect、调只读 API；写诊断输出到 `-o` / stdout / 用户文件系统。
**收益**：生产环境、事故现场、客户机器都能放心跑——但仍然能做需要发包的实际诊断（比如端口扫描、API 可达性）。

### 2. 零运行时依赖
只用 Python 3.8+ 标准库。不写 `requirements.txt`，不要 `pip install`。
**唯一例外**：`croniter` 在 `06_cron_jobs.py` 中可选导入（缺失时退化到从历史 runs 推算间隔）。
**注意**：系统工具（`curl` / `dig` / `free` / `df` / `ss` / `journalctl`）属于「诊断装备」，不算运行时依赖。
**收益**：任何能跑 OpenClaw 的节点都能跑诊断。

### 3. 仓库内独立（含入口能力等价）
- 每个诊断脚本能 `python3 diag/X.py` 单独跑通；不依赖 dispatcher、不需要先 source env。
- `bin/openclaw-diag.js`（Node）和 `bin/ocdiag`（Python）入口必须**能力等价**：任何子命令两边都通，输出一致。
  - Node 入口是薄壳，所有逻辑在 Python 侧。
  - 模块清单是 single source of truth：`ocdiag/dispatcher.py`，Node 通过 `ocdiag list --json` 读取。

### 4. 双视角输出（文本 + JSON 必须一致）
默认人类可读文本，加 `--json` 输出结构化 JSON；`run all --json` 输出 NDJSON（每行一个模块）。
- **同一字段，文本和 JSON 值必须一致**——不只是「差不多」。
- stderr 严格分流，不污染 stdout 上的 NDJSON。
- **崩溃也输出 NDJSON 错误行**：`{module, status:"error", error, traceback}`——dispatcher 在模块崩溃时仍会发一条 NDJSON 行到 stdout，让 NDJSON 永远是 N 行（N = 模块数 - skip 数）。stderr 上还有完整 traceback。

### 5. 数据溯源（含缺失分类）
脚本输出的每个数字、每个状态都必须能溯源——见 README 的「数据来源表」。
- **缺失数据分类报告**，不允许用 `None` / `""` 模糊代替。
- 推荐结构：`{"found": false, "reason": "config_not_found", "checked": "/path"}` 或在主字段旁加 `<field>_status` 子字段。

### 6. 失败显式
- 单模块崩溃**不能**带崩 `run all`：dispatcher 在 `runpy.run_path` 外包 try/except。
- 单数据源缺失（配置不存在、日志没生成）**不能**抛异常，要明确报告（公理 #5 的结构化 missing）。
- 失败要在 stderr 留 traceback，rc 非 0；不静默吞异常。
- **禁止 silent swallow 无注释**：所有 `except: pass / continue / return` 必须有显式注释说明"这里 swallow 是正常为空 / 该数据可选缺失"，否则改成明确报告。

### 7. 默认脱敏（Default sanitization）
- 任何可能含 secret 的字段必须过统一 sanitizer (`ocdiag.sensitive.sanitize_text`)，应用到 shell history、plugin 错误样本、systemd 服务文件、session message content。
- 默认 mask；`--unmask` 显式 opt-in 关掉脱敏（用于安全的离线分析）。
- Sanitizer 是 best-effort，不是保证：用户仍需对扫描内容承担最终责任。

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

注册到 dispatcher：在 `ocdiag/dispatcher.py:STATE_COLLECTORS` 或 `OBJECT_INSPECTORS` 加一行。这是 single source of truth；Node 入口从这里读模块清单，不要在两处分别维护。

**强约束**：
- 流式读 JSONL（`for line in open(...)`），不能 `.read().split('\n')`
- 子进程调用必须带 `timeout`
- 数据源缺失明确报告"未找到"，不抛异常；用 `{found, reason, checked}` 结构（公理 #5）
- 用户输出含 secret 风险的字段（消息文本、错误样本、服务文件）走 `sanitize_text()`（公理 #7）
- 单模块崩溃由 dispatcher 兜底；不要 try-except `BaseException` 自己吞掉

## 不做的事（反模式）

| 不做 | 原因 |
|---|---|
| 不写测试框架 | 优先靠 ground truth 对齐验证 |
| 不加 web UI / TUI / Rich | 与公理 #2、#4 冲突 |
| 不需要 `pip install` | 公理 #2、#3 |
| 不改变被诊断系统的状态（POST/重启/写配置） | 公理 #1 — 但允许只读探测 |
| 不强制配置 / 不强制 token | 任何节点 clone 即跑 |
| 不引入 jq 子进程 | Python 自带 json |
| 不内嵌 Python 在 bash heredoc | 这是我们要替代的旧形态 |
| 不在 Node 侧实现业务逻辑 | 公理 #3 — Node 是薄壳，逻辑全在 Python |
| 不输出未脱敏的 free-form 文本 | 公理 #7 — 默认 sanitize，`--unmask` opt-in |

## 来历

由 4391 行的 `openclaw-diag.sh`（10 个 bash 模块 + 10 段 heredoc Python）拆分重写。原 bash 脚本仍在维护，面向"打包采集 + 远程发送"的 all-in-one 用例；本仓库面向"模块化、自动化、可推理"的诊断场景。

## License

MIT
