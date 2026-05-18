#!/usr/bin/env python3
"""Trace the processing timeline of a user message in an OpenClaw session.

Channel-agnostic. Uses only universal data sources:
  1. session.jsonl  (required)  — message-level timeline
  2. trajectory.jsonl (optional) — run-level metadata
  3. gateway log     (optional) — embedded run start/prompt start/prompt end
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import output, paths, sessions


DEFAULT_BASE_DIR = paths.SESSIONS_BASE
DEFAULT_LOG_DIR = paths.LOG_DIR


def iso_to_epoch_ms(iso: str) -> int:
    s = iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0
    return int(dt.timestamp() * 1000)


def epoch_ms_to_iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def fmt_duration(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    m = int(ms // 60_000)
    s = (ms % 60_000) / 1000
    return f"{m}m{s:.1f}s"


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "toolCall":
                    parts.append(f"[toolCall:{c.get('name','')}]")
        return " ".join(parts)
    return str(content)


def resolve_session_file(
    session_id: str,
    base_dir: str = DEFAULT_BASE_DIR,
    agent: Optional[str] = None,
) -> Tuple[Optional[str], List[str]]:
    """Resolve UUID-or-prefix to a single session file path.

    Returns ``(path, candidates)``. ``path`` is None on miss or ambiguity;
    ``candidates`` is non-empty only when the prefix matched multiple
    distinct session UUIDs.
    """
    files, candidates = sessions.resolve(
        session_id, base_dir=base_dir, agent=agent, include_transient=False,
    )
    if candidates:
        return None, candidates
    if not files:
        return None, []
    return files[0][0], []


def find_trajectory_file(session_file: str) -> Optional[str]:
    d = os.path.dirname(session_file)
    base = os.path.basename(session_file).split(".jsonl")[0]
    traj = os.path.join(d, f"{base}.trajectory.jsonl")
    return traj if os.path.isfile(traj) else None


def find_gateway_logs(log_dir: str) -> List[str]:
    return sorted(glob.glob(os.path.join(log_dir, "openclaw-*.log")))


def load_records(filepath: str) -> List[Dict]:
    records: List[Dict] = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def find_user_messages(records: List[Dict]) -> List[Tuple[int, Dict]]:
    result = []
    for i, r in enumerate(records):
        if r.get("type") == "message":
            msg = r.get("message", {})
            if msg.get("role") == "user":
                result.append((i, r))
    return result


def find_first_message(records: List[Dict]) -> List[Tuple[int, Dict]]:
    """Fall back: any record whose type=='message' (regardless of role)."""
    result = []
    for i, r in enumerate(records):
        if r.get("type") == "message" and isinstance(r.get("message"), dict):
            result.append((i, r))
    return result


def select_user_message(records, msg_index=None, msg_id=None, msg_match=None):
    user_msgs = find_user_messages(records)
    if not user_msgs:
        # No user messages — fall back to scanning all message records so trace
        # still works for assistant-only streams (e.g. cron delivery sessions).
        user_msgs = find_first_message(records)
        if not user_msgs:
            print("Error: no message records found in session", file=sys.stderr)
            sys.exit(1)
        print(
            f"Note: no user-role messages; tracing from first message record "
            f"({len(user_msgs)} message(s) total)",
            file=sys.stderr,
        )
    if msg_id is not None:
        for idx, r in user_msgs:
            if r.get("id") == msg_id:
                return idx, r
        print(f"Error: no message with id '{msg_id}'", file=sys.stderr)
        sys.exit(1)
    if msg_match is not None:
        for idx, r in user_msgs:
            text = extract_text(r.get("message", {}).get("content", ""))
            if msg_match in text:
                return idx, r
        print(f"Error: no message matching '{msg_match}'", file=sys.stderr)
        sys.exit(1)
    if msg_index is not None:
        if msg_index < 0 or msg_index >= len(user_msgs):
            print(f"Error: msg-index {msg_index} out of range (0..{len(user_msgs)-1})",
                  file=sys.stderr)
            sys.exit(1)
        return user_msgs[msg_index]
    return user_msgs[-1]


def extract_trace_records(records, start_idx):
    trace = []
    for i in range(start_idx, len(records)):
        r = records[i]
        if i > start_idx and r.get("type") == "message":
            msg = r.get("message", {})
            if msg.get("role") == "user":
                break
        trace.append(r)
    return trace


def _tool_batch_duration(results, prev_epoch):
    if not results or prev_epoch is None:
        return 0
    max_ts = max(r.get("message", {}).get("timestamp", 0) for r in results)
    return max(0, max_ts - prev_epoch)


def _flush_tool_batch(events, tool_execs, results, base_ms, prev_epoch):
    if not results:
        return
    batch_start_epoch = prev_epoch or base_ms
    batch_end_epoch = max(r.get("message", {}).get("timestamp", 0) for r in results)
    batch_dur = max(0, batch_end_epoch - batch_start_epoch)
    by_name: Dict[str, int] = {}
    errors = 0
    for r in results:
        msg = r.get("message", {})
        name = msg.get("toolName", "?")
        by_name[name] = by_name.get(name, 0) + 1
        if msg.get("isError"):
            errors += 1
    parts = [(f"{n}" + (f" ×{cnt}" if cnt > 1 else "")) for n, cnt in by_name.items()]
    tools_str = " + ".join(parts)
    status = "ok" if errors == 0 else f"{errors} error(s)"
    events.append({
        "offset_ms": max(0, (batch_start_epoch - base_ms)),
        "type": "tool_batch",
        "detail": f"{tools_str} → {status} ({fmt_duration(batch_dur)})",
        "count": len(results),
        "duration_ms": batch_dur,
    })
    for r in results:
        msg = r.get("message", {})
        name = msg.get("toolName", "?")
        ts = msg.get("timestamp", 0)
        dur = max(0, ts - batch_start_epoch) if ts and batch_start_epoch else 0
        tool_execs.append({
            "name": name,
            "duration_ms": dur,
            "is_error": msg.get("isError", False),
        })


def analyze_phases(trace):
    events: List[Dict] = []
    model_calls: List[Dict] = []
    tool_execs: List[Dict] = []

    user_rec = trace[0]
    user_msg = user_rec.get("message", {})
    base_ms = user_msg.get("timestamp", 0)
    if not base_ms:
        base_ms = iso_to_epoch_ms(user_rec.get("timestamp", ""))

    events.append({"offset_ms": 0, "type": "user", "detail": "Message received"})

    model_num = 0
    tool_num = 0
    prev_assistant_record_epoch: Optional[int] = None
    pending_tool_results: List[Dict] = []
    total_model_ms = 0
    total_tool_ms = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read = 0
    total_cache_write = 0

    for r in trace[1:]:
        rtype = r.get("type")
        if rtype == "message":
            msg = r.get("message", {})
            role = msg.get("role")
            if role == "assistant":
                if pending_tool_results:
                    _flush_tool_batch(events, tool_execs, pending_tool_results,
                                      base_ms, prev_assistant_record_epoch)
                    batch_dur = _tool_batch_duration(pending_tool_results, prev_assistant_record_epoch)
                    total_tool_ms += batch_dur
                    tool_num += len(pending_tool_results)
                    pending_tool_results = []
                model_num += 1
                msg_ts = msg.get("timestamp", 0)
                record_epoch = iso_to_epoch_ms(r.get("timestamp", ""))
                duration_ms = record_epoch - msg_ts if (record_epoch and msg_ts) else 0
                usage = msg.get("usage", {})
                out_tok = usage.get("output", 0)
                in_tok = usage.get("input", 0)
                cache_r = usage.get("cacheRead", 0)
                cache_w = usage.get("cacheWrite", 0)
                stop = msg.get("stopReason", "")
                provider = msg.get("provider", "")
                model = msg.get("model", "")
                rate = out_tok / (duration_ms / 1000) if duration_ms > 0 else 0
                start_offset = msg_ts - base_ms if msg_ts else 0
                end_offset = record_epoch - base_ms if record_epoch else 0
                events.append({
                    "offset_ms": start_offset, "type": "model_start", "num": model_num,
                    "detail": f"Call started → {provider}/{model}" if provider else "Call started",
                })
                events.append({
                    "offset_ms": end_offset, "type": "model_end", "num": model_num,
                    "detail": f"Completed (stopReason={stop})" + (" ← FINAL" if stop == "stop" else ""),
                    "duration_ms": duration_ms, "tokens_in": in_tok, "tokens_out": out_tok,
                    "cache_read": cache_r, "cache_write": cache_w, "rate": round(rate, 1),
                })
                tool_names = []
                content = msg.get("content", [])
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "toolCall":
                            tool_names.append(c.get("name", "?"))
                model_calls.append({
                    "num": model_num, "duration_ms": duration_ms,
                    "tokens_out": out_tok, "tokens_in": in_tok,
                    "cache_read": cache_r, "cache_write": cache_w,
                    "stop_reason": stop, "tool_names": tool_names,
                    "provider": provider, "model": model, "rate": round(rate, 1),
                })
                total_model_ms += duration_ms
                total_input_tokens += in_tok
                total_output_tokens += out_tok
                total_cache_read += cache_r
                total_cache_write += cache_w
                prev_assistant_record_epoch = record_epoch
            elif role == "toolResult":
                pending_tool_results.append(r)
        elif rtype == "custom" and r.get("customType") == "openclaw:prompt-error":
            data = r.get("data", {})
            err_ts = data.get("timestamp", 0)
            offset = err_ts - base_ms if err_ts else 0
            events.append({
                "offset_ms": offset, "type": "error",
                "detail": f"prompt-error: {data.get('error', '?')}",
                "provider": data.get("provider", ""), "model": data.get("model", ""),
            })

    if pending_tool_results:
        _flush_tool_batch(events, tool_execs, pending_tool_results,
                          base_ms, prev_assistant_record_epoch)
        batch_dur = _tool_batch_duration(pending_tool_results, prev_assistant_record_epoch)
        total_tool_ms += batch_dur
        tool_num += len(pending_tool_results)

    last_offset = events[-1]["offset_ms"] if events else 0
    return {
        "events": events, "model_calls": model_calls, "tool_execs": tool_execs,
        "summary": {
            "total_ms": last_offset, "model_count": model_num,
            "model_total_ms": total_model_ms, "tool_count": tool_num,
            "tool_total_ms": total_tool_ms, "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read": total_cache_read, "total_cache_write": total_cache_write,
        },
        "base_epoch_ms": base_ms,
    }


def load_trajectory_info(traj_path, base_epoch_ms):
    runs: Dict[str, List[Dict]] = {}
    try:
        with open(traj_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = r.get("runId", "")
                if rid:
                    runs.setdefault(rid, []).append(r)
    except OSError:
        return None
    if not runs:
        return None
    best_run = None
    best_delta = float("inf")
    for rid, evts in runs.items():
        for e in evts:
            if e.get("type") == "session.started":
                ts = iso_to_epoch_ms(e.get("ts", ""))
                delta = abs(ts - base_epoch_ms)
                if delta < best_delta:
                    best_delta = delta
                    best_run = rid
                break
    if best_run is None or best_delta > 60_000:
        return None
    evts = runs[best_run]
    info: Dict[str, Any] = {"runId": best_run}
    ts_map: Dict[str, int] = {}
    for e in evts:
        etype = e.get("type", "")
        ts_map[etype] = iso_to_epoch_ms(e.get("ts", ""))
        if etype == "session.started":
            data = e.get("data", {})
            info["trigger"] = data.get("trigger")
            info["toolCount"] = data.get("toolCount")
        elif etype == "trace.metadata":
            data = e.get("data", {})
            model_info = data.get("model", {})
            info["model_config"] = {
                k: model_info.get(k)
                for k in ("provider", "name", "api", "thinkLevel", "reasoningLevel")
                if model_info.get(k) is not None
            }
        elif etype == "session.ended":
            data = e.get("data", {})
            info["status"] = data.get("status")
            info["aborted"] = data.get("aborted")
            info["timedOut"] = data.get("timedOut")
    if "session.started" in ts_map and "context.compiled" in ts_map:
        info["context_compilation_ms"] = ts_map["context.compiled"] - ts_map["session.started"]
    if "context.compiled" in ts_map and "prompt.submitted" in ts_map:
        info["prompt_submission_ms"] = ts_map["prompt.submitted"] - ts_map["context.compiled"]

    # Bracket the run's wall-clock window so callers can decide whether the
    # session-store systemPromptReport (which only retains the *latest* run)
    # actually describes this traced run, or a later one.
    best_run_started = ts_map.get("session.started")
    if isinstance(best_run_started, int) and best_run_started > 0:
        info["session_started_ms"] = best_run_started
        later_starts: List[int] = []
        for rid, evt_list in runs.items():
            if rid == best_run:
                continue
            for ev in evt_list:
                if ev.get("type") == "session.started":
                    ts = iso_to_epoch_ms(ev.get("ts", ""))
                    if ts > best_run_started:
                        later_starts.append(ts)
                    break
        if later_starts:
            info["next_run_started_ms"] = min(later_starts)

    # Fallback system prompt size: the trajectory's last context.compiled event
    # carries either the literal systemPrompt string (small prompts) or a
    # truncation envelope with originalChars (>32K chars). Size here may be
    # ~1-2% off vs the runtime store value because it reflects what was logged
    # to trajectory, not what was actually submitted to the model.
    sp_chars: Optional[int] = None
    sp_truncated = False
    sp_tools_count: Optional[int] = None
    sp_messages_count: Optional[int] = None
    for e in evts:
        if e.get("type") != "context.compiled":
            continue
        data = e.get("data") or {}
        sp = data.get("systemPrompt")
        if isinstance(sp, str):
            sp_chars = len(sp)
            sp_truncated = False
        elif isinstance(sp, dict) and sp.get("truncated"):
            try:
                sp_chars = int(sp.get("originalChars") or 0) or None
            except (TypeError, ValueError):
                sp_chars = None
            sp_truncated = True
        tools = data.get("tools")
        if isinstance(tools, list):
            sp_tools_count = len(tools)
        msgs = data.get("messages")
        if isinstance(msgs, list):
            sp_messages_count = len(msgs)
    if sp_chars is not None:
        info["systemPrompt"] = {
            "chars": sp_chars,
            "source": "trajectory",
            "truncated_in_trajectory": sp_truncated,
            "tools_in_request": sp_tools_count,
            "messages_in_request": sp_messages_count,
        }
    return info


def _parse_log_ts(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def load_gateway_timing(log_files, session_id, base_epoch_ms):
    if not log_files:
        return None
    run_start = None
    prompt_start = None
    prompt_end = None
    duration = None
    base_date = epoch_ms_to_iso(base_epoch_ms)[:10]
    for lf in log_files:
        if base_date not in os.path.basename(lf):
            continue
        try:
            with open(lf, "r") as f:
                for line in f:
                    if session_id not in line or "agent/embedded" not in line:
                        continue
                    try:
                        rec = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    msg = rec.get("1", "")
                    ts_str = rec.get("time", "")
                    if "embedded run start:" in msg and f"sessionId={session_id}" in msg:
                        ts = _parse_log_ts(ts_str)
                        if ts and abs(ts - base_epoch_ms) < 120_000:
                            run_start = ts
                    elif "embedded run prompt start:" in msg and f"sessionId={session_id}" in msg:
                        ts = _parse_log_ts(ts_str)
                        if ts and abs(ts - base_epoch_ms) < 120_000:
                            prompt_start = ts
                    elif "embedded run prompt end:" in msg and f"sessionId={session_id}" in msg:
                        ts = _parse_log_ts(ts_str)
                        if run_start and ts and ts > run_start:
                            prompt_end = ts
                            m = re.search(r"durationMs=(\d+)", msg)
                            duration = int(m.group(1)) if m else None
        except OSError:
            continue
    if run_start is None:
        return None
    result: Dict[str, Any] = {}
    if run_start and prompt_start:
        result["run_to_prompt_ms"] = prompt_start - run_start
    if prompt_start and prompt_end:
        result["prompt_duration_ms"] = prompt_end - prompt_start
    if duration:
        result["reported_duration_ms"] = duration
    return result if result else None


SEP = "═" * 66
LINE = "─" * 66


def _pct(part, total):
    if total == 0:
        return "0%"
    return f"{part / total * 100:.1f}%"


def _estimate_tokens(chars: int) -> int:
    """Rough char-to-token estimator (~4 chars/token). Fine for this UI."""
    if chars <= 0:
        return 0
    return chars // 4


def build_system_prompt_info(
    store_report: Optional[Dict[str, Any]],
    traj_info: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Merge sessions.json (preferred) and trajectory (fallback) into one view.

    Store data is more accurate (it's what the runtime computed before any
    trajectory truncation) and richer (project/non-project breakdown,
    injected files, skills/tools schema sizes). Trajectory only kicks in when
    the store has no entry — e.g. an old session that was reset and the store
    no longer tracks it.

    Caveat: the store keeps only the **most recent** systemPromptReport per
    session. When the user traces an older message in an active session
    (--msg-index/--msg-id/--msg-match), the store entry can describe a later
    run. We use the trajectory's per-run window (session_started_ms ..
    next_run_started_ms) to detect that mismatch and fall back to the
    trajectory data for that older run.
    """
    use_store = bool(store_report)
    if store_report and traj_info:
        gen_at = store_report.get("generatedAt")
        run_started = traj_info.get("session_started_ms")
        next_started = traj_info.get("next_run_started_ms")
        if isinstance(gen_at, int) and isinstance(run_started, int):
            in_window = gen_at >= run_started
            if isinstance(next_started, int):
                in_window = in_window and gen_at < next_started
            use_store = in_window
    if use_store and store_report:
        sp = store_report.get("systemPrompt") or {}
        chars = sp.get("chars")
        if not isinstance(chars, int) or chars <= 0:
            return None
        tools = store_report.get("tools") or {}
        skills = store_report.get("skills") or {}
        injected_raw = store_report.get("injectedWorkspaceFiles") or []
        injected: List[Dict[str, Any]] = []
        for f in injected_raw:
            if not isinstance(f, dict):
                continue
            injected.append({
                "name": f.get("name") or "?",
                "rawChars": f.get("rawChars"),
                "injectedChars": f.get("injectedChars"),
                "truncated": bool(f.get("truncated")),
            })
        tools_entries = tools.get("entries") if isinstance(tools.get("entries"), list) else []
        skills_entries = skills.get("entries") if isinstance(skills.get("entries"), list) else []
        return {
            "source": store_report.get("source") or "run",
            "chars": chars,
            "estimatedTokens": _estimate_tokens(chars),
            "projectContextChars": sp.get("projectContextChars"),
            "nonProjectContextChars": sp.get("nonProjectContextChars"),
            "tools": {
                "count": len(tools_entries),
                "schemaChars": tools.get("schemaChars"),
            },
            "skills": {
                "count": len(skills_entries),
                "promptChars": skills.get("promptChars"),
            },
            "injectedWorkspaceFiles": injected,
            "truncatedInTrajectory": False,
            "provider": store_report.get("provider"),
            "model": store_report.get("model"),
        }
    if traj_info and isinstance(traj_info.get("systemPrompt"), dict):
        tsp = traj_info["systemPrompt"]
        chars = tsp.get("chars")
        if not isinstance(chars, int) or chars <= 0:
            return None
        return {
            "source": "trajectory",
            "chars": chars,
            "estimatedTokens": _estimate_tokens(chars),
            "tools": {"count": tsp.get("tools_in_request")},
            "messages_in_request": tsp.get("messages_in_request"),
            "truncatedInTrajectory": bool(tsp.get("truncated_in_trajectory")),
        }
    return None


