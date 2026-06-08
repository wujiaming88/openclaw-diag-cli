"""performance collector — model/tool P50/P95, slow calls, cache, throughput."""

from __future__ import annotations

import glob
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List

from .. import trajectory
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict
from ..timeutil import parse_msg_ts, parse_obj_ts
from ..tokens import fmt_tokens, pct


NORMAL_STOPS = {"stop", "end_turn", "toolUse", "tool_calls", ""}


def _fmt_args(tool_name, tc_args, max_len=100):
    if isinstance(tc_args, str):
        try:
            tc_args = json.loads(tc_args)
        except (json.JSONDecodeError, ValueError):
            return (tc_args or "")[:max_len]
    if not isinstance(tc_args, dict) or not tc_args:
        return ""

    def trunc(s, n):
        s = "" if s is None else str(s)
        return s[:n] + ("..." if len(s) > n else "")

    name = (tool_name or "").lower()
    if name == "exec":
        return trunc(tc_args.get("command", ""), max_len)
    if name == "web_fetch":
        return trunc(tc_args.get("url", ""), max_len)
    if name == "web_search":
        return trunc(tc_args.get("query", ""), max_len)
    if name == "sessions_spawn":
        aid = tc_args.get("agentId", "")
        task = trunc(tc_args.get("task", ""), 60)
        return trunc(f"agentId={aid}, task={task}", max_len)
    if name in ("read", "write", "edit"):
        return trunc(tc_args.get("path", ""), max_len)
    if name == "cron":
        action = tc_args.get("action", "")
        jid = tc_args.get("jobId", "")
        s = f"action={action}, jobId={jid}" if jid else f"action={action}"
        return trunc(s, max_len)
    if name in ("image", "image_generate"):
        return trunc(tc_args.get("prompt", ""), 60)
    parts = []
    for k, v in list(tc_args.items())[:3]:
        sv = str(v)
        if len(sv) > 50:
            sv = sv[:50] + "..."
        parts.append(f"{k}={sv}")
    return trunc(", ".join(parts), max_len)


def _categorize_api_error(msg, stop):
    text = str(msg.get("error", ""))
    content = msg.get("content", "")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                t = c.get("text") or c.get("content") or ""
                if t:
                    text += " " + str(t)
            elif isinstance(c, str):
                text += " " + c
    elif isinstance(content, str):
        text += " " + content
    text = text[:1000]
    low = text.lower()
    if "429" in text or "rate limit" in low or "throttl" in low:
        return "rate_limit(429)"
    if "503" in text or "service unavailable" in low:
        return "service_unavailable(503)"
    if (
        "401" in text or "403" in text
        or "unauthorized" in low or "forbidden" in low
    ):
        return "auth_error(401/403)"
    if "500" in text or "internal server" in low:
        return "server_error(500)"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "connection" in low and (
        "refused" in low or "reset" in low or "aborted" in low
    ):
        return "connection_error"
    if stop == "aborted" or "aborted" in low:
        return "aborted"
    return f"other(stop={stop or 'n/a'})"


def _collect_session_files(sessions_base, limit=50):
    files = []
    pattern1 = os.path.join(sessions_base, "*", "*", "*.jsonl")
    pattern2 = os.path.join(sessions_base, "*", "*", "*.jsonl.reset.*")
    for pat in (pattern1, pattern2):
        for p in glob.glob(pat):
            if p.endswith(".trajectory.jsonl"):
                continue
            if ".acp-stream" in p:
                continue
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            files.append((m, p))
    files.sort(reverse=True)
    return [p for _, p in files[:limit]]


def _collect_session_files_by_window(sessions_base, days: int = 7):
    """Return all session files whose mtime is within the last `days` days.

    Used by daily_trend to give an honest view: the perf-sampling window
    (latest 20 files by mtime) skews toward today/yesterday and would
    silently report 0 calls for older days that still have real activity.
    """
    cutoff = time.time() - days * 86400
    files = []
    pattern1 = os.path.join(sessions_base, "*", "*", "*.jsonl")
    pattern2 = os.path.join(sessions_base, "*", "*", "*.jsonl.reset.*")
    for pat in (pattern1, pattern2):
        for p in glob.glob(pat):
            if p.endswith(".trajectory.jsonl"):
                continue
            if ".acp-stream" in p:
                continue
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m < cutoff:
                continue
            files.append((m, p))
    files.sort(reverse=True)
    return [p for _, p in files]


def _parse_daily_stats(session_files):
    """Lightweight parse: only what daily_trend needs.

    Reads timestamp + assistant/usage + duration + output tokens. Skips
    openclaw/* internal markers (matches _analyze_sessions filter).
    """
    daily_stats = defaultdict(lambda: {"calls": 0, "durs": [], "output": 0})
    for path in session_files:
        max_msg_ms = 0
        for raw_line in _tail_lines(path):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = obj.get("message", {}) or {}
            if msg.get("role") != "assistant":
                continue
            if (msg.get("provider") or "") == "openclaw":
                continue
            msg_ts_raw = msg.get("timestamp")
            if msg_ts_raw is not None:
                if msg_ts_raw < max_msg_ms - 1000:
                    continue
                if msg_ts_raw > max_msg_ms:
                    max_msg_ms = msg_ts_raw
            obj_ts = parse_obj_ts(obj.get("timestamp"))
            if not obj_ts:
                continue
            usage = msg.get("usage", {}) or {}
            out_v = usage.get("output", 0) or 0
            msg_ts = parse_msg_ts(msg_ts_raw)
            dur = None
            if msg_ts:
                d = (obj_ts - msg_ts).total_seconds()
                if 0 <= d <= 600:
                    dur = d
            day_key = obj_ts.astimezone().strftime("%m-%d")
            d = daily_stats[day_key]
            d["calls"] += 1
            if dur is not None:
                d["durs"].append(dur)
            d["output"] += out_v
    return daily_stats


