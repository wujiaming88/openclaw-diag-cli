"""sessions_diag collector — session inventory, per-agent analysis, stuck detection."""

from __future__ import annotations

import glob
import json
import os
import re
import time
from collections import defaultdict
from typing import Optional

from .. import trajectory
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section
from ..timeutil import fmt_duration, parse_msg_ts, parse_obj_ts
from ..tokens import fmt_tokens, human_size, pct


NORMAL_STOPS = {"stop", "end_turn", "toolUse", "tool_calls", ""}

LARGE_FILE_BYTES = 100 * 1024 * 1024  # 100MB threshold


def _build_id_to_key_map(agent_dir: str) -> dict:
    sess_json = os.path.join(agent_dir, "sessions", "sessions.json")
    id_to_key: dict = {}
    try:
        with open(sess_json) as f:
            store = json.load(f)
        if isinstance(store, dict):
            for key, entry in store.items():
                if isinstance(entry, dict) and "sessionId" in entry:
                    id_to_key[entry["sessionId"]] = key
    except (FileNotFoundError, json.JSONDecodeError, AttributeError, OSError):
        pass
    return id_to_key


def _analyze_session_file(fpath: str) -> dict:
    role_counts: dict = defaultdict(int)
    first_ts = None
    last_ts = None
    total_input = total_output = total_cache_read = total_cache_write = 0
    total_cost = 0.0
    model_calls = 0
    models_seen: set = set()
    model_latencies: list = []
    last_call_input: Optional[int] = None
    per_call_inputs: list = []
    tool_calls_total = 0
    tool_errors = 0
    tool_counts: dict = defaultdict(int)
    tool_durations: dict = defaultdict(list)
    anomalies: list = []
    parse_failed = False

    try:
        with open(fpath, errors="replace") as fp:
            for raw in fp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                obj_ts = parse_obj_ts(obj.get("timestamp"))
                if obj_ts:
                    if first_ts is None or obj_ts < first_ts:
                        first_ts = obj_ts
                    if last_ts is None or obj_ts > last_ts:
                        last_ts = obj_ts
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                if not role:
                    continue
                role_counts[role] += 1
                if role == "assistant":
                    model_calls += 1
                    provider = msg.get("provider") or ""
                    model = msg.get("model") or ""
                    if provider == "openclaw" and model in (
                        "delivery-mirror", "gateway-injected",
                    ):
                        model_calls -= 1
                        role_counts[role] -= 1
                        continue
                    model_key = f"{provider}/{model}" if provider else model
                    if model_key:
                        models_seen.add(model_key)
                    usage = msg.get("usage") or {}
                    inp = usage.get("input", 0) or 0
                    out_v = usage.get("output", 0) or 0
                    cr = usage.get("cacheRead", 0) or 0
                    cw = usage.get("cacheWrite", 0) or 0
                    cost_obj = usage.get("cost") or {}
                    cost = (
                        cost_obj.get("total", 0) or 0
                        if isinstance(cost_obj, dict) else 0
                    )
                    total_input += inp
                    total_output += out_v
                    total_cache_read += cr
                    total_cache_write += cw
                    total_cost += cost
                    per_call_inputs.append(inp + cr)
                    last_call_input = inp + cr
                    dur_ms = usage.get("durationMs")
                    dur_s = None
                    if isinstance(dur_ms, (int, float)) and dur_ms >= 0:
                        dur_s = dur_ms / 1000.0
                    else:
                        msg_ts = parse_msg_ts(msg.get("timestamp"))
                        if obj_ts and msg_ts:
                            d = (obj_ts - msg_ts).total_seconds()
                            if 0 <= d <= 600:
                                dur_s = d
                    if dur_s is not None:
                        model_latencies.append(dur_s)
                    stop = msg.get("stopReason") or ""
                    if stop and stop not in NORMAL_STOPS:
                        label = (
                            obj_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                            if obj_ts else "?"
                        )
                        detail = (
                            f"stop={stop} | in={fmt_tokens(inp + cr)} "
                            f"out={fmt_tokens(out_v)}"
                        )
                        anomalies.append((label, "model", detail))
                elif role == "toolResult":
                    tool_calls_total += 1
                    tname = msg.get("toolName") or "?"
                    tool_counts[tname] += 1
                    is_err = bool(msg.get("isError", False))
                    details = msg.get("details") or {}
                    dur_ms = (
                        details.get("durationMs")
                        if isinstance(details, dict) else None
                    )
                    if isinstance(dur_ms, (int, float)) and dur_ms >= 0:
                        tool_durations[tname].append(dur_ms / 1000.0)
                    if is_err:
                        tool_errors += 1
                        label = (
                            obj_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                            if obj_ts else "?"
                        )
                        err_brief = ""
                        c = msg.get("content")
                        if isinstance(c, list):
                            for item in c:
                                if isinstance(item, dict):
                                    t = (
                                        item.get("text")
                                        or item.get("content") or ""
                                    )
                                    if t:
                                        err_brief = str(t)
                                        break
                                elif isinstance(item, str):
                                    err_brief = item
                                    break
                        elif isinstance(c, str):
                            err_brief = c
                        err_brief = err_brief.replace("\n", " ")[:80]
                        detail = f"{tname} | isError=true"
                        if err_brief:
                            detail += f" | {err_brief}"
                        anomalies.append((label, "tool", detail))
    except OSError:
        parse_failed = True

    return {
        "role_counts": dict(role_counts),
        "first_ts": first_ts, "last_ts": last_ts,
        "total_input": total_input, "total_output": total_output,
        "total_cache_read": total_cache_read,
        "total_cache_write": total_cache_write,
        "total_cost": total_cost, "model_calls": model_calls,
        "models_seen": models_seen, "model_latencies": model_latencies,
        "last_call_input": last_call_input,
        "per_call_inputs": per_call_inputs,
        "tool_calls_total": tool_calls_total,
        "tool_errors": tool_errors,
        "tool_counts": dict(tool_counts),
        "tool_durations": dict(tool_durations),
        "anomalies": anomalies, "parse_failed": parse_failed,
    }


