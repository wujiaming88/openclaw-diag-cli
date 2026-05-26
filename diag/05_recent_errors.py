#!/usr/bin/env python3
"""模块 5：近期错误日志（应用日志 + journalctl + 工具调用错误）。"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output, recent_logs, trajectory


_ERR_RE = re.compile(r'"logLevelName"\s*:\s*"(ERROR|FATAL)"')
_LEVEL_KEY = re.compile(r'"(logLevelName|level)"\s*:\s*"(ERROR|WARN|error|warn)"')
_HTTP_ERR_RE = re.compile(
    r"HTTP [45][0-9][0-9]|\"status\":\s*(?:4[0-9][0-9]|5[0-9][0-9])|"
    r"rate.limit|quota.exceeded",
    re.IGNORECASE,
)
_API_EXCLUDE_SUB_RE = re.compile(
    r'"subsystem":\s*"(tools|agent/embedded)"|allowlist contains',
    re.IGNORECASE,
)
_API_EXCLUDE_TXT_RE = re.compile(r"embedded run agent|agent end|agent start", re.IGNORECASE)
_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\]]*)\]\s*(.*)")
_SUBSYSTEM_STRIP_RE = re.compile(r'\s*\[\{[^}]*"subsystem"[^}]*\}\]\s*')


def extract_msg(obj):
    parts = []
    for k in ("0", "1", "2", "msg", "message"):
        v = obj.get(k, "")
        if not v or not isinstance(v, str):
            continue
        if v.startswith("{"):
            try:
                inner = json.loads(v)
                if isinstance(inner, dict):
                    meaningful = {ik: iv for ik, iv in inner.items() if ik != "subsystem"}
                    if meaningful:
                        parts.append(" ".join(f"{ik}={iv}" for ik, iv in meaningful.items()))
                    continue
            except Exception:
                pass
        parts.append(v)
    return " ".join(parts) if parts else None


def render_log_line(line: str, max_len: int = 300) -> str:
    line = line.strip()
    if not line:
        return ""
    try:
        obj = json.loads(line)
        ts = obj.get("time", "")[:19]
        msg = extract_msg(obj)
        if not msg:
            msg = str({k: v for k, v in obj.items() if k not in ("_meta", "time")})
        if isinstance(msg, str) and len(msg) > max_len:
            msg = msg[:max_len] + "..."
        level = obj.get("_meta", {}).get("logLevelName", "ERROR")
        return f"[{ts}] {level}: {msg}"
    except Exception:
        line = _SUBSYSTEM_STRIP_RE.sub(" ", line).strip()
        m = _TS_RE.match(line)
        if m:
            line = f"[{m.group(1)[:19]}] {m.group(2)}"
        if len(line) > max_len:
            line = line[:max_len] + "..."
        return line


def collect_error_lines(log_files: List[str]):
    """Returns (matched_lines, unreadable_files). One unreadable file does not
    abort the whole scan, but we tell the caller which paths failed."""
    out: List[str] = []
    unreadable: List[dict] = []
    for lf in log_files:
        try:
            with open(lf, errors="replace") as f:
                for ln in f:
                    if _ERR_RE.search(ln):
                        out.append(ln.rstrip("\n"))
        except OSError as e:
            unreadable.append({"path": lf, "error": f"{type(e).__name__}: {e}"})
    return out, unreadable


def collect_api_errors(log_files: List[str]):
    out: List[str] = []
    unreadable: List[dict] = []
    for lf in log_files:
        try:
            with open(lf, errors="replace") as f:
                for ln in f:
                    if not _LEVEL_KEY.search(ln):
                        continue
                    if not _HTTP_ERR_RE.search(ln):
                        continue
                    if _API_EXCLUDE_SUB_RE.search(ln):
                        continue
                    if _API_EXCLUDE_TXT_RE.search(ln):
                        continue
                    out.append(ln.rstrip("\n"))
        except OSError as e:
            unreadable.append({"path": lf, "error": f"{type(e).__name__}: {e}"})
    return out, unreadable


def journalctl_errors() -> str:
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "openclaw-gateway",
             "--since", "today", "--priority", "err", "--no-pager"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def find_recent_session(sessions_base: str):
    if not os.path.isdir(sessions_base):
        return None
    best = None
    best_mtime = -1.0
    for f in glob.glob(os.path.join(sessions_base, "*", "**", "*.jsonl"), recursive=True):
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if m > best_mtime:
            best_mtime = m
            best = f
    return best


def tool_errors_from_session(session_path: str):
    counts = Counter()
    try:
        # tail-equivalent: load all but only keep last 500
        with open(session_path, errors="replace") as f:
            lines = f.readlines()
        for line in lines[-500:]:
            try:
                obj = json.loads(line)
                msg = obj.get("message", {}) or {}
                if msg.get("isError"):
                    counts[msg.get("toolName", "unknown")] += 1
            except (json.JSONDecodeError, ValueError):
                # Expected: session.jsonl can have malformed lines from
                # interrupted writes; skip and keep counting.
                continue
    except OSError:
        # Session file disappeared between glob() and open(). Caller already
        # falls back to "no recent session"; reporting per-file unreadable
        # would mostly add noise here.
        return counts
    return counts


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 5：采集近期错误日志",
    )
    args = parser.parse_args()

    out = output.init("recent_errors", json_mode=args.json, no_color=args.no_color)
    out.section("模块 5：近期日志")

    logs = recent_logs.discover_recent_logs(args.log_dir)
    out.set_data("scanned_logs", [os.path.basename(p) for p in logs])

    if logs:
        out.item(f"今日有更新的日志文件 ({len(logs)} 个):")
        for lf in logs:
            try:
                ts = os.path.getmtime(lf)
                ts_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            except OSError:
                ts_str = "?"
            out.item(f"  {os.path.basename(lf)} (mtime: {ts_str})")
    else:
        out.item("今日无更新的日志文件")

    out.line("")

    if logs:
        out.progress(1, 3, "应用日志")
        err_lines, err_unreadable = collect_error_lines(logs)
        out.set_data("app_error_count", len(err_lines))
        if err_unreadable:
            out.set_data("app_log_unreadable", err_unreadable)
        if err_lines:
            out.item(f"应用日志 ERROR 级别: {len(err_lines)} 条 — Gateway 运行时报错，包括工具失败、模型异常等")
            rendered = []
            for ln in err_lines[:100]:
                r = render_log_line(ln, 300)
                if r:
                    rendered.append(r)
            if len(err_lines) > 100:
                rendered.append(f"... 共 {len(err_lines)} 条")
            out.evidence("近期日志", "\n".join(rendered))
        else:
            out.item("应用日志 ERROR 级别: 0 条 — Gateway 运行时报错")

        api_lines, _api_unreadable = collect_api_errors(logs)
        out.set_data("api_error_count", len(api_lines))
        if api_lines:
            out.item(f"模型 API HTTP 错误: {len(api_lines)} 条 ")
            rendered = []
            for ln in api_lines[:100]:
                r = render_log_line(ln, 500)
                if r:
                    rendered.append(r)
            out.evidence("近期日志", "\n".join(rendered))
    else:
        out.item("应用日志未找到（今日无更新的日志文件）")

    out.progress(2, 3, "Journalctl")
    journal_out = journalctl_errors()
    if journal_out and "No entries" not in journal_out and "no entries" not in journal_out:
        lines = journal_out.splitlines()[:50]
        if lines:
            out.item("Journalctl ERROR 级别:")
            out.evidence("journalctl --priority err", "\n".join(lines))
        out.set_data("journalctl_errors", len(lines))
    else:
        out.item("Journalctl ERROR: 0 条 — 系统级进程错误")
        out.set_data("journalctl_errors", 0)

    out.progress(3, 4, "Session 错误")
    recent_session = find_recent_session(args.sessions_base)
    if recent_session:
        counts = tool_errors_from_session(recent_session)
        total = sum(counts.values())
        out.item(f"最近 Session 的工具调用错误: {total} — 工具返回 error 的次数，过多说明某个工具持续异常")
        out.set_data("session_tool_error_count", total)
        if total > 0:
            detail = "; ".join(f"{n}:{c}" for n, c in counts.most_common(10))
            out.evidence(os.path.basename(recent_session), detail)
            out.set_data("session_tool_errors", dict(counts))
    else:
        out.item("未找到 Session 文件，跳过工具调用检查")

    out.progress(4, 4, "Trajectory 错误信号 (7d)")
    trajectory_error_dimension(out, args.sessions_base)

    return out.done()


def trajectory_error_dimension(out: output.Output, sessions_base: str) -> None:
    """Surface run-level error signal from trajectory data (7d window).

    Source events: ``trace.artifacts`` (abort flags, promptErrorSource,
    finalStatus, itemLifecycle, toolMetas, compactionCount). Different
    audience from session-jsonl based collectors: this is run-level (one
    record per agent invocation) rather than message-level.
    """
    out.line("")
    out.line("  ── Trajectory: 7d 内的 Run 错误信号 ──")
    out.line("")
    files = trajectory.discover_trajectory_files(sessions_base)
    if not files:
        out.item("未发现 trajectory 文件 — 跳过 run 错误信号")
        out.set_data("trajectory_errors", {"found": False})
        return

    runs = trajectory.collect_runs(
        files, since_ms=trajectory.ms_ago(7 * 86400 * 1000),
    )
    runs_24h = [
        r for r in runs
        if r.started_ts_ms and r.started_ts_ms >= trajectory.ms_ago(86400 * 1000)
    ]
    if not runs:
        out.item("最近 7d 无 trajectory run")
        out.set_data("trajectory_errors", {"found": True, "runs_7d": 0})
        return

    abort_breakdown = {
        "aborted": 0, "externalAbort": 0, "timedOut": 0,
        "idleTimedOut": 0, "timedOutDuringCompaction": 0,
        "timedOutDuringToolExecution": 0,
    }
    abort_breakdown_24h = {k: 0 for k in abort_breakdown}
    pes_dist = {}
    leak_runs = []
    error_runs = []
    compaction_runs_24h = 0
    for r in runs:
        flags = (
            ("aborted", r.aborted),
            ("externalAbort", r.external_abort),
            ("timedOut", r.timed_out),
            ("idleTimedOut", r.idle_timed_out),
            ("timedOutDuringCompaction", r.timed_out_during_compaction),
            ("timedOutDuringToolExecution", r.timed_out_during_tool_execution),
        )
        is_24h = (r.started_ts_ms and
                  r.started_ts_ms >= trajectory.ms_ago(86400 * 1000))
        for name, val in flags:
            if val:
                abort_breakdown[name] += 1
                if is_24h:
                    abort_breakdown_24h[name] += 1
        if r.prompt_error_source:
            pes_dist[r.prompt_error_source] = pes_dist.get(r.prompt_error_source, 0) + 1
        if r.active_count > 0:
            leak_runs.append(r)
        if r.final_status == "error":
            error_runs.append(r)
        if is_24h and r.compaction_count > 0:
            compaction_runs_24h += 1

    abort_total_24h = sum(abort_breakdown_24h.values())
    leak_count = len(leak_runs)

    out.item(f"窗口: 最近 7 天，共 {len(runs)} 个 run（24h 内 {len(runs_24h)} 个）")
    nonzero_aborts = {k: v for k, v in abort_breakdown.items() if v}
    if nonzero_aborts:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(nonzero_aborts.items(),
                                                       key=lambda x: -x[1]))
        if abort_total_24h > 10:
            out.item(f"FATAL: 24h abort/timeout 事件: {abort_total_24h} 次（>10 阈值）")
        elif abort_total_24h > 0:
            out.item(f"警告：24h abort/timeout 事件: {abort_total_24h} 次")
        else:
            out.item(f"24h abort/timeout: 0 条")
        out.item(f"  abort 分类（7d）: {parts}")
    else:
        out.item("abort/timeout: 0 条 — 7d 内无 abort 事件")

    if pes_dist:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(pes_dist.items(),
                                                       key=lambda x: -x[1]))
        out.item(f"  promptErrorSource: {parts}")

    if leak_count:
        out.item(f"FATAL: 工具调用泄漏 (active_count>0): {leak_count} 个 run（7d）")
        for r in sorted(leak_runs,
                        key=lambda x: x.started_ts_ms, reverse=True)[:3]:
            tn = ",".join(m.get("toolName", "?") for m in r.tool_metas[:6])
            out.item(f"    {r.session_id[:8]}#{r.run_id[:8]} trigger={r.trigger} "
                     f"active={r.active_count} status={r.final_status} tools=[{tn}]")
    else:
        out.item("工具调用泄漏: 0 — active_count 无残留")

    failing_tools = {}
    for r in error_runs:
        for m in r.tool_metas:
            tn = m.get("toolName")
            if tn:
                failing_tools[tn] = failing_tools.get(tn, 0) + 1
    if failing_tools:
        ranked = sorted(failing_tools.items(), key=lambda x: -x[1])[:10]
        parts = ", ".join(f"{n}:{c}" for n, c in ranked)
        out.item(f"  最常失败工具（7d, error final_status）: {parts}")

    # Recent abort/error samples
    samples = [r for r in runs if (
        r.aborted or r.external_abort or r.timed_out
        or r.idle_timed_out or r.final_status == "error"
    )]
    samples.sort(key=lambda x: x.started_ts_ms, reverse=True)
    sample_payload = []
    for r in samples[:5]:
        causes = []
        if r.aborted: causes.append("aborted")
        if r.external_abort: causes.append("externalAbort")
        if r.timed_out: causes.append("timedOut")
        if r.idle_timed_out: causes.append("idleTimedOut")
        if r.timed_out_during_compaction: causes.append("timedOutDuringCompaction")
        if r.timed_out_during_tool_execution: causes.append("timedOutDuringToolExecution")
        if r.final_status == "error" and not causes:
            causes = ["final_status=error"]
        out.item(f"    {r.session_id[:8]}#{r.run_id[:8]} trigger={r.trigger} "
                 f"causes=[{','.join(causes)}]")
        sample_payload.append({
            "sessionId": r.session_id, "runId": r.run_id,
            "trigger": r.trigger, "causes": causes,
            "started_ts_ms": r.started_ts_ms,
        })

    compaction_rate_24h = (compaction_runs_24h / len(runs_24h) * 100) if runs_24h else 0.0
    if len(runs_24h) >= 5 and compaction_rate_24h > 20:
        out.item(f"警告：24h compaction 率偏高: {compaction_rate_24h:.1f}% "
                 f"({compaction_runs_24h}/{len(runs_24h)})")

    out.set_data("trajectory_errors", {
        "found": True,
        "runs_7d": len(runs),
        "runs_24h": len(runs_24h),
        "abort_breakdown_7d": abort_breakdown,
        "abort_breakdown_24h": abort_breakdown_24h,
        "abort_total_24h": abort_total_24h,
        "prompt_error_sources": pes_dist,
        "tool_leak_count": leak_count,
        "top_failing_tools": dict(sorted(failing_tools.items(),
                                         key=lambda x: -x[1])[:10]),
        "compaction_rate_24h_pct": round(compaction_rate_24h, 2),
        "recent_error_samples": sample_payload,
    })


if __name__ == "__main__":
    sys.exit(main())
