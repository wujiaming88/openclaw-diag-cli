"""Dispatcher: list / run <name> / run all."""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import time
from pathlib import Path
from typing import List


REPO_ROOT = Path(__file__).resolve().parent.parent

# Module ID -> (label, script filename relative to REPO_ROOT)
MODULES = [
    ("sys_health",     "系统健康检查",     "diag/01_sys_health.py"),
    ("environment",    "采集基础环境",     "diag/02_environment.py"),
    ("configuration",  "采集配置",         "diag/03_configuration.py"),
    ("gateway",        "采集 Gateway 状态", "diag/04_gateway.py"),
    ("recent_errors",  "采集近期日志",     "diag/05_recent_errors.py"),
    ("cron_jobs",      "采集定时任务",     "diag/06_cron_jobs.py"),
    ("performance",    "采集模型与性能数据", "diag/07_performance.py"),
    ("sessions",       "采集 Session 数据", "diag/08_sessions.py"),
    ("plugin_diag",    "采集插件诊断",     "diag/09_plugin_diag.py"),
    ("shell_history",  "采集命令执行历史",  "diag/10_shell_history.py"),
]

MODULE_BY_ID = {mid: (label, script) for mid, label, script in MODULES}


def cmd_list() -> int:
    print("Available modules:")
    for mid, label, _ in MODULES:
        print(f"  [x] {mid:<16s} {label}")
    print()
    print("Usage: ocdiag run <id> | ocdiag run all [--skip id1,id2] [--json]")
    return 0


def run_script(script_rel: str, extra_args: List[str]) -> int:
    script_path = REPO_ROOT / script_rel
    if not script_path.is_file():
        print(f"Error: script not found: {script_path}", file=sys.stderr)
        return 2
    saved_argv = sys.argv[:]
    try:
        sys.argv = [str(script_path), *extra_args]
        runpy.run_path(str(script_path), run_name="__main__")
        return 0
    except SystemExit as e:
        try:
            return int(e.code) if e.code is not None else 0
        except (TypeError, ValueError):
            return 1
    except Exception as e:
        print(f"  ERROR: {script_path.name} crashed: {type(e).__name__}: {e}",
              file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 2
    finally:
        sys.argv = saved_argv


def cmd_run(target: str, extra_args: List[str], skip_ids: List[str]) -> int:
    json_mode = "--json" in extra_args
    progress_stream = sys.stderr if json_mode else sys.stdout
    if target == "all":
        rc_overall = 0
        total = sum(1 for mid, _, _ in MODULES if mid not in skip_ids)
        n = 0
        for mid, label, script in MODULES:
            if mid in skip_ids:
                continue
            n += 1
            print(f"\n[{n}/{total}] {label} ({mid})...", flush=True, file=progress_stream)
            t0 = time.time()
            rc = run_script(script, extra_args)
            elapsed = time.time() - t0
            print(f"[{n}/{total}] {label} ({mid}) ... done ({elapsed:.1f}s)", flush=True, file=progress_stream)
            if rc != 0:
                rc_overall = rc
        return rc_overall
    if target not in MODULE_BY_ID:
        print(f"Error: unknown module '{target}'. Use `ocdiag list`.", file=sys.stderr)
        return 2
    _, script = MODULE_BY_ID[target]
    return run_script(script, extra_args)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help"):
        print("ocdiag — OpenClaw 诊断 CLI dispatcher")
        print()
        print("Usage:")
        print("  ocdiag list                      列出所有诊断模块")
        print("  ocdiag run <id>                  运行单个模块（id 或 all）")
        print("  ocdiag run all [--skip ids]      运行全部模块，可跳过若干")
        print()
        print("--skip 后接逗号分隔的 module id 列表（如 performance,sessions）。")
        print("其它参数（--config / --log-dir / --json / --no-color）原样传递。")
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd == "list":
        return cmd_list()

    if cmd == "run":
        if not rest:
            print("Error: run requires a target (module id or 'all').", file=sys.stderr)
            return 2
        target = rest[0]
        sub = rest[1:]
        skip_ids: List[str] = []
        passthrough: List[str] = []
        i = 0
        while i < len(sub):
            a = sub[i]
            if a == "--skip" and i + 1 < len(sub):
                skip_ids.extend(s.strip() for s in sub[i + 1].split(",") if s.strip())
                i += 2
                continue
            passthrough.append(a)
            i += 1
        return cmd_run(target, passthrough, skip_ids)

    print(f"Error: unknown command '{cmd}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
