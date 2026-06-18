"""v2 CLI entry point — the default path since v1.0.0.

Routes to registered collectors / inspectors via @register decorators.
The ``--legacy`` flag (or ``OCDIAG_LEGACY=1``) on the launcher falls back
to the pre-v2 dispatcher in ``_legacy/``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional

from . import __version__, paths
from .core import registry
from .core.context import DiagContext
from .core.errors import (
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    EXIT_WARN_OR_FAIL,
    DiagError,
    exit_code_for,
)
from .core.types import Report, Verdict
from .render.human import HumanRenderer
from .render.json_renderer import JsonRenderer
from .render.ndjson import NdjsonRenderer


_FORMAT_CHOICES = ("pretty", "json", "ndjson")


# 集中式命令描述表（id → 一句话中文说明），供 list / <id> --help / 顶层 help 复用。
# 新增 collector / inspector 时，请同步在此添加一行；缺失时 _desc() 回退空串，不报错。
_COMMAND_DESC = {
    # 扫描类 collectors（state）
    "channel":        "扫描渠道（飞书/Telegram 等）连接与消息收发日志，识别断连、过期丢弃、鉴权失败信号",
    "configuration":  "解析 openclaw.json 关键配置（agents/models/plugins/channels），标记缺失或风险项",
    "cron_jobs":      "列出全部 cron job 完整配置（调度/payload/sessionTarget/delivery/enabled），检测不 fire/投递失败",
    "doctor":         "检查 Node / Python / openclaw-diag / OpenClaw 安装与版本是否就绪",
    "environment":    "采集主机环境、Gateway 进程、OpenClaw 版本等基础信息",
    "gateway":        "分析 Gateway 进程生命周期（启动/重启/WS 连接/崩溃）日志",
    "performance":    "统计模型调用延迟（E2E/TTFT）、工具耗时与可用性",
    "plugin_diag":    "检查插件加载状态、hook 订阅、trust gate 与插件错误",
    "recent_errors":  "从日志中提取近期 error/异常并归类",
    "run_health":     "评估 agent run 完成率、卡死与中断情况",
    "sessions_diag":  "扫描 session.jsonl，统计 toolCall/toolResult 配对、孤儿与异常",
    "shell_history":  "汇总 agent 执行过的 shell 命令历史",
    "sys_health":     "检查 CPU/内存/磁盘/OOM/进程等系统级健康",
    "task_health":    "评估后台 task（openclaw tasks）状态与健康度",
    # 对象类 inspectors
    "extract":        "把指定 session 文件导出为可读格式（含 active/reset/deleted/backup 版本）",
    "panorama":       "360° 全景诊断：关联 trajectory + 应用日志 + 子任务 + cron",
    "trace":          "追踪一条用户消息的完整生命周期（prompt→toolCall→toolResult→reply）",
}


def _desc(mid: str) -> str:
    """返回命令 id 的一句话中文描述；未登记的命令回退空串，不报错。"""
    return _COMMAND_DESC.get(mid, "")


def _paged_print(text: str) -> None:
    """Print text through a pager when stdout is a TTY and output is long.

    Uses $PAGER or falls back to 'less -R' (preserves ANSI colors).
    If pager is unavailable or stdout is not a TTY, prints directly.
    """
    import shutil as _shutil
    import subprocess as _sp

    if not sys.stdout.isatty():
        sys.stdout.write(text)
        sys.stdout.flush()
        return

    term_lines = _shutil.get_terminal_size().lines
    text_lines = text.count("\n")
    if text_lines <= term_lines - 2:
        sys.stdout.write(text)
        sys.stdout.flush()
        return

    pager_cmd = os.environ.get("PAGER", "less -R")
    try:
        proc = _sp.Popen(
            pager_cmd, shell=True, stdin=_sp.PIPE,
            encoding="utf-8", errors="replace",
        )
        proc.communicate(input=text)
    except (OSError, BrokenPipeError):
        sys.stdout.write(text)
        sys.stdout.flush()


def _resolve_format(args) -> str:
    """Resolve effective output format from --format / --json flags.

    --format X      → X
    --json          → json (backward compat)
    neither         → pretty
    """
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "pretty"


def _build_context(args) -> DiagContext:
    fmt = _resolve_format(args)
    return DiagContext(
        openclaw_home=Path(args.openclaw_home),
        config_path=Path(args.config),
        log_dir=Path(args.log_dir),
        sessions_base=Path(args.sessions_base),
        unmask=getattr(args, "unmask", False),
        no_color=getattr(args, "no_color", False),
        json_mode=fmt != "pretty",
        account_id=getattr(args, "account", None) or None,
    )


def _common_arguments(p: argparse.ArgumentParser) -> None:
    """注册所有子命令通用的全局选项；放进 argument group 让 --help 输出更整齐。

    注意：default 值保持 paths.* 不变，本次只补 help 文本和分组。
    """
    g = p.add_argument_group("全局选项 (global options)")
    g.add_argument(
        "--config", metavar="PATH", default=paths.CONFIG,
        help="openclaw.json 配置文件路径（默认 ~/.openclaw/openclaw.json）",
    )
    g.add_argument(
        "--log-dir", metavar="PATH", default=paths.LOG_DIR,
        help="OpenClaw 日志目录（默认 /tmp/openclaw）",
    )
    g.add_argument(
        "--sessions-base", metavar="PATH", default=paths.SESSIONS_BASE,
        help="sessions 根目录（默认 ~/.openclaw/agents）",
    )
    g.add_argument(
        "--openclaw-home", metavar="PATH", default=paths.OPENCLAW_HOME,
        help="OpenClaw 主目录（默认 ~/.openclaw）",
    )
    g.add_argument(
        "--format",
        choices=list(_FORMAT_CHOICES),
        default=None,
        help="输出格式 pretty|json|ndjson（默认 pretty；json/ndjson 适合 agent/脚本消费）",
    )
    g.add_argument(
        "--json", action="store_true",
        help="等价于 --format json",
    )
    g.add_argument(
        "--no-color", action="store_true",
        help="关闭 ANSI 颜色（输出到文件或管道时使用）",
    )
    g.add_argument(
        "--unmask", action="store_true",
        help="不脱敏，显示原始敏感内容（token/消息正文等）；默认会脱敏",
    )


def _channel_arguments(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("channel 选项")
    g.add_argument(
        "--account", default=None,
        help="按 account 子串过滤 channel 日志信号"
             "（匹配消息正文里的渠道前缀，例如 ``--account default`` "
             "只保留 ``feishu[default]:`` 行）；默认不过滤",
    )


def _render(report: Report, args) -> None:
    fmt = _resolve_format(args)
    if fmt == "json":
        JsonRenderer().write(report)
    elif fmt == "ndjson":
        NdjsonRenderer().write(report)
    else:
        text = HumanRenderer(no_color=getattr(args, "no_color", False)).render(report)
        _paged_print(text + "\n")


def _exit_code(report: Report) -> int:
    """Map a Report to a process exit code.

    0 — OK (verdict ok)
    1 — verdict warn or fail (no structured error)
    2 — input error (DiagError with input-class code)
    3 — runtime error (DiagError with runtime-class code, or unstructured error)
    """
    if report.diag_error is not None:
        return exit_code_for(report.diag_error)
    if report.error:
        return EXIT_RUNTIME_ERROR
    if report.verdict == Verdict.OK:
        return EXIT_OK
    return EXIT_WARN_OR_FAIL


def cmd_list(args) -> int:
    state = registry.all_state()
    inspectors = registry.all_inspectors()
    fmt = _resolve_format(args)
    if fmt != "pretty":
        payload = {
            "state_collectors": [
                {"id": c.id, "label": c.title, "description": _desc(c.id)}
                for c in state
            ],
            "object_inspectors": [
                {"id": c.id, "label": c.title, "description": _desc(c.id)}
                for c in inspectors
            ],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    print("openclaw-diag — 可用诊断 (v2)")
    print()
    print("  扫描类（无需参数）：")
    for c in state:
        d = _desc(c.id)
        suffix = f" — {d}" if d else ""
        print(f"    {c.id:<16s} {c.title}{suffix}")
    print()
    if inspectors:
        print("  对象类（需要 session uuid）：")
        for c in inspectors:
            d = _desc(c.id)
            suffix = f" — {d}" if d else ""
            print(f"    {c.id:<16s} {c.title}{suffix}")
        print()
    print("  其它命令：")
    print("    all              一次跑完所有扫描类")
    print("    doctor           检查 Node / Python / openclaw-diag / OpenClaw 环境")
    print("    examples         打印常用使用示例")
    return 0


def cmd_examples() -> int:
    print("""openclaw-diag — 常用场景

  # 全面体检
  openclaw-diag all

  # JSON 输出（Agent / 脚本）
  openclaw-diag all --format json

  # 查 Gateway 状态
  openclaw-diag gateway

  # 追踪一条消息的完整生命周期
  openclaw-diag trace <uuid>
  openclaw-diag trace abc12345 --msg-index 0

  # 导出 session 对话内容
  openclaw-diag extract <uuid>
  openclaw-diag extract abc12345 --summary

  # session 全景诊断（关联到 trajectory + 日志 + 子任务 + cron）
  openclaw-diag panorama <uuid>
  openclaw-diag panorama abc12345 --strict-correlation --format json

  # 模型性能
  openclaw-diag performance

  # 定时任务状态
  openclaw-diag cron_jobs

  # jq 快速看 verdict
  openclaw-diag all --format json | jq '.data.verdict'
