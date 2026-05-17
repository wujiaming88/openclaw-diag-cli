"""Dispatcher: every diagnostic is a top-level subcommand.

Layout:
  ocdiag <state-collector>      runs that collector (e.g. `ocdiag gateway`)
  ocdiag <object-inspector> ARG runs that inspector  (e.g. `ocdiag trace UUID`)
  ocdiag all [--skip a,b]       runs every state collector
  ocdiag list                   prints the catalogue grouped by parameter mode
  ocdiag run <id> [args...]     legacy alias retained for 0.1.x users
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import time
import traceback
from pathlib import Path
from typing import List


REPO_ROOT = Path(__file__).resolve().parent.parent

# State collectors: zero required args, parameter-free observation of system state.
STATE_COLLECTORS = [
    ("sys_health",     "系统健康检查",          "diag/01_sys_health.py"),
    ("environment",    "OpenClaw 基础环境",     "diag/02_environment.py"),
    ("configuration",  "配置展平（脱敏）",      "diag/03_configuration.py"),
    ("gateway",        "Gateway 状态",          "diag/04_gateway.py"),
    ("recent_errors",  "近期错误聚合",          "diag/05_recent_errors.py"),
    ("cron_jobs",      "定时任务状态",          "diag/06_cron_jobs.py"),
    ("performance",    "模型/工具性能",         "diag/07_performance.py"),
    ("sessions",       "Session 数据",          "diag/08_sessions.py"),
    ("plugin_diag",    "插件诊断",              "diag/09_plugin_diag.py"),
    ("shell_history",  "Shell 历史",            "diag/10_shell_history.py"),
]

# Object inspectors: take a session uuid (or other identifier) and inspect it.
OBJECT_INSPECTORS = [
    ("trace",   "追踪用户消息时间轴",  "tools/oc_session_trace.py"),
    ("extract", "导出 session 为可读格式", "tools/oc_session_extract.py"),
]

STATE_BY_ID = {mid: (label, script) for mid, label, script in STATE_COLLECTORS}
OBJECT_BY_ID = {mid: (label, script) for mid, label, script in OBJECT_INSPECTORS}
MODULE_BY_ID = {**STATE_BY_ID, **OBJECT_BY_ID}
MODULE_IDS = set(MODULE_BY_ID.keys())


def cmd_list_json() -> int:
    """Machine-readable module catalogue. Single source of truth consumed
    by the Node shell and the bundle script (axiom #3)."""
    payload = {
        "state_collectors": [
            {"id": mid, "label": label, "script": rel}
            for mid, label, rel in STATE_COLLECTORS
        ],
        "object_inspectors": [
            {"id": mid, "label": label, "script": rel}
            for mid, label, rel in OBJECT_INSPECTORS
        ],
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_list() -> int:
    print("Available diagnostics:")
    print()
    print("  State collectors (no args needed):")
    for mid, label, _ in STATE_COLLECTORS:
        print(f"    {mid:<16s} {label}")
    print()
    print("  Object inspectors (require session uuid):")
    for mid, label, _ in OBJECT_INSPECTORS:
        print(f"    {mid:<16s} {label}")
    print()
    print("  Meta:")
    print("    all              跑全部 state collectors")
    print("    doctor           检查 Node/Python/OpenClaw 环境")
    print("    bundle <id>      打包成 self-contained 单文件")
    return 0


def run_script(
    script_rel: str,
    extra_args: List[str],
    module_id: str = None,
) -> int:
    """Execute a diag script in-process. Returns the rc.

    On crash, in addition to the human-readable stderr trace we emit a single
    NDJSON error record to stdout when the script was invoked with --json.
    This guarantees `all --json` produces N records for N modules — including
    crashes — so downstream parsers don't silently lose modules. (Axiom #4)
    """
    script_path = REPO_ROOT / script_rel
    if not script_path.is_file():
        print(f"Error: script not found: {script_path}", file=sys.stderr)
        return 2
    json_mode = "--json" in extra_args
    mid = module_id or script_path.stem
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
    except BaseException as e:  # noqa: BLE001 — emit then re-classify
        print(f"  ERROR: {script_path.name} crashed: {type(e).__name__}: {e}",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        if json_mode:
            err_record = {
                "module": mid,
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            }
            try:
                sys.stdout.write(json.dumps(err_record, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except Exception:
                # If stdout itself is broken (closed pipe), there's nothing
                # productive to do — the stderr trace above already records
                # the crash.
                pass
        return 2
    finally:
        sys.argv = saved_argv


def cmd_all(extra_args: List[str], skip_ids: List[str]) -> int:
    json_mode = "--json" in extra_args
    progress_stream = sys.stderr if json_mode else sys.stdout
    rc_overall = 0
    total = sum(1 for mid, _, _ in STATE_COLLECTORS if mid not in skip_ids)
    n = 0
    for mid, label, script in STATE_COLLECTORS:
        if mid in skip_ids:
            continue
        n += 1
        print(f"\n[{n}/{total}] {label} ({mid})...", flush=True, file=progress_stream)
        t0 = time.time()
        rc = run_script(script, extra_args, module_id=mid)
        elapsed = time.time() - t0
        print(f"[{n}/{total}] {label} ({mid}) ... done ({elapsed:.1f}s)",
              flush=True, file=progress_stream)
        if rc != 0:
            rc_overall = rc
    return rc_overall


def _split_skip(rest: List[str]) -> (List[str], List[str]):
    """Pull out --skip a,b out of an argv tail; return (skip_ids, passthrough)."""
    skip_ids: List[str] = []
    passthrough: List[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--skip" and i + 1 < len(rest):
            skip_ids.extend(s.strip() for s in rest[i + 1].split(",") if s.strip())
            i += 2
            continue
        passthrough.append(a)
        i += 1
    return skip_ids, passthrough


def print_help() -> None:
    print("ocdiag — OpenClaw 诊断工具箱")
    print()
    print("Usage:")
    print("  ocdiag <id> [args...]            跑单个诊断（state collector 或 object inspector）")
    print("  ocdiag all [--skip a,b]          跑全部 state collectors")
    print("  ocdiag list                      列出所有诊断")
    print("  ocdiag run <id> [args...]        旧用法别名（0.1.x 兼容）")
    print()
    print("State collectors:")
    print("  " + "  ".join(mid for mid, _, _ in STATE_COLLECTORS))
    print("Object inspectors:")
    print("  " + "  ".join(mid for mid, _, _ in OBJECT_INSPECTORS))
    print()
    print("--skip 后接逗号分隔 id 列表（仅对 all 有意义）。")
    print("其它参数（--config / --log-dir / --json / --no-color）原样传递给脚本。")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help"):
        print_help()
        return 0

    head, rest = argv[0], argv[1:]

    if head == "list":
        if "--json" in rest:
            return cmd_list_json()
        return cmd_list()

    if head == "doctor":
        from ocdiag import doctor
        json_mode = "--json" in rest
        node_version = None
        for i, a in enumerate(rest):
            if a == "--node-version" and i + 1 < len(rest):
                node_version = rest[i + 1]
                break
        return doctor.run(json_mode=json_mode, node_version=node_version)

    if head == "all":
        skip_ids, passthrough = _split_skip(rest)
        return cmd_all(passthrough, skip_ids)

    # Backward-compat alias: `ocdiag run <id> [args...]` still works.
    if head == "run":
        if not rest:
            print("Error: run requires a target (module id or 'all').", file=sys.stderr)
            return 2
        target, sub = rest[0], rest[1:]
        if target == "all":
            skip_ids, passthrough = _split_skip(sub)
            return cmd_all(passthrough, skip_ids)
        if target in MODULE_BY_ID:
            _, script = MODULE_BY_ID[target]
            return run_script(script, sub, module_id=target)
        print(f"Error: unknown diagnostic '{target}'. Use `ocdiag list`.", file=sys.stderr)
        return 2

    if head in MODULE_BY_ID:
        _, script = MODULE_BY_ID[head]
        return run_script(script, rest, module_id=head)

    print(f"Error: unknown command '{head}'. Use `ocdiag list` to see available diagnostics.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
