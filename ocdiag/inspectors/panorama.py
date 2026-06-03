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


def _build_tool_waterfall(
    records: List[Dict[str, Any]],
    *,
    mask: bool,
) -> List[Dict[str, Any]]:
    """Pair toolCalls (assistant content blocks) with toolResults by id.

    Each pair → ``{name, callId, start_ms, end_ms, duration_ms, is_error,
    args}``. Unmatched calls (still pending or never dispatched) get
    ``end_ms=None``.
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
                }
                order.append(cid)
        elif role == "toolResult":
            cid = msg.get("toolCallId") or msg.get("toolUseId") or msg.get("id")
            if not isinstance(cid, str) or not cid:
                continue
            entry = calls.get(cid)
            if entry is None:
                # Result without a known call — still record it so callers
                # see it. start_ms unknown → set equal to end_ms.
                calls[cid] = {
                    "callId": cid,
                    "name": msg.get("toolName") or "?",
                    "start_ms": ts,
                    "end_ms": ts,
                    "duration_ms": 0,
                    "is_error": bool(msg.get("isError")),
                    "args": None,
                }
                order.append(cid)
                continue
            entry["end_ms"] = ts
            if entry["start_ms"] and ts:
                entry["duration_ms"] = max(0, ts - entry["start_ms"])
            entry["is_error"] = bool(msg.get("isError"))
            if not entry.get("name") or entry["name"] == "?":
                tn = msg.get("toolName")
                if tn:
                    entry["name"] = tn
    return [calls[cid] for cid in order]


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
        prompting = md.get("prompting") or {}
        if isinstance(prompting, dict):
            spr = prompting.get("systemPromptReport") or {}
            if isinstance(spr, dict):
                sp = spr.get("systemPrompt") or {}
                if isinstance(sp, dict):
                    ctx["system_prompt_chars"] = sp.get("chars")
                    ctx["project_context_chars"] = sp.get("projectContextChars")
                tools = spr.get("tools") or {}
                if isinstance(tools, dict):
                    ctx["tools_schema_chars"] = tools.get("schemaChars")
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


def _health_signals(
    runs: List[Dict[str, Any]],
    correlated_logs: List[Dict[str, Any]],
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
            })
        il = ad.get("itemLifecycle") or {}
        if isinstance(il, dict) and il.get("activeCount"):
            signals.append({
                "kind": "tool_call_leak",
                "runId": run["runId"],
                "active": il.get("activeCount"),
                "started": il.get("startedCount"),
                "completed": il.get("completedCount"),
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
    return signals


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

        runtime_blocks = [_runtime_context(r) for r in (selected_runs or traj_runs)]
        decisions = _model_decisions(selected_runs or traj_runs, correlated_logs)
        signals = _health_signals(selected_runs or traj_runs, correlated_logs)
        children = [_summarize_child_task(row) for row in child_rows]

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
        report.data["tool_waterfall"] = waterfall
        report.data["tool_stats"] = wf_stats
        report.data["runtime_context"] = runtime_blocks
        report.data["correlated_logs"] = correlated_logs
        report.data["model_decisions"] = decisions
        report.data["health_signals"] = signals
        report.data["child_tasks"] = children
        if cron_runs:
            report.data["cron_runs"] = cron_runs

        # ── Sections ─────────────────────────────────────────────────────
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
        for src, ok in sources_present.items():
            if ok:
                s_overview.ok(f"source.{src}", f"source: {src} ✓")
            else:
                s_overview.warn(f"source.{src}", f"source: {src} (missing)")

        s_corr = report.section("Panorama · Correlation Graph")
        s_corr.ok(
            "ids.sessionId", f"sessionId: {graph.session_id}",
            data={"path": graph.paths.get(graph.session_id)},
        )
        if graph.session_key:
            s_corr.ok(
                "ids.sessionKey", f"sessionKey: {graph.session_key}",
                data={"path": graph.paths.get(graph.session_key)},
            )
        s_corr.ok(
            "ids.runIds",
            f"runIds: {len(graph.run_ids)}",
            data={"runIds": sorted(graph.run_ids)},
        )
        s_corr.ok(
            "ids.toolCallIds",
            f"toolCallIds: {len(graph.tool_call_ids)}",
            data={"sample": sorted(graph.tool_call_ids)[:8]},
        )
        s_corr.ok(
            "ids.childSessionIds",
            f"childSessionIds: {len(graph.child_session_ids)}",
            data={"childSessionIds": sorted(graph.child_session_ids)},
        )
        if graph.cron_job_id:
            s_corr.ok(
                "ids.cronJobId", f"cronJobId: {graph.cron_job_id}",
                data={"path": graph.paths.get(graph.cron_job_id)},
            )

        s_timeline = report.section("Panorama · Timeline")
        # Don't just show count — show key events
        if not timeline:
            s_timeline.warn("timeline.empty", "no timeline events")
        else:
            s_timeline.ok(
                "timeline.window",
                f"{len(timeline)} events over "
                f"{fmt_duration((timeline[-1]['ts_ms'] - timeline[0]['ts_ms']) / 1000)}",
                data={"count": len(timeline)},
            )

        s_runtime = report.section("Panorama · Runtime Context")
        for blk in runtime_blocks:
            rid = blk.get("runId", "?")[:8]
            s_runtime.ok(
                f"runtime.model.{rid}",
                f"model: {blk.get('provider')}/{blk.get('model_id')}",
                data=blk,
            )
            s_runtime.ok(
                f"runtime.trigger.{rid}",
                f"trigger: {blk.get('trigger')} | channel: {blk.get('channel')}",
            )
            sp_chars = blk.get("system_prompt_chars")
            pc_chars = blk.get("project_context_chars")
            if sp_chars:
                s_runtime.ok(
                    f"runtime.prompt.{rid}",
                    f"system prompt: {sp_chars:,} chars"
                    + (f" (project context: {pc_chars:,})" if pc_chars else ""),
                )
            tool_count = blk.get("tool_count")
            skill_count = blk.get("skill_count")
            plugin_count = blk.get("plugin_count")
            s_runtime.ok(
                f"runtime.tools.{rid}",
                f"tools: {tool_count or '?'} | skills: {skill_count or '?'} | plugins: {plugin_count or '?'}",
            )
            schema_chars = blk.get("tools_schema_chars")
            if schema_chars:
                s_runtime.ok(
                    f"runtime.schema.{rid}",
                    f"tools schema: {schema_chars:,} chars",
                )
            err_count = len(blk.get("plugin_errors") or [])
            if err_count:
                for pe in (blk.get("plugin_errors") or [])[:5]:
                    s_runtime.warn(
                        f"runtime.plugin_error.{pe.get('id','?')}",
                        f"plugin error: {pe.get('id')} — {pe.get('error','?')[:100]}",
                    )
        if not runtime_blocks:
            s_runtime.warn("runtime.missing", "no trajectory runs available")

        # ── Model Calls section ──
        s_model = report.section("Panorama · Model Calls")
        model_calls = []
        for rec in session_records:
            if not isinstance(rec, dict) or rec.get("type") != "message":
                continue
            msg = rec.get("message", {})
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            # Extract triggered tools from content
            triggered_tools: List[str] = []
            content = msg.get("content", [])
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "toolCall":
                        triggered_tools.append(c.get("name", "?"))
            model_calls.append({
                "provider": msg.get("provider", "?"),
                "model": msg.get("model", "?"),
                "stopReason": msg.get("stopReason", "?"),
                "input": usage.get("input") or usage.get("inputTokens") or 0,
                "output": usage.get("output") or usage.get("outputTokens") or 0,
                "cacheRead": usage.get("cacheRead") or usage.get("cacheReadInputTokens") or 0,
                "cacheWrite": usage.get("cacheWrite") or usage.get("cacheCreationInputTokens") or 0,
                "totalTokens": usage.get("totalTokens") or usage.get("total") or 0,
                "cost": usage.get("cost"),
                "tools": triggered_tools,
                "ts": rec.get("timestamp", ""),
            })
        if not model_calls:
            s_model.ok("model.none", "no model calls in selected run")
        else:
            total_input = sum(c["input"] for c in model_calls)
            total_output = sum(c["output"] for c in model_calls)
            total_cache_read = sum(c["cacheRead"] for c in model_calls)
            total_cache_write = sum(c["cacheWrite"] for c in model_calls)
            total_tokens = sum(c["totalTokens"] for c in model_calls)
            total_cost = None
            costs = [c["cost"] for c in model_calls if isinstance(c.get("cost"), dict)]
            if costs:
                total_cost = round(sum(
                    (co.get("input", 0) or 0) + (co.get("output", 0) or 0) +
                    (co.get("cacheRead", 0) or 0) + (co.get("cacheWrite", 0) or 0)
                    for co in costs
                ), 4)
            # Summary line
            s_model.ok(
                "model.summary",
                f"{len(model_calls)} calls | out={total_output} tok | "
                f"cache_read={total_cache_read} | cache_write={total_cache_write}"
                + (f" | cost=${total_cost:.4f}" if total_cost else ""),
                data={
                    "count": len(model_calls), "input": total_input,
                    "output": total_output, "cache_read": total_cache_read,
                    "cache_write": total_cache_write, "total": total_tokens,
                    "cost_usd": total_cost,
                },
            )
            # Output rate
            if duration_ms and total_output:
                rate = round(total_output / (duration_ms / 1000), 1)
                s_model.ok(
                    "model.rate",
                    f"avg output rate: {rate} tok/s",
                    data={"tokens_per_sec": rate},
                )
            # Per-call detail (every call)
            for idx, c in enumerate(model_calls, 1):
                tools_s = ",".join(c["tools"][:3]) if c["tools"] else "→ final"
                s_model.ok(
                    f"model.call.{idx}",
                    f"#{idx} out={c['output']} ({c['stopReason']}) "
                    f"[{tools_s}] "
                    f"cr={c['cacheRead']} cw={c['cacheWrite']}",
                    data=c,
                )

        # ── Tool Execution section ──
        s_tools = report.section("Panorama · Tool Execution")
        if not waterfall:
            s_tools.ok("tools.none", "no tool calls in this run")
        else:
            # Summary stats
            if wf_stats["completed"]:
                s_tools.ok(
                    "tools.timing",
                    f"{wf_stats['total']} calls | "
                    f"avg={wf_stats['avg_ms']}ms p50={wf_stats['p50_ms']}ms "
                    f"p95={wf_stats['p95_ms']}ms max={wf_stats['max_ms']}ms",
                    data=wf_stats,
                )
            # Each tool call individually
            for idx, t in enumerate(waterfall, 1):
                name = t.get("name", "?")
                dur = t.get("duration_ms")
                dur_s = f"{dur}ms" if dur is not None else "?"
                is_err = t.get("is_error")
                v = Verdict.WARN if is_err else Verdict.OK
                status = "✗" if is_err else "✓"
                s_tools.add(
                    f"tools.call.{idx}",
                    v,
                    f"#{idx} {name} {dur_s} {status}",
                    data=t,
                )

        s_logs = report.section("Panorama · Correlated Logs")
        if not log_files:
            s_logs.warn("logs.missing", "no app log files found in log_dir")
        elif not correlated_logs:
            s_logs.ok("logs.none", "no correlated log entries found")
        else:
            # Show actual ERROR entries (most important)
            error_entries = [
                rec for rec in correlated_logs
                if (_log_level(rec) or "").upper() == "ERROR"
            ]
            for rec in error_entries[:10]:
                msg = parse_log_msg(rec) or "?"
                sub = get_log_subsystem(rec) or "?"
                s_logs.fail(
                    f"logs.error.{sub}",
                    f"[ERROR] [{sub}] {msg[:200]}",
                    data={"correlation": rec.get("correlation")},
                )
            # Show actual WARN entries
            warn_entries = [
                rec for rec in correlated_logs
                if (_log_level(rec) or "").upper() == "WARN"
            ]
            for rec in warn_entries[:10]:
                msg = parse_log_msg(rec) or "?"
                sub = get_log_subsystem(rec) or "?"
                s_logs.warn(
                    f"logs.warn.{sub}",
                    f"[WARN] [{sub}] {msg[:200]}",
                    data={"correlation": rec.get("correlation")},
                )
            # If only INFO, just note the count
            info_count = len(correlated_logs) - len(error_entries) - len(warn_entries)
            if info_count and not error_entries and not warn_entries:
                s_logs.ok(
                    "logs.info_only",
                    f"{info_count} INFO-level correlated log entries (no errors)",
                )

        s_decisions = report.section("Panorama · Model Decisions")
        if not decisions:
            s_decisions.ok("decisions.none", "no model fallback or selection events")
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

        s_children = report.section("Panorama · Child Tasks")
        if not children:
            s_children.ok("children.none", "no child tasks correlated")
        else:
            # Show failed tasks with error detail
            failed = [c for c in children if c.get("status") in (
                "failed", "errored", "error",
            ) or c.get("terminal_outcome") in ("failed", "error")]
            for c in failed:
                err = c.get("error") or c.get("terminal_summary") or "unknown error"
                dur = c.get("duration_ms")
                dur_s = f"{dur}ms" if dur is not None else "?"
                s_children.warn(
                    f"child.failed.{c.get('task_id', '?')[:8]}",
                    f"FAILED: {c.get('agent_id')}/{c.get('runtime')} "
                    f"duration={dur_s} error={str(err)[:150]}",
                    data=c,
                )
            # Show succeeded tasks briefly
            succeeded = [c for c in children if c.get("status") == "succeeded"]
            if succeeded:
                s_children.ok(
                    "children.succeeded",
                    f"{len(succeeded)} child tasks succeeded",
                )

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

        s_health = report.section("Panorama · Health Signals")
        if not signals:
            s_health.ok("health.clean", "no abort/timeout/leak/stall signals")
        else:
            for sig in signals:
                kind = sig.get("kind", "?")
                if kind == "trajectory_artifact":
                    s_health.fail(
                        f"health.{kind}",
                        f"run {sig.get('runId', '?')[:8]}: "
                        f"{','.join(sig.get('flags') or [])} "
                        f"finalStatus={sig.get('final_status')}",
                        data=sig,
                    )
                elif kind == "tool_call_leak":
                    s_health.warn(
                        f"health.{kind}",
                        f"tool-call leak: active={sig.get('active')} "
                        f"started={sig.get('started')} "
                        f"completed={sig.get('completed')}",
                        data=sig,
                    )
                elif kind == "log_stall":
                    s_health.warn(
                        f"health.{kind}",
                        f"stall: {sig.get('summary')}",
                        data=sig,
                    )
                elif kind == "gateway_pid_change":
                    s_health.warn(
                        f"health.{kind}",
                        f"gateway pid change: {sig.get('pids')}",
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