def _fmt_int(n: Optional[int]) -> str:
    if not isinstance(n, int):
        return "?"
    return f"{n:,}"


def render_system_prompt_text(sp: Dict[str, Any], indent: str = "  ") -> List[str]:
    """Render the 'Context size:' block for the text-mode trace output."""
    lines: List[str] = []
    src = sp.get("source") or "?"
    chars = sp.get("chars") or 0
    tok = sp.get("estimatedTokens") or _estimate_tokens(chars)
    lines.append(f"{indent}Context size:")
    lines.append(
        f"{indent}  System prompt: {_fmt_int(chars)} chars (~{_fmt_int(tok)} tok) [{src}]"
    )
    pc = sp.get("projectContextChars")
    npc = sp.get("nonProjectContextChars")
    if isinstance(pc, int):
        lines.append(f"{indent}    project-context: {_fmt_int(pc)} chars")
    if isinstance(npc, int):
        lines.append(f"{indent}    non-project:     {_fmt_int(npc)} chars")
    if sp.get("truncatedInTrajectory"):
        lines.append(f"{indent}    (approximate, full text not stored in trajectory)")
    tools = sp.get("tools") or {}
    if isinstance(tools.get("schemaChars"), int):
        lines.append(
            f"{indent}  Tool schemas (JSON): {_fmt_int(tools['schemaChars'])} chars "
            f"({tools.get('count', '?')} tools)"
        )
    elif isinstance(tools.get("count"), int):
        lines.append(f"{indent}  Tools in request:    {tools['count']}")
    skills = sp.get("skills") or {}
    if isinstance(skills.get("promptChars"), int):
        lines.append(
            f"{indent}  Skills (text):       {_fmt_int(skills['promptChars'])} chars "
            f"({skills.get('count', '?')} skills)"
        )
    msgs_n = sp.get("messages_in_request")
    if isinstance(msgs_n, int):
        lines.append(f"{indent}  Context messages:    {msgs_n}")
    files = sp.get("injectedWorkspaceFiles") or []
    if files:
        lines.append(f"{indent}  Injected workspace files ({len(files)}):")
        for f in files[:8]:
            raw = f.get("rawChars")
            inj = f.get("injectedChars")
            tag = " (TRUNCATED)" if f.get("truncated") else ""
            lines.append(
                f"{indent}    {f.get('name', '?')}: raw {_fmt_int(raw)} → injected {_fmt_int(inj)}{tag}"
            )
        if len(files) > 8:
            lines.append(f"{indent}    ... +{len(files) - 8} more")
    return lines