""")
    return 0


def cmd_doctor(args, node_version: Optional[str] = None) -> int:
    """Run the v2 doctor collector.

    The ``--node-version`` value forwarded by the Node launcher is stashed in
    an env var the collector reads, so we keep DiagContext free of CLI noise.
    """
    if node_version:
        os.environ["OCDIAG_NODE_VERSION"] = node_version
    return cmd_run_collector(args, "doctor")


def cmd_all(args, skip_ids: List[str]) -> int:
    ctx = _build_context(args)
    state = [c for c in registry.all_state() if c.id not in skip_ids]
    rc_overall = 0
    fmt = _resolve_format(args)
    if fmt != "pretty":
        for c in state:
            t0 = time.time()
            try:
                report = c.collect(ctx)
            except BaseException as e:  # noqa: BLE001
                report = Report(module_id=c.id, title=c.title)
                report.error = f"{type(e).__name__}: {e}"
                report.diag_error = DiagError(
                    code="RUNTIME_ERROR",
                    message=f"{type(e).__name__}: {e}",
                )
                report.elapsed_ms = (time.time() - t0) * 1000
                rc_overall = EXIT_RUNTIME_ERROR
                traceback.print_exc(file=sys.stderr)
            _render(report, args)
            rc = _exit_code(report)
            if rc != 0 and rc > rc_overall:
                rc_overall = rc
        return rc_overall

    total = len(state)
    n = 0
    renderer = HumanRenderer(no_color=getattr(args, "no_color", False))
    buffered_output: List[str] = []
    for c in state:
        n += 1
        print(
            f"\n[{n}/{total}] {c.title} ({c.id})...",
            flush=True, file=sys.stderr,
        )
        t0 = time.time()
        try:
            report = c.collect(ctx)
        except BaseException as e:  # noqa: BLE001
            report = Report(module_id=c.id, title=c.title)
            report.error = f"{type(e).__name__}: {e}"
            report.diag_error = DiagError(
                code="RUNTIME_ERROR",
                message=f"{type(e).__name__}: {e}",
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            traceback.print_exc(file=sys.stderr)
            rc_overall = EXIT_RUNTIME_ERROR
        buffered_output.append(renderer.render(report))
        elapsed = report.elapsed_ms / 1000.0
        print(
            f"[{n}/{total}] {c.title} ({c.id}) ... done ({elapsed:.1f}s)",
            flush=True, file=sys.stderr,
        )
        rc = _exit_code(report)
        if rc != 0 and rc > rc_overall:
            rc_overall = rc

    # Output all buffered content, using pager if needed
    full_output = "\n".join(buffered_output) + "\n"
    _paged_print(full_output)
    return rc_overall


def cmd_run_collector(args, mid: str) -> int:
    c = registry.get(mid)
    if c is None:
        print(f"Error: 未知 collector '{mid}'", file=sys.stderr)
        return EXIT_INPUT_ERROR
    ctx = _build_context(args)
    t0 = time.time()
    try:
        report = c.collect(ctx)
    except BaseException as e:  # noqa: BLE001
        report = Report(module_id=c.id, title=c.title)
        report.error = f"{type(e).__name__}: {e}"
        report.diag_error = DiagError(
            code="RUNTIME_ERROR",
            message=f"{type(e).__name__}: {e}",
        )
        report.elapsed_ms = (time.time() - t0) * 1000
        traceback.print_exc(file=sys.stderr)
    _render(report, args)
    return _exit_code(report)


_TRACE_EPILOG = """示例:
  openclaw-diag trace 7e9f3b31                    # 该 session 最后一条用户消息
  openclaw-diag trace 7e9f3b31 --msg-index 0      # 第一条
  openclaw-diag trace 7e9f3b31 --msg-match deploy # 按内容匹配
  openclaw-diag trace 7e9f3b31 --all-messages     # 一次跑完全部用户消息（每轮一段）
  openclaw-diag trace 7e9f3b31 -A --format json   # 全部用户消息，JSON 输出
