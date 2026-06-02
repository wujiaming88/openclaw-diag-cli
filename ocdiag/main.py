"""v2 CLI entry point.

Activated via ``OCDIAG_V2=1`` env var or ``--v2`` flag on the launcher. The
old v1 path (ocdiag.dispatcher → diag/XX_*.py) remains the default during
the transition.
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


def cmd_doctor(args) -> int:
    # Reuse the existing v1 doctor module; it already prints structured output.
    from . import doctor
    return doctor.run(json_mode=args.json)


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


def cmd_trace_or_extract(args, head: str, rest: List[str]) -> int:
    """Phase 1: delegate to legacy tools/oc_session_trace.py and extract.py."""
    import runpy
    from pathlib import Path as _P
    repo_root = _P(__file__).resolve().parent.parent
    script = (
        repo_root / "tools" / "oc_session_trace.py" if head == "trace"
        else repo_root / "tools" / "oc_session_extract.py"
    )
    if not script.is_file():
        print(f"Error: tool not found: {script}", file=sys.stderr)
        return 2
    saved_argv = sys.argv[:]
    try:
        sys.argv = [str(script), *rest]
        os.environ.setdefault("OPENCLAW_DIAG_PROG", f"openclaw-diag {head}")
        runpy.run_path(str(script), run_name="__main__")
        return 0
    except SystemExit as e:
        try:
            return int(e.code) if e.code is not None else 0
        except (TypeError, ValueError):
            return 1
    finally:
        sys.argv = saved_argv


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
        args, _ = parser.parse_known_args(rest)
        return cmd_doctor(args)

    if head == "all":
        skip_ids, passthrough = _split_skip(rest)
        args, _ = parser.parse_known_args(passthrough)
        return cmd_all(args, skip_ids)

    if head in ("trace", "extract"):
        return cmd_trace_or_extract(None, head, rest)

    if registry.get(head) is not None:
        args, _ = parser.parse_known_args(rest)
        return cmd_run_collector(args, head)

    print(f"Error: 未知命令 '{head}'", file=sys.stderr)
    print("运行 `openclaw-diag list` 查看全部诊断。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