def _tail_lines(path, n=2000):
    """Read the last n lines of a file. Increased from 500 to capture more tool calls."""
    try:
        size = os.path.getsize(path)
        # For files under 2MB, read the whole thing (more complete data)
        if size < 2 * 1024 * 1024:
            with open(path, "r", errors="replace") as f:
                return f.readlines()
        with open(path, "r", errors="replace") as f:
            return f.readlines()[-n:]
    except OSError:
        return []


def _analyze_sessions(session_files):
    model_stats = defaultdict(lambda: {
        "calls": 0, "input": 0, "output": 0,
        "cache_read": 0, "cache_write": 0, "cost": 0.0,
        "durations": [], "stop_reasons": defaultdict(int),
    })
    tool_stats = defaultdict(lambda: {
        "calls": 0, "errors": 0, "durations": [],
        "records": [], "error_records": [],
    })
    all_model_durations = []
    all_tool_durations = []
    abnormal_stops = []
    slow_calls_top = []
    ctx_buckets_def = [
        ("<50K", 50_000),
        ("50K-100K", 100_000),
        ("100K-200K", 200_000),
        (">200K", float("inf")),
    ]
    ctx_bucket_durs = defaultdict(list)
    daily_stats = defaultdict(lambda: {"calls": 0, "durs": [], "output": 0})
    cache_total_calls = 0
    cache_calls_with_cache = 0
    cache_sum_input = 0
    cache_sum_cache_read = 0
    cache_sum_cache_write = 0
    session_stats = defaultdict(
        lambda: {"calls": 0, "tokens": 0, "duration": 0.0},
    )
    e2e_latencies = []
    api_error_stats = defaultdict(int)
    api_total_assistant_calls = 0

    for session_path in session_files:
        sess_id = os.path.basename(session_path).split(".jsonl")[0]
        current_session_id = sess_id
        max_msg_ms = 0
        pending_tool_calls = {}
        current_turn_user_ts = None
        current_turn_last_assistant_ts = None
        last_assistant_record_epoch_ms = None  # for tool duration fallback

        for raw_line in _tail_lines(session_path):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue

            msg = obj.get("message", {}) or {}
            role = msg.get("role", "")
            obj_ts = parse_obj_ts(obj.get("timestamp"))
            msg_ts_raw = msg.get("timestamp")
            msg_ts = parse_msg_ts(msg_ts_raw)

            if msg_ts_raw is not None:
                if msg_ts_raw < max_msg_ms - 1000:
                    continue
                if msg_ts_raw > max_msg_ms:
                    max_msg_ms = msg_ts_raw

            if role == "assistant":
                # Track record timestamp for tool duration fallback
                if obj_ts:
                    last_assistant_record_epoch_ms = obj_ts.timestamp() * 1000
                provider = msg.get("provider") or ""
                model = msg.get("model") or "?"
                # openclaw/* entries are internal runtime markers, not real model calls
                if provider == "openclaw":
                    continue
                model_key = f"{provider}/{model}" if provider else model
                usage = msg.get("usage", {}) or {}
                stop = msg.get("stopReason", "") or ""
                inp = usage.get("input", 0) or 0
                out_v = usage.get("output", 0) or 0
                cr = usage.get("cacheRead", 0) or 0
                cw = usage.get("cacheWrite", 0) or 0
                cost_obj = usage.get("cost", {}) or {}
                cost = (
                    (cost_obj.get("total", 0) or 0)
                    if isinstance(cost_obj, dict) else 0
                )

                s = model_stats[model_key]
                s["calls"] += 1
                s["input"] += inp
                s["output"] += out_v
                s["cache_read"] += cr
                s["cache_write"] += cw
                s["cost"] += cost
                if stop:
                    s["stop_reasons"][stop] += 1

                dur = None
                if obj_ts and msg_ts:
                    dur = (obj_ts - msg_ts).total_seconds()
                    if 0 <= dur <= 600:
                        s["durations"].append(dur)
                        all_model_durations.append((dur, model_key))
                        label = obj_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                        dedup_key = (label, model_key, inp)
                        slow_calls_top.append((
                            dur, "model",
                            f"{label} | {model_key} | {dur:.1f}s | "
                            f"in={fmt_tokens(inp + cr)} out={fmt_tokens(out_v)} | "
                            f"stop={stop or 'n/a'}",
                            dedup_key,
                        ))
                        ctx_size = inp + cr
                        for (b_label, b_upper) in ctx_buckets_def:
                            if ctx_size < b_upper:
                                ctx_bucket_durs[b_label].append(dur)
                                break

                if obj_ts:
                    day_key = obj_ts.astimezone().strftime("%m-%d")
                    d = daily_stats[day_key]
                    d["calls"] += 1
                    if dur is not None and 0 <= dur <= 600:
                        d["durs"].append(dur)
                    d["output"] += out_v

                if current_session_id:
                    ss = session_stats[current_session_id]
                    ss["calls"] += 1
                    ss["tokens"] += inp + cr + out_v
                    if dur is not None and 0 <= dur <= 600:
                        ss["duration"] += dur

                cache_total_calls += 1
                if cr > 0:
                    cache_calls_with_cache += 1
                cache_sum_input += inp
                cache_sum_cache_read += cr
                cache_sum_cache_write += cw

                api_total_assistant_calls += 1
                if stop and stop not in NORMAL_STOPS:
                    label = (
                        obj_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                        if obj_ts else "?"
                    )
                    abnormal_stops.append(
                        f"{label} | {model_key} | stop={stop} | "
                        f"in={fmt_tokens(inp + cr)} "
                        f"out={fmt_tokens(out_v)}",
                    )
                    api_error_stats[_categorize_api_error(msg, stop)] += 1

                if (obj_ts is not None
                        and current_turn_user_ts is not None
                        and out_v > 0):
                    current_turn_last_assistant_ts = obj_ts

                for part in msg.get("content", []) or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") != "toolCall":
                        continue
                    tc_id = part.get("id", "")
                    if not tc_id:
                        continue
                    tc_name = part.get("name", "") or ""
                    tc_args = part.get("arguments", part.get("input", ""))
                    pending_tool_calls[tc_id] = (tc_name, tc_args)

            elif role == "toolResult":
                tool_name = msg.get("toolName") or "?"
                tool_id = msg.get("toolCallId") or ""
                details = msg.get("details") or {}
                dur_ms = (
                    details.get("durationMs")
                    if isinstance(details, dict) else None
                )
                # Fallback: compute duration from record timestamps
                if dur_ms is None and obj_ts and last_assistant_record_epoch_ms:
                    result_epoch_ms = obj_ts.timestamp() * 1000
                    inferred = result_epoch_ms - last_assistant_record_epoch_ms
                    if 0 < inferred < 600000:  # sanity: 0 < dur < 10 min
                        dur_ms = inferred
                is_error = bool(msg.get("isError", False))

                ts = tool_stats[tool_name]
                ts["calls"] += 1
                if is_error:
                    ts["errors"] += 1

                pending = (
                    pending_tool_calls.pop(tool_id, None) if tool_id else None
                )
                if pending:
                    args_name, raw_args = pending
                    args_str = _fmt_args(args_name or tool_name, raw_args)
                else:
                    args_str = ""

                err_brief = ""
                if is_error:
                    content = msg.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict):
                                t = c.get("text") or c.get("content") or ""
                                if t:
                                    err_brief = str(t)
                                    break
                            elif isinstance(c, str):
                                err_brief = c
                                break
                    elif isinstance(content, str):
                        err_brief = content
                    err_brief = err_brief.replace("\n", " ")[:80]

                dur_s = None
                if isinstance(dur_ms, (int, float)) and dur_ms >= 0:
                    dur_s = dur_ms / 1000.0

                rec = {
                    "dur": dur_s, "args": args_str,
                    "is_error": is_error, "err_brief": err_brief,
                    "ts": obj_ts,
                }
                ts["records"].append(rec)
                if is_error:
                    ts["error_records"].append(rec)

                if dur_s is not None:
                    ts["durations"].append(dur_s)
                    all_tool_durations.append((dur_s, tool_name))
                    label = (
                        obj_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                        if obj_ts else "?"
                    )
                    brief = f" | {args_str}" if args_str else ""
                    err_suffix = (
                        f" | {err_brief}"
                        if (is_error and err_brief) else ""
                    )
                    err_tag = " | error=True" if is_error else ""
                    slow_calls_top.append((
                        dur_s, "tool",
                        f"{label} | {tool_name} | {dur_s:.1f}s"
                        f"{err_tag}{brief}{err_suffix}",
                        None,
                    ))

            elif role == "user":
                if (current_turn_user_ts is not None
                        and current_turn_last_assistant_ts is not None):
                    _lat = (
                        current_turn_last_assistant_ts - current_turn_user_ts
                    ).total_seconds()
                    if 1 <= _lat <= 3600:
                        e2e_latencies.append(_lat)
                current_turn_user_ts = obj_ts
                current_turn_last_assistant_ts = None

        if (current_turn_user_ts is not None
                and current_turn_last_assistant_ts is not None):
            _lat = (
                current_turn_last_assistant_ts - current_turn_user_ts
            ).total_seconds()
            if 1 <= _lat <= 3600:
                e2e_latencies.append(_lat)

    return dict(
        model_stats=model_stats,
        tool_stats=tool_stats,
        all_model_durations=all_model_durations,
        all_tool_durations=all_tool_durations,
        abnormal_stops=abnormal_stops,
        slow_calls_top=slow_calls_top,
        ctx_buckets_def=ctx_buckets_def,
        ctx_bucket_durs=ctx_bucket_durs,
        daily_stats=daily_stats,
        cache_total_calls=cache_total_calls,
        cache_calls_with_cache=cache_calls_with_cache,
        cache_sum_input=cache_sum_input,
        cache_sum_cache_read=cache_sum_cache_read,
        cache_sum_cache_write=cache_sum_cache_write,
        session_stats=session_stats,
        e2e_latencies=e2e_latencies,
        api_error_stats=api_error_stats,
        api_total_assistant_calls=api_total_assistant_calls,
    )


