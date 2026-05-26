#!/usr/bin/env python3
"""模块 11：Run 健康度（trajectory 视角的总体运行状况）。

Complementary to ``recent_errors``. Where ``recent_errors`` aggregates log
errors and per-tool failures, this module gives a single global view of
*runs* — each one a complete agent invocation — across configurable time
windows.

Data source: ``<sessionId>.trajectory.jsonl`` files emitted by OpenClaw
runtime (event types: session.started / trace.metadata / context.compiled /
prompt.submitted / model.completed / trace.artifacts / session.ended).

Output: text + JSON, with ``windows = {24h, 7d, 30d}`` (default 7d). Verdict
escalates to *fail* on any active tool-call leak in 24h, *warn* on >5%
error rate or >30% compaction rate in 24h.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output
from ocdiag.trajectory import (
    Run,
    collect_runs,
    discover_trajectory_files,
    ms_ago,
    now_ms,
)


WINDOW_MS = {
    "24h": 24 * 3600 * 1000,
    "7d":  7 * 24 * 3600 * 1000,
    "30d": 30 * 24 * 3600 * 1000,
    "all": None,
}


def _filter_window(runs: List[Run], window: str) -> List[Run]:
    ms = WINDOW_MS.get(window)
    if ms is None:
        return list(runs)
    cutoff = ms_ago(ms)
    return [r for r in runs if r.started_ts_ms and r.started_ts_ms >= cutoff]


def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(round(p * (len(sorted_vals) - 1)))
    return float(sorted_vals[idx])


def _window_stats(runs: List[Run]) -> Dict:
    """Compute a single window's stats. Pure function."""
    total = len(runs)
    by_trigger = Counter(r.trigger for r in runs)
    by_status = Counter()
    for r in runs:
        if r.incomplete:
            by_status["incomplete"] += 1
        else:
            by_status[r.final_status or "incomplete"] += 1

    abort_breakdown = Counter()
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

    durations_by_trigger: Dict[str, List[float]] = {}
    for r in runs:
        d = r.duration_ms
        if d is None or d < 0:
            continue
        durations_by_trigger.setdefault(r.trigger, []).append(d / 1000.0)

    duration_stats = {}
    for trig, durs in durations_by_trigger.items():
        durs.sort()
        duration_stats[trig] = {
            "count": len(durs),
            "avg_s": round(sum(durs) / len(durs), 3) if durs else 0.0,
            "p50_s": round(_percentile(durs, 0.50), 3),
            "p95_s": round(_percentile(durs, 0.95), 3),
            "max_s": round(durs[-1], 3) if durs else 0.0,
        }

    compaction_count = sum(1 for r in runs if r.compaction_count > 0)
    error_count = by_status.get("error", 0)

    return {
        "total": total,
        "by_trigger": dict(by_trigger),
        "by_final_status": dict(by_status),
        "abort_breakdown": dict(abort_breakdown),
        "active_leak_count": len(leaks),
        "active_leak_samples": [
            {
                "sessionId": r.session_id,
                "runId": r.run_id,
                "trigger": r.trigger,
                "active": r.active_count,
                "started_ts_ms": r.started_ts_ms,
                "tool_metas": [m.get("toolName") for m in r.tool_metas],
            }
            for r in leaks[:5]
        ],
        "duration_stats": duration_stats,
        "compaction_runs": compaction_count,
        "compaction_rate_pct": round(compaction_count / total * 100, 2) if total else 0.0,
        "error_count": error_count,
        "error_rate_pct": round(error_count / total * 100, 2) if total else 0.0,
    }


def _top_long_runs(runs: List[Run], n: int = 10) -> List[Dict]:
    by_dur = []
    for r in runs:
        d = r.duration_ms
        if d is None:
            continue
        by_dur.append((d, r))
    by_dur.sort(key=lambda kv: -kv[0])
    out: List[Dict] = []
    for d, r in by_dur[:n]:
        out.append({
            "sessionId": r.session_id,
            "runId": r.run_id,
            "trigger": r.trigger,
            "duration_s": round(d / 1000.0, 1),
            "final_status": r.final_status,
            "compaction_count": r.compaction_count,
            "started_ts_ms": r.started_ts_ms,
        })
    return out


def _format_pct(part: int, total: int) -> str:
    if not total:
        return "0%"
    return f"{part / total * 100:.1f}%"


