"""Dispatcher: every diagnostic is a top-level subcommand.

Layout:
  ocdiag <state-collector>      runs that collector (e.g. `ocdiag gateway`)
  ocdiag <object-inspector> ARG runs that inspector  (e.g. `ocdiag trace UUID`)
  ocdiag all [--skip a,b]       runs every state collector
  ocdiag list                   prints the catalogue grouped by parameter mode
  ocdiag bundle <id>            emits a self-contained single-file .py
  ocdiag doctor                 environment health check
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent

# State collectors: zero required args, parameter-free observation of system state.
STATE_COLLECTORS = [
    ("sys_health",     "系统健康（DNS / 网络 / CPU / 内存 / 磁盘 / 进程 / 时间）",  "diag/01_sys_health.py"),
    ("environment",    "OpenClaw 版本、Gateway 进程环境变量",                       "diag/02_environment.py"),
    ("configuration",  "openclaw.json 展平（敏感字段脱敏）",                        "diag/03_configuration.py"),
    ("gateway",        "Gateway 进程、端口、24h 重启、WS 生命周期、错误码",         "diag/04_gateway.py"),
    ("recent_errors",  "应用日志 / journalctl / session 工具调用错误聚合",           "diag/05_recent_errors.py"),
    ("cron_jobs",      "定时任务状态、连续失败、调度漂移、静默检测",                "diag/06_cron_jobs.py"),
    ("performance",    "模型/工具耗时 P50/P95、慢调用、E2E 延迟、Cache 命中率",     "diag/07_performance.py"),
    ("sessions",       "Session 总览、活跃度、Stuck 探测",                          "diag/08_sessions.py"),
    ("plugin_diag",    "插件状态一致性、ERROR/WARN、Hook、Channel、外部 DNS",       "diag/09_plugin_diag.py"),
    ("shell_history",  "Shell 历史中的高危命令与最近操作",                          "diag/10_shell_history.py"),
]

# Object inspectors: take a session uuid (or other identifier) and inspect it.
OBJECT_INSPECTORS = [
    ("trace",   "追踪一条用户消息从进入到响应的完整时间轴", "tools/oc_session_trace.py"),
    ("extract", "导出 session.jsonl 为可读格式",            "tools/oc_session_extract.py"),
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
    print("openclaw-diag — 可用诊断")
    print()
    print("  扫描类（无需参数）：")
    for mid, label, _ in STATE_COLLECTORS:
        print(f"    {mid:<16s} {label}")
    print()
    print("  对象类（需要 session uuid）：")
    for mid, label, _ in OBJECT_INSPECTORS:
        print(f"    {mid:<16s} {label}")
    print()
    print("  其它命令：")
    print("    all              一次跑完所有扫描类")
    print("    doctor           检查 Node / Python / openclaw-diag / OpenClaw 环境")
    print("    bundle <id>      生成 self-contained 单文件 .py（离线机器用）")
    return 0


def run_script(
    script_rel: str,
    extra_args: List[str],
    module_id: Optional[str] = None,
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
    saved_prog = os.environ.get("OPENCLAW_DIAG_PROG")
    try:
        # runpy.run_path resets sys.argv[0] to the script path, so we
        # advertise the user-facing name through an env var instead. cli.py
        # picks it up as the argparse prog so --help reads as
        # "openclaw-diag sys_health" rather than "01_sys_health.py".
        os.environ["OPENCLAW_DIAG_PROG"] = f"openclaw-diag {mid}"
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
        if saved_prog is None:
            os.environ.pop("OPENCLAW_DIAG_PROG", None)
        else:
            os.environ["OPENCLAW_DIAG_PROG"] = saved_prog


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


def cmd_bundle(rest: List[str]) -> int:
    """Generate a self-contained single-file diag script.

    Lives here (rather than in lib/bundle.py only) so the Python entry has
    parity with Node — `python3 bin/ocdiag bundle gateway` works the same as
    `node bin/openclaw-diag.js bundle gateway`. (Axiom #3)
    """
    if not rest or rest[0] in ("-h", "--help"):
        print("Usage: openclaw-diag bundle <id>", file=sys.stderr)
        print("       Emits the bundle to stdout. Use shell redirection to save.", file=sys.stderr)
        print(file=sys.stderr)
        print("Available ids:", file=sys.stderr)
        for mid, _label, _ in STATE_COLLECTORS:
            print(f"  {mid}", file=sys.stderr)
        return 0 if rest else 2
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    import bundle  # type: ignore
    return bundle.main(rest)


def _split_skip(rest: List[str]) -> Tuple[List[str], List[str]]:
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


def _suggest_command(unknown: str) -> Optional[str]:
    """Best-effort typo suggestion for a misspelled command."""
    import difflib
    candidates = list(MODULE_BY_ID.keys()) + ["all", "list", "doctor", "bundle"]
    matches = difflib.get_close_matches(unknown, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def print_help() -> None:
    print("openclaw-diag — OpenClaw 诊断工具箱")
    print()
    print("用法：")
    print("  openclaw-diag <id> [args...]      跑单个诊断")
    print("  openclaw-diag all [--skip a,b]    跑全部 state collectors")
    print("  openclaw-diag list                列出所有诊断")
    print("  openclaw-diag doctor              检查环境")
    print("  openclaw-diag bundle <id>         生成单文件 .py")
    print()
    print("扫描类（无需参数）：")
    print("  " + "  ".join(mid for mid, _, _ in STATE_COLLECTORS))
    print("对象类（需要 session uuid）：")
    print("  " + "  ".join(mid for mid, _, _ in OBJECT_INSPECTORS))
    print()
    print("常用 flag：--json（结构化输出）  --no-color（关掉颜色）  --unmask（不脱敏）")


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

    if head == "bundle":
        return cmd_bundle(rest)

    if head in MODULE_BY_ID:
        _, script = MODULE_BY_ID[head]
        return run_script(script, rest, module_id=head)

    suggestion = _suggest_command(head)
    hint = f"（你是不是想说 `{suggestion}`？）" if suggestion else ""
    print(f"Error: 未知命令 '{head}'{hint}", file=sys.stderr)
    print(f"运行 `openclaw-diag list` 查看全部诊断。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
