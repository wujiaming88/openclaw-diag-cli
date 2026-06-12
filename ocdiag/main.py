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
    p.add_argument("--config", default=paths.CONFIG)
    p.add_argument("--log-dir", default=paths.LOG_DIR)
    p.add_argument("--sessions-base", default=paths.SESSIONS_BASE)
    p.add_argument("--openclaw-home", default=paths.OPENCLAW_HOME)
    p.add_argument(
        "--format",
        choices=list(_FORMAT_CHOICES),
        default=None,
        help="Output format (pretty|json|ndjson). Default: pretty.",
    )
    p.add_argument("--json", action="store_true", help="Alias for --format json.")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--unmask", action="store_true")


def _channel_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--account", default=None,
        help="Filter channel signals by account substring "
             "(matched against the channel-prefix portion of the message body, "
             "e.g. ``--account default`` to keep only ``feishu[default]:`` lines). "
             "Default: no filter.",
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
                {"id": c.id, "label": c.title} for c in state
            ],
            "object_inspectors": [
                {"id": c.id, "label": c.title} for c in inspectors
            ],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    print("openclaw-diag — 可用诊断 (v2)")
    print()
    print("  扫描类（无需参数）：")
    for c in state:
        print(f"    {c.id:<16s} {c.title}")
    print()
    if inspectors:
        print("  对象类（需要 session uuid）：")
        for c in inspectors:
            print(f"    {c.id:<16s} {c.title}")
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
  openclaw-diag trace 7e9f3b31 --format json      # JSON 输出
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
        add_help=True,
        epilog=_TRACE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", help="Session UUID (full or 8+ char prefix)")
    p.add_argument("--msg-index", type=int, default=None,
                   help="Nth user message (0-based)")
    p.add_argument("--msg-id", default=None, help="Message by id field")
    p.add_argument("--msg-match", default=None,
                   help="First user message containing TEXT")
    p.add_argument("--no-trajectory", action="store_true")
    p.add_argument("--no-log", action="store_true")
    p.add_argument("--show-tool-metas", action="store_true")
    p.add_argument("--show-plugin-snapshot", action="store_true")
    p.add_argument("--mask", action="store_true")
    p.add_argument("--agent", default=None, help="Limit to specific agent")
    _common_arguments(p)
    return p


def _build_extract_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openclaw-diag extract",
        add_help=True,
        epilog=_EXTRACT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", help="Session UUID (full or 8+ char prefix)")
    p.add_argument("--summary", action="store_true",
                   help="Per-file record-count summary, no record dump")
    p.add_argument("-a", "--all", action="store_true", dest="all_versions",
                   help="Extract all versions (active + reset + deleted + backup)")
    p.add_argument("--list", action="store_true", dest="list_only",
                   help="List matching files; do not extract")
    p.add_argument("--types", default=None,
                   help="Filter by record type (comma-separated)")
    p.add_argument("--agent", default=None, help="Limit to specific agent")
    _common_arguments(p)
    return p


def _build_panorama_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openclaw-diag panorama",
        add_help=True,
        epilog=_PANORAMA_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", help="Session UUID (full or 8+ char prefix)")
    p.add_argument("--mask", action="store_true",
                   help="Sanitize tool args / message content / api keys")
    p.add_argument("--run-index", type=int, default=None,
                   help="Pick the Nth run (default: -1 = latest)")
    p.add_argument("--all-runs", action="store_true",
                   help="Include every run in the session")
    p.add_argument("--strict-correlation", action="store_true",
                   help="Match only on sessionId / runIds (drops sessionKey "
                        "and toolCallId hits)")
    p.add_argument("--agent", default=None, help="Limit to specific agent")
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
        kwargs = {
            "session_id": ns.session_id,
            "msg_index": ns.msg_index,
            "msg_id": ns.msg_id,
            "msg_match": ns.msg_match,
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
    print(f"openclaw-diag v{__version__} (v2)")
    print()
    print("用法：")
    print("  openclaw-diag <id> [args...]      跑单个诊断")
    print("  openclaw-diag all [--skip a,b]    跑全部 state collectors")
    print("  openclaw-diag list [--format X]   列出所有诊断")
    print("  openclaw-diag doctor              检查环境")
    print("  openclaw-diag trace <uuid>        追踪一条用户消息")
    print("  openclaw-diag extract <uuid>      导出 session 为可读格式")
    print("  openclaw-diag panorama <uuid>     360° session 全景诊断")
    print("  openclaw-diag examples            打印常用示例")
    print()
    print("通用 flag：--format pretty|json|ndjson  --json (alias)  --no-color  --unmask  --version")


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
        cparser = argparse.ArgumentParser(
            prog=f"openclaw-diag {head}",
            description=f"{coll.title} ({coll.id})",
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