def format_text(session_id, user_msg_index, user_msg_id, analysis,
                traj_info=None, gw_info=None, system_prompt=None):
    lines: List[str] = []
    lines.append(SEP)
    lines.append(f"Message Trace: session {session_id}")
    lines.append(f"User Message #{user_msg_index} (id: {user_msg_id})")
    lines.append(SEP)
    lines.append("")
    lines.append("Timeline:")
    lines.append(LINE)
    for ev in analysis["events"]:
        off = ev["offset_ms"]
        etype = ev["type"]
        detail = ev.get("detail", "")
        if etype == "user":
            lines.append(f"  T+{off:<10} [user]        {detail}")
        elif etype == "model_start":
            lines.append(f"  T+{off:<10} [model #{ev['num']}]    {detail}")
        elif etype == "model_end":
            lines.append(f"  T+{off:<10} [model #{ev['num']}]    {detail}")
            lines.append(
                f"                             ├─ tokens: in={ev.get('tokens_in',0)} out={ev.get('tokens_out',0)}"
                + (f" cache_read={ev['cache_read']}" if ev.get("cache_read") else "")
                + (f" cache_write={ev['cache_write']}" if ev.get("cache_write") else "")
            )
            lines.append(f"                             ├─ duration: {fmt_duration(ev.get('duration_ms', 0))}")
            lines.append(f"                             └─ rate: {ev.get('rate', 0)} tok/s")
        elif etype == "tool_batch":
            lines.append(f"  T+{off:<10} [tool]        {detail}")
        elif etype == "error":
            lines.append(f"  T+{off:<10} [ERROR]       {detail}")
    lines.append(LINE)
    lines.append("")

    s = analysis["summary"]
    total = s["total_ms"]
    lines.append("Summary:")
    lines.append(f"  Total time:          {fmt_duration(total)}")
    lines.append(
        f"  Model calls:         {s['model_count']}"
        + (f", total {fmt_duration(s['model_total_ms'])} ({_pct(s['model_total_ms'], total)})"
           if s["model_count"] else "")
    )
    lines.append(
        f"  Tool executions:     {s['tool_count']}"
        + (f", total {fmt_duration(s['tool_total_ms'])} ({_pct(s['tool_total_ms'], total)})"
           if s["tool_count"] else "")
    )
    lines.append(
        f"  Tokens:              in={s['total_input_tokens']} out={s['total_output_tokens']}"
        + (f" cache_read={s['total_cache_read']}" if s["total_cache_read"] else "")
        + (f" cache_write={s['total_cache_write']}" if s["total_cache_write"] else "")
    )
    avg_rate = s["total_output_tokens"] / (s["model_total_ms"] / 1000) if s["model_total_ms"] > 0 else 0
    lines.append(f"  Avg output rate:     {avg_rate:.1f} tok/s")
    lines.append("")

    if analysis["model_calls"]:
        lines.append("  Model breakdown:")
        for mc in analysis["model_calls"]:
            tools_str = ""
            if mc["stop_reason"] == "toolUse" and mc["tool_names"]:
                tnames = mc["tool_names"]
                if len(tnames) <= 3:
                    tools_str = ",".join(tnames)
                else:
                    tools_str = f"{tnames[0]}+{len(tnames)-1}more"
                tools_str = f" (toolUse → {tools_str})"
            elif mc["stop_reason"] == "stop":
                tools_str = " (stop) ← final"
            else:
                tools_str = f" ({mc['stop_reason']})" if mc["stop_reason"] else ""
            lines.append(
                f"    #{mc['num']:<3} {fmt_duration(mc['duration_ms']):>8}  "
                f"out={mc['tokens_out']:<6}{tools_str}"
            )
        lines.append("")

    if analysis["tool_execs"]:
        by_name: Dict[str, Dict] = {}
        for te in analysis["tool_execs"]:
            name = te["name"]
            if name not in by_name:
                by_name[name] = {"count": 0, "total_ms": 0, "errors": 0}
            by_name[name]["count"] += 1
            by_name[name]["total_ms"] += te["duration_ms"]
            if te["is_error"]:
                by_name[name]["errors"] += 1
        lines.append("  Tool breakdown:")
        for name, info in sorted(by_name.items(), key=lambda x: -x[1]["total_ms"]):
            avg = info["total_ms"] / info["count"] if info["count"] else 0
            err_str = f" ({info['errors']} errors)" if info["errors"] else ""
            lines.append(
                f"    {name + ':':<24} {info['count']} call(s), "
                f"{fmt_duration(info['total_ms'])} total, "
                f"avg {fmt_duration(avg)}{err_str}"
            )
        lines.append("")

    if traj_info:
        lines.append("  Run metadata (from trajectory):")
        lines.append(f"    runId: {traj_info.get('runId', '?')}")
        if traj_info.get("trigger"):
            lines.append(f"    trigger: {traj_info['trigger']}")
        if traj_info.get("context_compilation_ms") is not None:
            lines.append(f"    context compilation: {fmt_duration(traj_info['context_compilation_ms'])}")
        if traj_info.get("model_config"):
            cfg = traj_info["model_config"]
            parts = [f"{k}={v}" for k, v in cfg.items() if v is not None]
            lines.append(f"    model config: {', '.join(parts)}")
        if traj_info.get("status"):
            lines.append(f"    status: {traj_info['status']}")
        lines.append("")

    if system_prompt:
        lines.extend(render_system_prompt_text(system_prompt))
        lines.append("")

    if gw_info:
        lines.append("  Gateway timing (from log):")
        if "run_to_prompt_ms" in gw_info:
            lines.append(f"    run_start → prompt_start: {fmt_duration(gw_info['run_to_prompt_ms'])} (context compilation)")
        if "prompt_duration_ms" in gw_info:
            lines.append(f"    prompt_start → prompt_end: {fmt_duration(gw_info['prompt_duration_ms'])} (total embedded run)")
        lines.append("")

    return "\n".join(lines)


