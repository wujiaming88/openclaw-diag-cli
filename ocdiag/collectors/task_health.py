"""task_health collector — task/subagent execution diagnostics.

Reads ~/.openclaw/tasks/runs.sqlite to surface success/failure rates,
timeout patterns, stuck tasks, and per-runtime breakdowns.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict


STUCK_AGE_MS = 3600 * 1000  # 1 hour
RUNTIMES = ("acp", "subagent", "cron", "cli")
TERMINAL_STATUSES = ("succeeded", "failed", "timed_out")
SAMPLE_LIMIT = 5


def _fmt_iso(ms: Optional[int]) -> str:
    if not ms:
        return "?"
    try:
        return _dt.datetime.fromtimestamp(
            ms / 1000.0, tz=_dt.timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, ValueError, OverflowError):
        return "?"


def _short_id(s: Optional[str], n: int = 8) -> str:
    if not s:
        return "?"
    return s[:n]


def _truncate(s: Optional[str], n: int = 100) -> str:
    if not s:
        return ""
    flat = re.sub(r"\s+", " ", str(s)).strip()
    return flat[:n]


def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(round(p * (len(sorted_vals) - 1)))
    return float(sorted_vals[idx])


def _open_db(path: str) -> Optional[sqlite3.Connection]:
    if not os.path.isfile(path):
        return None
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _fetch_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    try:
        cur = conn.execute(
            "SELECT task_id, runtime, status, agent_id, label, "
            "created_at, started_at, ended_at, last_event_at, "
            "error, terminal_outcome FROM task_runs",
        )
        return cur.fetchall()
    except sqlite3.Error:
        return []


def _section_overview(s: Section, rows: List[sqlite3.Row]) -> Dict[str, Any]:
    total = len(rows)
    by_status = Counter(r["status"] for r in rows)
    by_runtime = Counter(r["runtime"] for r in rows)

    succeeded = by_status.get("succeeded", 0)
    failed = by_status.get("failed", 0)
    timed_out = by_status.get("timed_out", 0)
    running = by_status.get("running", 0)
    terminal_total = succeeded + failed + timed_out

    fail_rate = (failed / terminal_total * 100) if terminal_total else 0.0
    timeout_rate = (
        timed_out / terminal_total * 100 if terminal_total else 0.0
    )

    payload = {
        "total": total,
        "by_status": dict(by_status),
        "by_runtime": dict(by_runtime),
        "fail_rate_pct": round(fail_rate, 2),
        "timeout_rate_pct": round(timeout_rate, 2),
    }

    body_lines = [
        f"总任务: {total}",
        f"状态: succeeded={succeeded} failed={failed} "
        f"timed_out={timed_out} running={running}",
    ]
    if by_runtime:
        body_lines.append(
            "运行时: "
            + " ".join(f"{k}={by_runtime[k]}" for k in sorted(by_runtime)),
        )
    body_lines.append(
        f"失败率: {fail_rate:.1f}% | 超时率: {timeout_rate:.1f}%",
    )
    body = "\n".join(body_lines)

    if total == 0:
        s.ok("task_health.overview", "数据库无任务记录", data=payload)
        return payload

    if fail_rate > 40:
        s.fail(
            "task_health.overview",
            f"失败率 {fail_rate:.1f}% > 40%",
            evidence=body, data=payload,
        )
    elif fail_rate > 20:
        s.warn(
            "task_health.overview",
            f"失败率 {fail_rate:.1f}% > 20%",
            evidence=body, data=payload,
        )
    elif timeout_rate > 10:
        s.warn(
            "task_health.overview",
            f"超时率 {timeout_rate:.1f}% > 10%",
            evidence=body, data=payload,
        )
    else:
        s.ok(
            "task_health.overview",
            f"任务总览: {total} 个 / 失败率 {fail_rate:.1f}% / "
            f"超时率 {timeout_rate:.1f}%",
            evidence=body, data=payload,
        )
    return payload


def _section_failures(s: Section, rows: List[sqlite3.Row]) -> Dict[str, Any]:
    failures = [r for r in rows if r["status"] == "failed"]
    failures.sort(key=lambda r: r["created_at"] or 0, reverse=True)

    samples: List[Dict[str, Any]] = []
    for r in failures[:SAMPLE_LIMIT]:
        samples.append({
            "task_id": _short_id(r["task_id"]),
            "runtime": r["runtime"],
            "agent_id": r["agent_id"],
            "error": _truncate(r["error"], 100),
            "created_at": _fmt_iso(r["created_at"]),
        })

    error_patterns = Counter()
    for r in failures:
        err = _truncate(r["error"], 60)
        if err:
            error_patterns[err] += 1

    payload = {
        "failed_total": len(failures),
        "recent_failures": samples,
        "top_error_patterns": [
            {"pattern": p, "count": c}
            for p, c in error_patterns.most_common(5)
        ],
    }

    if not failures:
        s.ok(
            "task_health.failures",
            "无失败任务",
            data=payload,
        )
        return payload

    body_lines = [f"共 {len(failures)} 个失败任务，最近 {len(samples)} 个:"]
    for sample in samples:
        body_lines.append(
            f"  {sample['task_id']} runtime={sample['runtime']} "
            f"agent={sample['agent_id'] or '-'} "
            f"@ {sample['created_at']}",
        )
        if sample["error"]:
            body_lines.append(f"    error: {sample['error']}")
    if error_patterns:
        body_lines.append("常见错误模式:")
        for p, c in error_patterns.most_common(3):
            body_lines.append(f"  ×{c} {p}")

    consecutive = _count_recent_consecutive_failures(rows)
    payload["consecutive_failures"] = consecutive

    if consecutive > 5:
        s.fail(
            "task_health.failures",
            f"连续失败 {consecutive} 次（>5）",
            evidence="\n".join(body_lines),
            data=payload,
        )
    else:
        s.warn(
            "task_health.failures",
            f"失败任务: {len(failures)} 个",
            evidence="\n".join(body_lines),
            data=payload,
        )
    return payload


def _count_recent_consecutive_failures(rows: List[sqlite3.Row]) -> int:
    """Count terminal task runs in reverse chronological order until non-fail."""
    terminal = [
        r for r in rows
        if r["status"] in TERMINAL_STATUSES and r["created_at"]
    ]
    terminal.sort(key=lambda r: r["created_at"], reverse=True)
    n = 0
    for r in terminal:
        if r["status"] == "failed":
            n += 1
        else:
            break
    return n


def _section_timeouts(s: Section, rows: List[sqlite3.Row]) -> Dict[str, Any]:
    timed = [r for r in rows if r["status"] == "timed_out"]
    by_runtime: Counter = Counter(r["runtime"] for r in timed)

    durations: List[float] = []
    for r in timed:
        if r["ended_at"] and r["created_at"]:
            d = (r["ended_at"] - r["created_at"]) / 1000.0
            if d >= 0:
                durations.append(d)
    avg_dur = (
        round(sum(durations) / len(durations), 1) if durations else 0.0
    )

    payload = {
        "timed_out_total": len(timed),
        "by_runtime": dict(by_runtime),
        "avg_duration_s": avg_dur,
    }

    if not timed:
        s.ok("task_health.timeouts", "无超时任务", data=payload)
        return payload

    body_lines = [
        f"共 {len(timed)} 个超时任务",
        "按 runtime 分布: "
        + ", ".join(f"{k}={v}" for k, v in by_runtime.most_common()),
        f"平均耗时（created→ended）: {avg_dur:.1f}s",
    ]
    s.warn(
        "task_health.timeouts",
        f"超时任务: {len(timed)} 个",
        evidence="\n".join(body_lines),
        data=payload,
    )
    return payload


def _section_stuck(s: Section, rows: List[sqlite3.Row]) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - STUCK_AGE_MS
    running = [r for r in rows if r["status"] == "running"]
    stuck: List[Dict[str, Any]] = []
    young: List[Dict[str, Any]] = []
    for r in running:
        ts = r["created_at"]
        if not ts:
            continue
        age_h = round((now_ms - ts) / 3600000.0, 2)
        entry = {
            "task_id": _short_id(r["task_id"]),
            "runtime": r["runtime"],
            "agent_id": r["agent_id"],
            "age_hours": age_h,
            "created_at": _fmt_iso(ts),
        }
        if ts < cutoff:
            stuck.append(entry)
        else:
            young.append(entry)
    stuck.sort(key=lambda e: e["age_hours"], reverse=True)

    payload = {
        "running_total": len(running),
        "stuck_count": len(stuck),
        "stuck_samples": stuck[:10],
        "running_under_1h": len(young),
    }

    if not running:
        s.ok(
            "task_health.stuck",
            "无 running 任务",
            data=payload,
        )
        return payload

    if not stuck:
        s.ok(
            "task_health.stuck",
            f"running 任务: {len(running)} 个，全部 < 1h",
            data=payload,
        )
        return payload

    body_lines = [
        f"检测到 {len(stuck)} 个卡住任务 (running > 1h)，"
        f"另有 {len(young)} 个仍在正常窗口内",
    ]
    for e in stuck[:10]:
        body_lines.append(
            f"  {e['task_id']} runtime={e['runtime']} "
            f"agent={e['agent_id'] or '-'} age={e['age_hours']}h "
            f"created={e['created_at']}",
        )
    s.fail(
        "task_health.stuck",
        f"卡住任务: {len(stuck)} 个 running > 1h",
        evidence="\n".join(body_lines),
        data=payload,
    )
    return payload


def _section_runtime_breakdown(
    s: Section, rows: List[sqlite3.Row],
) -> Dict[str, Any]:
    by_runtime: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        by_runtime.setdefault(r["runtime"], []).append(r)

    breakdown: Dict[str, Dict[str, Any]] = {}
    warnings: List[Tuple[str, float]] = []

    for rt, rrows in sorted(by_runtime.items()):
        total = len(rrows)
        succ = sum(1 for r in rrows if r["status"] == "succeeded")
        terminal = sum(1 for r in rrows if r["status"] in TERMINAL_STATUSES)
        success_rate = (succ / terminal * 100) if terminal else None

        durations: List[float] = []
        for r in rrows:
            if r["ended_at"] and r["started_at"]:
                d = (r["ended_at"] - r["started_at"]) / 1000.0
                if d >= 0:
                    durations.append(d)
        durations.sort()
        avg_dur = (
            round(sum(durations) / len(durations), 1) if durations else 0.0
        )
        p95_dur = round(_percentile(durations, 0.95), 1) if durations else 0.0

        breakdown[rt] = {
            "total": total,
            "terminal": terminal,
            "success_rate_pct": (
                round(success_rate, 2) if success_rate is not None else None
            ),
            "avg_duration_s": avg_dur,
            "p95_duration_s": p95_dur,
            "duration_sample_size": len(durations),
        }
        if (
            success_rate is not None
            and terminal >= 5
            and success_rate < 80
        ):
            warnings.append((rt, success_rate))

    payload = {"runtime_breakdown": breakdown}

    if not breakdown:
        s.ok(
            "task_health.runtime",
            "无任务可分类",
            data=payload,
        )
        return payload

    body_lines = []
    for rt, st in breakdown.items():
        sr = (
            f"{st['success_rate_pct']:.1f}%"
            if st["success_rate_pct"] is not None else "n/a"
        )
        body_lines.append(
            f"{rt}: total={st['total']} terminal={st['terminal']} "
            f"success={sr} avg={st['avg_duration_s']:.1f}s "
            f"P95={st['p95_duration_s']:.1f}s",
        )

    if warnings:
        warn_msg = ", ".join(f"{rt}={sr:.1f}%" for rt, sr in warnings)
        s.warn(
            "task_health.runtime",
            f"runtime 成功率偏低: {warn_msg}",
            evidence="\n".join(body_lines),
            data=payload,
        )
    else:
        s.ok(
            "task_health.runtime",
            f"runtime 分布: {len(breakdown)} 类",
            evidence="\n".join(body_lines),
            data=payload,
        )
    return payload


@register
class TaskHealthCollector:
    id = "task_health"
    title = "Task 健康度"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)

        db_path = os.path.join(str(ctx.openclaw_home), "tasks", "runs.sqlite")
        report.data["db_path"] = db_path

        s_disc = report.section("12.1 数据源")
        conn = _open_db(db_path)
        if conn is None:
            s_disc.ok(
                "task_health.discovery",
                "tasks/runs.sqlite 不存在或不可读 — 跳过 task 健康度分析",
                data={"found": False, "checked": db_path},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        try:
            rows = _fetch_rows(conn)
        finally:
            conn.close()

        s_disc.ok(
            "task_health.discovery",
            f"读取 task_runs: {len(rows)} 条记录",
            data={"found": True, "rows": len(rows), "db_path": db_path},
        )
        report.data["task_runs_total"] = len(rows)

        s_overview = report.section("12.2 总览")
        report.data.update(_section_overview(s_overview, rows))

        s_fail = report.section("12.3 失败分析")
        report.data.update(_section_failures(s_fail, rows))

        s_to = report.section("12.4 超时分析")
        report.data["timeout_analysis"] = _section_timeouts(s_to, rows)

        s_stuck = report.section("12.5 卡住检测")
        report.data["stuck_analysis"] = _section_stuck(s_stuck, rows)

        s_rt = report.section("12.6 运行时分布")
        report.data.update(_section_runtime_breakdown(s_rt, rows))

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