def _section_models(s: Section, data: dict, file_count: int) -> dict:
    out_data: dict = {}
    model_stats = data["model_stats"]
    models_payload = {}
    if not model_stats:
        s.ok(
            "perf.models",
            "最近 Session 中未发现模型使用数据",
            data={"file_count": file_count, "models": {}},
        )
        out_data["models"] = {}
        return out_data

    detail_lines = [f"数据来源: 最近 {file_count} 个 session 文件", ""]
    max_p95 = 0.0
    for model_key in sorted(
        model_stats.keys(),
        key=lambda k: model_stats[k]["calls"], reverse=True,
    ):
        ms = model_stats[model_key]
        durs = sorted(ms["durations"])
        calls = ms["calls"]
        p50 = pct(durs, 0.50)
        p95 = pct(durs, 0.95)
        if p95 > max_p95:
            max_p95 = p95
        mx = durs[-1] if durs else 0.0
        total_dur = sum(durs)
        throughput = (
            ms["output"] / total_dur if total_dur > 0 and ms["output"] else None
        )
        stops = ms["stop_reasons"]
        normal = sum(v for k, v in stops.items() if k in NORMAL_STOPS)
        success = (normal / calls * 100) if calls else 0.0
        detail_lines.append(model_key)
        detail_lines.append(
            f"  调用: {calls} | P50: {p50:.1f}s | P95: {p95:.1f}s | "
            f"Max: {mx:.1f}s | 成功率: {success:.0f}%",
        )
        models_payload[model_key] = {
            "calls": calls,
            "p50_s": round(p50, 3),
            "p95_s": round(p95, 3),
            "max_s": round(mx, 3),
            "throughput_tok_s": (
                round(throughput, 1) if throughput else None
            ),
            "input_tokens": ms["input"],
            "output_tokens": ms["output"],
            "cache_read_tokens": ms["cache_read"],
            "cache_write_tokens": ms["cache_write"],
            "cost_usd": round(ms["cost"], 6),
            "success_rate_pct": round(success, 1),
            "stop_reasons": dict(stops),
        }
        detail_lines.append("")

    out_data["models"] = models_payload
    out_data["model_p95_max"] = round(max_p95, 3)

    # Decouple availability from latency: fail only when success rate is bad.
    # Slow-but-healthy heavy models (e.g. Opus on long agentic turns) get
    # demoted to warn instead of being flagged as a failure. Sample size of
    # <10 calls is too small to drive verdict on success rate alone; those
    # models still appear in detail/data but are excluded from min computation.
    eligible = {
        k: v for k, v in models_payload.items() if v["calls"] >= 10
    }
    if eligible:
        min_success_model = min(
            eligible, key=lambda k: eligible[k]["success_rate_pct"],
        )
        min_success_rate = eligible[min_success_model]["success_rate_pct"]
    else:
        min_success_model = None
        min_success_rate = None

    out_data["min_success_rate_pct"] = min_success_rate
    out_data["min_success_rate_model"] = min_success_model

    payload_data = {
        "models": models_payload,
        "max_p95_s": round(max_p95, 3),
        "min_success_rate_pct": min_success_rate,
        "min_success_rate_model": min_success_model,
    }

    if min_success_rate is not None and min_success_rate < 90:
        trigger = "availability_critical"
        msg = (
            f"模型可用性: 最低成功率 {min_success_rate:.0f}% "
            f"({min_success_model}) <90%"
        )
        if max_p95 > 60:
            msg += f"；最大 P95 {max_p95:.1f}s"
        out_data["verdict_trigger"] = trigger
        s.fail(
            "perf.models", msg,
            detail="\n".join(detail_lines),
            data={**payload_data, "verdict_trigger": trigger},
        )
    elif min_success_rate is not None and min_success_rate < 95:
        trigger = "availability"
        msg = (
            f"模型可用性: 最低成功率 {min_success_rate:.0f}% "
            f"({min_success_model}) <95%"
        )
        if max_p95 > 60:
            msg += f"；最大 P95 {max_p95:.1f}s"
        out_data["verdict_trigger"] = trigger
        s.warn(
            "perf.models", msg,
            detail="\n".join(detail_lines),
            data={**payload_data, "verdict_trigger": trigger},
        )
    elif max_p95 > 60:
        trigger = "latency"
        msg = f"模型延迟: 最大 P95 {max_p95:.1f}s（>60s，可用性正常）"
        out_data["verdict_trigger"] = trigger
        s.warn(
            "perf.models", msg,
            detail="\n".join(detail_lines),
            data={**payload_data, "verdict_trigger": trigger},
        )
    else:
        trigger = "ok"
        msg = (
            f"模型性能: {len(models_payload)} 模型 / 最大 P95 {max_p95:.1f}s"
        )
        if min_success_rate is not None:
            msg += f" / 最低成功率 {min_success_rate:.0f}%"
        out_data["verdict_trigger"] = trigger
        s.ok(
            "perf.models", msg,
            detail="\n".join(detail_lines),
            data={**payload_data, "verdict_trigger": trigger},
        )
    return out_data