def _section_inventory(s: Section, sessions_base: str) -> dict:
    data: dict = {}
    active_cutoff = time.time() - 7 * 86400
    all_files = []
    active_files = []
    large_files = []

    if not os.path.isdir(sessions_base):
        s.warn(
            "sessions.inventory",
            f"Session 目录不存在: {sessions_base}",
            data={"path": sessions_base},
        )
        return data

    for agent_dir in sorted(glob.glob(os.path.join(sessions_base, "*"))):
        if not os.path.isdir(agent_dir):
            continue
        sess_dir = os.path.join(agent_dir, "sessions")
        if not os.path.isdir(sess_dir):
            continue
        for f in os.listdir(sess_dir):
            fp = os.path.join(sess_dir, f)
            if not os.path.isfile(fp):
                continue
            if f.endswith(".trajectory.jsonl"):
                continue
            if not (f.endswith(".jsonl") or ".jsonl.reset." in f):
                continue
            try:
                st = os.stat(fp)
            except OSError:
                continue
            all_files.append((agent_dir, f, fp, st.st_size, st.st_mtime))
            if st.st_mtime >= active_cutoff:
                active_files.append((agent_dir, f, fp, st.st_size, st.st_mtime))
            if st.st_size >= LARGE_FILE_BYTES:
                large_files.append({
                    "agent": os.path.basename(agent_dir),
                    "file": f,
                    "size_bytes": st.st_size,
                    "size_human": human_size(st.st_size),
                })

    total_files = len(all_files)
    total_size = sum(x[3] for x in all_files)
    active_count = len(active_files)
    summary = {
        "total_files": total_files,
        "total_size_bytes": total_size,
        "total_size_human": human_size(total_size),
        "active_count": active_count,
        "large_file_count": len(large_files),
    }
    data["disk_summary"] = summary

    msg = (
        f"Session 总览: {total_files} 个文件, 总大小 {human_size(total_size)}, "
        f"活跃(7天内) {active_count} 个"
    )
    s.ok("sessions.inventory", msg, data=summary)

    if large_files:
        evidence = "\n".join(
            f"{lf['agent']}/{lf['file']}: {lf['size_human']}"
            for lf in large_files[:10]
        )
        s.warn(
            "sessions.large_files",
            f"超大 session 文件 (>100MB): {len(large_files)} 个",
            evidence=evidence,
            data={"large_files": large_files},
        )
    else:
        s.ok(
            "sessions.large_files",
            "超大 session 文件 (>100MB): 0",
            data={"large_files": []},
        )

    data["_active_files"] = active_files
    return data