"""

_EXTRACT_EPILOG = """示例:
  openclaw-diag extract 7e9f3b31              # 默认导出 active 文件
  openclaw-diag extract 7e9f3b31 --summary    # 只看统计
  openclaw-diag extract 7e9f3b31 --all        # 含 reset / deleted / backup
  openclaw-diag extract 7e9f3b31 --format json
"""

_PANORAMA_EPILOG = """示例:
  openclaw-diag panorama 7e9f3b31                       # latest run
  openclaw-diag panorama 7e9f3b31 --all-runs            # every run
  openclaw-diag panorama 7e9f3b31 --run-index 0         # first run
  openclaw-diag panorama 7e9f3b31 --strict-correlation  # only sessionId / runIds
  openclaw-diag panorama 7e9f3b31 --format json --mask
"""


def _build_trace_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openclaw-diag trace",
        description=f"Session Trace (trace) — {_desc('trace')}",
        add_help=True,
        epilog=_TRACE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_argument_group("trace 选项")
    g.add_argument(
        "session_id",
        help="目标 session 的 UUID（完整或 8+ 字符前缀均可）",
    )
    g.add_argument(
        "--msg-index", type=int, default=None,
        help="按序号选择第 N 条用户消息（0-based；默认：最后一条）",
    )
    g.add_argument(
        "--msg-id", default=None,
        help="按消息 id 字段选择具体一条用户消息",
    )
    g.add_argument(
        "--msg-match", default=None,
        help="按内容子串匹配，选第一条包含 TEXT 的用户消息",
    )
    g.add_argument(
        "-A", "--all-messages", action="store_true", dest="all_messages",
        help="一次追踪 session 内全部用户消息（每轮一段；"
             "与 --msg-index/--msg-id/--msg-match 互斥）",
    )
    g.add_argument(
        "--no-trajectory", action="store_true",
        help="不读取 trajectory.jsonl，仅基于 session.jsonl 分析",
    )
    g.add_argument(
        "--no-log", action="store_true",
        help="不关联 openclaw 应用日志（只看 session/trajectory）",
    )
    g.add_argument(
        "--show-tool-metas", action="store_true",
        help="显示每个 toolCall 的完整 meta 信息（默认折叠）",
    )
    g.add_argument(
        "--show-plugin-snapshot", action="store_true",
        help="显示插件快照（hook/状态），用于排查插件介入",
    )
    g.add_argument(
        "--mask", action="store_true",
        help="强制脱敏（trace 默认不脱敏；与全局 --unmask 相反）",
    )
    g.add_argument(
        "--agent", default=None,
        help="只在指定 agent 名下查找 session（多 agent 共用 uuid 时用）",
    )
    _common_arguments(p)
    return p


def _build_extract_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openclaw-diag extract",
        description=f"Session Extract (extract) — {_desc('extract')}",
        add_help=True,
        epilog=_EXTRACT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_argument_group("extract 选项")
    g.add_argument(
        "session_id",
        help="目标 session 的 UUID（完整或 8+ 字符前缀均可）",
    )
    g.add_argument(
        "--summary", action="store_true",
        help="只打印每文件的记录条数统计，不 dump 记录正文",
    )
    g.add_argument(
        "-a", "--all", action="store_true", dest="all_versions",
        help="导出全部版本（active + reset + deleted + backup）",
    )
    g.add_argument(
        "--list", action="store_true", dest="list_only",
        help="只列出匹配到的文件，不实际导出内容",
    )
    g.add_argument(
        "--types", default=None,
        help="按记录类型过滤（逗号分隔，例如 user,assistant,toolCall）",
    )
    g.add_argument(
        "--agent", default=None,
        help="只在指定 agent 名下查找 session",
    )
    _common_arguments(p)
    return p


def _build_panorama_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openclaw-diag panorama",
        description=f"Session Panorama (panorama) — {_desc('panorama')}",
        add_help=True,
        epilog=_PANORAMA_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_argument_group("panorama 选项")
    g.add_argument(
        "session_id",
        help="目标 session 的 UUID（完整或 8+ 字符前缀均可）",
    )
    g.add_argument(
        "--mask", action="store_true",
        help="脱敏 tool 参数 / 消息正文 / api key 等敏感字段",
    )
    g.add_argument(
        "--run-index", type=int, default=None,
        help="选择第 N 个 run（默认 -1 = 最新一次）",
    )
    g.add_argument(
        "--all-runs", action="store_true",
        help="包含 session 内全部 run（默认只看最新一次）",
    )
    g.add_argument(
        "--strict-correlation", action="store_true",
        help="只用 sessionId / runIds 关联，丢弃 sessionKey 与 toolCallId 命中（更严格）",
    )
    g.add_argument(
        "--agent", default=None,
        help="只在指定 agent 名下查找 session",
    )
    _common_arguments(p)
    return p


def cmd_inspector(head: str, rest: List[str]) -> int:
    inspector = registry.get(head)
    if inspector is None or inspector.kind != "inspector":
        print(f"Error: 未知 inspector '{head}'", file=sys.stderr)
        return EXIT_INPUT_ERROR
    if head == "trace":
        parser = _build_trace_parser()
        ns = parser.parse_args(rest)
        # --all-messages traces every user turn; it is mutually exclusive with
        # the single-turn selectors. Reject the combination at parse time so
        # the inspector never sees an ambiguous request.
        if ns.all_messages and (
            ns.msg_index is not None
            or ns.msg_id is not None
            or ns.msg_match is not None
        ):
            parser.error(
                "--all-messages/-A cannot be combined with "
                "--msg-index/--msg-id/--msg-match",
            )
        kwargs = {
            "session_id": ns.session_id,
            "msg_index": ns.msg_index,
            "msg_id": ns.msg_id,
            "msg_match": ns.msg_match,
            "all_messages": ns.all_messages,
            "no_trajectory": ns.no_trajectory,
            "no_log": ns.no_log,
            "show_tool_metas": ns.show_tool_metas,
            "show_plugin_snapshot": ns.show_plugin_snapshot,
            "mask": ns.mask,
            "agent": ns.agent,
        }
    elif head == "extract":
        parser = _build_extract_parser()
        ns = parser.parse_args(rest)
        kwargs = {
            "session_id": ns.session_id,
            "summary": ns.summary,
            "all_versions": ns.all_versions,
            "list_only": ns.list_only,
            "types_filter": ns.types,
            "agent": ns.agent,
            "unmask": ns.unmask,
        }
    elif head == "panorama":
        parser = _build_panorama_parser()
        ns = parser.parse_args(rest)
        kwargs = {
            "session_id": ns.session_id,
            "mask": ns.mask,
            "unmask": ns.unmask,
            "run_index": ns.run_index,
            "all_runs": ns.all_runs,
            "strict_correlation": ns.strict_correlation,
            "agent": ns.agent,
        }
    else:
        print(f"Error: inspector '{head}' has no argument schema", file=sys.stderr)
        return EXIT_INPUT_ERROR

    ctx = _build_context(ns)
    t0 = time.time()
    try:
        report = inspector.collect(ctx, **kwargs)
    except BaseException as e:  # noqa: BLE001
        report = Report(module_id=inspector.id, title=inspector.title)
        report.error = f"{type(e).__name__}: {e}"
        report.diag_error = DiagError(
            code="RUNTIME_ERROR",
            message=f"{type(e).__name__}: {e}",
        )
        report.elapsed_ms = (time.time() - t0) * 1000
        traceback.print_exc(file=sys.stderr)
    _render(report, ns)

    # Extract: dump records to stdout after summary in pretty mode
    # (json/ndjson modes already include the records under report.data).
    if (
        head == "extract"
        and not report.error
        and _resolve_format(ns) == "pretty"
    ):
        _dump_extract_records(report, ctx)

    return _exit_code(report)


def _dump_extract_records(report: Report, ctx: DiagContext) -> None:
    """Dump session records to stdout after the Report summary.

    This restores the legacy extract behavior: the primary output is the
    session content itself, not just the summary stats.
    """
    import json as _json
    files_payload = report.data.get("files_payload", [])
    buf: List[str] = []
    for entry in files_payload:
        records = entry.get("records")
        if not records:
            continue
        path = entry.get("path", "")
        state = entry.get("state", "")
        sep = "─" * 76
        buf.append(f"\n{sep}\n")
        buf.append(f"  Records: {os.path.basename(path)} [{state}]\n")
        buf.append(f"{sep}\n\n")
        for rec in records:
            buf.append(_json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
            buf.append("\n")
    if buf:
        _paged_print("".join(buf))


def _split_skip(rest: List[str]):
    skip_ids: List[str] = []
    passthrough: List[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--skip" and i + 1 < len(rest):
            skip_ids.extend(
                s.strip() for s in rest[i + 1].split(",") if s.strip()
            )
            i += 2
            continue
        passthrough.append(a)
        i += 1
    return skip_ids, passthrough


def _print_help() -> None:
    """渲染顶层 --help。

    structure：工具简介 → 用法 → 体检命令 → 扫描类（动态从 registry） →
    对象类 → 辅助命令 → 全局选项 → 退出码 → 更多。
    扫描类列表的 id/描述从 registry + _COMMAND_DESC 动态拼接，
    保证未来新增 collector 不会漏排或漏描述。
    """
    state_ids = [c.id for c in registry.all_state()]
    width = 16
    lines: List[str] = []
    lines.append(f"openclaw-diag v{__version__} — OpenClaw 运维诊断 CLI")
    lines.append("")
    lines.append("一句话：扫描配置/日志/session，定位 OpenClaw 部署中的连接、性能、cron、插件、run 等问题。")
    lines.append("")
    lines.append("用法:")
    lines.append("  openclaw-diag <命令> [参数...]")
    lines.append("  openclaw-diag <命令> --help        查看某命令的详细参数")
    lines.append("")
    lines.append("体检命令:")
    lines.append(f"  {'all':<{width}s}一次跑完全部扫描类诊断（推荐首选）")
    lines.append(f"  {'doctor':<{width}s}{_desc('doctor')}")
    lines.append("")
    lines.append("扫描类诊断（无需参数）:")
    for sid in state_ids:
        if sid == "doctor":
            # doctor 已在「体检命令」展示，避免重复
            continue
        lines.append(f"  {sid:<{width}s}{_desc(sid)}")
    lines.append("")
    lines.append("对象诊断（需要 session uuid）:")
    lines.append(f"  {'trace <uuid>':<{width}s}{_desc('trace')}")
    lines.append(f"  {'extract <uuid>':<{width}s}{_desc('extract')}")
    lines.append(f"  {'panorama <uuid>':<{width}s}{_desc('panorama')}")
    lines.append("")
    lines.append("辅助命令:")
    lines.append(f"  {'list':<{width}s}列出全部可用诊断（支持 --format json）")
    lines.append(f"  {'examples':<{width}s}打印常用使用场景示例")
    lines.append("")
    lines.append("全局选项（所有命令通用）:")
    lines.append("  --format pretty|json|ndjson   输出格式（默认 pretty）")
    lines.append("  --json                        等价 --format json")
    lines.append("  --no-color                    关闭颜色")
    lines.append("  --unmask                      不脱敏显示原始内容")
    lines.append("  --config / --log-dir / --sessions-base / --openclaw-home  覆盖默认路径")
    lines.append("")
    lines.append("退出码:")
    lines.append("  0  正常（无 warn/fail）")
    lines.append("  1  有 warn 或 fail")
    lines.append("  2  输入错误（参数/uuid 不对）")
    lines.append("  3  运行时错误")
    lines.append("")
    lines.append("更多: `openclaw-diag list` 看全部诊断，`openclaw-diag <命令> --help` 看单条详情。")
    print("\n".join(lines))


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    registry.discover()

    if not argv or argv[0] in ("-h", "--help"):
        _print_help()
        return 0

    if argv[0] in ("--version", "-V", "-v", "version"):
        print(__version__)
        return 0

    head, rest = argv[0], argv[1:]

    parser = argparse.ArgumentParser(add_help=False)
    _common_arguments(parser)

    if head == "list":
        # --json / --format matter for `list`
        args, _ = parser.parse_known_args(rest)
        return cmd_list(args)

    if head == "examples":
        return cmd_examples()

    if head == "doctor":
        # Pull --node-version out before argparse so the v2 doctor collector
        # can read it from the env without a custom DiagContext field.
        node_version: Optional[str] = None
        passthrough: List[str] = []
        i = 0
        while i < len(rest):
            if rest[i] == "--node-version" and i + 1 < len(rest):
                node_version = rest[i + 1]
                i += 2
                continue
            passthrough.append(rest[i])
            i += 1
        args, _ = parser.parse_known_args(passthrough)
        return cmd_doctor(args, node_version=node_version)

    if head == "all":
        skip_ids, passthrough = _split_skip(rest)
        args, _ = parser.parse_known_args(passthrough)
        return cmd_all(args, skip_ids)

    if head in ("trace", "extract", "panorama"):
        return cmd_inspector(head, rest)

    coll = registry.get(head)
    if coll is not None:
        if coll.kind == "inspector":
            return cmd_inspector(head, rest)
        # Build a per-collector parser with add_help=True so `<id> --help`
        # prints usage + flag docs and exits, instead of being swallowed by
        # parse_known_args (which would otherwise execute the diagnostic).
        # parse_known_args is preserved to keep the existing lenient handling
        # of unrecognized flags from external callers.
        d = _desc(coll.id)
        desc = f"{coll.title} ({coll.id})" + (f" — {d}" if d else "")
        cparser = argparse.ArgumentParser(
            prog=f"openclaw-diag {head}",
            description=desc,
            add_help=True,
        )
        _common_arguments(cparser)
        if head == "channel":
            _channel_arguments(cparser)
        args, _ = cparser.parse_known_args(rest)
        return cmd_run_collector(args, head)

    print(f"Error: 未知命令 '{head}'", file=sys.stderr)
    print("运行 `openclaw-diag list` 查看全部诊断。", file=sys.stderr)
    return EXIT_INPUT_ERROR


if __name__ == "__main__":
    sys.exit(main())