def _section_tools(s: Section, data: dict) -> dict:
    out_data: dict = {}
    timed_tools = {
        n: ts for n, ts in data["tool_stats"].items() if ts["durations"]
    }
    tools_payload = {}
    if not timed_tools:
        s.ok(
            "perf.tools",
            "工具性能: 无工具调用数据",
            data={"tools": {}},
        )
        out_data["tools"] = {}
        return out_data

    ranked = sorted(
        timed_tools.items(), key=lambda kv: kv[1]["calls"], reverse=True,
    )[:10]
    detail_lines = []
    for name, ts in ranked:
        durs = sorted(ts["durations"])
        calls = ts["calls"]
        p50 = pct(durs, 0.50)
        p95 = pct(durs, 0.95)
        mx = durs[-1]
        err_rate = (ts["errors"] / calls * 100) if calls else 0.0
        detail_lines.append(
            f"{name}: {calls} 次 | P50={p50:.3f}s P95={p95:.3f}s "
            f"Max={mx:.3f}s | 错误 {err_rate:.0f}%",
        )
        tools_payload[name] = {
            "calls": calls,
            "errors": ts["errors"],
            "error_rate_pct": round(err_rate, 1),
            "p50_s": round(p50, 3),
            "p95_s": round(p95, 3),
            "max_s": round(mx, 3),
        }
    out_data["tools"] = tools_payload
    s.ok(
        "perf.tools",
        f"工具性能: Top {len(tools_payload)} 工具",
        detail="\n".join(detail_lines),
        data={"tools": tools_payload},
    )
    return out_data