def _section_per_agent(s: Section, active_files: list) -> dict:
    data: dict = {}
    by_agent: dict = defaultdict(list)
    for agent_dir, fname, fpath, size, mtime in active_files:
        by_agent[agent_dir].append((fname, fpath, size, mtime))

    agents_data: dict = {}
    parse_failed_count = 0
    total_tool_errors = 0
    total_anomalies = 0

    for agent_dir in sorted(by_agent.keys()):
        agent_name = os.path.basename(agent_dir)
        id_to_key = _build_id_to_key_map(agent_dir)
        files = sorted(by_agent[agent_dir], key=lambda x: x[3], reverse=True)

        agent_sessions = []
        for fname, fpath, fsize, _mtime in files:
            sess_id = fname.split(".jsonl")[0]
            is_reset = ".reset." in fname
            a = _analyze_session_file(fpath)
            sess_key = id_to_key.get(sess_id, "")
            if a["parse_failed"]:
                parse_failed_count += 1
                agent_sessions.append({
                    "id": sess_id, "key": sess_key,
                    "size_bytes": fsize,
                    "parse_failed": True,
                })
                continue

            dur_total = None
            if a["first_ts"] and a["last_ts"]:
                dur_total = (a["last_ts"] - a["first_ts"]).total_seconds()

            total_tool_errors += a["tool_errors"]
            total_anomalies += len(a["anomalies"])

            entry = {
                "id": sess_id, "key": sess_key,
                "size_bytes": fsize,
                "duration_s": dur_total,
                "model_calls": a["model_calls"],
                "tool_calls": a["tool_calls_total"],
                "tool_errors": a["tool_errors"],
                "anomaly_count": len(a["anomalies"]),
                "is_reset": is_reset,
                "total_input_tokens": a["total_input"],
                "total_output_tokens": a["total_output"],
                "total_cost": round(a["total_cost"], 6),
                "models_seen": sorted(a["models_seen"]),
            }
            if a["model_latencies"]:
                sl = sorted(a["model_latencies"])
                entry["latency_p50_s"] = round(pct(sl, 0.50), 2)
                entry["latency_p95_s"] = round(pct(sl, 0.95), 2)
                entry["latency_max_s"] = round(sl[-1], 2)
            if a["last_call_input"] is not None:
                entry["last_call_input_tokens"] = a["last_call_input"]
            agent_sessions.append(entry)

        agents_data[agent_name] = {
            "session_count": len(agent_sessions),
            "sessions": agent_sessions,
        }

    data["agents"] = agents_data
    data["parse_failed_count"] = parse_failed_count
    data["total_tool_errors"] = total_tool_errors
    data["total_anomalies"] = total_anomalies

    detail_lines = []
    for an in sorted(agents_data.keys()):
        ad = agents_data[an]
        detail_lines.append(f"{an}: {ad['session_count']} sessions")

    detail = "\n".join(detail_lines) if detail_lines else None

    if parse_failed_count > 0:
        s.warn(
            "sessions.per_agent",
            f"Session 分析: {len(agents_data)} 个 agent，"
            f"{parse_failed_count} 个文件解析失败",
            detail=detail,
            data={
                "agent_count": len(agents_data),
                "parse_failed": parse_failed_count,
                "total_tool_errors": total_tool_errors,
                "total_anomalies": total_anomalies,
            },
        )
    else:
        s.ok(
            "sessions.per_agent",
            f"Session 分析: {len(agents_data)} 个 agent",
            detail=detail,
            data={
                "agent_count": len(agents_data),
                "total_tool_errors": total_tool_errors,
                "total_anomalies": total_anomalies,
            },
        )
    return data


_STUCK_RE = re.compile(
    r"stuck session:\s*"
    r"sessionId=(\S+)\s+"
    r"sessionKey=(\S+)\s+"
    r"state=(\S+)\s+"
    r"age=(\S+)\s+"
    r"queueDepth=(\S+)",
)


def _extract_stuck_match(obj):
    raw = obj.get("1", "") or obj.get("msg", "") or obj.get("message", "")
    if isinstance(raw, dict):
        raw = str(raw)
    m = _STUCK_RE.search(raw) if isinstance(raw, str) else None
    if m:
        return m
    for v in obj.values():
        if isinstance(v, str) and "stuck session" in v:
            m = _STUCK_RE.search(v)
            if m:
                return m
    return None