def _render_window(out: output.Output, label: str, stats: Dict) -> None:
    out.subsection(f"窗口 {label}")
    total = stats["total"]
    if total == 0:
        out.item("窗口内无 run（trajectory 数据空或时间窗口外）")
        return

    by_trigger = stats["by_trigger"]
    out.item(f"runs total: {total}")
    if by_trigger:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(by_trigger.items(),
                                                       key=lambda x: -x[1]))
        out.item(f"  by trigger: {parts}")

    bs = stats["by_final_status"]
    parts = ", ".join(f"{k}={v}" for k, v in sorted(bs.items(),
                                                   key=lambda x: -x[1]))
    out.item(f"  by final_status: {parts}")
    out.item(f"  error 占比: {_format_pct(stats['error_count'], total)}")

    ab = stats["abort_breakdown"]
    if ab:
        ab_parts = ", ".join(f"{k}={v}" for k, v in sorted(ab.items(),
                                                          key=lambda x: -x[1]))
        out.item(f"  abort flags: {ab_parts}")

    leaks = stats["active_leak_count"]
    if leaks:
        out.item(f"  警告：工具调用泄漏 {leaks} 个 run（active_count > 0）")
        for s in stats["active_leak_samples"]:
            tn = ",".join(s["tool_metas"]) or "?"
            out.item(f"    {s['sessionId'][:8]}#{s['runId'][:8]} "
                     f"trigger={s['trigger']} active={s['active']} tools=[{tn}]")
    else:
        out.item("  active_count 泄漏: 0")

    crate = stats["compaction_rate_pct"]
    out.item(f"  compaction 触发: {stats['compaction_runs']} 个 run "
             f"({crate:.1f}%)")

    ds = stats["duration_stats"]
    if ds:
        out.item("  wall 耗时（按 trigger）:")
        for trig in sorted(ds.keys()):
            d = ds[trig]
            out.item(f"    {trig}: n={d['count']} avg={d['avg_s']:.1f}s "
                     f"P50={d['p50_s']:.1f}s P95={d['p95_s']:.1f}s "
                     f"Max={d['max_s']:.1f}s")


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 11：Run 健康度（trajectory 视角的总体运行状况）",
    )
    parser.add_argument(
        "--window", default="7d", choices=sorted(WINDOW_MS.keys()),
        help="主时间窗口（额外报告 24h / 7d / 30d 三个窗口的对比数据）",
    )
    parser.add_argument(
        "--mask", action="store_true",
        help="对 trajectory 文本字段（assistantTexts 等）启用 sanitize（默认 plaintext）",
    )
    args = parser.parse_args()

    out = output.init("run_health", json_mode=args.json, no_color=args.no_color)
    out.section("模块 11：Run 健康度")

    out.progress(1, 3, "扫描 trajectory")
    files = discover_trajectory_files(args.sessions_base)
    if not files:
        out.item("未发现任何 trajectory 文件 — OpenClaw 2026.5.x 之前版本不会生成")
        out.set_data("found", False)
        out.set_data("checked", args.sessions_base)
        return out.done()

    out.set_data("trajectory_files", len(files))

    out.progress(2, 3, "聚合 run（多窗口）")
    # Single sweep — keep all runs in memory once, then filter per-window.
    # Bound: ~few thousand Run dataclasses, well under our 500MB cap.
    all_runs = collect_runs(files)
    out.set_data("runs_total_all_time", len(all_runs))

    out.item(f"扫描了 {len(files)} 个 trajectory 文件，共 {len(all_runs)} 个 run")

    windows_payload: Dict[str, Dict] = {}
    out.progress(3, 3, "渲染窗口")
    for w in ("24h", "7d", "30d"):
        runs_w = _filter_window(all_runs, w)
        stats = _window_stats(runs_w)
        windows_payload[w] = stats

    # Render the user-selected primary window prominently first, then the
    # other two for context.
    primary = args.window if args.window in windows_payload else "7d"
    other = [w for w in ("24h", "7d", "30d") if w != primary]
    _render_window(out, f"{primary} (主)", windows_payload[primary])
    for w in other:
        _render_window(out, w, windows_payload[w])

    # Top 10 longest runs in 7d for ops-style triage.
    runs_7d = _filter_window(all_runs, "7d")
    top_long = _top_long_runs(runs_7d, n=10)
    out.subsection("最近 7d 最慢的 10 个 run")
    if not top_long:
        out.item("（无足够 run 数据）")
    else:
        for i, r in enumerate(top_long, 1):
            tail = f" compaction={r['compaction_count']}" if r["compaction_count"] else ""
            status = r["final_status"] or "incomplete"
            out.item(f"  #{i} {r['sessionId'][:8]}#{r['runId'][:8]} "
                     f"trigger={r['trigger']} {r['duration_s']}s "
                     f"status={status}{tail}")

    out.set_data("windows", windows_payload)
    out.set_data("top_long_runs_7d", top_long)
    out.set_data("primary_window", primary)

    # Verdict signals (consumed by Output's keyword detector). We surface
    # explicit phrasing that the negative-pattern filter does NOT catch when
    # numbers are non-zero.
    w24 = windows_payload["24h"]
    out.subsection("Verdict 信号 (24h)")
    if w24["active_leak_count"] > 0:
        out.item(
            f"FATAL: 24h 内检测到 {w24['active_leak_count']} 个 stuck "
            "（active_count>0 的运行）"
        )
    else:
        out.item("近 24h 无 active_count 泄漏")
    rate = w24["error_rate_pct"]
    if rate > 5:
        out.item(f"警告：24h error rate 偏高: {rate}% — 共 {w24['error_count']} 次错误")
    else:
        out.item(f"24h error rate: {rate}% (健康)")
    crate = w24["compaction_rate_pct"]
    if crate > 30:
        out.item(f"警告：24h compaction 率偏高: {crate}% — 共 {w24['compaction_runs']} 次")
    else:
        out.item(f"24h compaction 率: {crate}% (健康)")

    return out.done()


if __name__ == "__main__":
    sys.exit(main())