def _section_slow_calls(s: Section, data: dict) -> dict:
    out_data: dict = {}
    slow = sorted(data["slow_calls_top"], key=lambda x: x[0], reverse=True)
    seen_keys = set()
    dedup = []
    for entry in slow:
        key = entry[3]
        if key is not None:
            if key in seen_keys:
                continue
            seen_keys.add(key)
        dedup.append(entry)
    top20 = dedup[:20]
    payload = [
        {"duration_s": round(e[0], 3), "kind": e[1], "summary": e[2]}
        for e in top20
    ]
    out_data["slow_calls_top20"] = payload
    if not top20:
        s.ok("perf.slow_calls", "慢调用 Top 20: 无数据", data={"top": []})
    else:
        detail = "\n".join(
            f"[{i}] {e[2]}" for i, e in enumerate(top20, 1)
        )
        s.ok(
            "perf.slow_calls",
            f"慢调用 Top {len(top20)}",
            detail=detail,
            data={"top": payload},
        )
    return out_data


def _section_api_errors(s: Section, data: dict) -> dict:
    out_data: dict = {}
    api_err_total = sum(data["api_error_stats"].values())
    api_total = data["api_total_assistant_calls"]
    err_rate = (
        api_err_total / api_total * 100 if api_total else 0.0
    )
    payload = {
        "total_calls": api_total,
        "error_count": api_err_total,
        "error_rate_pct": round(err_rate, 2),
        "by_category": dict(data["api_error_stats"]),
    }
    out_data["api_errors"] = payload

    body_lines = []
    if api_total == 0:
        body_lines.append("无调用数据")
    else:
        body_lines.append(
            f"总异常: {api_err_total} / {api_total} ({err_rate:.1f}%)",
        )
        for cat, n in sorted(
            data["api_error_stats"].items(), key=lambda kv: -kv[1],
        ):
            body_lines.append(f"  {cat}: {n}")
    abnormal = data["abnormal_stops"]
    if abnormal:
        body_lines.append("")
        body_lines.append(f"异常 stopReason 样本: {len(abnormal)}")
        body_lines.extend(abnormal[:20])

    out_data["abnormal_stops"] = abnormal

    if api_total > 0 and err_rate > 5:
        s.warn(
            "perf.api_errors",
            f"API 错误率: {err_rate:.1f}% ({api_err_total}/{api_total})",
            detail="\n".join(body_lines),
            data=payload,
        )
    elif api_err_total > 0:
        s.warn(
            "perf.api_errors",
            f"API 错误: {api_err_total} 条",
            detail="\n".join(body_lines),
            data=payload,
        )
    else:
        s.ok(
            "perf.api_errors",
            f"API 错误: 0 / {api_total} 调用",
            detail="\n".join(body_lines),
            data=payload,
        )
    return out_data


def _section_e2e(s: Section, data: dict) -> dict:
    out_data: dict = {}
    e2e = data["e2e_latencies"]
    if not e2e:
        s.ok(
            "perf.e2e_latency",
            "端到端延迟: 数据不足（无 user→assistant 配对）",
            data={"count": 0},
        )
        out_data["e2e_latency"] = {"count": 0}
        return out_data
    lat_sorted = sorted(e2e)
    p50 = pct(lat_sorted, 0.50)
    p95 = pct(lat_sorted, 0.95)
    mx = lat_sorted[-1]
    e2e_buckets = [
        ("<10s", 10.0), ("10-30s", 30.0), ("30-60s", 60.0),
        ("60-120s", 120.0), (">120s", float("inf")),
    ]
    bucket_counts = {lbl: 0 for lbl, _ in e2e_buckets}
    for v in lat_sorted:
        for (lbl, upper) in e2e_buckets:
            if v < upper:
                bucket_counts[lbl] += 1
                break
    payload = {
        "count": len(lat_sorted),
        "p50_s": round(p50, 3),
        "p95_s": round(p95, 3),
        "max_s": round(mx, 3),
        "buckets": dict(bucket_counts),
    }
    out_data["e2e_latency"] = payload
    detail_lines = [
        f"样本: {len(lat_sorted)} | P50={p50:.1f}s P95={p95:.1f}s "
        f"Max={mx:.1f}s",
        "分布:",
    ]
    for (lbl, _) in e2e_buckets:
        n = bucket_counts[lbl]
        pct_v = (n / len(lat_sorted) * 100) if lat_sorted else 0.0
        detail_lines.append(f"  {lbl}: {n} ({pct_v:.1f}%)")

    if p95 > 60:
        s.warn(
            "perf.e2e_latency",
            f"端到端延迟 P95={p95:.1f}s (>60s)",
            detail="\n".join(detail_lines),
            data=payload,
        )
    else:
        s.ok(
            "perf.e2e_latency",
            f"端到端延迟 P50={p50:.1f}s P95={p95:.1f}s",
            detail="\n".join(detail_lines),
            data=payload,
        )
    return out_data