def _section_stuck(s: Section, log_dir: str) -> dict:
    data: dict = {}
    log_files = sorted(
        glob.glob(os.path.join(log_dir, "openclaw-*.log")),
        key=lambda p: os.path.getmtime(p) if os.path.isfile(p) else 0,
        reverse=True,
    )
    if not log_files:
        s.ok(
            "sessions.stuck",
            "未找到任何日志文件，跳过 stuck 检测",
            data={"stuck_sessions": [], "scanned_logs": []},
        )
        return data

    all_entries = []
    files_read = []
    for lf in log_files:
        files_read.append(os.path.basename(lf))
        try:
            with open(lf, errors="replace") as f:
                for line in f:
                    if "stuck session" not in line:
                        continue
                    try:
                        obj = json.loads(line.strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    m = _extract_stuck_match(obj)
                    if not m:
                        continue
                    sess_id, sess_key, state, age, qd = m.group(1, 2, 3, 4, 5)
                    ts = obj.get("time", "")[:19]
                    all_entries.append((ts, sess_id, sess_key, state, age, qd, lf))
        except OSError:
            continue

    all_entries.sort(key=lambda x: x[0])
    data["scanned_logs"] = files_read
    if not all_entries:
        s.ok(
            "sessions.stuck",
            "日志中未出现 stuck session 记录",
            data={"stuck_sessions": [], "scanned_logs": files_read},
        )
        return data

    sessions: dict = defaultdict(lambda: {
        "count": 0, "first_ts": "", "last_ts": "", "state": "",
        "age": "", "queueDepth": "", "sessionKey": "", "logfile": "",
    })
    for ts, sess_id, sess_key, state, age, qd, lf in all_entries:
        v = sessions[sess_id]
        v["count"] += 1
        if not v["first_ts"]:
            v["first_ts"] = ts
        v["last_ts"] = ts
        v["state"] = state
        v["age"] = age
        v["queueDepth"] = qd
        v["sessionKey"] = sess_key
        v["logfile"] = os.path.basename(lf)

    stuck_payload = []
    detail_lines = []
    for sid, v in sorted(
        sessions.items(), key=lambda x: x[1]["last_ts"], reverse=True,
    ):
        stuck_payload.append({
            "sessionId": sid,
            "sessionKey": v["sessionKey"],
            "state": v["state"],
            "age": v["age"],
            "queueDepth": v["queueDepth"],
            "first_ts": v["first_ts"],
            "last_ts": v["last_ts"],
            "count": v["count"],
            "logfile": v["logfile"],
        })
        detail_lines.append(
            f"{v['sessionKey']} (id={sid[:12]}) "
            f"state={v['state']} age={v['age']} qd={v['queueDepth']} "
            f"count={v['count']}",
        )

    s.warn(
        "sessions.stuck",
        f"检测到 {len(sessions)} 个 stuck session",
        evidence="\n".join(detail_lines),
        data={
            "stuck_sessions": stuck_payload,
            "scanned_logs": files_read,
        },
    )
    return data


def _section_trajectory(s: Section, sessions_base: str) -> dict:
    data: dict = {}
    files = trajectory.discover_trajectory_files(sessions_base)
    if not files:
        s.ok(
            "sessions.trajectory",
            "未发现任何 trajectory 文件",
            data={"trajectory": {"found": False}},
        )
        return data

    summaries = trajectory.collect_summaries(files)
    runs_total = sum(s_["total_runs"] for s_ in summaries)
    incomplete_total = sum(s_["incomplete_runs"] for s_ in summaries)
    largest_bytes = max((s_["size_bytes"] for s_ in summaries), default=0)
    largest_mb = largest_bytes / (1024 * 1024)
    triggers: dict = {}
    statuses: dict = {}
    aborts: dict = {}
    for s_ in summaries:
        for k, v in s_["by_trigger"].items():
            triggers[k] = triggers.get(k, 0) + v
        for k, v in s_["by_final_status"].items():
            statuses[k] = statuses.get(k, 0) + v
        for k, v in s_["by_abort_flag"].items():
            aborts[k] = aborts.get(k, 0) + v

    if runs_total == 0:
        s.ok(
            "sessions.trajectory",
            f"扫描了 {len(files)} 个 trajectory 文件，未发现任何 run 数据",
            data={"trajectory": {
                "found": True, "files": len(files), "runs_total": 0,
            }},
        )
        return data

    incomplete_pct = (
        incomplete_total / runs_total * 100 if runs_total else 0.0
    )
    nonzero_aborts = {k: v for k, v in aborts.items() if v}
    leak_total = sum(s_["active_leak_runs"] for s_ in summaries)

    summary = {
        "found": True,
        "files": len(files),
        "runs_total": runs_total,
        "runs_by_trigger": triggers,
        "runs_by_final_status": statuses,
        "abort_breakdown": nonzero_aborts,
        "incomplete_runs": incomplete_total,
        "incomplete_pct": round(incomplete_pct, 2),
        "largest_trajectory_mb": round(largest_mb, 2),
        "runs_with_active_leaks": leak_total,
    }
    data["trajectory"] = summary

    body_lines = [
        f"trajectory 文件: {len(files)} | run 总数: {runs_total} | "
        f"最大单文件: {largest_mb:.1f}MB",
    ]
    if triggers:
        parts = ", ".join(
            f"{k}={v}" for k, v in
            sorted(triggers.items(), key=lambda x: -x[1])
        )
        body_lines.append(f"by trigger: {parts}")
    if statuses:
        parts = ", ".join(
            f"{k}={v}" for k, v in
            sorted(statuses.items(), key=lambda x: -x[1])
        )
        body_lines.append(f"by final_status: {parts}")
    body_lines.append(
        f"incomplete-run: {incomplete_pct:.1f}% "
        f"({incomplete_total}/{runs_total})",
    )
    if nonzero_aborts:
        parts = ", ".join(
            f"{k}={v}" for k, v in
            sorted(nonzero_aborts.items(), key=lambda x: -x[1])
        )
        body_lines.append(f"abort flags: {parts}")
    body_lines.append(f"active_count 工具调用泄漏: {leak_total} 个 run")

    leak_samples = []
    if leak_total > 0:
        leak_files = [
            s_["path"] for s_ in summaries if s_["active_leak_runs"] > 0
        ]
        for path in leak_files:
            for run in trajectory.iter_runs(path):
                if run.active_count > 0:
                    leak_samples.append(run)
        leak_samples.sort(
            key=lambda r: r.started_ts_ms or 0, reverse=True,
        )

    silent_leak_with_success = sum(
        1 for r in leak_samples
        if r.final_status == "success" and r.active_count > 0
    )

    leak_payload = []
    for r in leak_samples[:5]:
        tool_names = [m.get("toolName") for m in r.tool_metas]
        leak_payload.append({
            "sessionId": r.session_id,
            "runId": r.run_id,
            "trigger": r.trigger,
            "active": r.active_count,
            "started_ts_ms": r.started_ts_ms,
            "final_status": r.final_status,
            "tool_names": tool_names,
        })
    data["trajectory_top_leak_runs"] = leak_payload

    is_fail = silent_leak_with_success > 0 or largest_mb > 50 or incomplete_pct > 5
    is_warn = leak_total > 0 or largest_mb > 50 or incomplete_pct > 5

    summary_with_silent = dict(summary)
    summary_with_silent["silent_leak_with_success"] = silent_leak_with_success

    if silent_leak_with_success > 0:
        s.fail(
            "sessions.trajectory",
            f"工具调用泄漏（active_count>0 且 final_status=success）: "
            f"{silent_leak_with_success} 个",
            evidence="\n".join(body_lines),
            data=summary_with_silent,
        )
    elif is_warn:
        msg_parts = []
        if leak_total:
            msg_parts.append(f"工具泄漏 {leak_total}")
        if largest_mb > 50:
            msg_parts.append(f"单文件 {largest_mb:.1f}MB")
        if incomplete_pct > 5:
            msg_parts.append(f"incomplete {incomplete_pct:.1f}%")
        s.warn(
            "sessions.trajectory",
            f"Trajectory 异常: {'; '.join(msg_parts)}",
            evidence="\n".join(body_lines),
            data=summary_with_silent,
        )
    else:
        s.ok(
            "sessions.trajectory",
            f"Trajectory 健康: {len(files)} 文件, {runs_total} runs",
            evidence="\n".join(body_lines),
            data=summary_with_silent,
        )
    return data


@register
class SessionsDiagCollector:
    id = "sessions_diag"
    title = "Session 数据"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)

        s_inv = report.section("8.1 Session 文件清单")
        inv_data = _section_inventory(s_inv, str(ctx.sessions_base))
        active_files = inv_data.pop("_active_files", [])
        report.data.update(inv_data)

        s_agent = report.section("8.2 Per-Agent 分析")
        report.data.update(_section_per_agent(s_agent, active_files))

        s_stuck = report.section("8.3 Stuck Session 探测")
        report.data.update(_section_stuck(s_stuck, str(ctx.log_dir)))

        s_traj = report.section("8.4 Trajectory Run 健康度")
        report.data.update(_section_trajectory(s_traj, str(ctx.sessions_base)))

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
