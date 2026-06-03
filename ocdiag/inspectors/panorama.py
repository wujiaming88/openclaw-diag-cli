"""panorama inspector — 360° view of one session.

``trace`` zooms into a single user message; ``extract`` dumps conversation
records. ``panorama`` answers "what happened on this session, end-to-end,
across every standard data source we have?". It is designed for the case
where someone hands you a session UUID and asks "is this healthy?".

Inputs (all optional, all degrade silently when missing):

  ① session.jsonl       — per-message records and tool-call ids
  ② trajectory.jsonl    — per-run lifecycle / config / artifacts
  ③ sessions.json       — sessionKey, systemPromptReport, cache stats
  ④ openclaw-*.log      — gateway-level structured logs
  ⑤ runs.sqlite         — child task records (subagents)
  ⑥ cron/runs/*.jsonl   — cron-triggered run delivery records

Inclusion of any record is determined entirely by the correlation graph
expanded from ``sessionId`` (see ``ocdiag.correlation``). No subjective
filtering, no allow/deny lists. The only knobs are:

  --strict-correlation  match only on sessionId / runIds (ignores
                        sessionKey / toolCallId hits)
  --run-index N         pick the Nth run in a multi-run session
                        (default -1 = latest; --all-runs = every run)

Verdict semantics:
  FAIL  trace.artifacts shows abort/timeout, child task failed, or any
        correlated log entry is ERROR-level
  WARN  any correlated log entry is WARN-level, model fell back, stall
        detected, or end-to-end wall clock > 5min
  OK    everything else
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .. import paths as paths_mod, sessions
from ..core.context import DiagContext
from ..core.errors import DiagError
from ..core.registry import register
from ..core.types import Report, Section, Verdict
from ..correlation import (
    CorrelationGraph,
    build_graph,
    filter_log_files,
)
from ..jsonlog import get_log_subsystem, parse_log_msg
from ..recent_logs import discover_recent_logs
from ..sensitive import sanitize_text
from ..timeutil import fmt_duration, fmt_epoch_local
from ..tracing import (
    extract_trace_records,
    find_user_messages,
    iso_to_epoch_ms,
    load_records,
)


SLOW_E2E_MS = 5 * 60 * 1000
DEFAULT_LOG_RECORD_CAP = 5000
DEFAULT_TIMELINE_CAP = 2000


# ── helpers ────────────────────────────────────────────────────────────────


def _trajectory_path_for(session_file: str) -> Optional[str]:
    d = os.path.dirname(session_file)
    base = os.path.basename(session_file).split(".jsonl", 1)[0]
    p = os.path.join(d, f"{base}.trajectory.jsonl")
    return p if os.path.isfile(p) else None


def _sessions_json_for(session_file: str) -> Optional[str]:
    p = os.path.join(os.path.dirname(session_file), "sessions.json")
    return p if os.path.isfile(p) else None


def _runs_sqlite_path() -> str:
    return os.path.join(paths_mod.OPENCLAW_HOME, "tasks", "runs.sqlite")


def _cron_run_path(job_id: str) -> Optional[str]:
    p = os.path.join(paths_mod.CRON_RUNS_DIR, f"{job_id}.jsonl")
    return p if os.path.isfile(p) else None


def _safe_iso_to_ms(iso: Any) -> int:
    if not isinstance(iso, str) or not iso:
        return 0
    return iso_to_epoch_ms(iso) or 0


def _log_level(rec: Dict[str, Any]) -> str:
    """Best-effort extract of log level from an OpenClaw structured record."""
    lv = rec.get("level")
    if isinstance(lv, str):
        return lv.upper()
    if isinstance(lv, int):
        return {50: "ERROR", 40: "WARN", 30: "INFO", 20: "DEBUG"}.get(lv, str(lv))
    return ""


def _log_ts_ms(rec: Dict[str, Any]) -> int:
    """OpenClaw logs use either ``time`` (epoch-ms int) or ``time`` (ISO string) or ``ts`` (ISO)."""
    t = rec.get("time")
    if isinstance(t, (int, float)) and t > 0:
        return int(t)
    if isinstance(t, str) and t:
        return _safe_iso_to_ms(t)
    return _safe_iso_to_ms(rec.get("ts") or rec.get("timestamp"))


def _maybe_sanitize(value: Any, *, mask: bool) -> Any:
    if not mask:
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [_maybe_sanitize(v, mask=mask) for v in value]
    if isinstance(value, dict):
        return {k: _maybe_sanitize(v, mask=mask) for k, v in value.items()}
    return value


# ── trajectory grouping for multi-run sessions ─────────────────────────────


def _group_trajectory_runs(traj_path: str) -> List[Dict[str, Any]]:
    """Group every event in the trajectory file by ``runId``.

    Returns a list of run dicts ordered by their first observed timestamp,
    each containing:
      runId, started_ms, ended_ms, events: List[Dict], artifacts: Dict|None,
      session_started: Dict|None, trace_metadata: Dict|None
    """
    runs: Dict[str, Dict[str, Any]] = {}
    if not traj_path or not os.path.isfile(traj_path):
        return []
    try:
        with open(traj_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                rid = ev.get("runId")
                if not isinstance(rid, str) or not rid:
                    continue
                run = runs.setdefault(rid, {
                    "runId": rid,
                    "started_ms": 0,
                    "ended_ms": 0,
                    "events": [],
                    "session_started": None,
                    "trace_metadata": None,
                    "artifacts": None,
                })
                run["events"].append(ev)
                ts = _safe_iso_to_ms(ev.get("ts"))
                etype = ev.get("type")
                if etype == "session.started":
                    run["session_started"] = ev
                    if ts:
                        run["started_ms"] = ts
                elif etype == "trace.metadata":
                    run["trace_metadata"] = ev
                elif etype == "trace.artifacts":
                    run["artifacts"] = ev
                elif etype == "session.ended":
                    if ts:
                        run["ended_ms"] = ts
                if ts and (not run["started_ms"] or ts < run["started_ms"]):
                    if etype == "session.started" or run["started_ms"] == 0:
                        run["started_ms"] = ts
                if ts and ts > run["ended_ms"]:
                    run["ended_ms"] = ts
    except OSError:
        return []
    runs_list = list(runs.values())
    runs_list.sort(key=lambda r: r["started_ms"] or r["ended_ms"])
    return runs_list


def _select_runs(
    runs: List[Dict[str, Any]],
    *,
    run_index: Optional[int],
    all_runs: bool,
) -> List[Dict[str, Any]]:
    if not runs:
        return []
    if all_runs:
        return runs
    if run_index is None:
        run_index = -1
    if run_index < 0:
        run_index = len(runs) + run_index
    if run_index < 0 or run_index >= len(runs):
        return []
    return [runs[run_index]]


# ── tool-call waterfall from session.jsonl ─────────────────────────────────


def _extract_result_text(content: Any) -> str:
    """Flatten a toolResult ``content`` (string or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                t = c.get("text") or c.get("content")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _build_tool_waterfall(
    records: List[Dict[str, Any]],
    *,
    mask: bool,
) -> List[Dict[str, Any]]:
    """Pair toolCalls (assistant content blocks) with toolResults by id.

    Each pair carries ``name, callId, start_ms, end_ms, duration_ms,
    is_error, args, result_text, error_text``. ``args``/``result_text`` are
    sanitized when ``mask`` is set. Unmatched calls (still pending or never
    dispatched) keep ``end_ms=None``.
    """
    calls: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for rec in records:
        if rec.get("type") != "message":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        ts = msg.get("timestamp")
        if not isinstance(ts, int):
            ts = _safe_iso_to_ms(rec.get("timestamp"))
        if role == "assistant":
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "toolCall":
                    continue
                cid = c.get("id")
                if not isinstance(cid, str) or not cid:
                    continue
                if cid in calls:
                    continue
                args = c.get("input") if "input" in c else c.get("arguments")
                calls[cid] = {
                    "callId": cid,
                    "name": c.get("name") or "?",
                    "start_ms": ts,
                    "end_ms": None,
                    "duration_ms": None,
                    "is_error": False,
                    "args": _maybe_sanitize(args, mask=mask),
                    "result_text": "",
                    "error_text": "",
                }
                order.append(cid)
        elif role == "toolResult":
            cid = msg.get("toolCallId") or msg.get("toolUseId") or msg.get("id")
            if not isinstance(cid, str) or not cid:
                continue
            result_text = _extract_result_text(msg.get("content"))
            details = msg.get("details")
            error_text = ""
            if msg.get("isError"):
                if isinstance(details, dict) and details.get("error"):
                    error_text = str(details.get("error"))
                else:
                    error_text = result_text
            if mask and result_text:
                result_text = sanitize_text(result_text)
            if mask and error_text:
                error_text = sanitize_text(error_text)
            entry = calls.get(cid)
            if entry is None:
                calls[cid] = {
                    "callId": cid,
                    "name": msg.get("toolName") or "?",
                    "start_ms": ts,
                    "end_ms": ts,
                    "duration_ms": 0,
                    "is_error": bool(msg.get("isError")),
                    "args": None,
                    "result_text": result_text,
                    "error_text": error_text,
                }
                order.append(cid)
                continue
            entry["end_ms"] = ts
            if entry["start_ms"] and ts:
                entry["duration_ms"] = max(0, ts - entry["start_ms"])
            entry["is_error"] = bool(msg.get("isError"))
            entry["result_text"] = result_text
            entry["error_text"] = error_text
            if not entry.get("name") or entry["name"] == "?":
                tn = msg.get("toolName")
                if tn:
                    entry["name"] = tn
    return [calls[cid] for cid in order]


def _format_args_inline(args: Any, *, max_total: int = 80) -> str:
    """Render ``args`` as a compact inline string for the tool waterfall.

    Each value is truncated to 50 chars; the whole rendering is capped at
    ``max_total`` chars.
    """
    if args is None:
        return ""
    if not isinstance(args, dict):
        s = str(args)
        return s if len(s) <= max_total else s[:max_total - 1] + "…"
    parts: List[str] = []
    for k, v in args.items():
        if isinstance(v, (dict, list)):
            try:
                vs = json.dumps(v, ensure_ascii=False)
            except (TypeError, ValueError):
                vs = str(v)
        else:
            vs = str(v)
        if len(vs) > 50:
            vs = vs[:49] + "…"
        parts.append(f"{k}={vs}")
    out = "{" + ", ".join(parts) + "}"
    if len(out) > max_total:
        out = out[:max_total - 1] + "…"
    return out


def _format_result_inline(entry: Dict[str, Any], *, max_chars: int = 80) -> str:
    """Compact one-line summary of a tool result for the waterfall."""
    if entry.get("is_error"):
        err = entry.get("error_text") or entry.get("result_text") or "error"
        text = err.replace("\n", " ").strip()
    else:
        text = (entry.get("result_text") or "").replace("\n", " ").strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars - 1] + "…"
    return text