def _section_daily_trend(
    s: Section, daily_stats: dict, trend_file_count: int,
) -> dict:
    out_data: dict = {}
    if not daily_stats:
        s.ok(
            "perf.daily_trend",
            "每日趋势: 数据不足",
            detail=(
                f"数据来源: 7 天 mtime 窗口内 {trend_file_count} 个 session 文件"
            ),
            data={"trend": [], "trend_file_count": trend_file_count},
        )
        out_data["daily_trend"] = []
        return out_data
    today = datetime.now().date()
    day_list = [
        (today - timedelta(days=i)).strftime("%m-%d") for i in range(7)
    ]
    detail_lines = [
        f"数据来源: 7 天 mtime 窗口内 {trend_file_count} 个 session 文件",
        "",
        f"    {'日期':<10} {'调用数':>8} {'P50延迟':>10} {'输出tokens':>14}",
    ]
    daily_payload = []
    for d_key in day_list:
        d = daily_stats.get(d_key)
        if not d or d["calls"] == 0:
            detail_lines.append(
                f"    {d_key:<10} {0:>8} {'-':>10} {'-':>14}",
            )
            daily_payload.append({
                "date": d_key, "calls": 0,
                "p50_s": None, "output_tokens": 0,
            })
            continue
        durs = sorted(d["durs"])
        p50 = pct(durs, 0.50) if durs else 0.0
        detail_lines.append(
            f"    {d_key:<10} {d['calls']:>8} {p50:>9.1f}s "
            f"{fmt_tokens(d['output']):>14}",
        )
        daily_payload.append({
            "date": d_key,
            "calls": d["calls"],
            "p50_s": round(p50, 3),
            "output_tokens": d["output"],
        })
    out_data["daily_trend"] = daily_payload
    days_with_data = sum(1 for d in daily_payload if d["calls"])
    s.ok(
        "perf.daily_trend",
        f"每日趋势 (最近 7 天): {days_with_data} 天有数据",
        detail="\n".join(detail_lines),
        data={
            "trend": daily_payload,
            "trend_file_count": trend_file_count,
        },
    )
    return out_data


def _section_cache_session(s: Section, data: dict) -> dict:
    out_data: dict = {}
    if data["cache_total_calls"] == 0:
        s.ok(
            "perf.cache",
            "Session cache 命中率: 无数据",
            data={"total_calls": 0},
        )
        out_data["cache_hit_rate"] = {"total_calls": 0}
        return out_data
    hit_pct = (
        data["cache_calls_with_cache"] / data["cache_total_calls"] * 100
    )
    denom = data["cache_sum_input"] + data["cache_sum_cache_read"]
    ratio_pct = None
    if denom > 0:
        ratio_pct = round(
            data["cache_sum_cache_read"] / denom * 100, 3,
        )
    payload = {
        "total_calls": data["cache_total_calls"],
        "calls_with_cache_read": data["cache_calls_with_cache"],
        "hit_rate_pct": round(hit_pct, 2),
        "input_tokens": data["cache_sum_input"],
        "cache_read_tokens": data["cache_sum_cache_read"],
        "cache_write_tokens": data["cache_sum_cache_write"],
        "ctx_cache_ratio_pct": ratio_pct,
    }
    out_data["cache_hit_rate"] = payload
    detail = (
        f"总调用: {data['cache_total_calls']} | "
        f"触发 cache_read: {data['cache_calls_with_cache']} "
        f"({hit_pct:.1f}%)\n"
        f"cache_read: {fmt_tokens(data['cache_sum_cache_read'])} | "
        f"input: {fmt_tokens(data['cache_sum_input'])} | "
        f"cache_write: {fmt_tokens(data['cache_sum_cache_write'])}"
    )
    if ratio_pct is not None:
        detail += (
            f"\n上下文 cache 占比: {ratio_pct:.3f}%"
        )

    if hit_pct < 50 and data["cache_total_calls"] >= 10:
        s.warn(
            "perf.cache",
            f"Session cache 命中率: {hit_pct:.1f}% (<50%)",
            detail=detail,
            data=payload,
        )
    else:
        s.ok(
            "perf.cache",
            f"Session cache 命中率: {hit_pct:.1f}%",
            detail=detail,
            data=payload,
        )
    return out_data


