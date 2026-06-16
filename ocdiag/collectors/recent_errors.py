"""recent_errors collector — log error aggregation by level + session tools."""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import subprocess
import time
from collections import Counter
from typing import List, Tuple

from .. import recent_logs, trajectory
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section


_ERR_RE = re.compile(r'"logLevelName"\s*:\s*"(ERROR|FATAL)"')
_FATAL_RE = re.compile(r'"logLevelName"\s*:\s*"FATAL"')
_LEVEL_KEY = re.compile(
    r'"(logLevelName|level)"\s*:\s*"(ERROR|WARN|error|warn)"',
)
_HTTP_ERR_RE = re.compile(
    r"HTTP [45][0-9][0-9]|\"status\":\s*(?:4[0-9][0-9]|5[0-9][0-9])|"
    r"rate.limit|quota.exceeded",
    re.IGNORECASE,
)
_API_EXCLUDE_SUB_RE = re.compile(
    r'"subsystem":\s*"(tools|agent/embedded)"|allowlist contains',
    re.IGNORECASE,
)
_API_EXCLUDE_TXT_RE = re.compile(
    r"embedded run agent|agent end|agent start", re.IGNORECASE,
)
_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\]]*)\]\s*(.*)")
_SUBSYSTEM_STRIP_RE = re.compile(r'\s*\[\{[^}]*"subsystem"[^}]*\}\]\s*')


def _extract_msg(obj):
    parts = []
    for k in ("0", "1", "2", "msg", "message"):
        v = obj.get(k, "")
        if not v or not isinstance(v, str):
            continue
        if v.startswith("{"):
            try:
                inner = json.loads(v)
                if isinstance(inner, dict):
                    meaningful = {
                        ik: iv for ik, iv in inner.items() if ik != "subsystem"
                    }
                    if meaningful:
                        parts.append(
                            " ".join(
                                f"{ik}={iv}" for ik, iv in meaningful.items()
                            ),
                        )
                    continue
            except (json.JSONDecodeError, ValueError):
                pass
        parts.append(v)
    return " ".join(parts) if parts else None


def _render_log_line(line: str, max_len: int = 300) -> str:
    line = line.strip()
    if not line:
        return ""
    try:
        obj = json.loads(line)
        ts = obj.get("time", "")[:19]
        msg = _extract_msg(obj)
        if not msg:
            msg = str({k: v for k, v in obj.items() if k not in ("_meta", "time")})
        if isinstance(msg, str) and len(msg) > max_len:
            msg = msg[:max_len] + "..."
        level = obj.get("_meta", {}).get("logLevelName", "ERROR")
        return f"[{ts}] {level}: {msg}"
    except (json.JSONDecodeError, ValueError, AttributeError):
        line = _SUBSYSTEM_STRIP_RE.sub(" ", line).strip()
        m = _TS_RE.match(line)
        if m:
            line = f"[{m.group(1)[:19]}] {m.group(2)}"
        if len(line) > max_len:
            line = line[:max_len] + "..."
        return line


def _collect_error_lines(log_files: List[str]) -> Tuple[List[str], List[str], List[dict]]:
    """Return (error_lines, fatal_lines, unreadable_files)."""
    err_out: List[str] = []
    fatal_out: List[str] = []
    unreadable: List[dict] = []
    for lf in log_files:
        try:
            with open(lf, errors="replace") as f:
                for ln in f:
                    if _ERR_RE.search(ln):
                        err_out.append(ln.rstrip("\n"))
                        if _FATAL_RE.search(ln):
                            fatal_out.append(ln.rstrip("\n"))
        except OSError as e:
            unreadable.append({"path": lf, "error": f"{type(e).__name__}: {e}"})
    return err_out, fatal_out, unreadable


def _collect_api_errors(log_files: List[str]) -> Tuple[List[str], List[dict]]:
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


