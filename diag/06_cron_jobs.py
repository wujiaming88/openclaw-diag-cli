#!/usr/bin/env python3
"""模块 6：定时任务（jobs.json + jobs-state.json + runs/ 三源合并）。"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output, paths

try:
    from croniter import croniter  # type: ignore
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False


def fmt_ts(ms):
    if not ms:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms)


def fmt_duration(ms):
    if ms is None:
        return "?"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s/60:.1f}min"
    return f"{s/3600:.1f}h"


def fmt_age(ms_delta):
    s = abs(ms_delta) / 1000
    if s < 60:
        return f"{s:.0f}秒"
    if s < 3600:
        return f"{s/60:.0f}分钟"
    if s < 86400:
        return f"{s/3600:.1f}小时"
    return f"{s/86400:.1f}天"


def percentile(sorted_list, p):
    if not sorted_list:
        return None
    k = max(0, min(len(sorted_list) - 1, int(len(sorted_list) * p)))
    return sorted_list[k]


def format_schedule(sched):
    k = sched.get("kind", "?")
    if k == "cron":
        return f"cron {sched.get('expr','?')} (tz={sched.get('tz','UTC')})"
    if k == "every":
        return f"every {sched.get('everyMs',0)/1000:.0f}s"
    if k == "at":
        return f"at {sched.get('at','?')}"
    return str(sched)[:100]


def expected_interval_ms(sched, runs):
    k = sched.get("kind")
    if k == "every":
        return sched.get("everyMs")
    if k == "cron" and HAS_CRONITER:
        try:
            base = datetime.datetime.now()
            it = croniter(sched["expr"], base)
            t1 = it.get_next(datetime.datetime)
            t2 = it.get_next(datetime.datetime)
            return int((t2 - t1).total_seconds() * 1000)
        except Exception:
            pass
    if runs and len(runs) >= 3:
        ts_list = sorted([r.get("runAtMs") or r.get("ts") for r in runs
                          if (r.get("runAtMs") or r.get("ts"))])
        if len(ts_list) >= 3:
            gaps = sorted(ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1))
            return gaps[len(gaps) // 2]
    return None


def load_runs(runs_dir, jid):
    if not jid or not runs_dir:
        return []
    p = os.path.join(runs_dir, f"{jid}.jsonl")
    if not os.path.isfile(p):
        return []
    buf = deque(maxlen=200)
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
        except Exception:
            pass
    return out


def fmt_k(n):
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}K"
    return str(n)


def extract_usage(r):
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


def extract_error_text(r):
    err = r.get("error") or r.get("errorMessage")
    if not err and isinstance(r.get("result"), dict):
        err = r["result"].get("error")
    if isinstance(err, dict):
        err = err.get("message") or json.dumps(err, ensure_ascii=False)
    if not err:
        return ""
    return re.sub(r"\s+", " ", str(err))[:100]


def extract_delivery_reason(r):
    reason = r.get("deliveryError")
    if not reason:
        dlv = r.get("delivery")
        if isinstance(dlv, dict):
            res = dlv.get("resolved")
            if isinstance(res, dict):
                reason = res.get("error")
    if isinstance(reason, dict):
        reason = reason.get("message") or json.dumps(reason, ensure_ascii=False)
    if not reason:
        return ""
    return re.sub(r"\s+", " ", str(reason))[:100]


def analyze(job, runs, now_ms):
    state = job.get("state", {}) or {}
    enabled = job.get("enabled", True)

    finished = [r for r in runs if r.get("status") or r.get("action") == "finished"]
    recent = finished[-20:]

    ok = sum(1 for r in recent if r.get("status") == "ok")
    fail = len(recent) - ok
    success_rate = (ok / len(recent) * 100) if recent else None

    durs = sorted([r["durationMs"] for r in recent if r.get("durationMs") is not None])
    p50 = percentile(durs, 0.5)
    p95 = percentile(durs, 0.95)
    dur_max = durs[-1] if durs else None
    last_dur = recent[-1].get("durationMs") if recent else None

    deliv = Counter(r.get("deliveryStatus") for r in recent if r.get("deliveryStatus"))
    deliv_total = sum(deliv.values())
    deliv_ok = deliv.get("delivered", 0)
    deliv_unrequested = deliv.get("not-requested", 0)
    deliv_effective = deliv_total - deliv_unrequested
    deliv_fail_rate = None
    if deliv_effective > 0:
        deliv_fail_rate = (deliv_effective - deliv_ok) / deliv_effective * 100

    exp_interval = expected_interval_ms(job.get("schedule", {}), runs)

    flags = []
    if not enabled:
        return dict(status="disabled", flags=flags, recent=recent, success_rate=success_rate,
                    ok=ok, fail=fail, p50=p50, p95=p95, dur_max=dur_max, last_dur=last_dur,
                    deliv_ok=deliv_ok, deliv_total=deliv_total, deliv_effective=deliv_effective,
                    deliv_fail_rate=deliv_fail_rate, exp_interval=exp_interval)

    cerr = state.get("consecutiveErrors", 0) or 0
    last_err = job.get("lastError") or state.get("lastError") or ""
    if cerr >= 3:
        le = re.sub(r"\s+", " ", str(last_err))[:100] if last_err else ""
        flags.append(("error", f"连续失败 {cerr} 次" + (f"（最近错误: {le}）" if le else "")))
    elif cerr >= 1:
        flags.append(("note", f"最近失败 {cerr} 次"))

    if success_rate is not None and len(recent) >= 5 and success_rate < 80:
        flags.append(("error", f"成功率 {success_rate:.0f}%（最近 {len(recent)} 次）"))

    next_run = state.get("nextRunAtMs")
    if next_run and exp_interval:
        drift = now_ms - next_run
        if drift > 2 * exp_interval:
            flags.append(("error", f"调度卡住：nextRun 已过期 {fmt_age(drift)}（预期间隔 {fmt_age(exp_interval)}）"))

    if last_dur and p95 and len(durs) >= 5 and last_dur > p95 * 2:
        flags.append(("note", f"最近耗时 {fmt_duration(last_dur)} 超历史 P95 ({fmt_duration(p95)}) 两倍"))

    if deliv_effective >= 5 and deliv_fail_rate is not None and deliv_fail_rate > 20:
        flags.append(("error", f"投递失败率 {deliv_fail_rate:.0f}%（{deliv_effective - deliv_ok}/{deliv_effective}）"))

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
                flags.append(("silent", f"已 {fmt_age(idle)} 未执行（预期间隔 {fmt_age(exp_interval)}）"))
                is_silent = True

    if is_silent:
        status = "silent"
    elif any(f[0] == "error" for f in flags):
        status = "warn"
    else:
        status = "ok"

    return dict(status=status, flags=flags, recent=recent, success_rate=success_rate,
                ok=ok, fail=fail, p50=p50, p95=p95, dur_max=dur_max, last_dur=last_dur,
                deliv_ok=deliv_ok, deliv_total=deliv_total, deliv_effective=deliv_effective,
                deliv_fail_rate=deliv_fail_rate, exp_interval=exp_interval)


def section_jobs(out: output.Output, jobs_file: str, state_file: str, runs_dir: str) -> None:
    out.item("【OpenClaw 定时任务】— jobs.json + jobs-state.json + runs/")
    if not os.path.isfile(jobs_file):
        out.item("  jobs.json 不存在 — 未创建过定时任务")
        return

    try:
        with open(jobs_file) as f:
            data = json.load(f)
    except Exception as e:
        out.item(f"  jobs.json 解析失败: {e}")
        return

    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        if isinstance(jobs, dict):
            jobs = list(jobs.values())
        elif not isinstance(jobs, list):
            jobs = []
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = []

    if not jobs:
        out.item("  jobs.json 存在但无任务")
        return

    ext_state = {}
    if state_file and os.path.isfile(state_file):
        try:
            with open(state_file) as f:
                sd = json.load(f)
            ext_jobs = sd.get("jobs", {}) if isinstance(sd, dict) else {}
            for jid, entry in ext_jobs.items():
                if isinstance(entry, dict):
                    ext_state[jid] = entry.get("state", {}) or {}
        except Exception:
            pass

    for j in jobs:
        jid = j.get("id")
        if jid and not j.get("state") and jid in ext_state:
            j["state"] = ext_state[jid]

    now_ms = int(time.time() * 1000)
    analyses = []
    for j in jobs:
        runs = load_runs(runs_dir, j.get("id"))
        analyses.append((j, runs, analyze(j, runs, now_ms)))

    total = len(jobs)
    enabled_count = sum(1 for j in jobs if j.get("enabled", True))
    disabled_count = total - enabled_count
    out.item(f"  共 {total} 个任务（{enabled_count} 启用, {disabled_count} 禁用）")
    out.line("")

    ok_list = [a for a in analyses if a[2]["status"] == "ok"]
    warn_list = [a for a in analyses if a[2]["status"] == "warn"]
    silent_list = [a for a in analyses if a[2]["status"] == "silent"]
    disabled_list = [a for a in analyses if a[2]["status"] == "disabled"]

    out.item("  ── 状态概览 ──")
    if ok_list:
        out.item(f"    正常: {len(ok_list)} 个任务")
    if warn_list:
        out.item(f"    异常: {len(warn_list)} 个任务")
        for j, _, a in warn_list:
            nm = j.get("name") or j.get("id", "?")
            msg = next((f[1] for f in a["flags"] if f[0] == "error"), "")
            out.item(f"       · {nm}: {msg}")
    if silent_list:
        out.item(f"    静默: {len(silent_list)} 个任务超期未执行")
        for j, _, a in silent_list:
            nm = j.get("name") or j.get("id", "?")
            msg = next((f[1] for f in a["flags"] if f[0] == "silent"), "")
            out.item(f"       · {nm}: {msg}")
    if disabled_list:
        out.item(f"    禁用: {len(disabled_list)} 个任务（不纳入调度）")
    if not (ok_list or warn_list or silent_list or disabled_list):
        out.item("    (无任务)")

    out.line("")
    out.item("  ── 任务详情 ──")
    out.line("")

    for idx, (j, runs, a) in enumerate(analyses, 1):
        status = a["status"]
        nm = j.get("name") or j.get("id", "?")
        icon_label = {
            "ok": "正常", "warn": "异常", "silent": "静默", "disabled": "禁用",
        }.get(status, "?")
        out.item(f"  [{idx}] {nm} ({icon_label})")

        if status == "disabled":
            out.item(f"      调度: {format_schedule(j.get('schedule', {}))} | ID: {j.get('id', '?')}")
            out.line("")
            continue

        out.item(f"      调度: {format_schedule(j.get('schedule', {}))}")
        state = j.get("state", {}) or {}
        last_run = state.get("lastRunAtMs")
        if last_run:
            ls = state.get("lastStatus") or state.get("lastRunStatus") or "?"
            ld = state.get("lastDurationMs")
            line = f"      上次执行: {fmt_ts(last_run)} | {ls}"
            if ld is not None:
                line += f" | {fmt_duration(ld)}"
            out.item(line)
        else:
            out.item("      上次执行: 从未执行")

        nr = state.get("nextRunAtMs")
        if nr:
            delta = nr - now_ms
            if delta >= 0:
                out.item(f"      下次执行: {fmt_ts(nr)} (在 {fmt_age(delta)}后)")
            else:
                out.item(f"      下次执行: {fmt_ts(nr)} (已过期 {fmt_age(delta)})")

        if a["success_rate"] is not None:
            n = a["ok"] + a["fail"]
            out.item(f"      成功率: {a['success_rate']:.0f}% (最近 {n} 次: ok={a['ok']} fail={a['fail']})")

        if a["p50"] is not None:
            parts = [f"P50={fmt_duration(a['p50'])}"]
            if a["p95"] is not None and a["p95"] != a["p50"]:
                parts.append(f"P95={fmt_duration(a['p95'])}")
            if a["dur_max"] is not None and a["dur_max"] != a["p50"]:
                parts.append(f"Max={fmt_duration(a['dur_max'])}")
            out.item("      耗时: " + " ".join(parts))

        payload = j.get("payload") or {}
        session_target = j.get("sessionTarget")
        delivery = j.get("delivery") or {}
        payload_lines = []
        if isinstance(payload, dict) and payload:
            for pk, pv in payload.items():
                if pv is None or pv == "":
                    continue
                sv = str(pv)
                if len(sv) > 80:
                    sv = sv[:77] + "..."
                payload_lines.append(f"{pk}={sv}")
        if session_target:
            payload_lines.append(f"sessionTarget={session_target}")
        if isinstance(delivery, dict) and delivery:
            del_parts = [f"{dk}={dv}" for dk, dv in delivery.items()
                         if dv is not None and dv != ""]
            if del_parts:
                payload_lines.append(f"delivery={{ {', '.join(del_parts)} }}")
        if payload_lines:
            out.item("      payload: " + " | ".join(payload_lines))

        recent = a["recent"]
        input_sum = 0
        output_sum = 0
        cost_sum = 0.0
        has_usage = False
        has_cost = False
        for r in recent:
            inp, outp, cost = extract_usage(r)
            if inp is not None:
                input_sum += inp
                has_usage = True
            if outp is not None:
                output_sum += outp
                has_usage = True
            if cost is not None:
                cost_sum += cost
                has_cost = True
        if has_usage:
            line = f"      tokens(最近{len(recent)}次): in={fmt_k(input_sum)} out={fmt_k(output_sum)}"
            if has_cost:
                line += f" | cost=${cost_sum:.4f}"
            out.item(line)

        if status != "ok":
            cerr = state.get("consecutiveErrors", 0) or 0
            if cerr > 0:
                out.item(f"      连续失败: {cerr} 次")

        fail_runs = [r for r in recent if r.get("status") and r.get("status") != "ok"]
        if fail_runs:
            seen_errs = set()
            samples = []
            for r in reversed(fail_runs):
                err = extract_error_text(r) or "(无错误详情)"
                if err in seen_errs:
                    continue
                seen_errs.add(err)
                samples.append((r.get("ts") or r.get("runAtMs"), err))
                if len(samples) >= 3:
                    break
            if samples:
                out.item(f"      最近失败({len(samples)}):")
                for ts, err in samples:
                    out.item(f"        {fmt_ts(ts)} | {err}")

        delivery_cfg = j.get("delivery")
        if isinstance(delivery_cfg, dict) and delivery_cfg:
            deliv_meta_parts = []
            if delivery_cfg.get("mode"):
                deliv_meta_parts.append(f"模式={delivery_cfg['mode']}")
            if delivery_cfg.get("channel"):
                deliv_meta_parts.append(f"channel={delivery_cfg['channel']}")
            deliv_fails = []
            seen_reasons = set()
            for r in reversed(recent):
                ds = r.get("deliveryStatus")
                if ds in (None, "", "not-requested", "delivered"):
                    continue
                reason = extract_delivery_reason(r) or "(未知原因)"
                key = (ds, reason)
                if key in seen_reasons:
                    continue
                seen_reasons.add(key)
                deliv_fails.append((r.get("ts") or r.get("runAtMs"), ds, reason))
                if len(deliv_fails) >= 3:
                    break
            if deliv_meta_parts and deliv_fails:
                out.item("      投递: " + " ".join(deliv_meta_parts))
                out.item("      投递失败样本:")
                for ts, ds, reason in deliv_fails:
                    out.item(f"        {fmt_ts(ts)} | status={ds} | reason={reason}")

        finished_all = [r for r in runs if r.get("status") or r.get("action") == "finished"]
        if finished_all:
            today = datetime.datetime.now().date()
            buckets = {}
            for r in finished_all:
                t = r.get("runAtMs") or r.get("ts")
                if not t:
                    continue
                try:
                    d = datetime.datetime.fromtimestamp(t / 1000).date()
                except Exception:
                    continue
                delta = (today - d).days
                if 0 <= delta < 7:
                    b = buckets.setdefault(d, [0, 0])
                    b[0] += 1
                    if r.get("status") == "ok":
                        b[1] += 1
            if buckets:
                days_sorted = sorted(buckets.keys(), reverse=True)
                parts = []
                for d in days_sorted:
                    total_d, ok_d = buckets[d][0], buckets[d][1]
                    rate = (ok_d / total_d * 100) if total_d else 0
                    parts.append(f"{d.strftime('%m-%d')}: {total_d}次 {rate:.0f}%")
                out.item("      最近7天: " + " | ".join(parts))

        out.line("")


def section_heartbeat(out: output.Output, args) -> None:
    out.line("")
    out.item("【OpenClaw Heartbeat】— Agent 定期唤醒机制，用于执行 HEARTBEAT.md 中的周期性任务")

    hb_every = "未配置"
    if os.path.isfile(args.config):
        try:
            with open(args.config) as f:
                cfg = json.load(f)
            hb = cfg.get("agents", {}).get("defaults", {}).get("heartbeat", {})
            hb_every = hb.get("every", "未配置")
        except Exception:
            hb_every = "读取失败"
    out.item(f"  配置: agents.defaults.heartbeat.every = {hb_every}")

    sessions_base = args.sessions_base
    if os.path.isdir(sessions_base):
        try:
            cfg = json.load(open(args.config)) if os.path.isfile(args.config) else {}
        except Exception:
            cfg = {}
        agent_workspaces = {}
        for a in cfg.get("agents", {}).get("list", []) or []:
            if isinstance(a, dict) and a.get("id"):
                agent_workspaces[a["id"]] = a.get("workspace", "")
        for agent_dir in sorted(glob.glob(os.path.join(sessions_base, "*"))):
            agent_id = os.path.basename(agent_dir)
            ws_dir = agent_workspaces.get(agent_id, "")
            hb_file = os.path.join(ws_dir, "HEARTBEAT.md") if ws_dir else ""
            if ws_dir and os.path.isfile(hb_file):
                try:
                    with open(hb_file) as f:
                        content = f.read()
                    lines = [ln for ln in content.splitlines()
                             if ln.strip() and not ln.startswith(("#", "```", "<!--"))][:5]
                except OSError:
                    lines = []
                if not lines:
                    out.item(f"  {agent_id}: HEARTBEAT.md 存在但为空（不会触发 heartbeat）")
                else:
                    out.item(f"  {agent_id}: HEARTBEAT.md 有内容（会触发 heartbeat）")
                    out.evidence(hb_file, "\n".join(lines))
            else:
                out.item(f"  {agent_id}: HEARTBEAT.md 不存在")

    log_pattern = os.path.join(args.log_dir, "openclaw-*.log")
    interesting = []
    started = []
    for lf in sorted(glob.glob(log_pattern)):
        try:
            with open(lf, errors="replace") as f:
                for raw in f:
                    if "gateway/heartbeat" not in raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
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
    if interesting:
        out.item(f"  heartbeat 有效事件 {len(interesting)} 条（另有 {len(started)} 条启动记录）")
        out.evidence("应用日志 (heartbeat)", "\n".join(interesting[:50]))
    elif started:
        intervals = sorted({s[3] for s in started if s[3]})
        out.item(f"  heartbeat 调度器: {len(started)} 次启动记录，间隔 {'、'.join(intervals)}")
    else:
        out.item("  heartbeat 日志: 0 条 — 未发现 heartbeat 相关记录")


def section_system_crontab(out: output.Output) -> None:
    out.line("")
    out.item("【系统 crontab】")
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                           timeout=5, check=False)
        text = r.stdout if r.returncode == 0 else r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        text = ""
    if not text or "no crontab" in text.lower():
        out.item("  无（未配置系统定时任务）")
        return
    entries = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if entries:
        out.item(f"  共 {len(entries)} 条")
        out.evidence("crontab -l", "\n".join(entries))
    else:
        out.item("  无有效条目（仅注释）")


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 6：定时任务采集",
        prog="06_cron_jobs",
    )
    args = parser.parse_args()

    out = output.init("cron_jobs", json_mode=args.json, no_color=args.no_color)
    out.section("模块 6：定时任务")

    home = args.openclaw_home
    jobs_file = os.path.join(home, "cron", "jobs.json")
    state_file = os.path.join(home, "cron", "jobs-state.json")
    runs_dir = os.path.join(home, "cron", "runs")

    section_jobs(out, jobs_file, state_file, runs_dir)
    section_heartbeat(out, args)
    section_system_crontab(out)

    return out.done()


if __name__ == "__main__":
    sys.exit(main())