def _waterfall_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    durations = [r["duration_ms"] for r in rows if r.get("duration_ms")]
    durations.sort()
    n = len(durations)
    p50 = durations[n // 2] if n else 0
    p95 = durations[max(0, int(n * 0.95) - 1)] if n else 0
    avg = (sum(durations) / n) if n else 0
    slowest = max(rows, key=lambda r: r.get("duration_ms") or 0, default=None)
    return {
        "total": len(rows),
        "completed": n,
        "avg_ms": round(avg, 1),
        "p50_ms": p50,
        "p95_ms": p95,
        "max_ms": durations[-1] if n else 0,
        "slowest": (
            {"name": slowest["name"], "duration_ms": slowest["duration_ms"]}
            if slowest and slowest.get("duration_ms") else None
        ),
        "errors": sum(1 for r in rows if r.get("is_error")),
    }


# ── child-task summary ─────────────────────────────────────────────────────


def _summarize_child_task(row: Dict[str, Any]) -> Dict[str, Any]:
    started = row.get("started_at")
    ended = row.get("ended_at")
    duration_ms = None
    if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
        duration_ms = max(0, int(ended) - int(started))
    return {
        "task_id": row.get("task_id"),
        "runtime": row.get("runtime"),
        "agent_id": row.get("agent_id"),
        "status": row.get("status"),
        "delivery_status": row.get("delivery_status"),
        "scope_kind": row.get("scope_kind"),
        "child_session_key": row.get("child_session_key"),
        "run_id": row.get("run_id"),
        "label": row.get("label"),
        "created_at": row.get("created_at"),
        "started_at": started,
        "ended_at": ended,
        "duration_ms": duration_ms,
        "error": row.get("error"),
        "terminal_outcome": row.get("terminal_outcome"),
    }



# ── timeline merge ─────────────────────────────────────────────────────────


def _build_timeline(
    *,
    session_records: List[Dict[str, Any]],
    trajectory_runs: List[Dict[str, Any]],
    correlated_logs: List[Dict[str, Any]],
    cap: int = DEFAULT_TIMELINE_CAP,
) -> List[Dict[str, Any]]:
    """Merge all sources into one chronological list.

    Each entry has ``ts_ms`` (epoch ms), ``source``, ``event_type``,
    ``summary``, and an optional ``correlation`` block (carried over from
    correlated log entries).
    """
    out: List[Dict[str, Any]] = []

    for rec in session_records:
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type") or "?"
        ts_ms = 0
        msg = rec.get("message")
        if isinstance(msg, dict):
            mts = msg.get("timestamp")
            if isinstance(mts, int):
                ts_ms = mts
        if not ts_ms:
            ts_ms = _safe_iso_to_ms(rec.get("timestamp"))
        if not ts_ms:
            continue
        summary = rtype
        if rtype == "message" and isinstance(msg, dict):
            role = msg.get("role") or "?"
            summary = f"message:{role}"
            if role == "toolResult":
                tn = msg.get("toolName") or "?"
                err = " (error)" if msg.get("isError") else ""
                summary = f"toolResult:{tn}{err}"
            elif role == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    tool_calls = [
                        c.get("name", "?")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "toolCall"
                    ]
                    if tool_calls:
                        summary = f"assistant:toolCall[{','.join(tool_calls)}]"
        out.append({
            "ts_ms": ts_ms,
            "source": "session.jsonl",
            "event_type": rtype,
            "summary": summary,
        })

    for run in trajectory_runs:
        for ev in run.get("events", []):
            ts = _safe_iso_to_ms(ev.get("ts"))
            if not ts:
                continue
            out.append({
                "ts_ms": ts,
                "source": "trajectory.jsonl",
                "event_type": ev.get("type") or "?",
                "summary": f"{ev.get('type')} run={run['runId'][:8]}",
                "run_id": run["runId"],
            })

    for rec in correlated_logs:
        ts = _log_ts_ms(rec)
        if not ts:
            continue
        lvl = _log_level(rec) or "INFO"
        sub = get_log_subsystem(rec) or "?"
        text = parse_log_msg(rec)
        snippet = text[:120] if text else ""
        entry = {
            "ts_ms": ts,
            "source": "app_log",
            "event_type": f"log:{lvl}",
            "summary": f"[{sub}] {snippet}",
        }
        if "correlation" in rec:
            entry["correlation"] = rec["correlation"]
        out.append(entry)

    out.sort(key=lambda r: r["ts_ms"])
    if len(out) > cap:
        # Keep oldest 10% + newest 90% so context isn't lost on huge sessions.
        head = max(1, cap // 10)
        out = out[:head] + out[-(cap - head):]
    for entry in out:
        entry["ts_local"] = fmt_epoch_local(entry["ts_ms"])
    return out


# ── runtime / health extraction from trajectory ────────────────────────────


def _runtime_context(run: Dict[str, Any]) -> Dict[str, Any]:
    """Pull a flattened runtime snapshot out of one trajectory run."""
    ctx: Dict[str, Any] = {"runId": run["runId"]}
    started = run.get("session_started") or {}
    sd = started.get("data") or {}
    if sd:
        ctx["trigger"] = sd.get("trigger")
        ctx["agent_id"] = sd.get("agentId")
        ctx["channel"] = sd.get("messageChannel")
        ctx["provider"] = started.get("provider")
        ctx["model_id"] = started.get("modelId")
        ctx["tool_count"] = sd.get("toolCount")
        ctx["client_tool_count"] = sd.get("clientToolCount")
    meta = run.get("trace_metadata") or {}
    md = meta.get("data") or {}
    if md:
        harness = md.get("harness") or {}
        if isinstance(harness, dict):
            ctx["harness_version"] = harness.get("version")
            rt = harness.get("runtime") or {}
            if isinstance(rt, dict):
                ctx["node"] = rt.get("node")
        plugins = md.get("plugins") or {}
        if isinstance(plugins, dict):
            ents = plugins.get("entries")
            if isinstance(ents, list):
                ctx["plugin_count"] = len(ents)
                ctx["plugins_activated"] = [
                    e.get("id") for e in ents
                    if isinstance(e, dict) and e.get("activated")
                ]
                ctx["plugin_errors"] = [
                    {
                        "id": e.get("id"),
                        "error": e.get("error"),
                        "activated": e.get("activated"),
                    }
                    for e in ents
                    if isinstance(e, dict) and e.get("error")
                    and e.get("activated")
                ]
        skills = md.get("skills") or {}
        if isinstance(skills, dict):
            ents = skills.get("entries")
            if isinstance(ents, list):
                ctx["skill_count"] = len(ents)
                ctx["skill_names"] = [
                    e.get("name") or e.get("id")
                    for e in ents if isinstance(e, dict)
                ]
        prompting = md.get("prompting") or {}
        if isinstance(prompting, dict):
            spr = prompting.get("systemPromptReport") or {}
            if isinstance(spr, dict):
                sp = spr.get("systemPrompt") or {}
                if isinstance(sp, dict):
                    ctx["system_prompt_chars"] = sp.get("chars")
                    ctx["project_context_chars"] = sp.get("projectContextChars")
                    ctx["non_project_context_chars"] = sp.get(
                        "nonProjectContextChars")
                tools = spr.get("tools") or {}
                if isinstance(tools, dict):
                    ctx["tools_schema_chars"] = tools.get("schemaChars")
                bt = spr.get("bootstrapTruncation") or {}
                if isinstance(bt, dict):
                    ctx["bootstrap_truncation"] = {
                        "truncated_files": bt.get("truncatedFiles") or 0,
                        "near_limit_files": bt.get("nearLimitFiles") or 0,
                        "total_near_limit": bool(bt.get("totalNearLimit")),
                    }
                iwf = spr.get("injectedWorkspaceFiles")
                if isinstance(iwf, list):
                    ctx["injected_workspace_files"] = [
                        {
                            "name": e.get("name"),
                            "chars": e.get("injectedChars"),
                            "truncated": bool(e.get("truncated")),
                            "missing": bool(e.get("missing")),
                        }
                        for e in iwf if isinstance(e, dict)
                    ]
    # context.compiled — pull the latest such event in the run
    cc = None
    for ev in run.get("events", []):
        if isinstance(ev, dict) and ev.get("type") == "context.compiled":
            cc = ev
    if isinstance(cc, dict):
        cd = cc.get("data") or {}
        if isinstance(cd, dict):
            ctx["stream_strategy"] = cd.get("streamStrategy")
            ctx["transport"] = cd.get("transport")
            ctx["images_count"] = cd.get("imagesCount") or 0
            tools = cd.get("tools")
            if isinstance(tools, list):
                ctx["compiled_tool_count"] = len(tools)
                ctx["compiled_tool_names"] = [
                    t.get("name") for t in tools
                    if isinstance(t, dict) and t.get("name")
                ]
            msgs = cd.get("messages")
            if isinstance(msgs, list):
                ctx["compiled_messages_count"] = len(msgs)
    artifacts = run.get("artifacts") or {}
    ad = artifacts.get("data") or {}
    if ad:
        ctx["final_status"] = ad.get("finalStatus")
        ctx["aborted"] = bool(ad.get("aborted"))
        ctx["external_abort"] = bool(ad.get("externalAbort"))
        ctx["timed_out"] = bool(ad.get("timedOut"))
        ctx["idle_timed_out"] = bool(ad.get("idleTimedOut"))
        ctx["timed_out_during_compaction"] = bool(
            ad.get("timedOutDuringCompaction"))
        ctx["timed_out_during_tool_execution"] = bool(
            ad.get("timedOutDuringToolExecution"))
        ctx["prompt_error_source"] = ad.get("promptErrorSource") or None
        usage = ad.get("usage") or {}
        if isinstance(usage, dict):
            ctx["usage"] = {
                "input": usage.get("input") or 0,
                "output": usage.get("output") or 0,
                "cacheRead": usage.get("cacheRead") or 0,
                "cacheWrite": usage.get("cacheWrite") or 0,
                "total": usage.get("total") or 0,
            }
        pc = ad.get("promptCache") or {}
        if isinstance(pc, dict):
            obs = pc.get("observation") or {}
            if isinstance(obs, dict):
                ctx["cache_broke"] = obs.get("broke")
        ctx["compaction_count"] = ad.get("compactionCount") or 0
        il = ad.get("itemLifecycle") or {}
        if isinstance(il, dict):
            ctx["lifecycle"] = {
                "started": il.get("startedCount") or 0,
                "completed": il.get("completedCount") or 0,
                "active": il.get("activeCount") or 0,
            }
        ctx["did_send_via_messaging_tool"] = bool(
            ad.get("didSendViaMessagingTool"))
        ctx["messaging_targets"] = ad.get("messagingToolSentTargets") or []
        mts = ad.get("messagingToolSentTexts") or []
        ctx["messaging_text_count"] = len(mts) if isinstance(mts, list) else 0
        ctx["successful_cron_adds"] = ad.get("successfulCronAdds") or 0
        lte = ad.get("lastToolError")
        if lte:
            ctx["last_tool_error"] = lte
    return ctx


def _model_decisions(
    runs: List[Dict[str, Any]],
    correlated_logs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for run in runs:
        meta = run.get("trace_metadata") or {}
        md = meta.get("data") or {}
        model = md.get("model") if isinstance(md, dict) else None
        if isinstance(model, dict):
            out.append({
                "ts_ms": _safe_iso_to_ms(meta.get("ts")),
                "source": "trajectory",
                "kind": "model_select",
                "runId": run["runId"],
                "provider": model.get("provider"),
                "name": model.get("name"),
                "api": model.get("api"),
                "thinkLevel": model.get("thinkLevel"),
                "reasoningLevel": model.get("reasoningLevel"),
            })
    for rec in correlated_logs:
        text = parse_log_msg(rec)
        sub = get_log_subsystem(rec)
        if not text:
            continue
        # Look for known decision markers. Keep this list small and explicit
        # so we don't accidentally pull in unrelated log chatter.
        markers = (
            "model_fallback_decision",
            "harness_select",
            "context_overflow",
            "compaction_triggered",
        )
        if not any(m in text for m in markers):
            continue
        out.append({
            "ts_ms": _log_ts_ms(rec),
            "source": "app_log",
            "kind": "log_decision",
            "subsystem": sub,
            "summary": text[:200],
        })
    out.sort(key=lambda r: r.get("ts_ms") or 0)
    return out


LONG_TOOL_CALL_THRESHOLD_MS = 60_000


def _health_signals(
    runs: List[Dict[str, Any]],
    correlated_logs: List[Dict[str, Any]],
    *,
    waterfall: Optional[List[Dict[str, Any]]] = None,
    children: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []
    for run in runs:
        artifacts = run.get("artifacts") or {}
        ad = artifacts.get("data") or {}
        if not isinstance(ad, dict):
            continue
        flags = []
        for k in (
            "aborted", "externalAbort", "timedOut", "idleTimedOut",
            "timedOutDuringCompaction", "timedOutDuringToolExecution",
        ):
            if ad.get(k):
                flags.append(k)
        if flags:
            signals.append({
                "kind": "trajectory_artifact",
                "runId": run["runId"],
                "flags": flags,
                "final_status": ad.get("finalStatus"),
                "prompt_error_source": ad.get("promptErrorSource"),
                "ts_ms": run.get("ended_ms") or run.get("started_ms") or 0,
            })
        il = ad.get("itemLifecycle") or {}
        if isinstance(il, dict) and il.get("activeCount"):
            signals.append({
                "kind": "tool_call_leak",
                "runId": run["runId"],
                "active": il.get("activeCount"),
                "started": il.get("startedCount"),
                "completed": il.get("completedCount"),
                "ts_ms": run.get("ended_ms") or 0,
            })
        lte = ad.get("lastToolError")
        if lte:
            signals.append({
                "kind": "last_tool_error",
                "runId": run["runId"],
                "summary": str(lte)[:200],
                "ts_ms": run.get("ended_ms") or 0,
            })
    seen_pids: Set[Any] = set()
    for rec in correlated_logs:
        text = parse_log_msg(rec)
        if not text:
            continue
        if any(m in text for m in (
            "stalled session", "long running session", "long-running session",
            "stuck session",
        )):
            signals.append({
                "kind": "log_stall",
                "ts_ms": _log_ts_ms(rec),
                "subsystem": get_log_subsystem(rec),
                "summary": text[:200],
            })
        pid = rec.get("pid")
        if pid is not None:
            seen_pids.add(pid)
    if len(seen_pids) > 1:
        signals.append({
            "kind": "gateway_pid_change",
            "pids": sorted(p for p in seen_pids if isinstance(p, (int, str))),
        })
    # Long-running tool calls
    if waterfall:
        long_calls = sorted(
            [
                w for w in waterfall
                if (w.get("duration_ms") or 0) >= LONG_TOOL_CALL_THRESHOLD_MS
            ],
            key=lambda w: w.get("duration_ms") or 0, reverse=True,
        )
        for lc in long_calls[:5]:
            signals.append({
                "kind": "long_tool_call",
                "name": lc.get("name"),
                "duration_ms": lc.get("duration_ms"),
                "is_error": bool(lc.get("is_error")),
                "ts_ms": lc.get("end_ms") or lc.get("start_ms") or 0,
            })
    # Failed child tasks
    if children:
        failed = [
            c for c in children
            if c.get("status") in ("failed", "errored", "error")
            or c.get("terminal_outcome") in ("failed", "error")
        ]
        for c in failed[:5]:
            signals.append({
                "kind": "child_task_failed",
                "task_id": c.get("task_id"),
                "agent_id": c.get("agent_id"),
                "runtime": c.get("runtime"),
                "duration_ms": c.get("duration_ms"),
                "error": str(c.get("error") or "")[:200],
                "ts_ms": c.get("ended_at") or c.get("started_at") or 0,
            })
    return signals


# ── model-call extraction (with per-call duration) ────────────────────────


def _extract_model_calls(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Walk records in order; for each assistant message capture usage stats
    plus the wall-clock duration since the previous user/toolResult/assistant
    message. The first assistant call has no measurable upstream wait, so its
    duration is ``None``.
    """
    calls: List[Dict[str, Any]] = []
    last_ts: Optional[int] = None
    for rec in records:
        if not isinstance(rec, dict) or rec.get("type") != "message":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        ts = msg.get("timestamp")
        if not isinstance(ts, int):
            ts = _safe_iso_to_ms(rec.get("timestamp"))
        role = msg.get("role")
        if role != "assistant":
            if ts:
                last_ts = ts
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            if ts:
                last_ts = ts
            continue
        content = msg.get("content")
        triggered_tools: List[str] = []
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    triggered_tools.append(c.get("name", "?"))
        duration_ms: Optional[int] = None
        if last_ts and ts:
            duration_ms = max(0, ts - last_ts)
        calls.append({
            "ts_ms": ts,
            "duration_ms": duration_ms,
            "provider": msg.get("provider", "?"),
            "model": msg.get("model", "?"),
            "stopReason": msg.get("stopReason", "?"),
            "input": usage.get("input") or usage.get("inputTokens") or 0,
            "output": usage.get("output") or usage.get("outputTokens") or 0,
            "cacheRead": usage.get("cacheRead")
                or usage.get("cacheReadInputTokens") or 0,
            "cacheWrite": usage.get("cacheWrite")
                or usage.get("cacheCreationInputTokens") or 0,
            "totalTokens": usage.get("totalTokens") or usage.get("total") or 0,
            "cost": usage.get("cost"),
            "tools": triggered_tools,
        })
        if ts:
            last_ts = ts
    return calls


def _aggregate_model_calls(model_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_model: Dict[str, Dict[str, Any]] = {}
    total_cost = 0.0
    has_cost = False
    for c in model_calls:
        m = c.get("model") or "?"
        b = by_model.setdefault(m, {
            "model": m, "calls": 0, "input": 0, "output": 0,
            "cache_read": 0, "cache_write": 0, "duration_ms": 0,
            "stop_reasons": {},
        })
        b["calls"] += 1
        b["input"] += c.get("input") or 0
        b["output"] += c.get("output") or 0
        b["cache_read"] += c.get("cacheRead") or 0
        b["cache_write"] += c.get("cacheWrite") or 0
        if c.get("duration_ms"):
            b["duration_ms"] += c["duration_ms"]
        sr = c.get("stopReason") or "?"
        b["stop_reasons"][sr] = b["stop_reasons"].get(sr, 0) + 1
        cost = c.get("cost")
        if isinstance(cost, dict):
            has_cost = True
            for k in ("input", "output", "cacheRead", "cacheWrite"):
                v = cost.get(k)
                if isinstance(v, (int, float)):
                    total_cost += v
    out_models = []
    for m, b in by_model.items():
        if b["calls"]:
            b["avg_output"] = round(b["output"] / b["calls"], 1)
            if b["duration_ms"] and b["calls"]:
                b["avg_duration_ms"] = round(b["duration_ms"] / b["calls"], 1)
        out_models.append(b)
    out_models.sort(key=lambda x: x["calls"], reverse=True)
    return {
        "models": out_models,
        "total_cost_usd": round(total_cost, 4) if has_cost else None,
    }


# ── timeline key-moment summarization ──────────────────────────────────────


def _timeline_key_moments(timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick out diagnostic key moments from a merged timeline."""
    if not timeline:
        return {}
    out: Dict[str, Any] = {
        "first_ts_ms": timeline[0]["ts_ms"],
        "last_ts_ms": timeline[-1]["ts_ms"],
    }
    first_error = next(
        (e for e in timeline if e.get("event_type", "").startswith("log:ERROR")),
        None,
    )
    first_warn = next(
        (e for e in timeline if e.get("event_type", "").startswith("log:WARN")),
        None,
    )
    first_stall = next(
        (e for e in timeline
         if "stalled" in (e.get("summary") or "")
         or "long-running" in (e.get("summary") or "")
         or "long running" in (e.get("summary") or "")),
        None,
    )
    longest_gap = {"ms": 0, "after_ts_ms": 0, "before_ts_ms": 0}
    for a, b in zip(timeline, timeline[1:]):
        gap = b["ts_ms"] - a["ts_ms"]
        if gap > longest_gap["ms"]:
            longest_gap = {
                "ms": gap, "after_ts_ms": a["ts_ms"],
                "before_ts_ms": b["ts_ms"],
                "after_summary": a.get("summary"),
                "before_summary": b.get("summary"),
            }
    if first_error:
        out["first_error"] = first_error
    if first_warn:
        out["first_warn"] = first_warn
    if first_stall:
        out["first_stall"] = first_stall
    if longest_gap["ms"]:
        out["longest_gap"] = longest_gap
    return out


# ── representative log entries when only INFO is present ───────────────────


def _representative_logs(
    correlated_logs: List[Dict[str, Any]],
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Pick a small set of telling INFO entries: lifecycle starts/ends and
    anything mentioning a tool/model boundary. We pick from the head, the
    middle, and the tail to span the run.
    """
    if not correlated_logs:
        return []
    interesting: List[Dict[str, Any]] = []
    fallback: List[Dict[str, Any]] = []
    keywords = (
        "start", "end", "complete", "spawn", "deliver", "cron",
        "tool", "model", "compaction", "session",
    )
    for rec in correlated_logs:
        text = (parse_log_msg(rec) or "").lower()
        if any(k in text for k in keywords):
            interesting.append(rec)
        else:
            fallback.append(rec)
    pool = interesting or fallback
    if len(pool) <= limit:
        return pool
    # span: 1st, last, plus evenly spaced mids
    indices = [0, len(pool) - 1]
    inner = limit - 2
    if inner > 0:
        step = max(1, len(pool) // (inner + 1))
        for i in range(1, inner + 1):
            indices.append(min(len(pool) - 2, i * step))
    indices = sorted(set(indices))
    return [pool[i] for i in indices][:limit]


# ── main inspector ─────────────────────────────────────────────────────────


@register
class PanoramaInspector:
    id = "panorama"
    title = "Session Panorama"
    kind = "inspector"

    def collect(self, ctx: DiagContext, **kwargs) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)

        session_id: Optional[str] = kwargs.get("session_id")
        if not session_id:
            report.error = "missing session_id"
            report.diag_error = DiagError(
                code="MISSING_ARGUMENT",
                message="missing session_id",
                hint="usage: openclaw-diag panorama <session-uuid>",
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        ok, msg = sessions.is_valid_query(session_id)
        if not ok:
            report.error = msg
            report.diag_error = DiagError(
                code="INVALID_QUERY",
                message=msg,
                details={"query": session_id},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        files, candidates = sessions.resolve(
            session_id,
            base_dir=str(ctx.sessions_base),
            agent=kwargs.get("agent"),
            include_transient=False,
        )
        if candidates:
            report.error = (
                f"前缀 '{session_id}' 匹配多个 session: "
                + ", ".join(candidates)
            )
            report.diag_error = DiagError(
                code="AMBIGUOUS_SESSION",
                message=report.error,
                hint="provide a longer prefix or the full uuid",
                details={"query": session_id, "matches": candidates},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report
        if not files:
            recent = sessions.recent_session_ids(str(ctx.sessions_base), limit=5)
            hint_msg = (
                f"recent sessions: {', '.join(recent)}" if recent else None
            )
            report.error = f"找不到 session '{session_id}'" + (
                f"; recent: {', '.join(recent)}" if recent else ""
            )
            report.diag_error = DiagError(
                code="SESSION_NOT_FOUND",
                message=f"找不到 session '{session_id}'",
                hint=hint_msg,
                details={"query": session_id},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        session_file = files[0][0]
        full_session_id = os.path.basename(session_file).split(".jsonl", 1)[0]
        agent_id = os.path.basename(
            os.path.dirname(os.path.dirname(session_file))
        )

        # Discover sibling files / data sources
        traj_path = _trajectory_path_for(session_file)
        sessions_json_path = _sessions_json_for(session_file)
        log_files = discover_recent_logs(str(ctx.log_dir))
        runs_sqlite_path = _runs_sqlite_path()
        if not os.path.isfile(runs_sqlite_path):
            runs_sqlite_path = None

        sources_present: Dict[str, bool] = {
            "session.jsonl": True,
            "trajectory.jsonl": bool(traj_path),
            "sessions.json": bool(sessions_json_path),
            "app_log": bool(log_files),
            "runs.sqlite": bool(runs_sqlite_path),
        }

        # Build correlation graph from every available source.
        graph, child_rows = build_graph(
            full_session_id,
            session_file=session_file,
            sessions_json_path=sessions_json_path,
            trajectory_path=traj_path,
            app_log_files=log_files,
            runs_sqlite_path=runs_sqlite_path,
        )

        cron_run_path: Optional[str] = None
        if graph.cron_job_id:
            cron_run_path = _cron_run_path(graph.cron_job_id)
            if cron_run_path:
                sources_present["cron/runs"] = True

        # Resolve run selection.
        traj_runs = _group_trajectory_runs(traj_path) if traj_path else []
        run_index = kwargs.get("run_index")
        all_runs = bool(kwargs.get("all_runs"))
        selected_runs = _select_runs(
            traj_runs, run_index=run_index, all_runs=all_runs,
        )

        # Compute session time window. Use trajectory bounds when available,
        # else session.jsonl record timestamps.
        session_records = load_records(session_file)
        record_ts = [
            _safe_iso_to_ms(r.get("timestamp"))
            for r in session_records if isinstance(r, dict)
        ]
        record_ts = [t for t in record_ts if t]
        if traj_runs:
            window_start = min(
                (r["started_ms"] for r in traj_runs if r["started_ms"]),
                default=(min(record_ts) if record_ts else 0),
            )
            window_end = max(
                (r["ended_ms"] for r in traj_runs if r["ended_ms"]),
                default=(max(record_ts) if record_ts else 0),
            )
        else:
            window_start = min(record_ts) if record_ts else 0
            window_end = max(record_ts) if record_ts else 0
        duration_ms = max(0, window_end - window_start) if window_end else 0

        # Filter app logs through correlation graph.
        strict = bool(kwargs.get("strict_correlation"))
        correlated_logs = filter_log_files(
            log_files, graph,
            strict=strict, max_records=DEFAULT_LOG_RECORD_CAP,
        ) if log_files else []

        mask = bool(kwargs.get("mask")) and not (
            kwargs.get("unmask") or ctx.unmask
        )

        waterfall = _build_tool_waterfall(session_records, mask=mask)
        wf_stats = _waterfall_stats(waterfall)

        timeline = _build_timeline(
            session_records=session_records,
            trajectory_runs=selected_runs or traj_runs,
            correlated_logs=correlated_logs,
        )
        timeline_keys = _timeline_key_moments(timeline)

        runtime_blocks = [_runtime_context(r) for r in (selected_runs or traj_runs)]
        decisions = _model_decisions(selected_runs or traj_runs, correlated_logs)
        children = [_summarize_child_task(row) for row in child_rows]
        signals = _health_signals(
            selected_runs or traj_runs, correlated_logs,
            waterfall=waterfall, children=children,
        )
        model_calls = _extract_model_calls(session_records)
        model_aggregate = _aggregate_model_calls(model_calls)

        # Determine trigger / status / model / origin from the selected run(s).
        primary_run = (selected_runs[-1] if selected_runs
                       else (traj_runs[-1] if traj_runs else None))
        primary_runtime = (
            runtime_blocks[-1] if runtime_blocks else None
        )

        cron_runs: List[Dict[str, Any]] = []
        if cron_run_path:
            try:
                with open(cron_run_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(rec, dict):
                            cron_runs.append(rec)
            except OSError:
                pass

        # ── Aggregated session stats (cross-section summary) ─────────────
        total_input = sum(c.get("input") or 0 for c in model_calls)
        total_output = sum(c.get("output") or 0 for c in model_calls)
        total_cache_read = sum(c.get("cacheRead") or 0 for c in model_calls)
        total_cache_write = sum(c.get("cacheWrite") or 0 for c in model_calls)
        total_cost_usd = model_aggregate.get("total_cost_usd")
        session_stats = {
            "model_calls": len(model_calls),
            "tool_calls": wf_stats["total"],
            "tool_errors": wf_stats["errors"],
            "child_tasks": len(children),
            "child_failures": sum(
                1 for c in children
                if c.get("status") in ("failed", "errored", "error")
                or c.get("terminal_outcome") in ("failed", "error")
            ),
            "tokens": {
                "input": total_input,
                "output": total_output,
                "cache_read": total_cache_read,
                "cache_write": total_cache_write,
            },
            "total_cost_usd": total_cost_usd,
            "compaction_count": (
                primary_runtime.get("compaction_count")
                if primary_runtime else None
            ),
        }

        report.data["session_id"] = full_session_id
        report.data["agent"] = agent_id
        report.data["session_file"] = session_file
        report.data["sources_present"] = sources_present
        report.data["correlation_graph"] = graph.to_dict()
        report.data["window"] = {
            "start_ms": window_start,
            "end_ms": window_end,
            "duration_ms": duration_ms,
        }
        report.data["selected_runs"] = [r["runId"] for r in (selected_runs or [])]
        report.data["all_runs"] = [r["runId"] for r in traj_runs]
        report.data["timeline"] = timeline
        report.data["timeline_key_moments"] = timeline_keys
        report.data["tool_waterfall"] = waterfall
        report.data["tool_stats"] = wf_stats
        report.data["runtime_context"] = runtime_blocks
        report.data["correlated_logs"] = correlated_logs
        report.data["model_decisions"] = decisions
        report.data["model_calls"] = model_calls
        report.data["model_aggregate"] = model_aggregate
        report.data["health_signals"] = signals
        report.data["child_tasks"] = children
        report.data["session_stats"] = session_stats
        if cron_runs:
            report.data["cron_runs"] = cron_runs

        # ── Sections ─────────────────────────────────────────────────────
        # 1. Session Overview (merges old Overview + Correlation Graph)
        s_overview = report.section("Panorama · Session Overview")
        s_overview.ok(
            "session", f"sessionId: {full_session_id}",
            data={"session_id": full_session_id, "agent": agent_id},
        )
        s_overview.ok("agent", f"agent: {agent_id}")
        if graph.session_key:
            s_overview.ok("session_key", f"sessionKey: {graph.session_key}")
        if primary_runtime:
            trig = primary_runtime.get("trigger") or "?"
            s_overview.ok("trigger", f"trigger: {trig}")
            mp = primary_runtime.get("model_id") or "?"
            pv = primary_runtime.get("provider") or "?"
            s_overview.ok("model", f"model: {pv}/{mp}")
            if primary_runtime.get("channel"):
                s_overview.ok("channel", f"channel: {primary_runtime['channel']}")
            fs = primary_runtime.get("final_status")
            if fs:
                s_overview.ok("final_status", f"finalStatus: {fs}")
        if window_start and window_end:
            s_overview.ok(
                "window",
                f"window: {fmt_epoch_local(window_start)} → "
                f"{fmt_epoch_local(window_end)} ({fmt_duration(duration_ms / 1000)})",
                data={"start_ms": window_start, "end_ms": window_end,
                      "duration_ms": duration_ms},
            )
        s_overview.ok(
            "runs",
            f"runs: {len(traj_runs)} total, "
            f"{len(selected_runs) if selected_runs else 0} selected",
            data={"total": len(traj_runs),
                  "selected": [r["runId"] for r in (selected_runs or [])]},
        )
        # Activity stats
        cost_s = (f" | cost=${total_cost_usd:.4f}"
                  if isinstance(total_cost_usd, (int, float)) else "")
        s_overview.ok(
            "stats.activity",
            f"activity: {session_stats['model_calls']} model calls, "
            f"{session_stats['tool_calls']} tool calls "
            f"({session_stats['tool_errors']} errors)",
            data=session_stats,
        )
        s_overview.ok(
            "stats.tokens",
            f"tokens: in={total_input:,} out={total_output:,} "
            f"cacheRead={total_cache_read:,} cacheWrite={total_cache_write:,}"
            + cost_s,
        )
        if session_stats["child_tasks"]:
            s_overview.ok(
                "stats.children",
                f"child tasks: {session_stats['child_tasks']} "
                f"({session_stats['child_failures']} failed)",
            )
        compaction = session_stats.get("compaction_count") or 0
        if compaction:
            s_overview.warn(
                "stats.compaction",
                f"compactions: {compaction}",
            )
        # Correlation IDs (collapsed into one line each)
        s_overview.ok(
            "ids",
            f"ids: runIds={len(graph.run_ids)} toolCalls={len(graph.tool_call_ids)} "
            f"children={len(graph.child_session_ids)}"
            + (f" cron={graph.cron_job_id}" if graph.cron_job_id else ""),
            data={
                "sessionId": graph.session_id,
                "sessionKey": graph.session_key,
                "runIds": sorted(graph.run_ids),
                "toolCallIdsSample": sorted(graph.tool_call_ids)[:8],
                "childSessionIds": sorted(graph.child_session_ids),
                "cronJobId": graph.cron_job_id,
            },
        )
        # Sources (one line)
        present = [s for s, ok in sources_present.items() if ok]
        missing = [s for s, ok in sources_present.items() if not ok]
        s_overview.ok(
            "sources",
            f"sources: {', '.join(present)}"
            + (f" | missing: {', '.join(missing)}" if missing else ""),
            data=sources_present,
        )

        # 2. Timeline — show key moments, not just count
        s_timeline = report.section("Panorama · Timeline")
        if not timeline:
            s_timeline.warn("timeline.empty", "no timeline events")
        else:
            span_ms = timeline[-1]["ts_ms"] - timeline[0]["ts_ms"]
            s_timeline.ok(
                "timeline.window",
                f"{len(timeline)} events over {fmt_duration(span_ms / 1000)} "
                f"({fmt_epoch_local(timeline[0]['ts_ms'])} → "
                f"{fmt_epoch_local(timeline[-1]['ts_ms'])})",
                data={"count": len(timeline)},
            )
            # First / last event detail
            s_timeline.ok(
                "timeline.first",
                f"first: [{fmt_epoch_local(timeline[0]['ts_ms'])}] "
                f"{timeline[0].get('source')} · {timeline[0].get('summary', '')[:120]}",
            )
            s_timeline.ok(
                "timeline.last",
                f"last: [{fmt_epoch_local(timeline[-1]['ts_ms'])}] "
                f"{timeline[-1].get('source')} · {timeline[-1].get('summary', '')[:120]}",
            )
            # Key moments
            fe = timeline_keys.get("first_error")
            if fe:
                s_timeline.fail(
                    "timeline.first_error",
                    f"first error: [{fmt_epoch_local(fe['ts_ms'])}] "
                    f"{fe.get('summary', '')[:140]}",
                    data=fe,
                )
            fw = timeline_keys.get("first_warn")
            if fw and not fe:
                s_timeline.warn(
                    "timeline.first_warn",
                    f"first warn: [{fmt_epoch_local(fw['ts_ms'])}] "
                    f"{fw.get('summary', '')[:140]}",
                    data=fw,
                )
            fs = timeline_keys.get("first_stall")
            if fs:
                s_timeline.warn(
                    "timeline.first_stall",
                    f"first stall: [{fmt_epoch_local(fs['ts_ms'])}] "
                    f"{fs.get('summary', '')[:140]}",
                    data=fs,
                )
            lg = timeline_keys.get("longest_gap")
            if lg and lg["ms"] > 5000:
                s_timeline.ok(
                    "timeline.longest_gap",
                    f"longest gap: {fmt_duration(lg['ms'] / 1000)} "
                    f"({fmt_epoch_local(lg['after_ts_ms'])} → "
                    f"{fmt_epoch_local(lg['before_ts_ms'])})",
                    data=lg,
                )

        # 3. Runtime Context — full picture
        s_runtime = report.section("Panorama · Runtime Context")
        for blk in runtime_blocks:
            rid = blk.get("runId", "?")[:8]
            s_runtime.ok(
                f"runtime.model.{rid}",
                f"run {rid} · model: {blk.get('provider')}/{blk.get('model_id')}"
                + (f" · finalStatus={blk.get('final_status')}"
                   if blk.get('final_status') else ""),
                data=blk,
            )
            s_runtime.ok(
                f"runtime.trigger.{rid}",
                f"trigger: {blk.get('trigger')} | channel: {blk.get('channel')}"
                + (f" | harness: {blk.get('harness_version')}"
                   if blk.get('harness_version') else ""),
            )
            sp_chars = blk.get("system_prompt_chars")
            pc_chars = blk.get("project_context_chars")
            np_chars = blk.get("non_project_context_chars")
            if sp_chars:
                pc_s = f" project={pc_chars:,}" if pc_chars else ""
                np_s = f" nonProject={np_chars:,}" if np_chars else ""
                s_runtime.ok(
                    f"runtime.prompt.{rid}",
                    f"system prompt: {sp_chars:,} chars{pc_s}{np_s}",
                )
            tool_count = blk.get("tool_count")
            skill_count = blk.get("skill_count")
            plugin_count = blk.get("plugin_count")
            cmsg = blk.get("compiled_messages_count")
            s_runtime.ok(
                f"runtime.tools.{rid}",
                f"tools: {tool_count or '?'} | skills: {skill_count or '?'} "
                f"| plugins: {plugin_count or '?'}"
                + (f" | messages: {cmsg}" if cmsg is not None else ""),
            )
            schema_chars = blk.get("tools_schema_chars")
            if schema_chars:
                s_runtime.ok(
                    f"runtime.schema.{rid}",
                    f"tools schema: {schema_chars:,} chars",
                )
            ss = blk.get("stream_strategy")
            tp = blk.get("transport")
            ic = blk.get("images_count") or 0
            if ss or tp or ic:
                s_runtime.ok(
                    f"runtime.stream.{rid}",
                    f"streamStrategy: {ss or '?'} | transport: {tp or '?'} "
                    f"| images: {ic}",
                )
            cc = blk.get("compaction_count") or 0
            if cc:
                s_runtime.warn(
                    f"runtime.compaction.{rid}",
                    f"compactions: {cc}"
                    + (" | cache broke" if blk.get("cache_broke") else ""),
                )
            iwf = blk.get("injected_workspace_files") or []
            if iwf:
                names = ", ".join(
                    f"{e.get('name')}({e.get('chars'):,})"
                    + ("⚠truncated" if e.get('truncated') else "")
                    for e in iwf
                )
                s_runtime.ok(
                    f"runtime.workspace.{rid}",
                    f"workspace files: {len(iwf)} — {names[:200]}",
                    data={"files": iwf},
                )
            bt = blk.get("bootstrap_truncation")
            if isinstance(bt, dict) and (
                bt.get("truncated_files") or bt.get("near_limit_files")
            ):
                s_runtime.warn(
                    f"runtime.bootstrap.{rid}",
                    f"bootstrap: truncated={bt.get('truncated_files')} "
                    f"nearLimit={bt.get('near_limit_files')} "
                    f"totalNearLimit={bt.get('total_near_limit')}",
                    data=bt,
                )
            # Skill names list
            sn = blk.get("skill_names")
            if sn:
                preview = ", ".join(sn[:8])
                more = f" (+{len(sn) - 8} more)" if len(sn) > 8 else ""
                s_runtime.ok(
                    f"runtime.skill_names.{rid}",
                    f"skills: {preview}{more}",
                    data={"skills": sn},
                )
            # Plugins activated
            pa = blk.get("plugins_activated")
            if pa:
                s_runtime.ok(
                    f"runtime.plugins.{rid}",
                    f"plugins activated: {', '.join(pa)}",
                    data={"plugins_activated": pa},
                )
            err_count = len(blk.get("plugin_errors") or [])
            if err_count:
                for pe in (blk.get("plugin_errors") or [])[:5]:
                    s_runtime.warn(
                        f"runtime.plugin_error.{pe.get('id','?')}",
                        f"plugin error: {pe.get('id')} — "
                        f"{(pe.get('error') or '?')[:120]}",
                    )
        if not runtime_blocks:
            s_runtime.warn("runtime.missing", "no trajectory runs available")

        # 4. Model Calls — with per-call duration + per-model performance
        s_model = report.section("Panorama · Model Calls")
        if not model_calls:
            s_model.ok("model.none", "no model calls in selected run")
        else:
            cost_s = (f" | cost=${total_cost_usd:.4f}"
                      if isinstance(total_cost_usd, (int, float)) else "")
            s_model.ok(
                "model.summary",
                f"{len(model_calls)} calls | out={total_output:,} tok | "
                f"cache_read={total_cache_read:,} | cache_write={total_cache_write:,}"
                + cost_s,
                data={
                    "count": len(model_calls), "input": total_input,
                    "output": total_output, "cache_read": total_cache_read,
                    "cache_write": total_cache_write,
                    "cost_usd": total_cost_usd,
                },
            )
            if duration_ms and total_output:
                rate = round(total_output / (duration_ms / 1000), 1)
                s_model.ok(
                    "model.rate", f"avg output rate: {rate} tok/s",
                    data={"tokens_per_sec": rate},
                )
            # Per-model performance breakdown
            for m in model_aggregate.get("models", []):
                stop_summary = ", ".join(
                    f"{k}={v}" for k, v in sorted(
                        m["stop_reasons"].items(),
                        key=lambda kv: -kv[1],
                    )
                )
                avg_dur = m.get("avg_duration_ms")
                avg_dur_s = (f" | avg_dur={fmt_duration(avg_dur)}"
                             if avg_dur else "")
                s_model.ok(
                    f"model.by.{m['model']}",
                    f"{m['model']}: {m['calls']} calls | "
                    f"avg_out={m['avg_output']} tok{avg_dur_s} | "
                    f"stop[{stop_summary}]",
                    data=m,
                )
            # Per-call detail
            for idx, c in enumerate(model_calls, 1):
                tools_s = ",".join(c["tools"][:3]) if c["tools"] else "→ final"
                dur = c.get("duration_ms")
                dur_s = fmt_duration(dur) if dur is not None else "?"
                s_model.ok(
                    f"model.call.{idx}",
                    f"#{idx} {dur_s} out={c['output']} ({c['stopReason']}) "
                    f"[{tools_s}] cr={c['cacheRead']} cw={c['cacheWrite']}",
                    data=c,
                )

        # 5. Tool Execution — args + result summary inline
        s_tools = report.section("Panorama · Tool Execution")
        if not waterfall:
            s_tools.ok("tools.none", "no tool calls in this run")
        else:
            if wf_stats["completed"]:
                s_tools.ok(
                    "tools.timing",
                    f"{wf_stats['total']} calls "
                    f"({wf_stats['errors']} errors) | "
                    f"avg={wf_stats['avg_ms']}ms p50={wf_stats['p50_ms']}ms "
                    f"p95={wf_stats['p95_ms']}ms max={wf_stats['max_ms']}ms",
                    data=wf_stats,
                )
            for idx, t in enumerate(waterfall, 1):
                name = t.get("name", "?")
                dur = t.get("duration_ms")
                dur_s = f"{dur}ms" if dur is not None else "?"
                is_err = t.get("is_error")
                v = Verdict.WARN if is_err else Verdict.OK
                status = "✗" if is_err else "✓"
                args_s = _format_args_inline(t.get("args"))
                result_s = _format_result_inline(t)
                line = f"#{idx} {name} {dur_s} {status}"
                if args_s:
                    line += f" args={args_s}"
                if result_s:
                    arrow = " ⇒ ERR " if is_err else " → "
                    line += arrow + result_s
                s_tools.add(
                    f"tools.call.{idx}", v, line, data=t,
                )

        # 6. Correlated Logs — show errors/warns + representative INFO
        s_logs = report.section("Panorama · Correlated Logs")
        if not log_files:
            s_logs.warn("logs.missing", "no app log files found in log_dir")
        elif not correlated_logs:
            s_logs.ok("logs.none", "no correlated log entries found")
        else:
            error_entries = [
                rec for rec in correlated_logs
                if (_log_level(rec) or "").upper() == "ERROR"
            ]
            warn_entries = [
                rec for rec in correlated_logs
                if (_log_level(rec) or "").upper() == "WARN"
            ]
            info_count = (
                len(correlated_logs) - len(error_entries) - len(warn_entries)
            )
            s_logs.ok(
                "logs.summary",
                f"{len(correlated_logs)} correlated entries: "
                f"{len(error_entries)} ERROR, {len(warn_entries)} WARN, "
                f"{info_count} INFO",
                data={"total": len(correlated_logs),
                      "error": len(error_entries),
                      "warn": len(warn_entries), "info": info_count},
            )
            for rec in error_entries[:10]:
                msg_s = parse_log_msg(rec) or "?"
                sub = get_log_subsystem(rec) or "?"
                ts = _log_ts_ms(rec)
                tsfx = f"[{fmt_epoch_local(ts)}] " if ts else ""
                s_logs.fail(
                    f"logs.error.{sub}",
                    f"{tsfx}[ERROR] [{sub}] {msg_s[:200]}",
                    data={"correlation": rec.get("correlation")},
                )
            for rec in warn_entries[:10]:
                msg_s = parse_log_msg(rec) or "?"
                sub = get_log_subsystem(rec) or "?"
                ts = _log_ts_ms(rec)
                tsfx = f"[{fmt_epoch_local(ts)}] " if ts else ""
                s_logs.warn(
                    f"logs.warn.{sub}",
                    f"{tsfx}[WARN] [{sub}] {msg_s[:200]}",
                    data={"correlation": rec.get("correlation")},
                )
            # Representative INFO entries when nothing failed
            if info_count and not error_entries and not warn_entries:
                info_only = [
                    rec for rec in correlated_logs
                    if (_log_level(rec) or "").upper() not in ("ERROR", "WARN")
                ]
                reps = _representative_logs(info_only, limit=5)
                for idx, rec in enumerate(reps, 1):
                    msg_s = parse_log_msg(rec) or "?"
                    sub = get_log_subsystem(rec) or "?"
                    ts = _log_ts_ms(rec)
                    tsfx = f"[{fmt_epoch_local(ts)}] " if ts else ""
                    s_logs.ok(
                        f"logs.info.{idx}",
                        f"{tsfx}[INFO] [{sub}] {msg_s[:160]}",
                        data={"correlation": rec.get("correlation")},
                    )

        # 7. Model Decisions
        s_decisions = report.section("Panorama · Model Decisions")
        if not decisions:
            s_decisions.ok(
                "decisions.none", "no model fallback or selection events",
            )
        else:
            for d in decisions[:8]:
                kind = d.get("kind", "?")
                if kind == "model_select":
                    msg_s = (
                        f"model: {d.get('provider')}/{d.get('name')} "
                        f"(api={d.get('api')})"
                    )
                    s_decisions.ok(f"decision.{kind}", msg_s, data=d)
                else:
                    msg_s = d.get("summary", "?")
                    s_decisions.warn(f"decision.{kind}", msg_s, data=d)

        # 8. Child Tasks
        s_children = report.section("Panorama · Child Tasks")
        if not children:
            s_children.ok("children.none", "no child tasks correlated")
        else:
            failed = [c for c in children if c.get("status") in (
                "failed", "errored", "error",
            ) or c.get("terminal_outcome") in ("failed", "error")]
            for c in failed:
                err = c.get("error") or c.get("terminal_summary") or "unknown error"
                dur = c.get("duration_ms")
                dur_s = f"{dur}ms" if dur is not None else "?"
                s_children.warn(
                    f"child.failed.{(c.get('task_id') or '?')[:8]}",
                    f"FAILED: {c.get('agent_id')}/{c.get('runtime')} "
                    f"duration={dur_s} error={str(err)[:150]}",
                    data=c,
                )
            succeeded = [c for c in children if c.get("status") == "succeeded"]
            if succeeded:
                s_children.ok(
                    "children.succeeded",
                    f"{len(succeeded)} child tasks succeeded",
                )

        # 9. Delivery
        s_delivery = report.section("Panorama · Delivery")
        delivery_seen = False
        if cron_runs:
            delivery_seen = True
            last = cron_runs[-1]
            s_delivery.ok(
                "delivery.cron",
                f"cron job {graph.cron_job_id}: "
                f"action={last.get('action')} status={last.get('status')} "
                f"deliveryStatus={last.get('deliveryStatus')}",
                data=last,
            )
        if primary_runtime and primary_runtime.get("did_send_via_messaging_tool"):
            delivery_seen = True
            s_delivery.ok(
                "delivery.messaging",
                f"sent via messaging tool: targets="
                f"{len(primary_runtime.get('messaging_targets') or [])} "
                f"texts={primary_runtime.get('messaging_text_count')}",
                data={
                    "targets": primary_runtime.get("messaging_targets"),
                    "text_count": primary_runtime.get("messaging_text_count"),
                },
            )
        if not delivery_seen:
            s_delivery.ok("delivery.none", "no delivery records correlated")

        # 10. Health Signals — with timestamps + richer data
        s_health = report.section("Panorama · Health Signals")
        if not signals:
            s_health.ok(
                "health.clean",
                "no abort/timeout/leak/stall signals",
            )
        else:
            for sig in signals:
                kind = sig.get("kind", "?")
                ts_ms = sig.get("ts_ms") or 0
                tsfx = f"[{fmt_epoch_local(ts_ms)}] " if ts_ms else ""
                if kind == "trajectory_artifact":
                    s_health.fail(
                        f"health.{kind}.{(sig.get('runId') or '?')[:8]}",
                        f"{tsfx}run {(sig.get('runId') or '?')[:8]}: "
                        f"{','.join(sig.get('flags') or [])} "
                        f"finalStatus={sig.get('final_status')}",
                        data=sig,
                    )
                elif kind == "tool_call_leak":
                    s_health.warn(
                        f"health.{kind}.{(sig.get('runId') or '?')[:8]}",
                        f"{tsfx}tool-call leak: active={sig.get('active')} "
                        f"started={sig.get('started')} "
                        f"completed={sig.get('completed')}",
                        data=sig,
                    )
                elif kind == "log_stall":
                    s_health.warn(
                        f"health.{kind}.{ts_ms}",
                        f"{tsfx}stall: {sig.get('summary')}",
                        data=sig,
                    )
                elif kind == "gateway_pid_change":
                    s_health.warn(
                        f"health.{kind}",
                        f"gateway pid change: {sig.get('pids')}",
                        data=sig,
                    )
                elif kind == "long_tool_call":
                    err_s = " (error)" if sig.get("is_error") else ""
                    s_health.warn(
                        f"health.{kind}.{ts_ms}",
                        f"{tsfx}long tool call: {sig.get('name')} "
                        f"{fmt_duration((sig.get('duration_ms') or 0))}"
                        f"{err_s}",
                        data=sig,
                    )
                elif kind == "last_tool_error":
                    s_health.warn(
                        f"health.{kind}.{(sig.get('runId') or '?')[:8]}",
                        f"{tsfx}last tool error: {sig.get('summary')}",
                        data=sig,
                    )
                elif kind == "child_task_failed":
                    s_health.warn(
                        f"health.{kind}.{(sig.get('task_id') or '?')[:8]}",
                        f"{tsfx}child task failed: "
                        f"{sig.get('agent_id')}/{sig.get('runtime')} — "
                        f"{sig.get('error')}",
                        data=sig,
                    )
                else:
                    s_health.warn(f"health.{kind}", str(sig), data=sig)

        if duration_ms > SLOW_E2E_MS:
            s_overview.warn(
                "window.slow",
                f"E2E elapsed {fmt_duration(duration_ms / 1000)} "
                f"> {SLOW_E2E_MS // 1000}s",
                data={"duration_ms": duration_ms,
                      "threshold_ms": SLOW_E2E_MS},
            )

        report.data["verdict"] = report.verdict.value
        report.elapsed_ms = (time.time() - t0) * 1000
        return report
