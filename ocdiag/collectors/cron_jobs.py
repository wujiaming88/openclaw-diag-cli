"""cron_jobs collector — jobs.json + jobs-state.json + runs/ analysis."""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import subprocess
import time
from collections import Counter, deque
from typing import List, Optional

from .. import paths, trajectory
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict
from ..timeutil import fmt_age, fmt_ts
from ..tokens import fmt_tokens, percentile

try:
    from croniter import croniter  # type: ignore
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False


def _fmt_duration(ms):
    if ms is None:
        return "?"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s/60:.1f}min"
    return f"{s/3600:.1f}h"


def _format_schedule(sched) -> str:
    k = sched.get("kind", "?")
    if k == "cron":
        return f"cron {sched.get('expr','?')} (tz={sched.get('tz','local')})"
    if k == "every":
        return f"every {sched.get('everyMs',0)/1000:.0f}s"
    if k == "at":
        return f"at {sched.get('at','?')}"
    return str(sched)[:100]


def _expected_interval_ms(sched, runs):
    k = sched.get("kind")
    if k == "every":
        return sched.get("everyMs")
    if k == "cron" and HAS_CRONITER:
        try:
            base = _dt.datetime.now()
            it = croniter(sched["expr"], base)
            t1 = it.get_next(_dt.datetime)
            t2 = it.get_next(_dt.datetime)
            return int((t2 - t1).total_seconds() * 1000)
        except Exception:
            pass
    if runs and len(runs) >= 3:
        ts_list = sorted([
            r.get("runAtMs") or r.get("ts") for r in runs
            if (r.get("runAtMs") or r.get("ts"))
        ])
        if len(ts_list) >= 3:
            gaps = sorted(
                ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)
            )
            return gaps[len(gaps) // 2]
    return None


def _load_runs(runs_dir, jid) -> List[dict]:
    if not jid or not runs_dir:
        return []
    p = os.path.join(runs_dir, f"{jid}.jsonl")
    if not os.path.isfile(p):
        return []
    buf: deque = deque(maxlen=200)
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    buf.append(line)
    except OSError:
        return []
    out = []
    for line in buf:
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            pass
    return out


def _extract_usage(r):
    u = r.get("usage")
    if not u and isinstance(r.get("result"), dict):
        u = r["result"].get("usage")
    if not isinstance(u, dict):
        return None, None, None
    inp = u.get("input") or u.get("input_tokens")
    out = u.get("output") or u.get("output_tokens")
    cost = None
    c = u.get("cost")
    if isinstance(c, dict):
        cost = c.get("total")
    elif isinstance(c, (int, float)):
        cost = c
    return inp, out, cost


def _extract_error_text(r) -> str:
    err = r.get("error") or r.get("errorMessage")
    if not err and isinstance(r.get("result"), dict):
        err = r["result"].get("error")
    if isinstance(err, dict):
        err = err.get("message") or json.dumps(err, ensure_ascii=False)
    if not err:
        return ""
    return re.sub(r"\s+", " ", str(err))[:100]


def _extract_delivery_reason(r) -> str:
    reason = r.get("deliveryError")
    if not reason:
        dlv = r.get("delivery")
        if isinstance(dlv, dict):
            res = dlv.get("resolved")
            if isinstance(res, dict):
                reason = res.get("error")
    if isinstance(reason, dict):
        reason = (
            reason.get("message") or json.dumps(reason, ensure_ascii=False)
        )
    if not reason:
        return ""
    return re.sub(r"\s+", " ", str(reason))[:100]


def _analyze(job, runs, now_ms) -> dict:
    state = job.get("state", {}) or {}
    enabled = job.get("enabled", True)

    finished = [
        r for r in runs if r.get("status") or r.get("action") == "finished"
    ]
    recent = finished[-20:]

    ok = sum(1 for r in recent if r.get("status") == "ok")
    fail = len(recent) - ok
    success_rate = (ok / len(recent) * 100) if recent else None

    durs = sorted(
        [r["durationMs"] for r in recent if r.get("durationMs") is not None],
    )
    p50 = percentile(durs, 0.5)
    p95 = percentile(durs, 0.95)
    dur_max = durs[-1] if durs else None
    last_dur = recent[-1].get("durationMs") if recent else None

    deliv = Counter(
        r.get("deliveryStatus") for r in recent if r.get("deliveryStatus")
    )
    deliv_total = sum(deliv.values())
    deliv_ok = deliv.get("delivered", 0)
    deliv_unrequested = deliv.get("not-requested", 0)
    deliv_effective = deliv_total - deliv_unrequested
    deliv_fail_rate = None
    if deliv_effective > 0:
        deliv_fail_rate = (deliv_effective - deliv_ok) / deliv_effective * 100

    exp_interval = _expected_interval_ms(job.get("schedule", {}), runs)

    flags: list = []
    if not enabled:
        return dict(
            status="disabled", flags=flags, recent=recent,
            success_rate=success_rate, ok=ok, fail=fail,
            p50=p50, p95=p95, dur_max=dur_max, last_dur=last_dur,
            deliv_ok=deliv_ok, deliv_total=deliv_total,
            deliv_effective=deliv_effective, deliv_fail_rate=deliv_fail_rate,
            exp_interval=exp_interval, consecutive_errors=0,
        )

    cerr = state.get("consecutiveErrors", 0) or 0
    last_err = job.get("lastError") or state.get("lastError") or ""
    if cerr >= 3:
        le = re.sub(r"\s+", " ", str(last_err))[:100] if last_err else ""
        flags.append((
            "error",
            f"连续失败 {cerr} 次" + (f"（最近错误: {le}）" if le else ""),
        ))
    elif cerr >= 1:
        flags.append(("note", f"最近失败 {cerr} 次"))

    if (success_rate is not None and len(recent) >= 5
            and success_rate < 80):
        flags.append((
            "error",
            f"成功率 {success_rate:.0f}%（最近 {len(recent)} 次）",
        ))

    next_run = state.get("nextRunAtMs")
    if next_run and exp_interval:
        drift = now_ms - next_run
        if drift > 2 * exp_interval:
            flags.append((
                "error",
                f"调度卡住：nextRun 已过期 {fmt_age(drift)}"
                f"（预期间隔 {fmt_age(exp_interval)}）",
            ))

    if last_dur and p95 and len(durs) >= 5 and last_dur > p95 * 2:
        flags.append((
            "note",
            f"最近耗时 {_fmt_duration(last_dur)} 超历史 P95 "
            f"({_fmt_duration(p95)}) 两倍",
        ))

    if (deliv_effective >= 5 and deliv_fail_rate is not None
            and deliv_fail_rate > 20):
        flags.append((
            "error",
            f"投递失败率 {deliv_fail_rate:.0f}%"
            f"（{deliv_effective - deliv_ok}/{deliv_effective}）",
        ))

    created = job.get("createdAtMs", 0) or 0
    age_since_create = now_ms - created if created else 0
    last_run = state.get("lastRunAtMs")
    is_silent = False
    if age_since_create > 3600 * 1000:
        if not last_run and not runs:
            flags.append(("silent", "任务已创建但从未执行"))
            is_silent = True
        elif exp_interval and last_run:
            idle = now_ms - last_run
            if idle > 2 * exp_interval:
                flags.append((
                    "silent",
                    f"已 {fmt_age(idle)} 未执行"
                    f"（预期间隔 {fmt_age(exp_interval)}）",
                ))
                is_silent = True

    if is_silent:
        status = "silent"
    elif any(f[0] == "error" for f in flags):
        status = "warn"
    else:
        status = "ok"

    return dict(
        status=status, flags=flags, recent=recent,
        success_rate=success_rate, ok=ok, fail=fail,
        p50=p50, p95=p95, dur_max=dur_max, last_dur=last_dur,
        deliv_ok=deliv_ok, deliv_total=deliv_total,
        deliv_effective=deliv_effective, deliv_fail_rate=deliv_fail_rate,
        exp_interval=exp_interval, consecutive_errors=cerr,
    )


def _section_jobs(s: Section, jobs_file: str, state_file: str,
                  runs_dir: str) -> dict:
    data: dict = {}
    if not os.path.isfile(jobs_file):
        s.ok(
            "cron.jobs",
            "jobs.json 不存在 — 未创建过定时任务",
            data={"found": False},
        )
        return data

    try:
        with open(jobs_file) as f:
            jdata = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        s.warn(
            "cron.jobs",
            f"jobs.json 解析失败: {e}",
            data={"found": True, "parse_error": str(e)},
        )
        return data

    if isinstance(jdata, dict):
        jobs = jdata.get("jobs", [])
        if isinstance(jobs, dict):
            jobs = list(jobs.values())
        elif not isinstance(jobs, list):
            jobs = []
    elif isinstance(jdata, list):
        jobs = jdata
    else:
        jobs = []

    if not jobs:
        s.ok(
            "cron.jobs",
            "jobs.json 存在但无任务",
            data={"total_jobs": 0},
        )
        return data

    ext_state: dict = {}
    if state_file and os.path.isfile(state_file):
        try:
            with open(state_file) as f:
                sd = json.load(f)
            ext_jobs = sd.get("jobs", {}) if isinstance(sd, dict) else {}
            for jid, entry in ext_jobs.items():
                if isinstance(entry, dict):
                    ext_state[jid] = entry.get("state", {}) or {}
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    for j in jobs:
        jid = j.get("id")
        if jid and not j.get("state") and jid in ext_state:
            j["state"] = ext_state[jid]

    now_ms = int(time.time() * 1000)
    analyses = []
    for j in jobs:
        runs = _load_runs(runs_dir, j.get("id"))
        analyses.append((j, runs, _analyze(j, runs, now_ms)))

    total = len(jobs)
    enabled_count = sum(1 for j in jobs if j.get("enabled", True))
    disabled_count = total - enabled_count

    ok_list = [a for a in analyses if a[2]["status"] == "ok"]
    warn_list = [a for a in analyses if a[2]["status"] == "warn"]
    silent_list = [a for a in analyses if a[2]["status"] == "silent"]
    disabled_list = [a for a in analyses if a[2]["status"] == "disabled"]

    def _job_name(j):
        return j.get("name") or j.get("id", "?")

    data["total_jobs"] = total
    data["enabled_count"] = enabled_count
    data["disabled_count"] = disabled_count
    data["status_overview"] = {
        "ok": [_job_name(j) for j, _, _ in ok_list],
        "warn": [_job_name(j) for j, _, _ in warn_list],
        "silent": [_job_name(j) for j, _, _ in silent_list],
        "disabled": [_job_name(j) for j, _, _ in disabled_list],
    }

    jobs_payload = []
    for j, runs, a in analyses:
        state = j.get("state", {}) or {}
        jobs_payload.append({
            "id": j.get("id"),
            "name": j.get("name") or j.get("id"),
            "status": a["status"],
            "schedule": j.get("schedule", {}),
            "success_rate": a["success_rate"],
            "p50_ms": a["p50"],
            "p95_ms": a["p95"],
            "last_run_ts": state.get("lastRunAtMs"),
            "next_run_ts": state.get("nextRunAtMs"),
            "consecutive_errors": state.get("consecutiveErrors", 0) or 0,
            "flags": [{"kind": k, "msg": m} for k, m in a["flags"]],
        })
    data["jobs"] = jobs_payload

    overview_lines = [
        f"共 {total} 个任务（{enabled_count} 启用, {disabled_count} 禁用）",
    ]
    if ok_list:
        overview_lines.append(f"正常: {len(ok_list)} 个")
    if warn_list:
        overview_lines.append(f"异常: {len(warn_list)} 个")
        for j, _, a in warn_list:
            nm = _job_name(j)
            msg = next((f[1] for f in a["flags"] if f[0] == "error"), "")
            overview_lines.append(f"  · {nm}: {msg}")
    if silent_list:
        overview_lines.append(f"静默: {len(silent_list)} 个")
        for j, _, a in silent_list:
            nm = _job_name(j)
            msg = next((f[1] for f in a["flags"] if f[0] == "silent"), "")
            overview_lines.append(f"  · {nm}: {msg}")
    if disabled_list:
        overview_lines.append(f"禁用: {len(disabled_list)} 个")
    summary_msg = (
        f"任务总览: {total} 个（{enabled_count} 启用, "
        f"{disabled_count} 禁用 / {len(warn_list)} 异常, "
        f"{len(silent_list)} 静默）"
    )
    s.ok(
        "cron.overview",
        summary_msg,
        detail="\n".join(overview_lines),
        data={
            "total": total, "enabled": enabled_count,
            "disabled": disabled_count,
            "warn": len(warn_list), "silent": len(silent_list),
        },
    )

    detail_lines = []
    max_consec = 0
    for idx, (j, runs, a) in enumerate(analyses, 1):
        nm = _job_name(j)
        status = a["status"]
        icon_label = {
            "ok": "正常", "warn": "异常",
            "silent": "静默", "disabled": "禁用",
        }.get(status, "?")
        detail_lines.append(f"[{idx}] {nm} ({icon_label})")
        detail_lines.append(
            f"    调度: {_format_schedule(j.get('schedule', {}))}",
        )
        state = j.get("state", {}) or {}
        last_run = state.get("lastRunAtMs")
        if last_run:
            ls = state.get("lastStatus") or state.get("lastRunStatus") or "?"
            ld = state.get("lastDurationMs")
            line = f"    上次执行: {fmt_ts(last_run)} | {ls}"
            if ld is not None:
                line += f" | {_fmt_duration(ld)}"
            detail_lines.append(line)
        else:
            detail_lines.append("    上次执行: 从未执行")
        nr = state.get("nextRunAtMs")
        if nr:
            delta = nr - now_ms
            if delta >= 0:
                detail_lines.append(
                    f"    下次执行: {fmt_ts(nr)} (在 {fmt_age(delta)}后)",
                )
            else:
                detail_lines.append(
                    f"    下次执行: {fmt_ts(nr)} (已过期 {fmt_age(delta)})",
                )
        if a["success_rate"] is not None:
            n = a["ok"] + a["fail"]
            detail_lines.append(
                f"    成功率: {a['success_rate']:.0f}% "
                f"(最近 {n} 次: ok={a['ok']} fail={a['fail']})",
            )
        if a["p50"] is not None:
            parts = [f"P50={_fmt_duration(a['p50'])}"]
            if a["p95"] is not None and a["p95"] != a["p50"]:
                parts.append(f"P95={_fmt_duration(a['p95'])}")
            if a["dur_max"] is not None and a["dur_max"] != a["p50"]:
                parts.append(f"Max={_fmt_duration(a['dur_max'])}")
            detail_lines.append("    耗时: " + " ".join(parts))
        cerr = a.get("consecutive_errors", 0) or 0
        if cerr > max_consec:
            max_consec = cerr
        detail_lines.append("")
    detail = "\n".join(detail_lines)
    data["max_consecutive_errors"] = max_consec

    if max_consec >= 10:
        s.fail(
            "cron.consecutive_failures",
            f"任务连续失败次数最大为 {max_consec}（>=10）",
            detail=detail,
            data={"max_consecutive_errors": max_consec},
        )
    elif max_consec >= 3:
        s.warn(
            "cron.consecutive_failures",
            f"任务连续失败次数最大为 {max_consec}（>=3）",
            detail=detail,
            data={"max_consecutive_errors": max_consec},
        )
    else:
        s.ok(
            "cron.consecutive_failures",
            f"任务连续失败次数最大为 {max_consec}",
            detail=detail,
            data={"max_consecutive_errors": max_consec},
        )

    if silent_list:
        silent_names = ", ".join(_job_name(j) for j, _, _ in silent_list)
        s.warn(
            "cron.silent",
            f"静默任务: {len(silent_list)} 个未按预期执行",
            evidence=silent_names,
            data={"count": len(silent_list)},
        )
    else:
        s.ok(
            "cron.silent",
            "静默任务: 0",
            data={"count": 0},
        )
    return data


def _section_heartbeat(s: Section, ctx: DiagContext) -> dict:
    data: dict = {"agents": {}}

    cfg = ctx.config or {}
    hb_every = "未配置"
    hb = cfg.get("agents", {}).get("defaults", {}).get("heartbeat", {})
    if isinstance(hb, dict):
        hb_every = hb.get("every", "未配置")
    data["config_every"] = hb_every

    sessions_base = str(ctx.sessions_base)
    agent_workspaces: dict = {}
    for a in cfg.get("agents", {}).get("list", []) or []:
        if isinstance(a, dict) and a.get("id"):
            agent_workspaces[a["id"]] = a.get("workspace", "")

    agent_lines = [f"配置: agents.defaults.heartbeat.every = {hb_every}"]
    if os.path.isdir(sessions_base):
        for agent_dir in sorted(glob.glob(os.path.join(sessions_base, "*"))):
            agent_id = os.path.basename(agent_dir)
            ws_dir = agent_workspaces.get(agent_id, "")
            hb_file = os.path.join(ws_dir, "HEARTBEAT.md") if ws_dir else ""
            if ws_dir and os.path.isfile(hb_file):
                try:
                    with open(hb_file) as f:
                        content = f.read()
                    nonempty = [
                        ln for ln in content.splitlines()
                        if ln.strip()
                        and not ln.startswith(("#", "```", "<!--"))
                    ]
                except OSError:
                    nonempty = []
                if not nonempty:
                    agent_lines.append(
                        f"{agent_id}: HEARTBEAT.md 存在但为空（不会触发）",
                    )
                    data["agents"][agent_id] = {"heartbeat_md": "empty"}
                else:
                    agent_lines.append(
                        f"{agent_id}: HEARTBEAT.md 有内容（会触发）",
                    )
                    data["agents"][agent_id] = {"heartbeat_md": "active"}
            else:
                agent_lines.append(f"{agent_id}: HEARTBEAT.md 不存在")
                data["agents"][agent_id] = {"heartbeat_md": "missing"}

    log_pattern = os.path.join(str(ctx.log_dir), "openclaw-*.log")
    interesting: List[str] = []
    started: List[tuple] = []
    for lf in sorted(glob.glob(log_pattern)):
        try:
            with open(lf, errors="replace") as f:
                for raw in f:
                    if "gateway/heartbeat" not in raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    ts = d.get("time", "?")[:19]
                    interval = ""
                    if isinstance(d.get("1"), dict):
                        ms = d["1"].get("intervalMs", 0)
                        interval = f"interval={ms/1000:.0f}s"
                        msg = str(d.get("2", ""))
                    else:
                        msg = str(d.get("1", ""))
                        if isinstance(d.get("2"), str):
                            msg += " | " + d["2"]
                    level = d.get("_meta", {}).get("logLevelName", "")
                    line = f"{ts} | {level} | {msg} {interval}".strip()
                    if "started" in msg:
                        started.append((ts, level, msg, interval))
                    else:
                        interesting.append(line)
        except OSError:
            continue

    data["events"] = len(interesting)
    data["started_count"] = len(started)
    if interesting:
        s.ok(
            "cron.heartbeat",
            f"Heartbeat 事件: {len(interesting)} 条 / 启动 {len(started)} 条",
            detail="\n".join(agent_lines + ["", "Recent events:"]
                             + interesting[:50]),
            data={"events": len(interesting), "started": len(started)},
        )
    elif started:
        intervals = sorted({s_[3] for s_ in started if s_[3]})
        data["intervals"] = list(intervals)
        s.ok(
            "cron.heartbeat",
            f"Heartbeat 调度器: {len(started)} 次启动，间隔 "
            f"{'、'.join(intervals)}",
            detail="\n".join(agent_lines),
            data={"started": len(started), "intervals": list(intervals)},
        )
    else:
        s.ok(
            "cron.heartbeat",
            "Heartbeat 日志: 0 条",
            detail="\n".join(agent_lines),
            data={"events": 0, "started": 0},
        )
    return data


def _section_system_crontab(s: Section) -> dict:
    data: dict = {}
    try:
        r = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True,
            timeout=5, check=False,
        )
        text = r.stdout if r.returncode == 0 else r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        text = ""
    if not text or "no crontab" in text.lower():
        s.ok(
            "cron.system_crontab",
            "系统 crontab: 无（未配置）",
            data={"entries": []},
        )
        data["system_crontab"] = []
        return data
    entries = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    data["system_crontab"] = entries
    if entries:
        s.ok(
            "cron.system_crontab",
            f"系统 crontab: {len(entries)} 条",
            evidence="\n".join(entries),
            data={"entries": entries, "count": len(entries)},
        )
    else:
        s.ok(
            "cron.system_crontab",
            "系统 crontab: 仅注释",
            data={"entries": []},
        )
    return data


