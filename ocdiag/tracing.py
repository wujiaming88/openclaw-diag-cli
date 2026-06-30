"""Pure helper functions for session trace analysis.

Extracted from the legacy ``tools/oc_session_trace.py`` so the v2 inspector
(``ocdiag.inspectors.trace``) can reuse them without pulling in the legacy
tool's CLI bits. The legacy tool re-imports from here too.

Channel-agnostic. Operates on three universal sources:
  1. ``<uuid>.jsonl`` — message timeline (required)
  2. ``<uuid>.trajectory.jsonl`` — run-level enrichment (optional)
  3. ``openclaw-*.log`` — embedded run start/prompt-end timing (optional)
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


_BUILTIN_TOOLS_WITH_CONTEXT = {"read", "write", "edit", "exec", "bash"}
_INLINE_LIMIT = 160
_PATH_LIMIT = 180


def iso_to_epoch_ms(iso: str) -> int:
    s = iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0
    return int(dt.timestamp() * 1000)


def epoch_ms_to_iso(ms: int) -> str:
    """Format epoch-ms as local time."""
    dt = datetime.fromtimestamp(ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def epoch_ms_to_utc8(ms: int) -> str:
    """Format epoch-ms in UTC+8 for stable human trace output."""
    if not ms:
        return "?"
    tz = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(ms / 1000, tz=tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC+8")


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


def _shorten(value: Any, limit: int = _INLINE_LIMIT) -> str:
    text = str(value) if value is not None else ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _char_count(label: str, value: Any) -> str:
    return f"{label}={len(str(value))} chars"


def _tool_call_args(call: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(call, dict):
        return {}
    for key in ("arguments", "input", "args"):
        value = call.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return {"raw": value}
            if isinstance(parsed, dict):
                return parsed
            return {"raw": value}
    partial = call.get("partialArgs")
    if isinstance(partial, dict):
        return partial
    if isinstance(partial, str):
        try:
            parsed = json.loads(partial)
        except ValueError:
            return {"raw": partial}
        if isinstance(parsed, dict):
            return parsed
        return {"raw": partial}
    return {}


def _path_arg(args: Dict[str, Any]) -> str:
    for key in ("path", "file", "filePath", "filepath"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _skill_name_from_path(path: str) -> Optional[str]:
    if not path:
        return None
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if len(parts) < 2:
        return None
    if parts[-1].lower() != "skill.md":
        return None
    return parts[-2] or None


def _format_builtin_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Return compact timeline data and optional deferred detail.

    Non-file write/exec/edit payloads are never printed into the human trace.
    They are represented by stable refs such as ``write 1`` / ``exec 1``.
    """
    lower = name.lower()
    formatted: Dict[str, Any] = {
        "timeline_label": name,
        "timeline_detail_lines": [],
        "breakdown_label": name,
        "breakdown_detail_lines": [],
        "needs_breakdown_detail": False,
        "render_breakdown_detail": True,
        "timeline_ref_suffix": "",
        "detail_kind": lower,
        "builtin_context": False,
    }
    if lower not in _BUILTIN_TOOLS_WITH_CONTEXT:
        return formatted

    if lower == "read":
        path = _path_arg(args)
        skill_name = _skill_name_from_path(path)
        if skill_name:
            label = f"read {_shorten(path, _PATH_LIMIT)}"
            formatted.update({
                "timeline_label": label,
                "breakdown_label": skill_name,
                "builtin_context": True,
                "is_skill_load": True,
                "skill_name": skill_name,
                "skill_path": path,
            })
            return formatted
        if path:
            label = f"read {_shorten(path, _PATH_LIMIT)}"
            formatted.update({
                "timeline_label": label,
                "breakdown_label": label,
                "builtin_context": True,
            })
            return formatted
        formatted.update({"timeline_label": "read", "builtin_context": True})
        return formatted

    if lower == "write":
        path = _path_arg(args)
        content = args.get("content")
        content_summary = ""
        if content is not None:
            formatted["needs_breakdown_detail"] = True
            content_summary = _char_count("content", content)
        label = "write"
        path_label = ""
        if path:
            path_label = _shorten(path, _PATH_LIMIT)
            label += f" {path_label}"
        breakdown_label = path_label or "write"
        if content_summary:
            breakdown_label = f"{breakdown_label} {content_summary}"
        formatted.update({
            "timeline_label": label,
            "breakdown_label": breakdown_label,
            "timeline_ref_suffix": path_label,
            "detail_kind": "write",
            "builtin_context": True,
        })
        return formatted

    if lower == "edit":
        path = _path_arg(args)
        old = args.get("oldString") or args.get("old")
        new = args.get("newString") or args.get("new")
        details = []
        if old is not None:
            details.append(_char_count("old", old))
        if new is not None:
            details.append(_char_count("new", new))
        label = "edit"
        path_label = ""
        if path:
            path_label = _shorten(path, _PATH_LIMIT)
            label += f" {path_label}"
        if details:
            formatted["needs_breakdown_detail"] = True
        breakdown_label = path_label or "edit"
        if details:
            breakdown_label = f"{breakdown_label} {', '.join(details)}"
        formatted.update({
            "timeline_label": label,
            "breakdown_label": breakdown_label,
            "timeline_ref_suffix": path_label,
            "detail_kind": "edit",
            "builtin_context": True,
        })
        return formatted

    if lower in ("exec", "bash"):
        command = (
            args.get("command")
            or args.get("cmd")
            or args.get("script")
            or args.get("raw")
        )
        path = _path_arg(args)
        label = "exec"
        if command is not None:
            formatted["needs_breakdown_detail"] = True
            formatted["render_breakdown_detail"] = False
        elif path:
            path_label = _shorten(path, _PATH_LIMIT)
            label += f" {path_label}"
        formatted.update({
            "timeline_label": label,
            "breakdown_label": "exec",
            "detail_kind": "exec",
            "builtin_context": True,
        })
        return formatted

    return formatted


