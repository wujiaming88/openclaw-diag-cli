#!/usr/bin/env python3
"""模块 8：Session 数据（六维分析 + Stuck 探测）。"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output
from ocdiag.timeutil import fmt_duration, parse_msg_ts, parse_obj_ts
from ocdiag.tokens import fmt_tokens, human_size, pct


NORMAL_STOPS = {"stop", "end_turn", "toolUse", "tool_calls", ""}


def build_id_to_key_map(agent_dir):
    sess_json = os.path.join(agent_dir, "sessions", "sessions.json")
    id_to_key = {}
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


def analyze_session_file(fpath: str):
    role_counts = defaultdict(int)
    first_ts = None
    last_ts = None
    total_input = total_output = total_cache_read = total_cache_write = 0
    total_cost = 0.0
    model_calls = 0
    models_seen = set()
    model_latencies = []
    last_call_input = None
    per_call_inputs = []
    tool_calls_total = 0
    tool_errors = 0
    tool_counts = defaultdict(int)
    tool_durations = defaultdict(list)
    anomalies = []
    parse_failed = False

    try:
        with open(fpath, errors="replace") as fp:
            for raw in fp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
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
                    if provider == "openclaw" and model in ("delivery-mirror", "gateway-injected"):
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
                    cost = (cost_obj.get("total", 0) or 0) if isinstance(cost_obj, dict) else 0
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
                        label = obj_ts.strftime("%Y-%m-%d %H:%M:%S") if obj_ts else "?"
                        detail = f"stop={stop} | in={fmt_tokens(inp + cr)} out={fmt_tokens(out_v)}"
                        anomalies.append((label, "model", detail))
                elif role == "toolResult":
                    tool_calls_total += 1
                    tname = msg.get("toolName") or "?"
                    tool_counts[tname] += 1
                    is_err = bool(msg.get("isError", False))
                    details = msg.get("details") or {}
                    dur_ms = details.get("durationMs") if isinstance(details, dict) else None
                    if isinstance(dur_ms, (int, float)) and dur_ms >= 0:
                        tool_durations[tname].append(dur_ms / 1000.0)
                    if is_err:
                        tool_errors += 1
                        label = obj_ts.strftime("%Y-%m-%d %H:%M:%S") if obj_ts else "?"
                        err_brief = ""
                        c = msg.get("content")
                        if isinstance(c, list):
                            for item in c:
                                if isinstance(item, dict):
                                    t = item.get("text") or item.get("content") or ""
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
    except Exception:
        parse_failed = True

    return dict(
        role_counts=role_counts, first_ts=first_ts, last_ts=last_ts,
        total_input=total_input, total_output=total_output,
        total_cache_read=total_cache_read, total_cache_write=total_cache_write,
        total_cost=total_cost, model_calls=model_calls, models_seen=models_seen,
        model_latencies=model_latencies, last_call_input=last_call_input,
        per_call_inputs=per_call_inputs, tool_calls_total=tool_calls_total,
        tool_errors=tool_errors, tool_counts=tool_counts, tool_durations=tool_durations,
        anomalies=anomalies, parse_failed=parse_failed,
    )


def session_data_dimension(out: output.Output, sessions_base: str) -> None:
    out.progress(1, 3, "文件扫描")
    active_cutoff = time.time() - 7 * 86400
    all_files_info = []
    active_files = []

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
            all_files_info.append((f, fp, st.st_size, st.st_mtime))
            if st.st_mtime >= active_cutoff:
                active_files.append((agent_dir, f, fp, st.st_size, st.st_mtime))

    total_files = len(all_files_info)
    total_size = sum(x[2] for x in all_files_info)
    active_count = len(active_files)
    out.item(
        f"Session 总览: {total_files} 个文件, 总大小 {human_size(total_size)}, "
        f"活跃(7天内) {active_count} 个"
    )
    out.set_data("disk_summary", {
        "total_files": total_files,
        "total_size_bytes": total_size,
        "active_count": active_count,
    })

    agents_data: dict = {}

    out.progress(2, 3, "Session 分析")
    by_agent = defaultdict(list)
    for agent_dir, fname, fpath, size, mtime in active_files:
        by_agent[agent_dir].append((fname, fpath, size, mtime))

    for agent_dir in sorted(by_agent.keys()):
        agent_name = os.path.basename(agent_dir)
        id_to_key = build_id_to_key_map(agent_dir)
        files = by_agent[agent_dir]
        files.sort(key=lambda x: x[3], reverse=True)

        out.line("")
        out.item(f"Agent: {agent_name} ({len(files)} 个 session)")

        agent_sessions = []

        for fname, fpath, fsize, _mtime in files:
            sess_id = fname.split(".jsonl")[0]
            is_reset = ".reset." in fname
            tag = " [reset]" if is_reset else ""

            a = analyze_session_file(fpath)

            sess_key = id_to_key.get(sess_id, "")
            if a["parse_failed"]:
                out.line(f"    {sess_id}{tag}")
                if sess_key:
                    out.line(f"      sessionKey={sess_key} | size={human_size(fsize)} | <解析失败>")
                else:
                    out.line(f"      size={human_size(fsize)} | <解析失败>")
                agent_sessions.append({
                    "id": sess_id,
                    "key": sess_key,
                    "size_bytes": fsize,
                    "parse_failed": True,
                })
                continue

            if a["first_ts"] and a["last_ts"]:
                dur_total = (a["last_ts"] - a["first_ts"]).total_seconds()
                duration_str = fmt_duration(dur_total)
                start_str = a["first_ts"].strftime("%Y-%m-%d %H:%M:%S")
                end_str = a["last_ts"].strftime("%Y-%m-%d %H:%M:%S")
            else:
                dur_total = None
                duration_str = start_str = end_str = "?"

            agent_sessions.append({
                "id": sess_id,
                "key": sess_key,
                "size_bytes": fsize,
                "duration_s": dur_total,
                "model_calls": a["model_calls"],
                "tool_calls": a["tool_calls_total"],
                "tool_errors": a["tool_errors"],
                "anomaly_count": len(a["anomalies"]),
                "is_reset": is_reset,
            })

            out.line(f"    {sess_id}{tag}")
            sk_part = f"sessionKey={sess_key} | " if sess_key else ""
            out.line(f"      {sk_part}size={human_size(fsize)} | duration={duration_str}")
            out.line(f"      start={start_str} end={end_str}")

            role_order = ["user", "assistant", "toolResult", "system"]
            parts = []
            for r in role_order:
                if a["role_counts"].get(r, 0):
                    parts.append(f"{r}={a['role_counts'][r]}")
            for r in sorted(a["role_counts"].keys()):
                if r not in role_order and a["role_counts"][r]:
                    parts.append(f"{r}={a['role_counts'][r]}")
            if parts:
                out.line(f"      messages: {' '.join(parts)}")

            if a["model_calls"]:
                token_parts = [f"in={fmt_tokens(a['total_input'])}", f"out={fmt_tokens(a['total_output'])}"]
                if a["total_cache_read"]:
                    token_parts.append(f"cache_read={fmt_tokens(a['total_cache_read'])}")
                if a["total_cache_write"]:
                    token_parts.append(f"cache_write={fmt_tokens(a['total_cache_write'])}")
                cost_part = f" | cost=${a['total_cost']:.4f}" if a["total_cost"] > 0 else ""
                out.line(f"      tokens: {' '.join(token_parts)}{cost_part}")
                if a["per_call_inputs"]:
                    avg_in = sum(a["per_call_inputs"]) / len(a["per_call_inputs"])
                    last_in = a["last_call_input"] if a["last_call_input"] is not None else 0
                    out.line(f"      context: avg_input={fmt_tokens(int(avg_in))} "
                             f"last_input={fmt_tokens(last_in)}（当前上下文大小）")

            if a["model_calls"]:
                models_str = ", ".join(sorted(a["models_seen"])) if a["models_seen"] else "?"
                out.line(f"      model: [{models_str}] calls={a['model_calls']}")
                if a["model_latencies"]:
                    sl = sorted(a["model_latencies"])
                    p50 = pct(sl, 0.50)
                    p95 = pct(sl, 0.95)
                    mx = sl[-1]
                    total_dur = sum(sl)
                    tp = (a["total_output"] / total_dur) if total_dur > 0 else 0.0
                    out.line(f"        latency: P50={p50:.1f}s P95={p95:.1f}s Max={mx:.1f}s | "
                             f"throughput={tp:.1f} tok/s")

            if a["tool_calls_total"]:
                err_rate = (a["tool_errors"] / a["tool_calls_total"] * 100) if a["tool_calls_total"] else 0.0
                out.line(f"      tools: {a['tool_calls_total']} calls | error_rate={err_rate:.1f}%")
                top = sorted(a["tool_counts"].items(), key=lambda x: -x[1])[:5]
                top_str = ", ".join(f"{t}:{c}" for t, c in top)
                out.line(f"        top: [{top_str}]")
                timed = [(n, ds) for n, ds in a["tool_durations"].items() if ds]
                if timed:
                    timed.sort(key=lambda x: a["tool_counts"][x[0]], reverse=True)
                    timed_parts = []
                    for n, ds in timed[:4]:
                        dsr = sorted(ds)
                        timed_parts.append(f"{n} P50={pct(dsr,0.50):.2f}s P95={pct(dsr,0.95):.2f}s")
                    out.line(f"        耗时(有 durationMs 的): {' | '.join(timed_parts)}")

            if a["anomalies"]:
                n = len(a["anomalies"])
                out.line(f"      异常({n}):")
                for ts_label, kind, detail in a["anomalies"][:10]:
                    out.line(f"        {ts_label} | {kind} | {detail}")
                if n > 10:
                    out.line(f"        ... 省略 {n - 10} 条 ...")

        agents_data[agent_name] = {
            "session_count": len(agent_sessions),
            "sessions": agent_sessions,
        }

    out.set_data("agents", agents_data)


_STUCK_RE = re.compile(
    r"stuck session:\s*"
    r"sessionId=(\S+)\s+"
    r"sessionKey=(\S+)\s+"
    r"state=(\S+)\s+"
    r"age=(\S+)\s+"
    r"queueDepth=(\S+)"
)


def extract_stuck_match(obj):
    raw = obj.get("1", "") or obj.get("msg", "") or obj.get("message", "")
    if isinstance(raw, dict):
        raw = str(raw)
    m = _STUCK_RE.search(raw)
    if m:
        return m
    for v in obj.values():
        if isinstance(v, str) and "stuck session" in v:
            m = _STUCK_RE.search(v)
            if m:
                return m
    return None


def stuck_dimension(out: output.Output, log_dir: str) -> None:
    out.progress(3, 3, "Stuck 检测")
    out.line("")
    out.line("  ── Session Stuck 状态探测 ──")
    out.line("")

    log_files = sorted(glob.glob(os.path.join(log_dir, "openclaw-*.log")),
                       key=lambda p: os.path.getmtime(p) if os.path.isfile(p) else 0,
                       reverse=True)
    if not log_files:
        out.item("未找到任何日志文件")
        out.set_data("stuck_sessions", [])
        out.set_data("scanned_logs", [])
        return

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
                    except Exception:
                        continue
                    m = extract_stuck_match(obj)
                    if not m:
                        continue
                    sess_id, sess_key, state, age, qd = m.group(1, 2, 3, 4, 5)
                    ts = obj.get("time", "")[:19]
                    all_entries.append((ts, sess_id, sess_key, state, age, qd, lf))
        except OSError:
            continue

    all_entries.sort(key=lambda x: x[0])
    out.set_data("scanned_logs", files_read)
    if not all_entries:
        out.item(f"扫描: {', '.join(files_read)}")
        out.item("日志中未出现 stuck session 记录")
        out.set_data("stuck_sessions", [])
        return

    sessions = defaultdict(lambda: {
        "count": 0, "first_ts": "", "last_ts": "", "state": "",
        "age": "", "queueDepth": "", "sessionKey": "", "logfile": "",
    })
    for ts, sess_id, sess_key, state, age, qd, lf in all_entries:
        s = sessions[sess_id]
        s["count"] += 1
        if not s["first_ts"]:
            s["first_ts"] = ts
        s["last_ts"] = ts
        s["state"] = state
        s["age"] = age
        s["queueDepth"] = qd
        s["sessionKey"] = sess_key
        s["logfile"] = os.path.basename(lf)

    latest_logfile = os.path.basename(all_entries[-1][6])
    out.item(f"扫描: {', '.join(files_read)}")
    out.item(f"最新条目: {all_entries[-1][0]} [来自 {latest_logfile}]")
    out.item(f"检测到 {len(sessions)} 个 stuck session（按最后出现时间排序）：")
    stuck_payload = []
    for sid, s in sorted(sessions.items(), key=lambda x: x[1]["last_ts"], reverse=True):
        out.item(f"  {s['sessionKey']} (sessionId={sid}) [{s['logfile']}]")
        out.item(f"    state={s['state']}  age={s['age']}  queueDepth={s['queueDepth']}")
        out.item(f"    首次: {s['first_ts']}  最后: {s['last_ts']}  共 {s['count']} 条")
        stuck_payload.append({
            "sessionId": sid,
            "sessionKey": s["sessionKey"],
            "state": s["state"],
            "age": s["age"],
            "queueDepth": s["queueDepth"],
            "first_ts": s["first_ts"],
            "last_ts": s["last_ts"],
            "count": s["count"],
            "logfile": s["logfile"],
        })
    out.set_data("stuck_sessions", stuck_payload)


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 8：Session 数据采集 + Stuck 探测",
    )
    args = parser.parse_args()
    out = output.init("sessions", json_mode=args.json, no_color=args.no_color)
    out.section("模块 8：Session 数据")

    session_data_dimension(out, args.sessions_base)
    stuck_dimension(out, args.log_dir)

    return out.done()


if __name__ == "__main__":
    sys.exit(main())