def _section_cron_trajectory(s: Section, ctx: DiagContext) -> dict:
    data: dict = {}
    files = ctx.trajectory_files()
    if not files:
        s.ok(
            "trajectory.cron",
            "未发现 trajectory 文件 — 跳过 cron 投递审计",
            data={"found": False},
        )
        return data
    runs = ctx.collect_runs(
        since_ms=trajectory.ms_ago(7 * 86400 * 1000),
    )
    cron_runs = [r for r in runs if r.trigger == "cron"]
    if not cron_runs:
        s.ok(
            "trajectory.cron",
            "最近 7d 无 cron-trigger run",
            data={"found": True, "cron_runs_7d": 0},
        )
    else:
        sent_ok = sum(1 for r in cron_runs if r.did_send_via_messaging_tool)
        final_success = sum(
            1 for r in cron_runs if r.final_status == "success"
        )
        final_error = sum(1 for r in cron_runs if r.final_status == "error")
        successful_adds = sum(r.successful_cron_adds for r in cron_runs)
        silent_runs = []
        for r in cron_runs:
            if r.final_status != "success":
                continue
            if r.did_send_via_messaging_tool:
                continue
            if r.successful_cron_adds > 0:
                continue
            text_len = sum(len(t) for t in r.assistant_texts)
            if text_len < 64:
                silent_runs.append((r, text_len))

        success_rate = (
            final_success / len(cron_runs) * 100 if cron_runs else 0.0
        )
        send_rate = sent_ok / len(cron_runs) * 100 if cron_runs else 0.0

        body = [
            f"7d cron run: {len(cron_runs)} 个 | success={final_success} "
            f"error={final_error} | did_send_via_messaging_tool={sent_ok} "
            f"({send_rate:.1f}%)",
            f"successful_cron_adds 总计: {successful_adds}",
        ]
        if silent_runs:
            body.append(
                f"静默 cron: {len(silent_runs)} 个（success+无投递+adds=0+短）",
            )
            for r, text_len in silent_runs[:5]:
                body.append(
                    f"  {r.session_id[:8]}#{r.run_id[:8]} "
                    f"text_len={text_len} "
                    f"targets={len(r.messaging_targets)}",
                )

        traj_data = {
            "found": True,
            "cron_runs_7d": len(cron_runs),
            "send_rate_pct": round(send_rate, 2),
            "success_rate_pct": round(success_rate, 2),
            "successful_cron_adds_7d": successful_adds,
            "silent_cron_runs": [
                {
                    "sessionId": r.session_id, "runId": r.run_id,
                    "started_ts_ms": r.started_ts_ms,
                    "text_len": text_len,
                    "messaging_targets": len(r.messaging_targets),
                }
                for r, text_len in silent_runs[:20]
            ],
        }
        data["trajectory_cron"] = traj_data

        if silent_runs:
            s.fail(
                "trajectory.silent_cron",
                f"静默 cron: {len(silent_runs)} 个 run",
                evidence="\n".join(body),
                data=traj_data,
            )
        elif final_success and success_rate < 95:
            s.warn(
                "trajectory.cron",
                f"7d cron 成功率 {success_rate:.1f}% < 95%",
                evidence="\n".join(body),
                data=traj_data,
            )
        else:
            s.ok(
                "trajectory.cron",
                f"7d cron run: {len(cron_runs)} 个 / "
                f"成功率 {success_rate:.1f}%",
                evidence="\n".join(body),
                data=traj_data,
            )

    by_trigger_send: dict = {}
    for r in runs:
        st = by_trigger_send.setdefault(r.trigger, {
            "total": 0, "did_send": 0, "non_empty_text": 0,
        })
        st["total"] += 1
        if r.did_send_via_messaging_tool:
            st["did_send"] += 1
        if any(t.strip() for t in r.assistant_texts):
            st["non_empty_text"] += 1

    da_lines = []
    for trig in sorted(
        by_trigger_send.keys(), key=lambda x: -by_trigger_send[x]["total"],
    ):
        st = by_trigger_send[trig]
        send_pct = (st["did_send"] / st["total"] * 100) if st["total"] else 0.0
        da_lines.append(
            f"{trig}: {st['total']} run | did_send="
            f"{st['did_send']} ({send_pct:.0f}%) | non_empty_text="
            f"{st['non_empty_text']}",
        )

    heartbeat_send = (
        by_trigger_send.get("heartbeat", {}).get("did_send", 0) or 0
    )
    data["trajectory_delivery_audit"] = by_trigger_send
    if heartbeat_send > 0:
        s.warn(
            "trajectory.delivery_audit",
            f"heartbeat 触发但 did_send=true {heartbeat_send} 次（异常）",
            evidence="\n".join(da_lines),
            data=by_trigger_send,
        )
    elif da_lines:
        s.ok(
            "trajectory.delivery_audit",
            f"Delivery audit (跨 trigger): {len(by_trigger_send)} 类 trigger",
            evidence="\n".join(da_lines),
            data=by_trigger_send,
        )
    return data


@register
class CronJobsCollector:
    id = "cron_jobs"
    title = "定时任务"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)

        home = str(ctx.openclaw_home)
        jobs_file = os.path.join(home, "cron", "jobs.json")
        state_file = os.path.join(home, "cron", "jobs-state.json")
        runs_dir = os.path.join(home, "cron", "runs")

        s_jobs = report.section("6.1 任务列表")
        report.data.update(_section_jobs(s_jobs, jobs_file, state_file, runs_dir))

        s_hb = report.section("6.2 Heartbeat")
        report.data.update(_section_heartbeat(s_hb, ctx))

        s_sys = report.section("6.3 系统 crontab")
        report.data.update(_section_system_crontab(s_sys))

        s_traj = report.section("6.4 Trajectory cron 审计 (7d)")
        report.data.update(
            _section_cron_trajectory(s_traj, ctx),
        )

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
