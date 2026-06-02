"""run_health collector — multi-window trajectory analysis (24h / 7d / 30d)."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any, Dict, List, Optional

from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict
from ..trajectory import (
    Run,
    collect_runs,
    discover_trajectory_files,
    ms_ago,
    now_ms,
)


WINDOW_MS = {
    "24h": 24 * 3600 * 1000,
    "7d": 7 * 24 * 3600 * 1000,
    "30d": 30 * 24 * 3600 * 1000,
}

ACTIVE_LEAK_AGE_MS = 3600 * 1000  # 1h


def _filter_window(runs: List[Run], window: str) -> List[Run]:
    ms = WINDOW_MS.get(window)
    if ms is None:
        return list(runs)
    cutoff = ms_ago(ms)
    return [
        r for r in runs
        if r.started_ts_ms and r.started_ts_ms >= cutoff
    ]


def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(round(p * (len(sorted_vals) - 1)))
    return float(sorted_vals[idx])


def _window_stats(runs: List[Run]) -> Dict[str, Any]:
    total = len(runs)
    by_trigger = Counter(r.trigger for r in runs)
    by_status: Counter = Counter()
    for r in runs:
        if r.incomplete:
            by_status["incomplete"] += 1
        else:
            by_status[r.final_status or "incomplete"] += 1

    abort_breakdown: Counter = Counter()
    for r in runs:
        if r.aborted:
            abort_breakdown["aborted"] += 1
        if r.external_abort:
            abort_breakdown["externalAbort"] += 1
        if r.timed_out:
            abort_breakdown["timedOut"] += 1
        if r.idle_timed_out:
            abort_breakdown["idleTimedOut"] += 1
        if r.timed_out_during_compaction:
            abort_breakdown["timedOutDuringCompaction"] += 1
        if r.timed_out_during_tool_execution:
            abort_breakdown["timedOutDuringToolExecution"] += 1

    leaks = [r for r in runs if r.active_count > 0]

    durations: List[float] = []
    durations_by_trigger: Dict[str, List[float]] = {}
    for r in runs:
        d = r.duration_ms
        if d is None or d < 0:
            continue
        durations.append(d / 1000.0)
        durations_by_trigger.setdefault(r.trigger, []).append(d / 1000.0)

    durations.sort()
    p50 = round(_percentile(durations, 0.50), 3) if durations else 0.0
    p95 = round(_percentile(durations, 0.95), 3) if durations else 0.0
    avg = round(sum(durations) / len(durations), 3) if durations else 0.0
    max_dur = round(durations[-1], 3) if durations else 0.0

    duration_stats: Dict[str, Dict[str, float]] = {}
    for trig, durs in durations_by_trigger.items():
        durs.sort()
        duration_stats[trig] = {
            "count": len(durs),
            "avg_s": round(sum(durs) / len(durs), 3),
            "p50_s": round(_percentile(durs, 0.50), 3),
            "p95_s": round(_percentile(durs, 0.95), 3),
            "max_s": round(durs[-1], 3),
        }

    completed = (
        by_status.get("success", 0) + by_status.get("completed", 0)
    )
    aborted_total = sum(
        by_status.get(k, 0) for k in ("aborted", "abort", "incomplete")
    ) + abort_breakdown.get("aborted", 0) + abort_breakdown.get(
        "externalAbort", 0,
    )
    error_count = by_status.get("error", 0)
    abort_rate_pct = (
        round(sum(abort_breakdown.values()) / total * 100, 2) if total else 0.0
    )

    compaction_count = sum(1 for r in runs if r.compaction_count > 0)

    return {
        "total": total,
        "completed": completed,
        "error_count": error_count,
        "error_rate_pct": (
            round(error_count / total * 100, 2) if total else 0.0
        ),
        "by_trigger": dict(by_trigger),
        "by_final_status": dict(by_status),
        "abort_breakdown": dict(abort_breakdown),
        "abort_total": sum(abort_breakdown.values()),
        "abort_rate_pct": abort_rate_pct,
        "active_leak_count": len(leaks),
        "active_leak_samples": [
            {
                "sessionId": r.session_id, "runId": r.run_id,
                "trigger": r.trigger, "active": r.active_count,
                "started_ts_ms": r.started_ts_ms,
                "tool_metas": [m.get("toolName") for m in r.tool_metas],
                "last_tool_call_names": list(r.last_tool_call_names),
            }
            for r in leaks[:5]
        ],
        "avg_duration_s": avg,
        "p50_duration_s": p50,
        "p95_duration_s": p95,
        "max_duration_s": max_dur,
        "duration_stats_by_trigger": duration_stats,
        "compaction_runs": compaction_count,
        "compaction_rate_pct": (
            round(compaction_count / total * 100, 2) if total else 0.0
        ),
    }


def _detect_active_leaks(runs: List[Run]) -> List[Dict[str, Any]]:
    """Active runs older than 1h that never ended — runtime leaks."""
    cutoff = now_ms() - ACTIVE_LEAK_AGE_MS
    leaks: List[Dict[str, Any]] = []
    for r in runs:
        if not r.incomplete:
            continue
        if not r.started_ts_ms or r.started_ts_ms >= cutoff:
            continue
        leaks.append({
            "sessionId": r.session_id, "runId": r.run_id,
            "trigger": r.trigger,
            "started_ts_ms": r.started_ts_ms,
            "age_hours": round(
                (now_ms() - r.started_ts_ms) / 3600000.0, 1,
            ),
            "active_count": r.active_count,
        })
    leaks.sort(key=lambda x: x["age_hours"], reverse=True)
    return leaks


def _format_window_lines(label: str, stats: Dict[str, Any]) -> List[str]:
    total = stats["total"]
    if total == 0:
        return [f"{label}: 窗口内无 run"]
    lines = [
        f"{label}: total={total} | completed={stats['completed']} | "
        f"error={stats['error_count']} ({stats['error_rate_pct']}%) | "
        f"abort={stats['abort_total']} ({stats['abort_rate_pct']}%)",
    ]
    if stats["by_trigger"]:
        parts = ", ".join(
            f"{k}={v}" for k, v in
            sorted(stats["by_trigger"].items(), key=lambda x: -x[1])
        )
        lines.append(f"  by trigger: {parts}")
    if stats["abort_breakdown"]:
        parts = ", ".join(
            f"{k}={v}" for k, v in
            sorted(stats["abort_breakdown"].items(), key=lambda x: -x[1])
        )
        lines.append(f"  abort flags: {parts}")
    lines.append(
        f"  duration: avg={stats['avg_duration_s']:.1f}s "
        f"P50={stats['p50_duration_s']:.1f}s "
        f"P95={stats['p95_duration_s']:.1f}s "
        f"Max={stats['max_duration_s']:.1f}s",
    )
    crate = stats["compaction_rate_pct"]
    lines.append(
        f"  compaction: {stats['compaction_runs']} run ({crate:.1f}%)",
    )
    return lines


def _section_windows(
    s: Section, all_runs: List[Run],
) -> Dict[str, Any]:
    windows_payload: Dict[str, Dict[str, Any]] = {}
    for w in ("24h", "7d", "30d"):
        windows_payload[w] = _window_stats(_filter_window(all_runs, w))

    body_lines: List[str] = []
    for w in ("24h", "7d", "30d"):
        body_lines.extend(_format_window_lines(w, windows_payload[w]))

    abort_warn = False
    abort_warn_windows: List[str] = []
    for w, st in windows_payload.items():
        if st["total"] >= 10 and st["abort_rate_pct"] > 10:
            abort_warn = True
            abort_warn_windows.append(
                f"{w}={st['abort_rate_pct']}%",
            )

    payload = {"windows": windows_payload}

    runs_24h = windows_payload["24h"]["total"]
    if runs_24h == 0:
        # 0 runs in 24h with sessions present is suspicious — let upstream
        # callers see; stay OK since it might also just mean idle.
        s.warn(
            "run_health.activity",
            "近 24h 无任何 run",
            evidence="\n".join(body_lines),
            data=payload,
        )
    elif abort_warn:
        s.warn(
            "run_health.abort_rate",
            f"abort 率偏高: {', '.join(abort_warn_windows)}",
            evidence="\n".join(body_lines),
            data=payload,
        )
    else:
        s.ok(
            "run_health.windows",
            f"多窗口 run 健康度: 24h={runs_24h}, "
            f"7d={windows_payload['7d']['total']}, "
            f"30d={windows_payload['30d']['total']}",
            evidence="\n".join(body_lines),
            data=payload,
        )
    return payload


def _section_active_leaks(
    s: Section, all_runs: List[Run],
) -> Dict[str, Any]:
    leaks = _detect_active_leaks(all_runs)
    payload = {"active_leak_runs": leaks, "active_leak_count": len(leaks)}
    if not leaks:
        s.ok(
            "run_health.active_leaks",
            "无 active 泄漏 run（未结束 + age > 1h）",
            data=payload,
        )
        return payload
    body_lines = [
        f"检测到 {len(leaks)} 个未正常结束 + 启动超过 1h 的 run",
    ]
    for lk in leaks[:10]:
        body_lines.append(
            f"  {lk['sessionId'][:8]}#{lk['runId'][:8]} "
            f"trigger={lk['trigger']} age={lk['age_hours']}h "
            f"active={lk['active_count']}",
        )
    s.fail(
        "run_health.active_leaks",
        f"Active run 泄漏: {len(leaks)} 个",
        evidence="\n".join(body_lines),
        data=payload,
    )
    return payload


def _section_p95(
    s: Section, all_runs: List[Run],
) -> Dict[str, Any]:
    runs_24h = _filter_window(all_runs, "24h")
    runs_7d = _filter_window(all_runs, "7d")

    def _p95(runs):
        durs = sorted(
            r.duration_ms / 1000.0 for r in runs
            if r.duration_ms is not None and r.duration_ms >= 0
        )
        return _percentile(durs, 0.95) if durs else 0.0

    p95_24h = round(_p95(runs_24h), 2)
    p95_7d = round(_p95(runs_7d), 2)
    payload = {
        "p95_wall_24h_s": p95_24h,
        "p95_wall_7d_s": p95_7d,
    }
    body = (
        f"P95 wall: 24h={p95_24h:.1f}s | 7d={p95_7d:.1f}s"
    )
    primary = p95_24h if runs_24h else p95_7d
    if primary > 600:
        s.fail(
            "run_health.p95_wall",
            f"P95 wall 时间 {primary:.1f}s (> 600s)",
            evidence=body,
            data=payload,
        )
    elif primary > 300:
        s.warn(
            "run_health.p95_wall",
            f"P95 wall 时间 {primary:.1f}s (> 300s)",
            evidence=body,
            data=payload,
        )
    else:
        s.ok(
            "run_health.p95_wall",
            f"P95 wall 时间 {primary:.1f}s (健康)",
            evidence=body,
            data=payload,
        )
    return payload


def _section_top_long(
    s: Section, all_runs: List[Run],
) -> Dict[str, Any]:
    runs_7d = _filter_window(all_runs, "7d")
    by_dur: List[tuple] = []
    for r in runs_7d:
        d = r.duration_ms
        if d is None or d < 0:
            continue
        by_dur.append((d, r))
    by_dur.sort(key=lambda x: -x[0])

    top: List[Dict[str, Any]] = []
    for d, r in by_dur[:10]:
        top.append({
            "sessionId": r.session_id, "runId": r.run_id,
            "trigger": r.trigger,
            "duration_s": round(d / 1000.0, 1),
            "final_status": r.final_status,
            "compaction_count": r.compaction_count,
            "started_ts_ms": r.started_ts_ms,
        })
    payload = {"top_long_runs_7d": top}
    if not top:
        s.ok(
            "run_health.top_long",
            "近 7d 无足够 run 数据",
            data=payload,
        )
        return payload
    body_lines = [f"近 7d 最慢的 {len(top)} 个 run:"]
    for i, r in enumerate(top, 1):
        tail = (
            f" compaction={r['compaction_count']}"
            if r["compaction_count"] else ""
        )
        status = r["final_status"] or "incomplete"
        body_lines.append(
            f"  #{i} {r['sessionId'][:8]}#{r['runId'][:8]} "
            f"trigger={r['trigger']} {r['duration_s']}s "
            f"status={status}{tail}",
        )
    s.ok(
        "run_health.top_long",
        f"近 7d 最慢的 {len(top)} 个 run",
        evidence="\n".join(body_lines),
        data=payload,
    )
    return payload


@register
class RunHealthCollector:
    id = "run_health"
    title = "Run 健康度"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)

        sessions_base = str(ctx.sessions_base)
        files = discover_trajectory_files(sessions_base)
        if not files:
            s = report.section("11.1 Trajectory 数据")
            s.ok(
                "run_health.discovery",
                "未发现 trajectory 文件 — 跳过 run 健康度分析",
                data={"found": False, "checked": sessions_base},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        all_runs = collect_runs(files)
        report.data["trajectory_files"] = len(files)
        report.data["runs_total_all_time"] = len(all_runs)

        s_disc = report.section("11.1 Trajectory 扫描")
        s_disc.ok(
            "run_health.discovery",
            f"扫描了 {len(files)} 个 trajectory 文件，共 {len(all_runs)} 个 run",
            data={"files": len(files), "runs_total": len(all_runs)},
        )

        s_win = report.section("11.2 多窗口分析")
        report.data.update(_section_windows(s_win, all_runs))

        s_leaks = report.section("11.3 Active run 泄漏")
        report.data.update(_section_active_leaks(s_leaks, all_runs))

        s_p95 = report.section("11.4 P95 Wall 时间")
        report.data.update(_section_p95(s_p95, all_runs))

        s_top = report.section("11.5 7d 最慢 Run")
        report.data.update(_section_top_long(s_top, all_runs))

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