def _journalctl_errors() -> str:
    try:
        r = subprocess.run(
            [
                "journalctl", "--user", "-u", "openclaw-gateway",
                "--since", "today", "--priority", "err", "--no-pager",
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _find_recent_session(sessions_base: str):
    if not os.path.isdir(sessions_base):
        return None
    best = None
    best_mtime = -1.0
    for f in glob.glob(
        os.path.join(sessions_base, "*", "**", "*.jsonl"), recursive=True,
    ):
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if m > best_mtime:
            best_mtime = m
            best = f
    return best


def _tool_errors_from_session(session_path: str) -> Counter:
    counts: Counter = Counter()
    try:
        with open(session_path, errors="replace") as f:
            lines = f.readlines()
        for line in lines[-500:]:
            try:
                obj = json.loads(line)
                msg = obj.get("message", {}) or {}
                if msg.get("isError"):
                    counts[msg.get("toolName", "unknown")] += 1
            except (json.JSONDecodeError, ValueError):
                continue
    except OSError:
        return counts
    return counts


def _section_app_logs(s: Section, log_dir: str) -> dict:
    data: dict = {}
    logs = recent_logs.discover_recent_logs(log_dir)
    data["scanned_logs"] = [os.path.basename(p) for p in logs]
    if not logs:
        s.ok(
            "logs.app_today",
            "今日无更新的日志文件",
            data={"found": False},
        )
        return data

    file_lines = []
    for lf in logs:
        try:
            ts = os.path.getmtime(lf)
            ts_str = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        except OSError:
            ts_str = "?"
        file_lines.append(f"{os.path.basename(lf)} (mtime: {ts_str})")
    s.ok(
        "logs.app_today",
        f"今日有更新的日志文件: {len(logs)} 个",
        detail="\n".join(file_lines),
        data={"files": data["scanned_logs"]},
    )

    err_lines, fatal_lines, unreadable = _collect_error_lines(logs)
    data["app_error_count"] = len(err_lines)
    data["app_fatal_count"] = len(fatal_lines)
    if unreadable:
        data["app_log_unreadable"] = unreadable

    rendered = []
    for ln in err_lines[:100]:
        r = _render_log_line(ln, 300)
        if r:
            rendered.append(r)
    if len(err_lines) > 100:
        rendered.append(f"... 共 {len(err_lines)} 条")
    evidence = "\n".join(rendered) if rendered else None

    if fatal_lines:
        s.fail(
            "logs.app_errors",
            f"应用日志: FATAL {len(fatal_lines)} 条 / ERROR {len(err_lines)} 条",
            evidence=evidence,
            data={
                "error_count": len(err_lines),
                "fatal_count": len(fatal_lines),
            },
        )
    elif err_lines:
        s.warn(
            "logs.app_errors",
            f"应用日志: ERROR {len(err_lines)} 条",
            evidence=evidence,
            data={"error_count": len(err_lines), "fatal_count": 0},
        )
    else:
        s.ok(
            "logs.app_errors",
            "应用日志 ERROR 级别: 0 条",
            data={"error_count": 0, "fatal_count": 0},
        )

    # Model API HTTP errors removed — already checked by gateway collector
    return data


def _section_journalctl(s: Section) -> dict:
    data: dict = {}
    journal_out = _journalctl_errors()
    if (
        journal_out
        and "No entries" not in journal_out
        and "no entries" not in journal_out
    ):
        lines = journal_out.splitlines()[:50]
        data["journalctl_errors"] = len(lines)
        s.warn(
            "logs.journalctl",
            f"Journalctl ERROR 级别: {len(lines)} 条",
            evidence="\n".join(lines),
            data={"count": len(lines)},
        )
    else:
        data["journalctl_errors"] = 0
        s.ok(
            "logs.journalctl",
            "Journalctl ERROR: 0 条",
            data={"count": 0},
        )
    return data


def _section_session_errors(s: Section, sessions_base: str) -> dict:
    data: dict = {}
    recent_session = _find_recent_session(sessions_base)
    if not recent_session:
        s.ok(
            "logs.session_tools",
            "未找到最近 Session 文件，跳过工具调用检查",
            data={"found": False},
        )
        return data
    counts = _tool_errors_from_session(recent_session)
    total = sum(counts.values())
    data["session_tool_error_count"] = total
    detail = (
        "; ".join(f"{n}:{c}" for n, c in counts.most_common(10))
        if counts else None
    )
    if total > 0:
        data["session_tool_errors"] = dict(counts)
        s.warn(
            "logs.session_tools",
            f"最近 Session 工具调用错误: {total} 次",
            evidence=detail,
            data={
                "total": total,
                "session_path": recent_session,
                "by_tool": dict(counts),
            },
        )
    else:
        s.ok(
            "logs.session_tools",
            "最近 Session 工具调用错误: 0 次",
            data={"total": 0, "session_path": recent_session},
        )
    return data


def _section_trajectory_errors(s: Section, ctx: DiagContext) -> dict:
    data: dict = {}
    files = ctx.trajectory_files()
    if not files:
        s.ok(
            "trajectory.errors",
            "未发现 trajectory 文件 — 跳过 run 错误信号",
            data={"found": False},
        )
        return data

    runs = ctx.collect_runs(
        since_ms=trajectory.ms_ago(7 * 86400 * 1000),
    )
    if not runs:
        s.ok(
            "trajectory.errors",
            "最近 7d 无 trajectory run",
            data={"found": True, "runs_7d": 0},
        )
        return data

    runs_24h = [
        r for r in runs
        if r.started_ts_ms
        and r.started_ts_ms >= trajectory.ms_ago(86400 * 1000)
    ]

    abort_breakdown = {
        "aborted": 0, "externalAbort": 0, "timedOut": 0,
        "idleTimedOut": 0, "timedOutDuringCompaction": 0,
        "timedOutDuringToolExecution": 0,
    }
    abort_breakdown_24h = {k: 0 for k in abort_breakdown}
    pes_dist: dict = {}
    leak_runs: list = []
    error_runs: list = []
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
        is_24h = (
            r.started_ts_ms
            and r.started_ts_ms >= trajectory.ms_ago(86400 * 1000)
        )
        for name, val in flags:
            if val:
                abort_breakdown[name] += 1
                if is_24h:
                    abort_breakdown_24h[name] += 1
        if r.prompt_error_source:
            pes_dist[r.prompt_error_source] = (
                pes_dist.get(r.prompt_error_source, 0) + 1
            )
        if r.active_count > 0:
            leak_runs.append(r)
        if r.final_status == "error":
            error_runs.append(r)
        if is_24h and r.compaction_count > 0:
            compaction_runs_24h += 1

    abort_total_24h = sum(abort_breakdown_24h.values())
    leak_count = len(leak_runs)

    failing_tools: dict = {}
    for r in error_runs:
        for m in r.tool_metas:
            tn = m.get("toolName")
            if tn:
                failing_tools[tn] = failing_tools.get(tn, 0) + 1

    samples = [
        r for r in runs
        if r.aborted or r.external_abort or r.timed_out
        or r.idle_timed_out or r.final_status == "error"
    ]
    samples.sort(key=lambda x: x.started_ts_ms, reverse=True)
    sample_payload = []
    sample_lines = []
    for r in samples[:5]:
        causes = []
        if r.aborted:
            causes.append("aborted")
        if r.external_abort:
            causes.append("externalAbort")
        if r.timed_out:
            causes.append("timedOut")
        if r.idle_timed_out:
            causes.append("idleTimedOut")
        if r.timed_out_during_compaction:
            causes.append("timedOutDuringCompaction")
        if r.timed_out_during_tool_execution:
            causes.append("timedOutDuringToolExecution")
        if r.final_status == "error" and not causes:
            causes = ["final_status=error"]
        sample_lines.append(
            f"{r.session_id[:8]}#{r.run_id[:8]} trigger={r.trigger} "
            f"causes=[{','.join(causes)}]",
        )
        sample_payload.append({
            "sessionId": r.session_id, "runId": r.run_id,
            "trigger": r.trigger, "causes": causes,
            "started_ts_ms": r.started_ts_ms,
        })

    compaction_rate_24h = (
        compaction_runs_24h / len(runs_24h) * 100 if runs_24h else 0.0
    )

    summary = {
        "found": True,
        "runs_7d": len(runs),
        "runs_24h": len(runs_24h),
        "abort_breakdown_7d": abort_breakdown,
        "abort_breakdown_24h": abort_breakdown_24h,
        "abort_total_24h": abort_total_24h,
        "prompt_error_sources": pes_dist,
        "tool_leak_count": leak_count,
        "top_failing_tools": dict(
            sorted(failing_tools.items(), key=lambda x: -x[1])[:10],
        ),
        "compaction_rate_24h_pct": round(compaction_rate_24h, 2),
        "recent_error_samples": sample_payload,
    }
    data["trajectory_errors"] = summary

    body_lines = [
        f"窗口: 最近 7d 共 {len(runs)} 个 run（24h 内 {len(runs_24h)} 个）",
    ]
    nonzero_aborts = {k: v for k, v in abort_breakdown.items() if v}
    if nonzero_aborts:
        parts = ", ".join(
            f"{k}={v}" for k, v in sorted(
                nonzero_aborts.items(), key=lambda x: -x[1],
            )
        )
        body_lines.append(f"abort 分类（7d）: {parts}")
    if pes_dist:
        parts = ", ".join(
            f"{k}={v}" for k, v in sorted(pes_dist.items(), key=lambda x: -x[1])
        )
        body_lines.append(f"promptErrorSource: {parts}")
    if failing_tools:
        ranked = sorted(failing_tools.items(), key=lambda x: -x[1])[:10]
        body_lines.append(
            "最常失败工具: " + ", ".join(f"{n}:{c}" for n, c in ranked),
        )
    if sample_lines:
        body_lines.append("")
        body_lines.append("最近 abort/error 样本:")
        body_lines.extend(f"    {ln}" for ln in sample_lines)

    if leak_count > 0:
        s.fail(
            "trajectory.tool_leak",
            f"工具调用泄漏 (active_count>0): {leak_count} 个 run（7d）",
            data={"leak_count": leak_count},
        )
    else:
        s.ok(
            "trajectory.tool_leak",
            "工具调用泄漏: 0",
            data={"leak_count": 0},
        )

    if abort_total_24h > 10:
        s.fail(
            "trajectory.abort_24h",
            f"24h abort/timeout: {abort_total_24h} 次（>10）",
            evidence="\n".join(body_lines),
            data=summary,
        )
    elif abort_total_24h > 0:
        s.warn(
            "trajectory.abort_24h",
            f"24h abort/timeout: {abort_total_24h} 次",
            evidence="\n".join(body_lines),
            data=summary,
        )
    else:
        s.ok(
            "trajectory.abort_24h",
            "24h abort/timeout: 0 条",
            evidence="\n".join(body_lines),
            data=summary,
        )

    if len(runs_24h) >= 5 and compaction_rate_24h > 20:
        s.warn(
            "trajectory.compaction_24h",
            f"24h compaction 率偏高: {compaction_rate_24h:.1f}% "
            f"({compaction_runs_24h}/{len(runs_24h)})",
            data={
                "rate_pct": round(compaction_rate_24h, 2),
                "runs_with_compaction": compaction_runs_24h,
                "total_runs_24h": len(runs_24h),
            },
        )
    return data


@register
class RecentErrorsCollector:
    id = "recent_errors"
    title = "近期错误"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)
        report.add_scope("journald", "today")
        report.add_scope("app_logs", "today")
        report.add_scope("trajectory", "7d")

        s_app = report.section("5.1 应用日志")
        report.data.update(_section_app_logs(s_app, str(ctx.log_dir)))

        s_journal = report.section("5.2 Journalctl")
        report.data.update(_section_journalctl(s_journal))

        s_sess = report.section("5.3 Session 工具错误")
        report.data.update(
            _section_session_errors(s_sess, str(ctx.sessions_base)),
        )

        s_traj = report.section("5.4 Trajectory 错误信号 (7d)")
        report.data.update(
            _section_trajectory_errors(s_traj, ctx),
        )

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