def format_json(session_id, session_file, user_msg_index, user_msg_id, analysis,
                traj_info=None, gw_info=None, system_prompt=None):
    result = {
        "session_id": session_id, "session_file": session_file,
        "user_message_index": user_msg_index, "user_message_id": user_msg_id,
        "base_epoch_ms": analysis["base_epoch_ms"],
        "timeline": analysis["events"], "model_calls": analysis["model_calls"],
        "tool_execs": analysis["tool_execs"], "summary": analysis["summary"],
    }
    if traj_info:
        result["trajectory"] = traj_info
    if gw_info:
        result["gateway"] = gw_info
    if system_prompt:
        result["systemPrompt"] = system_prompt
    return json.dumps(result, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(
        prog=os.environ.get("OPENCLAW_DIAG_PROG") or None,
        description="Trace the processing timeline of a user message in an OpenClaw session.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("session_id", help="Session UUID to trace")
    parser.add_argument("--msg-index", type=int, default=None, help="Nth user message (0-based)")
    parser.add_argument("--msg-id", default=None, help="Message by id field")
    parser.add_argument("--msg-match", default=None, help="First user message containing TEXT")
    parser.add_argument("-o", "--output", default=None, help="Write output to file")
    parser.add_argument("--base-dir", default=DEFAULT_BASE_DIR, help="Agents base directory")
    parser.add_argument("--agent", default=None, help="Limit to specific agent")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help="Gateway log directory")
    parser.add_argument("--no-trajectory", action="store_true", help="Skip trajectory enrichment")
    parser.add_argument("--no-log", action="store_true", help="Skip gateway log enrichment")
    parser.add_argument("--json", action="store_true", help="Output as structured JSON")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    args = parser.parse_args()
    t0 = time.time()

    ok, msg = sessions.is_valid_query(args.session_id)
    if not ok:
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(2)
    session_file, candidates = resolve_session_file(
        args.session_id, args.base_dir, args.agent,
    )
    if candidates:
        print(
            f"Error: 前缀 '{args.session_id}' 匹配多个 session（请补长前缀）：",
            file=sys.stderr,
        )
        for sid in candidates:
            print(f"    {sid}", file=sys.stderr)
        sys.exit(1)
    if not session_file:
        print(f"Error: 找不到 session '{args.session_id}'（在 {args.base_dir} 下）",
              file=sys.stderr)
        suggestions = sessions.recent_session_ids(args.base_dir, limit=5)
        if suggestions:
            print(f"  最近的 5 个 session：", file=sys.stderr)
            for sid in suggestions:
                print(f"    {sid}", file=sys.stderr)
            print(f"  提示：UUID 完整 36 位，前缀也可（至少 8 位）。", file=sys.stderr)
        sys.exit(1)

    # If the user passed a prefix, recover the full UUID from the resolved
    # filename so log lookups and JSON output use the canonical id.
    resolved_basename = os.path.basename(session_file)
    full_session_id = resolved_basename.split(".jsonl", 1)[0]

    records = load_records(session_file)
    if not records:
        print(f"Error: session file is empty: {session_file}", file=sys.stderr)
        sys.exit(1)

    user_msgs = find_user_messages(records) or find_first_message(records)
    rec_idx, user_rec = select_user_message(records, args.msg_index, args.msg_id, args.msg_match)
    try:
        user_msg_ordinal = next(i for i, (ri, _) in enumerate(user_msgs) if ri == rec_idx)
    except StopIteration:
        user_msg_ordinal = 0
    user_msg_id = user_rec.get("id", "?")

    trace = extract_trace_records(records, rec_idx)
    if len(trace) < 2:
        print("Warning: trace contains only the user message (no response found)",
              file=sys.stderr)

    analysis = analyze_phases(trace)

    traj_info = None
    if not args.no_trajectory:
        traj_path = find_trajectory_file(session_file)
        if traj_path:
            traj_info = load_trajectory_info(traj_path, analysis["base_epoch_ms"])

    gw_info = None
    if not args.no_log:
        log_files = find_gateway_logs(args.log_dir)
        if log_files:
            gw_info = load_gateway_timing(log_files, full_session_id, analysis["base_epoch_ms"])

    # Prefer the runtime store's systemPromptReport (precise, rich); fall back
    # to whatever the trajectory recorded. Either source can fail silently.
    store_report = sessions.lookup_system_prompt_report(session_file, full_session_id)
    system_prompt = build_system_prompt_info(store_report, traj_info)

    if args.json:
        out_str = format_json(full_session_id, session_file, user_msg_ordinal,
                              user_msg_id, analysis, traj_info, gw_info,
                              system_prompt=system_prompt)
    else:
        out_str = format_text(full_session_id, user_msg_ordinal, user_msg_id,
                              analysis, traj_info, gw_info,
                              system_prompt=system_prompt)

    if not args.json:
        # When writing to a file, force no-color so the file does not contain
        # ANSI escape sequences. Otherwise honor the --no-color flag and let
        # render_*() decide based on stdout.isatty().
        no_color = args.no_color or bool(args.output)
        title = f"Message Trace · session {full_session_id[:8]}"
        banner = output.render_banner("trace", title, no_color=no_color)
        elapsed_ms = int((time.time() - t0) * 1000)
        footer = output.render_footer(elapsed_ms, no_color=no_color)
        out_str = f"{banner}\n\n{out_str}\n{footer}"

    if args.output:
        with open(args.output, "w") as f:
            f.write(out_str + "\n")
        print(f"Trace written to {args.output}", file=sys.stderr)
    else:
        try:
            print(out_str)
        except BrokenPipeError:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