def _apply_detail_ref(
    item: Dict[str, Any], detail_counters: Dict[str, int],
) -> None:
    if not item.get("needs_breakdown_detail"):
        return
    kind = item.get("detail_kind") or item["name"]
    detail_counters[kind] = detail_counters.get(kind, 0) + 1
    ref = f"{kind} {detail_counters[kind]}"
    item["detail_ref"] = ref
    item["timeline_label"] = ref
    breakdown_label = item.get("breakdown_label") or kind
    suffix = item.get("timeline_ref_suffix") or ""
    if suffix:
        item["timeline_label"] = f"{ref} {suffix}"
    if kind == "exec":
        item["timeline_label"] = ref
    item["breakdown_title"] = f"{ref}: {breakdown_label}"


def _format_tool_result(
    result: Dict[str, Any],
    call: Optional[Dict[str, Any]],
    batch_start_epoch: int,
    detail_counters: Dict[str, int],
) -> Dict[str, Any]:
    msg = result.get("message", {})
    name = msg.get("toolName") or (call or {}).get("name") or "?"
    call_id = msg.get("toolCallId") or (call or {}).get("id")
    ts = msg.get("timestamp", 0)
    duration_ms = max(0, ts - batch_start_epoch) if ts and batch_start_epoch else 0
    is_error = bool(msg.get("isError", False))
    status = "fail" if is_error else "success"

    args = _tool_call_args(call)
    formatted = _format_builtin_tool(name, args)
    if not formatted.get("builtin_context"):
        formatted.update({
            "timeline_label": name,
            "timeline_detail_lines": [],
            "breakdown_detail_lines": [],
            "needs_breakdown_detail": False,
        })
    item = {
        "name": name,
        "duration_ms": duration_ms,
        "is_error": is_error,
        "tool_call_id": call_id,
        "started_epoch_ms": batch_start_epoch,
        "completed_epoch_ms": ts,
        "status": status,
        **formatted,
    }
    _apply_detail_ref(item, detail_counters)
    summary = (
        f"{item['timeline_label']} -> {status} "
        f"({fmt_duration(duration_ms)})"
    )
    item["summary"] = summary
    if item.get("needs_breakdown_detail"):
        item["breakdown_summary"] = (
            f"{item.get('breakdown_title', item['timeline_label'])} -> "
            f"{status} ({fmt_duration(duration_ms)})"
        )
    return item


def find_trajectory_file(session_file: str) -> Optional[str]:
    d = os.path.dirname(session_file)
    base = os.path.basename(session_file).split(".jsonl")[0]
    traj = os.path.join(d, f"{base}.trajectory.jsonl")
    return traj if os.path.isfile(traj) else None


def find_gateway_logs(log_dir: str) -> List[str]:
    return sorted(glob.glob(os.path.join(log_dir, "openclaw-*.log")))


