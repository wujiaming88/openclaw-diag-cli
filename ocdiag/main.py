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
from .core.types import Report, Verdict
from .render.human import HumanRenderer
from .render.json_renderer import JsonRenderer, to_envelope


def _build_context(args) -> DiagContext:
    return DiagContext(
        openclaw_home=Path(args.openclaw_home),
        config_path=Path(args.config),
        log_dir=Path(args.log_dir),
        sessions_base=Path(args.sessions_base),
        unmask=getattr(args, "unmask", False),
        no_color=getattr(args, "no_color", False),
        json_mode=getattr(args, "json", False),
    )


def _common_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=paths.CONFIG)
    p.add_argument("--log-dir", default=paths.LOG_DIR)
    p.add_argument("--sessions-base", default=paths.SESSIONS_BASE)
    p.add_argument("--openclaw-home", default=paths.OPENCLAW_HOME)
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--unmask", action="store_true")


def _render(report: Report, ctx: DiagContext) -> None:
    if ctx.json_mode:
        JsonRenderer().write(report)
    else:
        HumanRenderer(no_color=ctx.no_color).write(report)


def _exit_code(report: Report) -> int:
    return 1 if report.verdict == Verdict.FAIL else 0


def cmd_list(args) -> int:
    state = registry.all_state()
    inspectors = registry.all_inspectors()
    if args.json:
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
    if args.json:
        for c in state:
            t0 = time.time()
            try:
                report = c.collect(ctx)
            except BaseException as e:  # noqa: BLE001
                report = Report(module_id=c.id, title=c.title)
                report.error = f"{type(e).__name__}: {e}"
                report.elapsed_ms = (time.time() - t0) * 1000
                rc_overall = 2
                traceback.print_exc(file=sys.stderr)
            _render(report, ctx)
            if _exit_code(report) != 0 and rc_overall == 0:
                rc_overall = _exit_code(report)
        return rc_overall

    total = len(state)
    n = 0
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
            report.elapsed_ms = (time.time() - t0) * 1000
            traceback.print_exc(file=sys.stderr)
            rc_overall = 2
        _render(report, ctx)
        elapsed = report.elapsed_ms / 1000.0
        print(
            f"[{n}/{total}] {c.title} ({c.id}) ... done ({elapsed:.1f}s)",
            flush=True, file=sys.stderr,
        )
        if _exit_code(report) != 0 and rc_overall == 0:
            rc_overall = _exit_code(report)
    return rc_overall


def cmd_run_collector(args, mid: str) -> int:
    c = registry.get(mid)
    if c is None:
        print(f"Error: 未知 collector '{mid}'", file=sys.stderr)
        return 2
    ctx = _build_context(args)
    t0 = time.time()
    try:
        report = c.collect(ctx)
    except BaseException as e:  # noqa: BLE001
        report = Report(module_id=c.id, title=c.title)
        report.error = f"{type(e).__name__}: {e}"
        report.elapsed_ms = (time.time() - t0) * 1000
        traceback.print_exc(file=sys.stderr)
    _render(report, ctx)
    return _exit_code(report)


def _build_trace_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openclaw-diag trace", add_help=True)
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
    p = argparse.ArgumentParser(prog="openclaw-diag extract", add_help=True)
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


def cmd_inspector(head: str, rest: List[str]) -> int:
    inspector = registry.get(head)
    if inspector is None or inspector.kind != "inspector":
        print(f"Error: 未知 inspector '{head}'", file=sys.stderr)
        return 2
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
    else:
        print(f"Error: inspector '{head}' has no argument schema", file=sys.stderr)
        return 2

    ctx = _build_context(ns)
    t0 = time.time()
    try:
        report = inspector.collect(ctx, **kwargs)
    except BaseException as e:  # noqa: BLE001
        report = Report(module_id=inspector.id, title=inspector.title)
        report.error = f"{type(e).__name__}: {e}"
        report.elapsed_ms = (time.time() - t0) * 1000
        traceback.print_exc(file=sys.stderr)
    _render(report, ctx)
    return _exit_code(report)


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
    print("  openclaw-diag list [--json]       列出所有诊断")
    print("  openclaw-diag doctor              检查环境")
    print("  openclaw-diag trace <uuid>        追踪一条用户消息")
    print("  openclaw-diag extract <uuid>      导出 session 为可读格式")
    print()
    print("通用 flag：--json --no-color --unmask")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    registry.discover()

    if not argv or argv[0] in ("-h", "--help"):
        _print_help()
        return 0

    head, rest = argv[0], argv[1:]

    parser = argparse.ArgumentParser(add_help=False)
    _common_arguments(parser)

    if head == "list":
        # only --json matters for `list`
        args, _ = parser.parse_known_args(rest)
        return cmd_list(args)

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

    if head in ("trace", "extract"):
        return cmd_inspector(head, rest)

    coll = registry.get(head)
    if coll is not None:
        if coll.kind == "inspector":
            return cmd_inspector(head, rest)
        args, _ = parser.parse_known_args(rest)
        return cmd_run_collector(args, head)

    print(f"Error: 未知命令 '{head}'", file=sys.stderr)
    print("运行 `openclaw-diag list` 查看全部诊断。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