def _section_trajectory_perf(s: Section, ctx: DiagContext) -> dict:
    out_data: dict = {}
    # Use the per-ctx trajectory cache so the second `_section_prompt_budget`
    # call in this same collector — and any of the other no-window callers in
    # the `all` command — share the parse instead of redoing it.
    files = ctx.trajectory_files()
    if not files:
        s.ok(
            "trajectory.cache_health",
            "未发现 trajectory 文件 — 跳过 cache/compaction 分析",
            data={"found": False},
        )
        return out_data
    runs = ctx.collect_runs()
    runs.sort(key=lambda r: r.started_ts_ms, reverse=True)
    recent = runs[:100]
    if not recent:
        s.ok(
            "trajectory.cache_health",
            "无 trajectory run 数据",
            data={"samples": 0},
        )
        return out_data

    broke_known = [r for r in recent if r.cache_broke is not None]
    broke = sum(1 for r in broke_known if r.cache_broke)
    broke_pct = (broke / len(broke_known) * 100) if broke_known else 0.0

    cache_total = sum(r.usage_total for r in recent)
    cache_read = sum(r.usage_cache_read for r in recent)
    ratio = (cache_read / cache_total * 100) if cache_total else 0.0

    compaction_runs = [r for r in recent if r.compaction_count > 0]
    compaction_max = max((r.compaction_count for r in recent), default=0)
    compaction_rate = (
        len(compaction_runs) / len(recent) * 100 if recent else 0.0
    )

    by_trig: dict = {}
    for r in recent:
        d = r.duration_ms
        if d is None or d < 0:
            continue
        by_trig.setdefault(r.trigger, []).append(d / 1000.0)
    latency_payload: dict = {}
    body_lines = [
        f"样本: 最近 {len(recent)} 个 run",
        f"  cache_broke=true: {broke}/{len(broke_known)} ({broke_pct:.1f}%)",
    ]
    if cache_total:
        body_lines.append(
            f"  cacheRead/total: {ratio:.1f}% "
            f"({fmt_tokens(cache_read)}/{fmt_tokens(cache_total)})",
        )
    body_lines.append(
        f"  compaction 触发: {len(compaction_runs)} 个 run "
        f"({compaction_rate:.1f}%) | max={compaction_max}",
    )
    if by_trig:
        body_lines.append("  per-trigger wall 耗时:")
        for trig in sorted(by_trig.keys()):
            durs = sorted(by_trig[trig])
            p50 = durs[len(durs) // 2]
            p95 = durs[int(0.95 * (len(durs) - 1))]
            mx = durs[-1]
            avg = sum(durs) / len(durs)
            body_lines.append(
                f"    {trig}: n={len(durs)} avg={avg:.1f}s "
                f"P50={p50:.1f}s P95={p95:.1f}s Max={mx:.1f}s",
            )
            latency_payload[trig] = {
                "count": len(durs),
                "avg_s": round(avg, 3),
                "p50_s": round(p50, 3),
                "p95_s": round(p95, 3),
                "max_s": round(mx, 3),
            }

    payload = {
        "found": True,
        "samples": len(recent),
        "cache_broke_pct": round(broke_pct, 2),
        "cache_broke_count": broke,
        "cache_read_total_ratio_pct": round(ratio, 2),
        "compaction_rate_pct": round(compaction_rate, 2),
        "compaction_runs": len(compaction_runs),
        "compaction_max": compaction_max,
        "per_trigger_latency": latency_payload,
    }
    out_data["trajectory_cache_health"] = payload

    verdict = Verdict.OK
    issues = []
    if 0 < ratio < 30:
        verdict = Verdict.WARN
        issues.append(f"cache 命中率 {ratio:.1f}% < 30%")
    if compaction_rate > 20:
        verdict = Verdict.WARN
        issues.append(f"compaction 率 {compaction_rate:.1f}% > 20%")
    if broke_pct > 30:
        verdict = Verdict.WARN
        issues.append(f"cache_broke 率 {broke_pct:.1f}% > 30%")

    msg = (
        f"Trajectory cache: {len(recent)} run | "
        f"cache_broke={broke_pct:.1f}% | compaction={compaction_rate:.1f}%"
    )
    if issues:
        msg += " — " + "; ".join(issues)

    if verdict == Verdict.WARN:
        s.warn(
            "trajectory.cache_health", msg,
            detail="\n".join(body_lines), data=payload,
        )
    else:
        s.ok(
            "trajectory.cache_health", msg,
            detail="\n".join(body_lines), data=payload,
        )
    return out_data


def _section_prompt_budget(s: Section, ctx: DiagContext) -> dict:
    out_data: dict = {}
    files = ctx.trajectory_files()
    if not files:
        s.ok(
            "trajectory.prompt_budget",
            "未发现 trajectory 文件 — 跳过 prompt budget",
            data={"found": False},
        )
        return out_data
    runs = ctx.collect_runs()
    runs.sort(key=lambda r: r.started_ts_ms, reverse=True)
    sample = [r for r in runs[:50] if r.system_prompt_chars > 0]
    if not sample:
        s.ok(
            "trajectory.prompt_budget",
            "最近 50 run 无 systemPromptReport 数据",
            data={"found": True, "samples": 0},
        )
        return out_data

    avg_total = sum(r.system_prompt_chars for r in sample) / len(sample)
    avg_proj = sum(
        r.system_prompt_project_chars for r in sample
    ) / len(sample)
    avg_nonproj = sum(
        r.system_prompt_non_project_chars for r in sample
    ) / len(sample)
    avg_skills = sum(r.skills_prompt_chars for r in sample) / len(sample)
    avg_tools = sum(r.tools_schema_chars for r in sample) / len(sample)
    truncation_runs = [
        r for r in sample if r.bootstrap_truncated_files > 0
    ]

    latest = sample[0]
    top_skills = latest.skills_top_entries[:10]
    top_tools = latest.tools_top_entries[:10]

    skill_warn = [
        sk for sk in top_skills if sk["blockChars"] > 5000
    ]
    skill_fail = [
        sk for sk in top_skills if sk["blockChars"] > 10000
    ]
    tool_warn = [
        tl for tl in top_tools if tl["schemaChars"] > 8000
    ]
    tool_fail = [
        tl for tl in top_tools if tl["schemaChars"] > 15000
    ]

    body = [
        f"样本: 最近 {len(sample)} run",
        f"avg systemPrompt chars: {int(avg_total):,}",
        f"  project={int(avg_proj):,} | non-project={int(avg_nonproj):,}",
        f"avg skills.promptChars: {int(avg_skills):,}",
        f"avg tools.schemaChars:  {int(avg_tools):,}",
    ]
    if top_skills:
        body.append("Top skills (blockChars):")
        for sk in top_skills:
            tag = ""
            if sk["blockChars"] > 10000:
                tag = "  FATAL"
            elif sk["blockChars"] > 5000:
                tag = "  WARN"
            body.append(f"  {sk['name']}: {sk['blockChars']:,} chars{tag}")
    if top_tools:
        body.append("Top tools (schemaChars):")
        for tl in top_tools:
            tag = ""
            if tl["schemaChars"] > 15000:
                tag = "  FATAL"
            elif tl["schemaChars"] > 8000:
                tag = "  WARN"
            body.append(
                f"  {tl['name']}: {tl['schemaChars']:,} chars "
                f"(properties={tl['propertiesCount']}){tag}",
            )

    payload = {
        "found": True,
        "samples": len(sample),
        "avg_chars": int(avg_total),
        "avg_project_chars": int(avg_proj),
        "avg_non_project_chars": int(avg_nonproj),
        "avg_skills_prompt_chars": int(avg_skills),
        "avg_tools_schema_chars": int(avg_tools),
        "skills_top": top_skills,
        "tools_top": top_tools,
        "skill_over_5000_count": len(skill_warn),
        "skill_over_10000_count": len(skill_fail),
        "tool_over_8000_count": len(tool_warn),
        "tool_over_15000_count": len(tool_fail),
        "bootstrap_truncated_runs": len(truncation_runs),
        "injected_workspace_files_latest": latest.injected_workspace_files,
    }
    out_data["trajectory_prompt_budget"] = payload

    if skill_fail or tool_fail:
        s.fail(
            "trajectory.prompt_budget",
            f"prompt budget: skill>10k={len(skill_fail)}, "
            f"tool>15k={len(tool_fail)}",
            detail="\n".join(body),
            data=payload,
        )
    elif skill_warn or tool_warn or truncation_runs:
        s.warn(
            "trajectory.prompt_budget",
            f"prompt budget: skill>5k={len(skill_warn)}, "
            f"tool>8k={len(tool_warn)}, bootstrap_truncated="
            f"{len(truncation_runs)}",
            detail="\n".join(body),
            data=payload,
        )
    else:
        s.ok(
            "trajectory.prompt_budget",
            f"prompt budget: avg {int(avg_total):,} chars / "
            f"{len(sample)} run",
            detail="\n".join(body),
            data=payload,
        )
    return out_data


@register
class PerformanceCollector:
    id = "performance"
    title = "性能"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)
        sessions_base = str(ctx.sessions_base)

        session_files = _collect_session_files(sessions_base, limit=20)
        report.data["session_files_analyzed"] = len(session_files)

        # daily_trend uses an independent 7-day mtime window so days with
        # real activity that fall outside the latest-20 perf sample aren't
        # silently reported as 0 calls.
        trend_files = _collect_session_files_by_window(sessions_base, days=7)
        report.data["trend_files_analyzed"] = len(trend_files)

        if session_files:
            data = _analyze_sessions(session_files)

            s_models = report.section("7.1 模型性能")
            report.data.update(_section_models(s_models, data, len(session_files)))

            s_tools = report.section("7.2 工具性能")
            report.data.update(_section_tools(s_tools, data))

            s_slow = report.section("7.3 慢调用 Top 20")
            report.data.update(_section_slow_calls(s_slow, data))

            s_api = report.section("7.4 API 错误分布")
            report.data.update(_section_api_errors(s_api, data))

            s_e2e = report.section("7.5 端到端延迟")
            report.data.update(_section_e2e(s_e2e, data))

            s_daily = report.section("7.6 每日趋势")
            daily_stats_trend = _parse_daily_stats(trend_files)
            report.data.update(
                _section_daily_trend(
                    s_daily, daily_stats_trend, len(trend_files),
                ),
            )

            s_cache = report.section("7.7 Session cache 命中率")
            report.data.update(_section_cache_session(s_cache, data))
        else:
            s_none = report.section("7.0 Session 扫描")
            s_none.ok(
                "perf.no_sessions",
                "未找到 Session 文件",
                data={"found": False},
            )

        s_traj = report.section("7.8 Trajectory cache + compaction")
        report.data.update(_section_trajectory_perf(s_traj, ctx))

        s_pb = report.section("7.9 Trajectory prompt budget")
        report.data.update(_section_prompt_budget(s_pb, ctx))

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