def load_records(filepath: str) -> List[Dict]:
    records: List[Dict] = []
    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
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
    """Pick the user message to trace.

    Raises SystemExit on caller error (legacy CLI behaviour). Callers that
    want exception-free behaviour should pre-validate, since the v2 inspector
    only invokes this with already-resolved indices.
    """
    user_msgs = find_user_messages(records)
    if not user_msgs:
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
            print(
                f"Error: msg-index {msg_index} out of range "
                f"(0..{len(user_msgs)-1})",
                file=sys.stderr,
            )
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


def _flush_tool_batch(
    events,
    tool_execs,
    results,
    base_ms,
    prev_epoch,
    tool_calls=None,
    detail_counters=None,
):
    if not results:
        return
    tool_calls = tool_calls or {}
    if detail_counters is None:
        detail_counters = {}
    batch_start_epoch = prev_epoch or base_ms
    batch_end_epoch = max(r.get("message", {}).get("timestamp", 0) for r in results)
    batch_dur = max(0, batch_end_epoch - batch_start_epoch)
    by_name: Dict[str, int] = {}
    errors = 0
    formatted_execs: List[Dict[str, Any]] = []
    for r in results:
        msg = r.get("message", {})
        name = msg.get("toolName", "?")
        by_name[name] = by_name.get(name, 0) + 1
        if msg.get("isError"):
            errors += 1
        call_id = msg.get("toolCallId")
        formatted_execs.append(
            _format_tool_result(
                r,
                tool_calls.get(call_id),
                batch_start_epoch,
                detail_counters,
            ),
        )
    parts = [
        (f"{n}" + (f" ×{cnt}" if cnt > 1 else ""))
        for n, cnt in by_name.items()
    ]
    tools_str = " + ".join(parts)
    status = "success" if errors == 0 else f"{errors} fail(s)"
    detail_lines: List[str] = []
    if len(formatted_execs) == 1:
        tools_str = formatted_execs[0]["summary"]
        detail_lines.extend(formatted_execs[0].get("timeline_detail_lines") or [])
    else:
        for item in formatted_execs:
            detail_lines.append(f"- {item['summary']}")
            detail_lines.extend(
                f"  {ln}" for ln in item.get("timeline_detail_lines") or []
            )
    events.append({
        "offset_ms": max(0, (batch_start_epoch - base_ms)),
        "type": "tool_batch",
        "detail": (
            tools_str if len(formatted_execs) == 1
            else f"{tools_str} -> {status} ({fmt_duration(batch_dur)})"
        ),
        "detail_lines": detail_lines,
        "count": len(results),
        "duration_ms": batch_dur,
    })
    for item in formatted_execs:
        tool_execs.append({
            "name": item["name"],
            "duration_ms": item["duration_ms"],
            "is_error": item["is_error"],
            "tool_call_id": item["tool_call_id"],
            "started_epoch_ms": item.get("started_epoch_ms"),
            "completed_epoch_ms": item.get("completed_epoch_ms"),
            "summary": item["summary"],
            "detail_lines": item.get("breakdown_detail_lines") or [],
            "timeline_detail_lines": item.get("timeline_detail_lines") or [],
            "needs_breakdown_detail": bool(item.get("needs_breakdown_detail")),
            "render_breakdown_detail": item.get("render_breakdown_detail", True),
            "detail_ref": item.get("detail_ref"),
            "breakdown_title": item.get("breakdown_title"),
            "breakdown_summary": item.get("breakdown_summary"),
            "builtin_context": item["builtin_context"],
            "is_skill_load": bool(item.get("is_skill_load")),
            "skill_name": item.get("skill_name"),
            "skill_path": item.get("skill_path"),
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
    tool_calls_by_id: Dict[str, Dict[str, Any]] = {}
    tool_detail_counters: Dict[str, int] = {}
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
                                      base_ms, prev_assistant_record_epoch,
                                      tool_calls_by_id,
                                      tool_detail_counters)
                    batch_dur = _tool_batch_duration(
                        pending_tool_results, prev_assistant_record_epoch,
                    )
                    total_tool_ms += batch_dur
                    tool_num += len(pending_tool_results)
                    pending_tool_results = []
                model_num += 1
                msg_ts = msg.get("timestamp", 0)
                record_epoch = iso_to_epoch_ms(r.get("timestamp", ""))
                duration_ms = (
                    record_epoch - msg_ts if (record_epoch and msg_ts) else 0
                )
                usage = msg.get("usage", {})
                out_tok = usage.get("output", 0)
                in_tok = usage.get("input", 0)
                cache_r = usage.get("cacheRead", 0)
                cache_w = usage.get("cacheWrite", 0)
                stop = msg.get("stopReason", "")
                provider = msg.get("provider", "")
                model = msg.get("model", "")
                response_id = (
                    msg.get("responseId")
                    or msg.get("response_id")
                    or msg.get("providerResponseId")
                )
                rate = (
                    out_tok / (duration_ms / 1000) if duration_ms > 0 else 0
                )
                start_offset = msg_ts - base_ms if msg_ts else 0
                end_offset = record_epoch - base_ms if record_epoch else 0
                events.append({
                    "offset_ms": start_offset, "type": "model_start",
                    "num": model_num,
                    "detail": (
                        f"Call started → {provider}/{model}"
                        if provider else "Call started"
                    ),
                })
                events.append({
                    "offset_ms": end_offset, "type": "model_end",
                    "num": model_num,
                    "detail": (
                        f"Completed (stopReason={stop})"
                        + (" ← FINAL" if stop == "stop" else "")
                    ),
                    "duration_ms": duration_ms, "tokens_in": in_tok,
                    "tokens_out": out_tok, "cache_read": cache_r,
                    "cache_write": cache_w, "rate": round(rate, 1),
                    "response_id": response_id,
                })
                tool_names = []
                content = msg.get("content", [])
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "toolCall":
                            tool_names.append(c.get("name", "?"))
                            call_id = c.get("id")
                            if call_id:
                                tool_calls_by_id[call_id] = c
                model_calls.append({
                    "num": model_num, "duration_ms": duration_ms,
                    "tokens_out": out_tok, "tokens_in": in_tok,
                    "cache_read": cache_r, "cache_write": cache_w,
                    "stop_reason": stop, "tool_names": tool_names,
                    "provider": provider, "model": model,
                    "rate": round(rate, 1), "response_id": response_id,
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
            # If model requested toolUse but tool never executed, emit warning
            if model_calls and not pending_tool_results:
                last_mc = model_calls[-1]
                if last_mc["stop_reason"] == "toolUse" and last_mc["tool_names"]:
                    names = ", ".join(last_mc["tool_names"])
                    events.append({
                        "offset_ms": events[-1]["offset_ms"] if events else 0,
                        "type": "tool_not_dispatched",
                        "detail": f"{names} (requested, never dispatched)",
                    })
            data = r.get("data", {})
            err_ts = data.get("timestamp", 0)
            offset = err_ts - base_ms if err_ts else 0
            events.append({
                "offset_ms": offset, "type": "error",
                "detail": f"prompt-error: {data.get('error', '?')}",
                "provider": data.get("provider", ""),
                "model": data.get("model", ""),
            })

    if pending_tool_results:
        _flush_tool_batch(events, tool_execs, pending_tool_results,
                          base_ms, prev_assistant_record_epoch,
                          tool_calls_by_id,
                          tool_detail_counters)
        batch_dur = _tool_batch_duration(
            pending_tool_results, prev_assistant_record_epoch,
        )
        total_tool_ms += batch_dur
        tool_num += len(pending_tool_results)

    last_offset = events[-1]["offset_ms"] if events else 0
    return {
        "events": events, "model_calls": model_calls, "tool_execs": tool_execs,
        "summary": {
            "total_ms": last_offset, "model_count": model_num,
            "model_total_ms": total_model_ms, "tool_count": tool_num,
            "tool_total_ms": total_tool_ms,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read": total_cache_read,
            "total_cache_write": total_cache_write,
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
            plugins = data.get("plugins", {})
            if isinstance(plugins, dict):
                ents = plugins.get("entries")
                if isinstance(ents, list):
                    info["plugin_snapshot"] = [
                        {
                            "id": p.get("id"),
                            "activated": p.get("activated"),
                            "status": p.get("status"),
                            "error": p.get("error"),
                            "activationReason": p.get("activationReason"),
                        }
                        for p in ents if isinstance(p, dict)
                    ]
        elif etype == "model.completed":
            data = e.get("data", {})
            info["compactionCount"] = data.get("compactionCount") or 0
            pc = data.get("promptCache") or {}
            obs = pc.get("observation") if isinstance(pc, dict) else None
            if isinstance(obs, dict):
                info["cache"] = {
                    "broke": obs.get("broke"),
                    "cacheRead": obs.get("cacheRead"),
                }
        elif etype == "trace.artifacts":
            data = e.get("data", {})
            info["finalStatus"] = data.get("finalStatus")
            info["aborted"] = bool(data.get("aborted"))
            info["externalAbort"] = bool(data.get("externalAbort"))
            info["timedOut"] = bool(data.get("timedOut"))
            info["idleTimedOut"] = bool(data.get("idleTimedOut"))
            info["timedOutDuringCompaction"] = bool(
                data.get("timedOutDuringCompaction"),
            )
            info["timedOutDuringToolExecution"] = bool(
                data.get("timedOutDuringToolExecution"),
            )
            pes = data.get("promptErrorSource")
            info["promptErrorSource"] = pes if pes else None
            il = data.get("itemLifecycle") or {}
            if isinstance(il, dict):
                info["lifecycle"] = {
                    "started": il.get("startedCount") or 0,
                    "completed": il.get("completedCount") or 0,
                    "active": il.get("activeCount") or 0,
                }
            tm = data.get("toolMetas") or []
            if isinstance(tm, list):
                info["toolMetas"] = [
                    {"toolName": m.get("toolName"), "meta": m.get("meta")}
                    for m in tm if isinstance(m, dict)
                ]
            info["didSendViaMessagingTool"] = bool(
                data.get("didSendViaMessagingTool"),
            )
            info["messagingTargets"] = data.get("messagingToolSentTargets") or []
            mts = data.get("messagingToolSentTexts") or []
            info["messagingTextCount"] = len(mts) if isinstance(mts, list) else 0
            info["messagingTexts"] = mts if isinstance(mts, list) else []
            info["successfulCronAdds"] = data.get("successfulCronAdds") or 0
            usage = data.get("usage") or {}
            if isinstance(usage, dict):
                info["usage"] = {
                    "input": usage.get("input") or 0,
                    "output": usage.get("output") or 0,
                    "cacheRead": usage.get("cacheRead") or 0,
                    "cacheWrite": usage.get("cacheWrite") or 0,
                    "total": usage.get("total") or 0,
                }
                cache_obj = info.get("cache") or {}
                if not isinstance(cache_obj.get("cacheRead"), int):
                    cache_obj["cacheRead"] = usage.get("cacheRead") or 0
                if "broke" not in cache_obj:
                    cache_obj["broke"] = None
                info["cache"] = cache_obj
            ats = data.get("assistantTexts") or []
            if isinstance(ats, list):
                info["assistantTexts"] = [str(t) for t in ats if t]
        elif etype == "session.ended":
            data = e.get("data", {})
            info["status"] = data.get("status")
            if "aborted" not in info:
                info["aborted"] = data.get("aborted")
            if "timedOut" not in info:
                info["timedOut"] = data.get("timedOut")
    if "session.started" in ts_map and "context.compiled" in ts_map:
        info["context_compilation_ms"] = (
            ts_map["context.compiled"] - ts_map["session.started"]
        )
    if "context.compiled" in ts_map and "prompt.submitted" in ts_map:
        info["prompt_submission_ms"] = (
            ts_map["prompt.submitted"] - ts_map["context.compiled"]
        )

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


def build_system_prompt_info(
    store_report: Optional[Dict[str, Any]],
    traj_info: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Merge sessions.json (preferred) and trajectory (fallback) into one view."""
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
        tools_entries = (
            tools.get("entries") if isinstance(tools.get("entries"), list) else []
        )
        skills_entries = (
            skills.get("entries") if isinstance(skills.get("entries"), list) else []
        )
        return {
            "source": store_report.get("source") or "run",
            "chars": chars,
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
            "tools": {"count": tsp.get("tools_in_request")},
            "messages_in_request": tsp.get("messages_in_request"),
            "truncatedInTrajectory": bool(tsp.get("truncated_in_trajectory")),
        }
    return None


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
                    if (
                        "embedded run start:" in msg
                        and f"sessionId={session_id}" in msg
                    ):
                        ts = _parse_log_ts(ts_str)
                        if ts and abs(ts - base_epoch_ms) < 120_000:
                            run_start = ts
                    elif (
                        "embedded run prompt start:" in msg
                        and f"sessionId={session_id}" in msg
                    ):
                        ts = _parse_log_ts(ts_str)
                        if ts and abs(ts - base_epoch_ms) < 120_000:
                            prompt_start = ts
                    elif (
                        "embedded run prompt end:" in msg
                        and f"sessionId={session_id}" in msg
                    ):
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
