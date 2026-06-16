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

Section layout (v1.4.13 — 7 sections; was 6 before the Findings add):

  0. Panorama · Findings              (NEW v1.4.13 — objective triage
       summary rendered FIRST. Re-surfaces the worst already-computed
       problem signals using a deterministic severity ordering. NEVER
       infers a cause, recommends a step, or qualifies with "likely"/
       "probably" etc. Built last in code so the verdict and signals
       are fully resolved, then moved to position 0 of the section list.)
  1. Panorama · Session Overview
  2. Panorama · Timeline
  3. Panorama · Model Calls
  4. Panorama · Tool Execution
  5. Panorama · Correlated Logs & Signals   (was: two separate sections —
       "Correlated Logs" and "Health Signals". v1.4.11 merged them since
       the signals are the human-readable explanation of what the logs
       show; the verdict-driving fail()/warn() calls are unchanged.
       v1.4.13 also sorts the rendered problem signals here using the
       same deterministic severity key as Findings.)
  6. Panorama · Child Tasks
"""

from __future__ import annotations

import json
import os
import re
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
    filter_log_files_with_otel,
)
from ..jsonlog import get_log_subsystem, parse_log_msg
from ..recent_logs import (
    discover_logs_for_window,
    discover_recent_logs,
    window_log_dates,
)
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
# Cap on the number of representative middle-of-the-run timeline events
# rendered under "timeline.sample". Tunable in one place so the render
# heuristic can be retuned without hunting for magic numbers. v1.4.11
# raised this from 20 → 40 so the middle of long runs is more visible
# (and the improved sampler now spreads filler picks across first→last).
TIMELINE_RENDER_SAMPLE = 40
# Slack added to the session window when bounding correlated log entries,
# so a clock-skewed log line written just before/after the run still counts.
LOG_WINDOW_GRACE_MS = 5_000
# Safety cap on rendered ERROR lines per section. Above this we still report
# the count via "+N more" so the section never grows unboundedly.
MAX_RENDERED_ERROR_LINES = 200
MAX_RENDERED_WARN_LINES = 10

# When a window has NO ERROR/WARN correlated entries, show this many
# representative INFO log lines (lifecycle/tool/model boundaries, spanning
# head→middle→tail) so a quiet window still shows what actually happened.
REPRESENTATIVE_INFO_LINES = 20



# ── helpers ────────────────────────────────────────────────────────────────


def _trajectory_path_for(session_file: str) -> Optional[str]:
    d = os.path.dirname(session_file)
    base = os.path.basename(session_file).split(".jsonl", 1)[0]
    p = os.path.join(d, f"{base}.trajectory.jsonl")
    return p if os.path.isfile(p) else None


def _sessions_json_for(session_file: str) -> Optional[str]:
    p = os.path.join(os.path.dirname(session_file), "sessions.json")
    return p if os.path.isfile(p) else None


def _runs_sqlite_path(openclaw_home: str) -> str:
    """Resolve runs.sqlite under the caller-provided OPENCLAW_HOME.

    The path is computed from ``ctx.openclaw_home`` (which already reflects
    the ``--openclaw-home`` CLI flag and ``OPENCLAW_HOME`` env var) rather
    than the import-time ``paths_mod.OPENCLAW_HOME`` constant, which only
    sees the env at module-load time.
    """
    return os.path.join(openclaw_home, "tasks", "runs.sqlite")


def _cron_run_path(job_id: str, openclaw_home: str) -> Optional[str]:
    """Resolve cron/runs/<job>.jsonl under the caller-provided OPENCLAW_HOME.

    A user who explicitly set ``OPENCLAW_CRON_RUNS`` (env-only knob, not on
    the CLI) should still win — we only fall back to the env-derived
    constant when the env var is set; otherwise we honour
    ``--openclaw-home`` by deriving the dir from it.
    """
    cron_runs_env = os.environ.get("OPENCLAW_CRON_RUNS")
    if cron_runs_env:
        base = cron_runs_env
    else:
        base = os.path.join(openclaw_home, "cron", "runs")
    p = os.path.join(base, f"{job_id}.jsonl")
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


def _bound_logs_to_window(
    correlated_logs: List[Dict[str, Any]],
    *,
    window_start: int,
    window_end: int,
    grace_ms: int = LOG_WINDOW_GRACE_MS,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Drop correlated log entries whose timestamp lies outside the session
    window (with a small ``grace_ms`` for clock skew). Entries with no
    parseable timestamp are kept (we can't prove they belong to a different
    window) but counted separately.

    Returns ``(in_window_logs, out_of_window_count, ts_less_count)``. When
    no window is computed (``window_start`` and ``window_end`` both 0), the
    list is returned unchanged with both counters at 0.
    """
    if not window_start and not window_end:
        return list(correlated_logs), 0, 0
    lo = (window_start - grace_ms) if window_start else 0
    hi = (window_end + grace_ms) if window_end else 0
    in_window: List[Dict[str, Any]] = []
    dropped = 0
    ts_less = 0
    for rec in correlated_logs:
        ts = _log_ts_ms(rec)
        if not ts:
            ts_less += 1
            in_window.append(rec)
            continue
        if (lo and ts < lo) or (hi and ts > hi):
            dropped += 1
            continue
        in_window.append(rec)
    return in_window, dropped, ts_less


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


# v1.4.5: a single ``runId`` can carry MULTIPLE attempt-cycles when the run
# retries internally. Each cycle is a full event sequence (session.started →
# trace.metadata → context.compiled → prompt.submitted → model.completed →
# trace.artifacts → session.ended) with ``seq`` resetting to 1 each cycle,
# all sharing the same runId. The pre-1.4.5 grouper kept only the LAST
# trace.artifacts/session.started/trace.metadata for a runId, so a failed
# attempt-1 was overwritten by a successful attempt-2 and never surfaced.
#
# The grouper now collects every cycle into ``run["attempts"]``. The
# top-level ``run["artifacts"] / session_started / trace_metadata`` keys
# still point at the LAST cycle (preserves behavior for everything that
# read those keys), and the per-attempt detail enables multi-attempt
# health reporting.


def _attempt_failure_flags(artifacts_data: Dict[str, Any]) -> List[str]:
    """List of failure flags an attempt's trace.artifacts.data exhibits.

    Returns the camelCase flag names so downstream renderers can keep using
    the same vocabulary as ``_health_signals``. ``promptErrorSource`` is
    treated as a flag too — it indicates the model never returned a clean
    response.
    """
    flags: List[str] = []
    for k in (
        "aborted", "externalAbort", "timedOut", "idleTimedOut",
        "timedOutDuringCompaction", "timedOutDuringToolExecution",
    ):
        if artifacts_data.get(k):
            flags.append(k)
    if artifacts_data.get("promptErrorSource"):
        flags.append("promptErrorSource")
    return flags


def _attempt_failed(artifacts_data: Dict[str, Any]) -> bool:
    """True if this attempt's trace.artifacts indicates a failure.

    Failure modes recognized: any of the abort/timeout flags fire,
    ``promptErrorSource`` is set, or ``finalStatus`` is anything other than
    ``success``/``ok``/``completed``. This is intentionally permissive —
    we'd rather warn about an attempt that recovered than miss one.
    """
    if _attempt_failure_flags(artifacts_data):
        return True
    fs = (artifacts_data.get("finalStatus") or "").lower()
    if fs and fs not in ("success", "ok", "completed"):
        return True
    return False


def _group_trajectory_runs(traj_path: str) -> List[Dict[str, Any]]:
    """Group every event in the trajectory file by ``runId``.

    Returns a list of run dicts ordered by their first observed timestamp,
    each containing:
      runId, started_ms, ended_ms, events: List[Dict], artifacts: Dict|None,
      session_started: Dict|None, trace_metadata: Dict|None,
      attempts: List[Dict], attempt_count: int, had_failed_attempt: bool

    A new attempt is signaled by either:
      - a repeated ``session.started`` event for the same runId, OR
      - a ``seq`` value that resets/decreases (typically going back to 1)
        on a non-session.started event when no attempt is open yet.

    Each entry of ``attempts`` carries:
      index, started_ms, ended_ms, artifacts_event, final_status,
      failure_flags, prompt_error_source, failed (bool).
    Single-cycle runs (the common case) yield ``attempts=[that one]``,
    ``attempt_count=1``, ``had_failed_attempt`` reflecting that one.
    """
    runs: Dict[str, Dict[str, Any]] = {}
    if not traj_path or not os.path.isfile(traj_path):
        return []
    # ``open_attempt`` per runId tracks the cycle currently being built.
    open_attempts: Dict[str, Dict[str, Any]] = {}

    def _close_attempt(rid: str) -> None:
        """Move the currently-open attempt for ``rid`` into the run's
        attempts list. No-op if there is no open attempt.
        """
        cur = open_attempts.pop(rid, None)
        if cur is None:
            return
        run = runs[rid]
        ad = (cur.get("artifacts_event") or {}).get("data") or {}
        cur["final_status"] = ad.get("finalStatus")
        cur["prompt_error_source"] = ad.get("promptErrorSource") or None
        cur["failure_flags"] = _attempt_failure_flags(ad) if ad else []
        # Without an artifacts_event we can't classify failure reliably;
        # fall back to "no artifacts" as a soft fail signal so callers can
        # still detect that something went wrong (e.g. crashed before the
        # final write). We treat "no artifacts" as failed: an attempt
        # without artifacts almost always means the run died mid-flight.
        if cur.get("artifacts_event") is None:
            cur["failed"] = True
            cur.setdefault("failure_flags", []).append("noArtifacts")
        else:
            cur["failed"] = _attempt_failed(ad)
        cur["index"] = len(run["attempts"]) + 1
        run["attempts"].append(cur)

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
                    "attempts": [],
                })
                run["events"].append(ev)
                ts = _safe_iso_to_ms(ev.get("ts"))
                etype = ev.get("type")

                # ── attempt-cycle bookkeeping ──────────────────────────
                # A fresh session.started always opens a new attempt. If
                # one is already open (no terminal session.ended seen),
                # close it first — the in-flight cycle is being abandoned.
                if etype == "session.started":
                    if rid in open_attempts:
                        _close_attempt(rid)
                    open_attempts[rid] = {
                        "started_ms": ts or 0,
                        "ended_ms": 0,
                        "artifacts_event": None,
                    }
                else:
                    # If we never saw a session.started for this attempt
                    # (truncated file, dropped event), open an implicit
                    # attempt on the first event of the runId so we still
                    # capture an artifacts_event.
                    if rid not in open_attempts:
                        # Detect a seq-reset boundary even without a
                        # session.started: if seq drops back to 1 after
                        # we already have at least one attempt, that's a
                        # new cycle. With no prior open attempt this is
                        # a no-op, but kept as the explicit signal for
                        # robustness against missing events.
                        open_attempts[rid] = {
                            "started_ms": ts or 0,
                            "ended_ms": 0,
                            "artifacts_event": None,
                        }

                # Capture artifacts on the open attempt.
                if etype == "trace.artifacts":
                    cur = open_attempts.get(rid)
                    if cur is not None:
                        cur["artifacts_event"] = ev
                        if ts and (not cur["ended_ms"] or ts > cur["ended_ms"]):
                            cur["ended_ms"] = ts

                # session.ended terminates the attempt; close it.
                if etype == "session.ended":
                    cur = open_attempts.get(rid)
                    if cur is not None:
                        if ts and (not cur["ended_ms"] or ts > cur["ended_ms"]):
                            cur["ended_ms"] = ts
                    _close_attempt(rid)

                # Track latest values on the run-level dict (preserves
                # pre-1.4.5 contract — these point at the FINAL cycle).
                # ``started_ms`` is the MIN session.started ts across all
                # cycles so the window covers the whole run; ``ended_ms``
                # is the MAX of any event ts, naturally capturing the
                # last cycle's session.ended.
                if etype == "session.started":
                    run["session_started"] = ev
                    if ts and (not run["started_ms"] or ts < run["started_ms"]):
                        run["started_ms"] = ts
                elif etype == "trace.metadata":
                    run["trace_metadata"] = ev
                elif etype == "trace.artifacts":
                    run["artifacts"] = ev
                if ts and (not run["started_ms"] or ts < run["started_ms"]):
                    if run["started_ms"] == 0:
                        run["started_ms"] = ts
                if ts and ts > run["ended_ms"]:
                    run["ended_ms"] = ts
    except OSError:
        return []

    # Close any attempt still open at EOF (no terminating session.ended).
    for rid in list(open_attempts.keys()):
        _close_attempt(rid)

    runs_list = list(runs.values())
    for run in runs_list:
        run["attempt_count"] = len(run["attempts"])
        run["had_failed_attempt"] = any(
            a.get("failed") for a in run["attempts"]
        )
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


def _fmt_tool_dur(ms: Optional[int]) -> str:
    """Humanize a tool-call duration. Sub-second keeps ms precision so the
    waterfall column can show the difference between a 12ms call and a
    900ms one. ``fmt_duration`` rounds anything <0.5s to "0s", which loses
    that signal.
    """
    if ms is None:
        return "?"
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60000:.1f}m"


def _collapse_ws(text: str) -> str:
    """Fold runs of whitespace (including newlines and the multi-space
    artifacts left by JSON pretty-printing) into a single space.
    """
    return re.sub(r"\s+", " ", text).strip()


def _format_args_inline(args: Any, *, max_total: int = 80) -> str:
    """Render ``args`` as a compact inline string for the tool waterfall.

    Each value is truncated to 50 chars; the whole rendering is capped at
    ``max_total`` chars. Whitespace runs (e.g. the indentation spaces left
    after ``json.dumps`` of a nested object) are collapsed to single spaces.
    """
    if args is None:
        return ""
    if not isinstance(args, dict):
        s = _collapse_ws(str(args))
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
        vs = _collapse_ws(vs)
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
        text = _collapse_ws(err)
    else:
        text = _collapse_ws(entry.get("result_text") or "")
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
    cron_runs: Optional[List[Dict[str, Any]]] = None,
    delivery_run_summary: Optional[Dict[str, Any]] = None,
    log_parsed: Optional[Dict[str, Any]] = None,
    cap: int = DEFAULT_TIMELINE_CAP,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Merge all sources into one chronological list.

    Each entry has ``ts_ms`` (epoch ms), ``source``, ``event_type``,
    ``summary``, and an optional ``correlation`` block (carried over from
    correlated log entries). Delivery events from cron and messaging-tool
    are folded in as ``source="delivery"`` so the timeline tells the full
    request → reply story without a separate Delivery section.

    Returns ``(timeline, stats)`` where ``stats`` carries:
      - ``skipped_no_ts``: records dropped because no parseable timestamp
      - ``dropped_middle``: events lost to the truncation cap (0 if the
        whole list fit)
      - ``truncated``: bool, True iff dropped_middle > 0
      - ``total_before_cap``: count before truncation, for honest reporting
    """
    out: List[Dict[str, Any]] = []
    skipped_no_ts = 0

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
            skipped_no_ts += 1
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
                skipped_no_ts += 1
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
            skipped_no_ts += 1
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

    # Cron run records — each line is a discrete delivery event.
    for c in (cron_runs or []):
        if not isinstance(c, dict):
            continue
        ts = c.get("ts")
        if not isinstance(ts, (int, float)) or ts <= 0:
            ts = _safe_iso_to_ms(c.get("ts"))
        if not ts:
            skipped_no_ts += 1
            continue
        action = c.get("action") or "?"
        status = c.get("status") or "?"
        ds = c.get("deliveryStatus") or "?"
        job_id = c.get("jobId") or "?"
        out.append({
            "ts_ms": int(ts),
            "source": "delivery",
            "event_type": "delivery",
            "summary": (
                f"cron {str(job_id)[:8]} action={action} "
                f"status={status} deliveryStatus={ds}"
            ),
            "delivery": {
                "kind": "cron",
                "jobId": job_id,
                "action": action,
                "status": status,
                "deliveryStatus": ds,
            },
        })

    # Messaging-tool send: a single summarized entry at the run end.
    if delivery_run_summary and delivery_run_summary.get("did_send"):
        ts = delivery_run_summary.get("ended_ms") or 0
        if ts:
            targets = delivery_run_summary.get("targets") or []
            text_count = delivery_run_summary.get("text_count") or 0
            out.append({
                "ts_ms": int(ts),
                "source": "delivery",
                "event_type": "delivery",
                "summary": (
                    f"messaging-tool send: targets={len(targets)} "
                    f"texts={text_count}"
                ),
                "delivery": {
                    "kind": "messaging_tool",
                    "targets": targets,
                    "text_count": text_count,
                },
            })

    # v1.4.4 task H: state transitions become first-class timeline entries.
    # We only fold the parsed transitions in (already window-bounded by
    # virtue of the correlated-log filter feeding this stage).
    if log_parsed:
        for tr in log_parsed.get("state_transitions") or []:
            ts = tr.get("ts_ms") or 0
            if not ts:
                skipped_no_ts += 1
                continue
            reason = (tr.get("reason") or "").strip()
            reason_s = f' reason="{reason}"' if reason else ""
            out.append({
                "ts_ms": int(ts),
                "source": "state",
                "event_type": "state",
                "summary": (
                    f"state {tr.get('prev')} → {tr.get('new')}"
                    f"{reason_s} qd={tr.get('queue_depth')}"
                ),
                "state": tr,
            })
        # v1.4.4 task I: applied/skipped config-reload events join the
        # timeline. Failed reloads also appear under health_signals so the
        # verdict still degrades; here they simply mark the moment.
        for rl in log_parsed.get("config_reloads") or []:
            ts = rl.get("ts_ms") or 0
            if not ts:
                skipped_no_ts += 1
                continue
            outcome = rl.get("outcome") or "?"
            if outcome == "applied":
                summary = (
                    f"config reload applied: "
                    f"{', '.join(rl.get('keys') or []) or '(none)'}"
                )
            else:
                summary = (
                    f"config reload SKIPPED (invalid): "
                    f"{(rl.get('reason') or '')[:120]}"
                )
            out.append({
                "ts_ms": int(ts),
                "source": "config",
                "event_type": "config_reload",
                "summary": summary,
                "config_reload": rl,
            })

    out.sort(key=lambda r: r["ts_ms"])
    total_before_cap = len(out)
    dropped_middle = 0
    if total_before_cap > cap:
        # Keep oldest 10% + newest 90% so context isn't lost on huge sessions.
        head = max(1, cap // 10)
        tail = cap - head
        dropped_middle = total_before_cap - head - tail
        out = out[:head] + out[-tail:]
    for entry in out:
        entry["ts_local"] = fmt_epoch_local(entry["ts_ms"])
    stats = {
        "skipped_no_ts": skipped_no_ts,
        "dropped_middle": dropped_middle,
        "truncated": dropped_middle > 0,
        "total_before_cap": total_before_cap,
    }
    return out, stats


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
                # v1.4.4 task A: capture broke + cacheRead + previousCacheRead
                # so we can render the full picture (lost-token magnitude).
                # Older runs may lack `observation` entirely — leave keys
                # absent rather than None when we have no signal at all.
                ctx["cache_broke"] = obs.get("broke")
                cr = obs.get("cacheRead")
                pcr = obs.get("previousCacheRead")
                if cr is not None:
                    ctx["cache_read_observed"] = cr
                if pcr is not None:
                    ctx["cache_read_previous"] = pcr
                if isinstance(cr, (int, float)) and isinstance(
                        pcr, (int, float)):
                    # Lost = previous − current, never negative.
                    ctx["cache_read_lost"] = max(0, int(pcr) - int(cr))
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


_LOG_DECISION_MARKERS = (
    "model_fallback_decision",
    "harness_select",
    "context_overflow",
    "compaction_triggered",
)


def _log_marker_signals(
    correlated_logs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Pull out structured decision markers from app logs.

    These are routed into ``health_signals`` (kind ``log_decision``) instead
    of standing alone — there are too few of them to warrant their own
    section, and they're really just another flavor of operational warning.
    The ``model_select`` entries from ``trace.metadata`` were dropped because
    they duplicate the model identity already shown in Session Overview.
    """
    out: List[Dict[str, Any]] = []
    for rec in correlated_logs:
        text = parse_log_msg(rec)
        if not text:
            continue
        if not any(m in text for m in _LOG_DECISION_MARKERS):
            continue
        out.append({
            "kind": "log_decision",
            "ts_ms": _log_ts_ms(rec),
            "subsystem": get_log_subsystem(rec),
            "summary": text[:200],
        })
    return out


LONG_TOOL_CALL_THRESHOLD_MS = 60_000

# v1.4.4 task E: a queue wait above this threshold gets a WARN.
SLOW_QUEUE_WAIT_MS = 2_000

_FAILED_DELIVERY_STATUSES = {"failed", "error", "errored", "undelivered"}


# ── v1.4.13 Findings: deterministic severity ranking ──────────────────────
#
# The Findings section (rendered FIRST, before Session Overview) re-surfaces
# the same problem signals already computed by ``_health_signals`` so the
# reader sees the worst issues without scrolling. It is OBJECTIVE only —
# it never invents a root cause, never says "likely"/"probably"/"because",
# never recommends a next step. Each line is a deterministic restatement
# of fields the signal already carries (ts, ids, finalStatus, flags,
# tool args, error text quoted verbatim). The same ordering key is also
# applied to the problem-signals list inside Correlated Logs & Signals so
# the detail list reads in the same order.
#
# SIGNAL_SEVERITY maps each known signal kind → (class, rank).
#   class ∈ {"fail", "warn"} mirrors the verdict-driving call the renderer
#     already makes for that kind in Correlated Logs & Signals (.fail() vs
#     .warn()). For ``retried_after_failure`` the class is computed at
#     ranking time from ``final_failed`` — both shapes appear in real data.
#   rank is an int; higher means more severe within its class. Used as
#     the secondary sort key so e.g. an artifact abort outranks a stalled
#     log line even though both are FAIL-class.
# The ordering key is (class_order desc, rank desc, ts asc, kind asc).
# Stable, total, traceable. Unknown kinds default to ("warn", 0) so a new
# signal added without updating this table still ranks after everything
# we've explicitly catalogued.
FINDINGS_TOP_N = 10

SIGNAL_SEVERITY: Dict[str, Tuple[str, int]] = {
    # FAIL class — drive verdict to FAIL.
    "trajectory_artifact": ("fail", 100),
    "cron_delivery_failed": ("fail", 90),
    # WARN class — drive verdict to WARN.
    "retried_after_failure": ("warn", 80),  # may upgrade to fail (see _signal_severity)
    "tool_call_leak": ("warn", 70),
    "items_incomplete": ("warn", 65),
    "prompt_cache_broke": ("warn", 60),
    "long_tool_call": ("warn", 55),
    "child_task_failed": ("warn", 50),
    "log_stall": ("warn", 45),
    "context_precheck_overflow": ("warn", 40),
    "state_transition_abnormal": ("warn", 38),
    "queue_wait_slow": ("warn", 35),
    "config_reload_failed": ("warn", 32),
    "log_decision": ("warn", 25),
    "last_tool_error": ("warn", 22),
    "gateway_pid_change": ("warn", 20),
}

_CLASS_ORDER = {"fail": 2, "warn": 1}


def _signal_severity(sig: Dict[str, Any]) -> Tuple[str, int]:
    """Look up (class, rank) for a signal, with the only kind-specific
    promotion: ``retried_after_failure`` upgrades from warn → fail when its
    final attempt also failed (matches the render-time .fail() call).
    Unknown kinds default to ("warn", 0) so they sort below catalogued
    entries but still appear.
    """
    kind = sig.get("kind") or ""
    cls, rank = SIGNAL_SEVERITY.get(kind, ("warn", 0))
    if kind == "retried_after_failure" and sig.get("final_failed"):
        cls = "fail"
    return cls, rank


def _signal_sort_key(sig: Dict[str, Any]) -> Tuple[int, int, int, str]:
    """Deterministic sort key. Negate class/rank so Python's ascending
    sort yields the desired (severity desc, rank desc, ts asc, kind asc).
    """
    cls, rank = _signal_severity(sig)
    ts = int(sig.get("ts_ms") or 0)
    return (-_CLASS_ORDER.get(cls, 0), -rank, ts, sig.get("kind") or "")


def _render_finding_summary(sig: Dict[str, Any]) -> str:
    """Build the OBJECTIVE one-line summary for a single signal.

    Reads only fields that already exist on the signal — never invents a
    cause, never speculates, never recommends. Forbidden words
    ("likely", "probably", "because", "recommend", ...) must not appear
    here. ``test_findings_lines_have_no_forbidden_words`` enforces this.
    """
    kind = sig.get("kind") or "?"
    if kind == "trajectory_artifact":
        rid = (sig.get("runId") or "?")[:8]
        flags = sig.get("flags") or []
        parts = [f"run {rid}"]
        if sig.get("final_status"):
            parts.append(f"finalStatus={sig.get('final_status')}")
        for flag in flags:
            parts.append(f"{flag}=true")
        if sig.get("prompt_error_source"):
            parts.append(f"promptErrorSource={sig.get('prompt_error_source')}")
        return "artifact " + ", ".join(parts)
    if kind == "cron_delivery_failed":
        return (
            f"cron_delivery jobId={sig.get('jobId')} "
            f"action={sig.get('action')} status={sig.get('status')} "
            f"deliveryStatus={sig.get('deliveryStatus')}"
        )
    if kind == "retried_after_failure":
        rid = (sig.get("runId") or "?")[:8]
        return (
            f"retried_after_failure run {rid} "
            f"attempts={sig.get('attempt_count')} "
            f"failed={sig.get('failed_count')} "
            f"final_status={sig.get('final_status')} "
            f"final_failed={sig.get('final_failed')}"
        )
    if kind == "tool_call_leak":
        rid = (sig.get("runId") or "?")[:8]
        return (
            f"tool_call_leak run {rid} active={sig.get('active')} "
            f"started={sig.get('started')} completed={sig.get('completed')}"
        )
    if kind == "items_incomplete":
        rid = (sig.get("runId") or "?")[:8]
        return (
            f"items_incomplete run {rid} started={sig.get('started')} "
            f"completed={sig.get('completed')} dropped={sig.get('dropped')}"
        )
    if kind == "prompt_cache_broke":
        rid = (sig.get("runId") or "?")[:8]
        return (
            f"prompt_cache_broke run {rid} "
            f"previous_cache_read={sig.get('previous_cache_read')} "
            f"cache_read={sig.get('cache_read')} "
            f"lost_tokens={sig.get('lost_tokens')}"
        )
    if kind == "long_tool_call":
        name = sig.get("name") or "?"
        args_s = sig.get("args_summary") or ""
        if args_s.startswith("{") and args_s.endswith("}"):
            args_s = "(" + args_s[1:-1] + ")"
        elif args_s:
            args_s = f"({args_s})"
        dur_ms = sig.get("duration_ms") or 0
        dur_s = fmt_duration(dur_ms / 1000)
        snippet = (sig.get("snippet") or "").strip()
        is_error = bool(sig.get("is_error"))
        line = f"long_tool_call {name}{args_s} duration={dur_s}"
        if is_error:
            line += f' is_error=true result="{snippet}"' if snippet else " is_error=true"
        elif snippet:
            line += f' result="{snippet}"'
        return line
    if kind == "child_task_failed":
        return (
            f"child_task_failed task_id={(sig.get('task_id') or '?')[:12]} "
            f"agent={sig.get('agent_id')} runtime={sig.get('runtime')} "
            f'error="{(sig.get("error") or "")[:120]}"'
        )
    if kind == "log_stall":
        return f'log_stall summary="{(sig.get("summary") or "")[:160]}"'
    if kind == "context_precheck_overflow":
        return (
            f"context_precheck_overflow route={sig.get('route')} "
            f"estimated_prompt_tokens={sig.get('estimated_prompt_tokens')} "
            f"provider={sig.get('provider')}"
        )
    if kind == "state_transition_abnormal":
        reason = sig.get("reason") or ""
        reason_s = f' reason="{reason}"' if reason else ""
        return (
            f"state_transition prev={sig.get('prev')} "
            f"new={sig.get('new')}{reason_s}"
        )
    if kind == "queue_wait_slow":
        return (
            f"queue_wait_slow wait_ms={sig.get('wait_ms')} "
            f"queue_size={sig.get('queue_size')} "
            f"lane={(sig.get('lane') or '?')[:80]}"
        )
    if kind == "config_reload_failed":
        return (
            f'config_reload_failed reason="{(sig.get("reason") or "")[:160]}"'
        )
    if kind == "log_decision":
        return f'log_decision summary="{(sig.get("summary") or "")[:160]}"'
    if kind == "last_tool_error":
        rid = (sig.get("runId") or "?")[:8]
        return (
            f'last_tool_error run {rid} '
            f'summary="{(sig.get("summary") or "")[:160]}"'
        )
    if kind == "gateway_pid_change":
        return f"gateway_pid_change pids={sig.get('pids')}"
    # Unknown kind: emit a stable, lossless restatement so future signal
    # types still surface even before this table is updated.
    return f"{kind} {sig}"


def _build_findings(
    problem_signals: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    """Return ``(ranked, more_count)``.

    ``ranked`` is the top ``FINDINGS_TOP_N`` problem signals as objective
    finding dicts ``{severity, kind, ts_ms, summary, ref}``, sorted by
    ``_signal_sort_key``. ``more_count`` is the number of additional
    problem signals that did not fit (0 when ≤ TOP_N).
    """
    sorted_sigs = sorted(problem_signals, key=_signal_sort_key)
    findings: List[Dict[str, Any]] = []
    for sig in sorted_sigs[:FINDINGS_TOP_N]:
        cls, _rank = _signal_severity(sig)
        findings.append({
            "severity": cls,
            "kind": sig.get("kind") or "?",
            "ts_ms": int(sig.get("ts_ms") or 0),
            "summary": _render_finding_summary(sig),
            "ref": "Correlated Logs & Signals",
        })
    more = max(0, len(sorted_sigs) - FINDINGS_TOP_N)
    return findings, more


# ── log-line parsers (v1.4.4) ──────────────────────────────────────────────
#
# All of these run over already-correlated, window-bounded log records, so
# they inherit window/strict/mask handling for free. Each parser is a tiny
# regex over the rendered text from ``parse_log_msg`` — values are echoed
# into structured dicts so JSON consumers see typed numbers, not strings.

# E. lane queue / concurrency
_LANE_DEQUEUE_RE = re.compile(
    r"lane dequeue: lane=(?P<lane>\S+) waitMs=(?P<wait>\d+) "
    r"queueSize=(?P<qs>\d+)"
)
_LANE_ENQUEUE_RE = re.compile(
    r"lane enqueue: lane=(?P<lane>\S+) queueSize=(?P<qs>\d+)"
)
_RUN_REGISTERED_RE = re.compile(
    r"run registered: sessionId=(?P<sid>[0-9a-fA-F-]+) "
    r"totalActive=(?P<active>\d+)"
)
_SESSION_STATE_RE = re.compile(
    r"session state: sessionId=(?P<sid>[0-9a-fA-F-]+) "
    r"sessionKey=(?P<sk>\S+) prev=(?P<prev>\S+) new=(?P<new>\S+) "
    r'reason="(?P<reason>[^"]*)" queueDepth=(?P<qd>\d+)'
)

# F. authoritative run duration
_RUN_PROMPT_END_RE = re.compile(
    r"embedded run prompt end: runId=(?P<rid>[0-9a-fA-F-]+) "
    r"sessionId=(?P<sid>[0-9a-fA-F-]+) durationMs=(?P<dur>\d+)"
)

# G. context-overflow precheck
_CONTEXT_PRECHECK_RE = re.compile(
    r"\[context-overflow-precheck\] pre-prompt check sessionKey=\S+ "
    r"provider=(?P<prov>\S+) route=(?P<route>\S+) "
    r"estimatedPromptTokens=(?P<est>\d+)"
)

# I. config hot reload
_CONFIG_RELOAD_APPLIED_RE = re.compile(
    r"config hot reload applied \((?P<keys>[^)]*)\)"
)
_CONFIG_RELOAD_SKIPPED_RE = re.compile(
    r"config reload skipped \(invalid config\): (?P<reason>.+)"
)

# Routes from precheck that indicate the run had to compact / would overflow.
_COMPACTION_ROUTES = {"compact", "compacting", "overflow", "overflowing"}


def _parse_log_lines(
    correlated_logs: List[Dict[str, Any]],
    *,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Pull queue/run/precheck/state/reload data from already-correlated logs.

    Single linear pass — every record is parsed once and routed by regex
    into the appropriate bucket. The returned dict is keyed by parser name
    so callers can pluck individual buckets without re-walking the list.

    Optional ``session_id`` filters the run-prompt-end and run-registered
    matches to ones whose ``sessionId`` field equals the seed. Those messages
    can carry sibling sessionIds in multi-session contention reports, so we
    enforce a strict equality at parse time.
    """
    queue_events: List[Dict[str, Any]] = []
    run_registered: List[Dict[str, Any]] = []
    state_transitions: List[Dict[str, Any]] = []
    run_durations: List[Dict[str, Any]] = []
    prechecks: List[Dict[str, Any]] = []
    reloads: List[Dict[str, Any]] = []

    for rec in correlated_logs:
        text = parse_log_msg(rec) or ""
        if not text:
            continue
        ts = _log_ts_ms(rec)
        sub = get_log_subsystem(rec) or ""

        m = _LANE_DEQUEUE_RE.search(text)
        if m:
            queue_events.append({
                "kind": "dequeue",
                "ts_ms": ts,
                "lane": m.group("lane"),
                "wait_ms": int(m.group("wait")),
                "queue_size": int(m.group("qs")),
            })
            continue

        m = _LANE_ENQUEUE_RE.search(text)
        if m:
            queue_events.append({
                "kind": "enqueue",
                "ts_ms": ts,
                "lane": m.group("lane"),
                "queue_size": int(m.group("qs")),
            })
            continue

        m = _RUN_REGISTERED_RE.search(text)
        if m:
            sid = m.group("sid")
            if session_id and sid != session_id:
                continue
            run_registered.append({
                "ts_ms": ts,
                "session_id": sid,
                "total_active": int(m.group("active")),
            })
            continue

        m = _SESSION_STATE_RE.search(text)
        if m:
            sid = m.group("sid")
            if session_id and sid != session_id:
                continue
            state_transitions.append({
                "ts_ms": ts,
                "session_id": sid,
                "session_key": m.group("sk"),
                "prev": m.group("prev"),
                "new": m.group("new"),
                "reason": m.group("reason"),
                "queue_depth": int(m.group("qd")),
            })
            continue

        m = _RUN_PROMPT_END_RE.search(text)
        if m:
            sid = m.group("sid")
            if session_id and sid != session_id:
                continue
            run_durations.append({
                "ts_ms": ts,
                "run_id": m.group("rid"),
                "session_id": sid,
                "duration_ms": int(m.group("dur")),
            })
            continue

        m = _CONTEXT_PRECHECK_RE.search(text)
        if m:
            prechecks.append({
                "ts_ms": ts,
                "provider": m.group("prov"),
                "route": m.group("route"),
                "estimated_prompt_tokens": int(m.group("est")),
            })
            continue

        m = _CONFIG_RELOAD_APPLIED_RE.search(text)
        if m:
            keys = [k.strip() for k in m.group("keys").split(",") if k.strip()]
            reloads.append({
                "ts_ms": ts,
                "subsystem": sub,
                "outcome": "applied",
                "keys": keys,
            })
            continue

        m = _CONFIG_RELOAD_SKIPPED_RE.search(text)
        if m:
            reloads.append({
                "ts_ms": ts,
                "subsystem": sub,
                "outcome": "skipped",
                "reason": m.group("reason").strip(),
            })
            continue

    # Compute derived queue-summary so renderers don't have to.
    queue_summary: Optional[Dict[str, Any]] = None
    dequeues = [q for q in queue_events if q["kind"] == "dequeue"]
    if dequeues or run_registered:
        max_wait = max((q["wait_ms"] for q in dequeues), default=0)
        max_queue = max(
            (q["queue_size"] for q in queue_events), default=0,
        )
        max_active = max(
            (r["total_active"] for r in run_registered), default=0,
        )
        queue_summary = {
            "dequeues": len(dequeues),
            "enqueues": sum(
                1 for q in queue_events if q["kind"] == "enqueue"
            ),
            "max_wait_ms": max_wait,
            "max_queue_size": max_queue,
            "max_concurrent_runs": max_active,
        }

    return {
        "queue_events": queue_events,
        "queue_summary": queue_summary,
        "run_registered": run_registered,
        "state_transitions": state_transitions,
        "run_durations": run_durations,
        "context_prechecks": prechecks,
        "config_reloads": reloads,
    }


# C. timeout/abort flag → human "where it hung" line
_FLAG_TO_HUMAN = [
    ("idle_timed_out", "went idle (no progress within idle timeout)"),
    ("timed_out_during_tool_execution", "hung during tool execution"),
    ("timed_out_during_compaction", "hung during context compaction"),
    ("timed_out", "exceeded turn timeout"),
    ("external_abort", "cancelled externally"),
    ("aborted", "aborted (internal)"),
]


def _classify_timeout_flags(
    artifacts_data: Dict[str, Any],
) -> List[str]:
    """Translate raw artifact flags into ordered human messages.

    Order matters: ``idle_timed_out`` / tool / compaction subsume the bare
    ``timed_out`` flag, so we emit the most specific reason first and only
    fall through to the generic one when no specialization applies.
    """
    out: List[str] = []
    flags = {
        "idle_timed_out": bool(artifacts_data.get("idleTimedOut")),
        "timed_out_during_tool_execution": bool(
            artifacts_data.get("timedOutDuringToolExecution")),
        "timed_out_during_compaction": bool(
            artifacts_data.get("timedOutDuringCompaction")),
        "timed_out": bool(artifacts_data.get("timedOut")),
        "external_abort": bool(artifacts_data.get("externalAbort")),
        "aborted": bool(artifacts_data.get("aborted")),
    }
    # Suppress the bare "timed_out" line when a more specific flag fired —
    # otherwise we'd say both "exceeded turn timeout" AND "hung during tool
    # execution" for the same event.
    if (flags["idle_timed_out"] or flags["timed_out_during_tool_execution"]
            or flags["timed_out_during_compaction"]):
        flags["timed_out"] = False
    # Same for "aborted" when externalAbort took over.
    if flags["external_abort"]:
        flags["aborted"] = False
    for key, msg in _FLAG_TO_HUMAN:
        if flags[key]:
            out.append(msg)
    pes = artifacts_data.get("promptErrorSource")
    if pes:
        out.append(f"prompt error source: {pes}")
    return out


def _classify_attempt_failure(attempt: Dict[str, Any]) -> str:
    """One-line human reason for why an attempt failed.

    Reuses the same vocabulary as ``_classify_timeout_flags`` (idle timeout
    → "went idle", tool-execution timeout → "hung during tool execution",
    etc.). When no classified reason applies, falls back to the raw
    ``finalStatus`` string. Returns ``"failed"`` as a last resort.
    """
    ev = attempt.get("artifacts_event") or {}
    ad = ev.get("data") or {}
    if not ad:
        # An attempt with no trace.artifacts at all (run died before
        # writing). Surface that explicitly so the user knows it's not
        # a classified timeout — the cycle simply never finished.
        return "no trace.artifacts written (attempt died mid-flight)"
    reasons = _classify_timeout_flags(ad)
    if reasons:
        return reasons[0]
    fs = (ad.get("finalStatus") or "").lower()
    if fs and fs not in ("success", "ok", "completed"):
        return f"finalStatus={fs}"
    return "failed"


def _health_signals(
    runs: List[Dict[str, Any]],
    correlated_logs: List[Dict[str, Any]],
    *,
    waterfall: Optional[List[Dict[str, Any]]] = None,
    children: Optional[List[Dict[str, Any]]] = None,
    cron_runs: Optional[List[Dict[str, Any]]] = None,
    log_decisions: Optional[List[Dict[str, Any]]] = None,
    log_parsed: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []
    # v1.4.5: when a runId has multiple attempt-cycles and any earlier
    # cycle failed, surface a `retried_after_failure` signal. This is
    # WARN-level when the FINAL attempt succeeded (the run recovered)
    # and FAIL-level when the final attempt also failed (caller decides
    # via verdict). Pre-1.4.5 the failed cycle was overwritten by the
    # successful one and never surfaced.
    for run in runs:
        attempts = run.get("attempts") or []
        if len(attempts) <= 1:
            continue
        any_failed = any(a.get("failed") for a in attempts)
        if not any_failed:
            continue
        final_attempt = attempts[-1]
        final_failed = bool(final_attempt.get("failed"))
        per_attempt_lines: List[str] = []
        for a in attempts:
            i = a.get("index", "?")
            sm = a.get("started_ms") or 0
            em = a.get("ended_ms") or sm
            dur_ms = max(0, em - sm) if sm and em else 0
            dur_s = (
                fmt_duration(dur_ms / 1000) if dur_ms else "?"
            )
            if a.get("failed"):
                reason = _classify_attempt_failure(a)
                per_attempt_lines.append(
                    f"#{i} {reason} ({dur_s})"
                )
            else:
                fs = (
                    (a.get("artifacts_event") or {}).get("data") or {}
                ).get("finalStatus") or "ok"
                per_attempt_lines.append(f"#{i} {fs} ({dur_s})")
        signals.append({
            "kind": "retried_after_failure",
            "runId": run["runId"],
            "attempt_count": len(attempts),
            "failed_count": sum(1 for a in attempts if a.get("failed")),
            "final_status": (
                (final_attempt.get("artifacts_event") or {}).get("data") or {}
            ).get("finalStatus"),
            "final_failed": final_failed,
            "per_attempt": per_attempt_lines,
            "ts_ms": run.get("ended_ms") or run.get("started_ms") or 0,
        })
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
        if flags or ad.get("promptErrorSource"):
            # v1.4.4 task C: alongside the raw flag list we attach a human
            # "where it hung" summary so renderers can drop the cryptic
            # camelCase boolean dump.
            signals.append({
                "kind": "trajectory_artifact",
                "runId": run["runId"],
                "flags": flags,
                "human_summary": _classify_timeout_flags(ad),
                "final_status": ad.get("finalStatus"),
                "prompt_error_source": ad.get("promptErrorSource"),
                "ts_ms": run.get("ended_ms") or run.get("started_ms") or 0,
            })
        il = ad.get("itemLifecycle") or {}
        if isinstance(il, dict):
            started = il.get("startedCount") or 0
            completed = il.get("completedCount") or 0
            active = il.get("activeCount") or 0
            if active:
                signals.append({
                    "kind": "tool_call_leak",
                    "runId": run["runId"],
                    "active": active,
                    "started": started,
                    "completed": completed,
                    "ts_ms": run.get("ended_ms") or 0,
                })
            elif started and completed < started:
                # v1.4.4 task B: items started but never completed and no
                # active leak — they were dropped or errored silently.
                signals.append({
                    "kind": "items_incomplete",
                    "runId": run["runId"],
                    "started": started,
                    "completed": completed,
                    "dropped": started - completed,
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
    # Long-running tool calls. v1.4.10 enriches each signal with the call's
    # arguments + a result/error snippet so the rendered line tells the user
    # WHICH tool invocation hung (not just "cron 2.4m"). The waterfall has
    # already applied masking when --mask was set, so we just forward the
    # already-sanitized values; downstream renderers don't need to know.
    if waterfall:
        long_calls = sorted(
            [
                w for w in waterfall
                if (w.get("duration_ms") or 0) >= LONG_TOOL_CALL_THRESHOLD_MS
            ],
            key=lambda w: w.get("duration_ms") or 0, reverse=True,
        )
        for lc in long_calls[:5]:
            args_summary = _format_args_inline(lc.get("args"), max_total=80)
            error_text = lc.get("error_text") or ""
            result_text = lc.get("result_text") or ""
            # Pick the snippet that explains the most: error first, then a
            # tail of the result. Truncated to keep the rendered line short.
            snippet = ""
            if lc.get("is_error"):
                snippet = (error_text or result_text or "").replace(
                    "\n", " ").strip()
            elif result_text:
                snippet = result_text.replace("\n", " ").strip()
            if len(snippet) > 120:
                snippet = snippet[:119] + "…"
            signals.append({
                "kind": "long_tool_call",
                "name": lc.get("name"),
                "duration_ms": lc.get("duration_ms"),
                "is_error": bool(lc.get("is_error")),
                "ts_ms": lc.get("end_ms") or lc.get("start_ms") or 0,
                # New v1.4.10 fields. Compact, inline-renderable strings;
                # raw args/result_text/error_text already on the waterfall
                # entry for JSON consumers needing the full payload.
                "args_summary": args_summary,
                "snippet": snippet,
                "callId": lc.get("callId"),
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
    # Failed cron deliveries — surface here so verdict logic still sees them
    # after the standalone Delivery section was removed.
    if cron_runs:
        for c in cron_runs:
            if not isinstance(c, dict):
                continue
            ds = (c.get("deliveryStatus") or "").lower()
            status = (c.get("status") or "").lower()
            if ds in _FAILED_DELIVERY_STATUSES or status in (
                "failed", "error", "errored",
            ):
                ts = c.get("ts")
                if not isinstance(ts, (int, float)):
                    ts = _safe_iso_to_ms(c.get("ts"))
                signals.append({
                    "kind": "cron_delivery_failed",
                    "ts_ms": int(ts or 0),
                    "jobId": c.get("jobId"),
                    "action": c.get("action"),
                    "status": c.get("status"),
                    "deliveryStatus": c.get("deliveryStatus"),
                    "summary": c.get("summary"),
                })
    # Log-marker decisions (model_fallback / harness_select / context_overflow
    # / compaction_triggered). These were the standalone "Model Decisions"
    # section in v1.4.x; they live here as WARN-level operational signals.
    if log_decisions:
        signals.extend(log_decisions)
    # v1.4.4 task A: per-run prompt-cache breakage signals (broke==true) and
    # cache hit notes (broke==false with non-zero cacheRead). The data lives
    # on runtime_context already; we surface a health entry per run so it
    # also drives the verdict.
    for run in runs:
        artifacts = run.get("artifacts") or {}
        ad = artifacts.get("data") or {}
        if not isinstance(ad, dict):
            continue
        pc = ad.get("promptCache") or {}
        if not isinstance(pc, dict):
            continue
        obs = pc.get("observation")
        if not isinstance(obs, dict):
            continue
        cr = obs.get("cacheRead")
        pcr = obs.get("previousCacheRead")
        if obs.get("broke"):
            lost = (
                max(0, int(pcr) - int(cr))
                if isinstance(cr, (int, float))
                and isinstance(pcr, (int, float))
                else None
            )
            signals.append({
                "kind": "prompt_cache_broke",
                "runId": run["runId"],
                "cache_read": cr,
                "previous_cache_read": pcr,
                "lost_tokens": lost,
                "ts_ms": run.get("ended_ms") or 0,
            })
    # v1.4.4 task E: queue-latency signal — emit a WARN when any single
    # dequeue waited longer than SLOW_QUEUE_WAIT_MS.
    if log_parsed:
        for q in log_parsed.get("queue_events") or []:
            if q.get("kind") != "dequeue":
                continue
            wait_ms = q.get("wait_ms") or 0
            if wait_ms < SLOW_QUEUE_WAIT_MS:
                continue
            signals.append({
                "kind": "queue_wait_slow",
                "ts_ms": q.get("ts_ms") or 0,
                "lane": q.get("lane"),
                "wait_ms": wait_ms,
                "queue_size": q.get("queue_size"),
            })
        # v1.4.4 task G: WARN when a precheck routed to compaction/overflow.
        for pc_ in log_parsed.get("context_prechecks") or []:
            route = (pc_.get("route") or "").lower()
            if route in _COMPACTION_ROUTES:
                signals.append({
                    "kind": "context_precheck_overflow",
                    "ts_ms": pc_.get("ts_ms") or 0,
                    "route": pc_.get("route"),
                    "estimated_prompt_tokens":
                        pc_.get("estimated_prompt_tokens"),
                    "provider": pc_.get("provider"),
                })
        # v1.4.4 task H: an abnormal terminal state (aborted/error/failed)
        # in the state-transition stream surfaces as a WARN.
        for tr in log_parsed.get("state_transitions") or []:
            new = (tr.get("new") or "").lower()
            if new in ("aborted", "error", "errored", "failed"):
                signals.append({
                    "kind": "state_transition_abnormal",
                    "ts_ms": tr.get("ts_ms") or 0,
                    "prev": tr.get("prev"),
                    "new": tr.get("new"),
                    "reason": tr.get("reason"),
                    "session_id": tr.get("session_id"),
                })
        # v1.4.4 task I: failed config reloads are WARN; successful reloads
        # are info — the WARN drives the verdict, the OK shows up in the
        # timeline.
        for rl in log_parsed.get("config_reloads") or []:
            if rl.get("outcome") == "skipped":
                signals.append({
                    "kind": "config_reload_failed",
                    "ts_ms": rl.get("ts_ms") or 0,
                    "subsystem": rl.get("subsystem"),
                    "reason": rl.get("reason"),
                })
    return signals


# ── positive health signals (v1.4.10) ──────────────────────────────────────
#
# `_health_signals` only surfaces problems (warn/fail). For a healthy run that
# leaves the section empty, the user has no positive confirmation that the
# tools, lifecycle, cache, outcome, or delivery are actually fine — they just
# see a single "no abort/timeout/..." line. The positives below provide a
# concise per-aspect "✓" so a clean run is visible, not just an empty section.
#
# These signals are ADDITIVE — they never change the verdict. They are also
# concise summary lines (counts, not per-call detail) so the section stays
# scannable.


def _positive_health_signals(
    runs: List[Dict[str, Any]],
    *,
    waterfall: Optional[List[Dict[str, Any]]],
    problem_signals: List[Dict[str, Any]],
    delivery_run_summary: Optional[Dict[str, Any]],
    cron_runs: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Return a list of OK-level health entries for aspects that look fine.

    Each entry is ``{kind: "ok_<aspect>", summary: <text>}``. The renderer
    routes them to ``s_health.ok``. Aspects covered:

      ``ok_tools``      no tool errors across the waterfall
      ``ok_lifecycle``  itemLifecycle started==completed and active==0
      ``ok_cache``      no ``prompt_cache_broke`` problem signal
      ``ok_outcome``    no abort/timeout/stall problem signal
      ``ok_delivery``   messaging-tool sent OR no cron failures
    """
    out: List[Dict[str, Any]] = []

    # Tools: count calls + errors. We emit even when there are zero calls,
    # so the user sees "no tool calls" affirmatively rather than nothing.
    if waterfall is not None:
        total = len(waterfall)
        errs = sum(1 for w in waterfall if w.get("is_error"))
        if total == 0:
            out.append({
                "kind": "ok_tools",
                "summary": "no tool calls in this run",
            })
        elif errs == 0:
            out.append({
                "kind": "ok_tools",
                "summary": f"{total} tool calls, 0 errors",
            })
        else:
            ok_count = total - errs
            out.append({
                "kind": "ok_tools",
                "summary": f"{ok_count}/{total} tool calls ok",
            })

    # Lifecycle: aggregate across selected runs. Only emit a positive when
    # every run has started == completed and active == 0; if any run shows
    # a leak/incomplete, the existing problem signal already speaks.
    if runs:
        all_clean = True
        agg_started = 0
        agg_completed = 0
        any_data = False
        for run in runs:
            ad = (run.get("artifacts") or {}).get("data") or {}
            il = ad.get("itemLifecycle") or {}
            if not isinstance(il, dict):
                continue
            started = il.get("startedCount") or 0
            completed = il.get("completedCount") or 0
            active = il.get("activeCount") or 0
            if not (started or completed or active):
                continue
            any_data = True
            agg_started += started
            agg_completed += completed
            if active or completed != started:
                all_clean = False
        if any_data and all_clean:
            out.append({
                "kind": "ok_lifecycle",
                "summary": (
                    f"no leaks (active=0), all {agg_completed} items "
                    f"completed"
                ),
            })

    # Cache: positive when no broke signal AND at least one run reported a
    # promptCache observation. We don't want to crow "cache healthy" when
    # we have no observation at all (older runs, missing data) — that
    # would be misleading.
    has_cache_break = any(
        s.get("kind") == "prompt_cache_broke" for s in problem_signals
    )
    has_observation = False
    for run in runs or []:
        pc = ((run.get("artifacts") or {}).get("data") or {}).get(
            "promptCache") or {}
        if isinstance(pc, dict) and isinstance(
                pc.get("observation"), dict):
            has_observation = True
            break
    if has_observation and not has_cache_break:
        out.append({
            "kind": "ok_cache",
            "summary": "cache healthy (no breaks)",
        })

    # Outcome: emit when none of the trajectory-level problem flags fired.
    bad_outcome_kinds = {
        "trajectory_artifact",
        "log_stall",
        "tool_call_leak",
        "items_incomplete",
        "retried_after_failure",
        "state_transition_abnormal",
    }
    has_bad_outcome = any(
        s.get("kind") in bad_outcome_kinds for s in problem_signals
    )
    if runs and not has_bad_outcome:
        out.append({
            "kind": "ok_outcome",
            "summary": "no aborts/timeouts/stalls",
        })

    # Delivery: prefer to confirm a successful messaging-tool send when the
    # run actually delivered. Otherwise (cron run with no failures) we
    # confirm the absence of failures. If we have neither signal nor data,
    # we say nothing — silence is correct here.
    has_delivery_failure = any(
        s.get("kind") == "cron_delivery_failed" for s in problem_signals
    )
    if delivery_run_summary and delivery_run_summary.get("did_send") \
            and not has_delivery_failure:
        targets = delivery_run_summary.get("targets") or []
        text_count = delivery_run_summary.get("text_count") or 0
        out.append({
            "kind": "ok_delivery",
            "summary": (
                f"delivered ok ({len(targets)} targets, "
                f"{text_count} texts)"
            ),
        })
    elif cron_runs and not has_delivery_failure:
        # When we have cron records but no failure signal, confirm.
        out.append({
            "kind": "ok_delivery",
            "summary": (
                f"delivered ok ({len(cron_runs)} cron records, "
                "no failures)"
            ),
        })

    return out


# ── model-call extraction (with per-call duration) ────────────────────────


# OpenClaw injects two kinds of assistant turns into the transcript that are
# NOT real LLM inferences — they're transcript-only synthetic messages used
# for delivery mirroring and gateway-side message injection. Excluding them
# from Model Calls keeps the section honest about real inference activity.
# Naming aligned with OpenClaw's own constant
# (TRANSCRIPT_ONLY_OPENCLAW_MODELS in dist/compaction-successor-transcript-*.js
# and TRANSCRIPT_ONLY_OPENCLAW_ASSISTANT_MODELS in dist/selection-*.js); see
# also docs/reference/transcript-hygiene.md ("Replay filters OpenClaw
# delivery-mirror and gateway-injected assistant turns.").
_TRANSCRIPT_ONLY_OPENCLAW_MODELS = frozenset({
    "delivery-mirror", "gateway-injected",
})


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
        # Skip OpenClaw transcript-only injected turns BEFORE updating
        # last_ts: these are not real model calls (per OpenClaw's own
        # judgement) and they are not real user/toolResult/assistant
        # boundary events for duration purposes — letting them update
        # last_ts would skew the next real call's duration_ms downward.
        # Treat them as if they were not in the stream at all.
        if (msg.get("provider") == "openclaw"
                and msg.get("model") in _TRANSCRIPT_ONLY_OPENCLAW_MODELS):
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
    if first_error:
        out["first_error"] = first_error
    if first_warn:
        out["first_warn"] = first_warn
    if first_stall:
        out["first_stall"] = first_stall
    return out


# ── timeline middle-event sampling (v1.4.10) ───────────────────────────────


def _is_interesting_timeline_event(entry: Dict[str, Any]) -> bool:
    """Whether a timeline entry is interesting enough to prioritize over an
    evenly-spaced filler sample. Any log error/warn, state transition,
    delivery event, model.completed, or tool-call boundary qualifies.
    """
    et = entry.get("event_type") or ""
    src = entry.get("source") or ""
    if et.startswith("log:ERROR") or et.startswith("log:WARN"):
        return True
    if src == "state":
        return True
    if src == "delivery":
        return True
    if et == "model.completed":
        return True
    summary = entry.get("summary") or ""
    if "toolCall" in summary or "toolResult" in summary:
        return True
    return False


def _timeline_sample(
    timeline: List[Dict[str, Any]],
    *,
    skip_ts_ms: Optional[set] = None,
    cap: int = TIMELINE_RENDER_SAMPLE,
) -> List[Dict[str, Any]]:
    """Build a bounded, chronological list of representative middle-of-the-
    run timeline events.

    Strategy:
      1. Drop entries whose ``ts_ms`` is in ``skip_ts_ms`` (typically the
         first/last/key-moment entries the renderer already shows).
      2. Take every "interesting" entry (errors, warns, state transitions,
         delivery, model.completed, tool-call boundaries) up to ``cap``.
      3. Pad with evenly-spaced filler from the remaining entries so the
         user can see the SHAPE of the run, not just its anomalies.
      4. De-dup by (ts_ms, source, summary) and return chronological.

    Linear time and bounded memory. ``skip_ts_ms`` may be ``None``.
    """
    if not timeline:
        return []
    skip = set(skip_ts_ms or ())

    # Pre-filter once.
    candidates = [e for e in timeline if e.get("ts_ms") not in skip]
    if not candidates:
        return []

    # Small timelines: just return everything (chronological, deduped).
    if len(candidates) <= cap:
        seen: set = set()
        out: List[Dict[str, Any]] = []
        for e in candidates:
            key = (e.get("ts_ms"), e.get("source"), e.get("summary"))
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
        return out

    # Phase 1: prioritize interesting events. When MORE than `cap` of them
    # exist, evenly sample across the interesting-event index range so the
    # picks cover the run's full span — the older "first cap of them in
    # order" loop hid late interesting events on long sessions where the
    # early interactions burned through the cap before the renderer ever
    # reached the tail.
    seen_keys: set = set()
    interesting_pool: List[Dict[str, Any]] = []
    for e in candidates:
        if not _is_interesting_timeline_event(e):
            continue
        key = (e.get("ts_ms"), e.get("source"), e.get("summary"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        interesting_pool.append(e)

    chosen: List[Dict[str, Any]] = []
    pn = len(interesting_pool)
    if pn <= cap:
        chosen.extend(interesting_pool)
    else:
        # Fractional even spacing across the interesting pool.
        # `(i * pn) // cap` for i in [0, cap) yields cap distinct indices
        # spread across [0, pn) without clustering at either end.
        for i in range(cap):
            idx = min(pn - 1, (i * pn) // cap)
            chosen.append(interesting_pool[idx])
        # The picks above are already chronological since
        # interesting_pool was iterated in chronological order.

    # Phase 2: pad with evenly-spaced filler from the remaining pool, only
    # if we have room. We sample positions across the FULL pool index range
    # using fractional spacing so the picks genuinely span first→last —
    # the older `i * (n // room)` formula clustered everything near index
    # 0 when `room` was much smaller than `n` (e.g. picks at 0, 50, 100
    # … and never reaching the tail of a 5000-event pool).
    if len(chosen) < cap:
        room = cap - len(chosen)
        # Build the filler pool: candidates not yet chosen, in chronological
        # order (candidates is a chrono pre-filter).
        pool = [
            e for e in candidates
            if (e.get("ts_ms"), e.get("source"), e.get("summary"))
            not in seen_keys
        ]
        n = len(pool)
        if n and room:
            # Fractional even spacing: pick indices 0, n/room, 2*n/room, …
            # (room−1)*n/room. This guarantees the last pick is at index
            # ≈ n − n/room (not 0), so the picks reach the run's tail.
            # Scan indices in the order produced; collisions with already-
            # chosen interesting events are skipped (cap holds room).
            for i in range(room):
                idx = min(n - 1, (i * n) // room)
                e = pool[idx]
                key = (e.get("ts_ms"), e.get("source"), e.get("summary"))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                chosen.append(e)

    chosen.sort(key=lambda e: e.get("ts_ms") or 0)
    return chosen[:cap]


def _representative_logs(
    correlated_logs: List[Dict[str, Any]],
    *,
    limit: int = REPRESENTATIVE_INFO_LINES,
) -> List[Dict[str, Any]]:
    """Pick telling INFO entries: lifecycle starts/ends and tool/model
    boundaries, spanning head→middle→tail so the selection reflects the whole
    run. Used only when a window has no ERROR/WARN entries, so a quiet window
    still shows concrete evidence of what happened instead of going silent.
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
    # Span the pool: first, last, plus evenly spaced mids via fractional
    # spacing so the picks spread across the whole run, not cluster at head.
    inner = limit - 2
    indices = {0, len(pool) - 1}
    if inner > 0:
        for i in range(1, inner + 1):
            indices.add((i * (len(pool) - 1)) // (inner + 1))
    return [pool[i] for i in sorted(indices)][:limit]


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

        # Discover sibling files / data sources. Note: app log discovery is
        # deferred until after we have computed the session window — see
        # below — because the older "today's mtime" heuristic excluded log
        # files for sessions that ran on previous days, leaving Correlated
        # Logs empty. v1.4.10 widens the candidate set to cover the window's
        # filename-date range.
        traj_path = _trajectory_path_for(session_file)
        sessions_json_path = _sessions_json_for(session_file)
        runs_sqlite_path = _runs_sqlite_path(str(ctx.openclaw_home))
        if not os.path.isfile(runs_sqlite_path):
            runs_sqlite_path = None

        # Resolve run selection (used to compute the window before we go
        # discover the right log files for it).
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

        # v1.4.10: pick log files whose filename date intersects the window
        # (with a ±1 day margin) so a session whose run finished on a
        # previous day still pulls in that day's log. When the window is 0
        # the helper falls back to ``discover_recent_logs``.
        log_files = discover_logs_for_window(
            str(ctx.log_dir), window_start, window_end,
        )

        report.add_scope(
            "session", f"session:{full_session_id[:8]}",
        )
        if log_files:
            # ``session_window`` = the session's actual time window plus
            # ±LOG_WINDOW_GRACE_MS for clock skew (see _bound_logs_to_window).
            grace_ms = LOG_WINDOW_GRACE_MS
            report.add_scope(
                "app_logs", "session_window",
                f"{len(log_files)} files (±{grace_ms // 1000}s grace)",
            )

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
            cron_run_path = _cron_run_path(
                graph.cron_job_id, str(ctx.openclaw_home),
            )
            if cron_run_path:
                sources_present["cron/runs"] = True

        # Filter app logs through correlation graph, then bound to the
        # session window so we don't drag in unrelated traffic that just
        # happens to share a long-lived sessionKey or reused toolCallId.
        # v1.4.4 task D: a second pass admits any log line whose OTel
        # ``traceId`` was harvested from a pass-1 hit. Strict mode flows
        # through and gates the harvest to sessionId/runId-seeded lines.
        strict = bool(kwargs.get("strict_correlation"))
        raw_correlated_logs, harvested_trace_ids = (
            filter_log_files_with_otel(
                log_files, graph,
                strict=strict, max_records=DEFAULT_LOG_RECORD_CAP,
            ) if log_files else ([], [])
        )
        correlated_logs, logs_dropped_oow, logs_ts_less = _bound_logs_to_window(
            raw_correlated_logs,
            window_start=window_start, window_end=window_end,
        )

        mask = bool(kwargs.get("mask")) and not (
            kwargs.get("unmask") or ctx.unmask
        )

        waterfall = _build_tool_waterfall(session_records, mask=mask)
        wf_stats = _waterfall_stats(waterfall)

        runtime_blocks = [_runtime_context(r) for r in (selected_runs or traj_runs)]
        children = [_summarize_child_task(row) for row in child_rows]
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

        # Build the run-level delivery summary (used by timeline + verdict).
        delivery_run_summary: Optional[Dict[str, Any]] = None
        if primary_runtime and primary_runtime.get("did_send_via_messaging_tool"):
            delivery_run_summary = {
                "did_send": True,
                "ended_ms": (primary_run or {}).get("ended_ms") or 0,
                "targets": primary_runtime.get("messaging_targets") or [],
                "text_count": primary_runtime.get("messaging_text_count") or 0,
            }

        # v1.4.4 tasks E/F/G/H/I: parse correlated logs once into typed
        # buckets (queue events, state transitions, run durations, context
        # prechecks, config reloads). Each parser is window-aware via the
        # already-bounded ``correlated_logs`` source.
        log_parsed = _parse_log_lines(
            correlated_logs, session_id=full_session_id,
        )

        # Timeline (now folds in window-bounded logs + delivery events,
        # plus log-derived state transitions and config reloads).
        timeline, timeline_stats = _build_timeline(
            session_records=session_records,
            trajectory_runs=selected_runs or traj_runs,
            correlated_logs=correlated_logs,
            cron_runs=cron_runs,
            delivery_run_summary=delivery_run_summary,
            log_parsed=log_parsed,
        )
        timeline_keys = _timeline_key_moments(timeline)

        # Health signals (now also includes log-marker decisions and
        # cron-delivery failures, replacing the standalone Model Decisions
        # and Delivery sections respectively).
        log_decisions = _log_marker_signals(correlated_logs)
        signals = _health_signals(
            selected_runs or traj_runs, correlated_logs,
            waterfall=waterfall, children=children,
            cron_runs=cron_runs, log_decisions=log_decisions,
            log_parsed=log_parsed,
        )
        # v1.4.10: positive (OK) signals so a healthy run shows confirmation
        # lines, not just an empty signals section. Additive only — they
        # never affect the verdict. v1.4.11 renders these in the merged
        # "Correlated Logs & Signals" section.
        positive_signals = _positive_health_signals(
            selected_runs or traj_runs,
            waterfall=waterfall,
            problem_signals=signals,
            delivery_run_summary=delivery_run_summary,
            cron_runs=cron_runs,
        )

        # v1.4.4 task F: the gateway log carries an authoritative end-to-
        # end run duration. When we have one for the primary run, expose it
        # on runtime_context so JSON consumers see it and renderers can
        # reference it next to the synthetic round-trip note.
        if primary_runtime and log_parsed.get("run_durations"):
            primary_rid = primary_runtime.get("runId")
            for rd in log_parsed["run_durations"]:
                if rd.get("run_id") == primary_rid:
                    primary_runtime["log_run_duration_ms"] = (
                        rd.get("duration_ms")
                    )
                    break

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
        # v1.4.5: per-run attempt details for JSON consumers. The full
        # ``trace.artifacts`` event is included for each attempt so
        # downstream tools can re-run their own per-attempt analysis
        # without having to reparse the trajectory.
        report.data["run_attempts"] = [
            {
                "runId": r.get("runId"),
                "attempt_count": r.get("attempt_count") or 0,
                "had_failed_attempt": bool(r.get("had_failed_attempt")),
                "attempts": [
                    {
                        "index": a.get("index"),
                        "started_ms": a.get("started_ms"),
                        "ended_ms": a.get("ended_ms"),
                        "duration_ms": (
                            max(0, (a.get("ended_ms") or 0)
                                - (a.get("started_ms") or 0))
                            if a.get("started_ms") and a.get("ended_ms")
                            else None
                        ),
                        "final_status": a.get("final_status"),
                        "failure_flags": a.get("failure_flags") or [],
                        "prompt_error_source": a.get("prompt_error_source"),
                        "failed": bool(a.get("failed")),
                    }
                    for a in (r.get("attempts") or [])
                ],
            }
            for r in (selected_runs or traj_runs)
        ]
        report.data["timeline"] = timeline
        report.data["timeline_stats"] = timeline_stats
        report.data["timeline_key_moments"] = timeline_keys
        report.data["tool_waterfall"] = waterfall
        report.data["tool_stats"] = wf_stats
        # runtime_context kept on the JSON envelope for backward-compat —
        # the standalone pretty section is gone (folded into Session Overview).
        report.data["runtime_context"] = runtime_blocks
        # When --mask is active, deep-sanitize a copy of each correlated log
        # record before placing it on the JSON envelope. The sanitizer walks
        # strings only and preserves dict/list structure, so level/timestamp
        # fields used by downstream consumers remain intact. The in-memory
        # `correlated_logs` list is left as-is so the rendering blocks above
        # (which sanitize `msg_s` individually) and any other in-process
        # consumers still see the unmodified records.
        report.data["correlated_logs"] = (
            [_maybe_sanitize(rec, mask=mask) for rec in correlated_logs]
            if mask else correlated_logs
        )
        report.data["model_calls"] = model_calls
        report.data["model_aggregate"] = model_aggregate
        # v1.4.13 Findings: deterministic ordering of problem signals so the
        # detail list under Correlated Logs & Signals reads in severity
        # order (fail → warn → desc rank → asc ts → asc kind name) and
        # matches the Findings summary at the top. The list of signals
        # itself is unchanged — only the order is.
        signals = sorted(signals, key=_signal_sort_key)
        report.data["health_signals"] = signals
        # v1.4.10: positive signals on the JSON envelope so consumers can
        # render their own "what looks fine" summary alongside problems.
        report.data["positive_health_signals"] = positive_signals
        # v1.4.13: ranked, objective findings on the JSON envelope.
        # Each entry is {severity, kind, ts_ms, summary, ref}; values
        # are pure restatements of fields already on the source signal
        # (no inference, no recommendation). more_count is the tail that
        # didn't fit under FINDINGS_TOP_N.
        findings_list, findings_more = _build_findings(signals)
        report.data["findings"] = findings_list
        report.data["findings_more_count"] = findings_more
        report.data["child_tasks"] = children
        report.data["session_stats"] = session_stats
        # v1.4.4: surface the parsed log buckets and the OTel traceIds we
        # used to expand correlation, so JSON consumers can do their own
        # analysis without re-walking the log.
        report.data["log_parsed"] = log_parsed
        if harvested_trace_ids:
            report.data["otel_trace_ids"] = harvested_trace_ids
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
        # v1.4.5: when any selected run had multiple attempt-cycles,
        # surface a one-line "attempts: N (M failed, final=<status>)"
        # for each multi-attempt run so the hidden retry is visible
        # before scrolling down to Correlated Logs & Signals.
        for run in (selected_runs or traj_runs):
            attempts = run.get("attempts") or []
            if len(attempts) <= 1:
                continue
            n = len(attempts)
            failed = sum(1 for a in attempts if a.get("failed"))
            final_a = attempts[-1]
            final_status = (
                (final_a.get("artifacts_event") or {}).get("data") or {}
            ).get("finalStatus") or (
                "failed" if final_a.get("failed") else "ok"
            )
            rid_s = (run.get("runId") or "?")[:8]
            data_block = {
                "runId": run.get("runId"),
                "attempt_count": n,
                "failed_count": failed,
                "final_status": final_status,
            }
            line = (
                f"attempts: {n} ({failed} failed, final={final_status}) "
                f"[run {rid_s}]"
            )
            if failed:
                s_overview.warn(
                    f"runs.attempts.{rid_s}", line, data=data_block,
                )
            else:
                s_overview.ok(
                    f"runs.attempts.{rid_s}", line, data=data_block,
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
        # v1.4.4 task A: prompt-cache observation. Three states:
        #   - broke=True  → WARN with lost-token magnitude
        #   - broke=False & cacheRead>0 → OK "cache hit"
        #   - missing observation (older runs)  → no line
        if primary_runtime is not None:
            broke = primary_runtime.get("cache_broke")
            cr_obs = primary_runtime.get("cache_read_observed")
            pcr_obs = primary_runtime.get("cache_read_previous")
            lost = primary_runtime.get("cache_read_lost")
            if broke is True:
                lost_s = (
                    f"~{lost:,} cached tokens"
                    if isinstance(lost, int) and lost > 0
                    else "cached tokens"
                )
                pcr_s = (
                    f" (prev cacheRead {pcr_obs:,} → {cr_obs:,})"
                    if isinstance(pcr_obs, int) and isinstance(cr_obs, int)
                    else ""
                )
                s_overview.warn(
                    "stats.cache_broke",
                    f"cache broke: lost {lost_s}{pcr_s}",
                    data={
                        "broke": True,
                        "cache_read": cr_obs,
                        "previous_cache_read": pcr_obs,
                        "lost_tokens": lost,
                    },
                )
            elif broke is False and isinstance(cr_obs, int) and cr_obs > 0:
                s_overview.ok(
                    "stats.cache_hit",
                    f"cache hit: cacheRead={cr_obs:,}",
                    data={"broke": False, "cache_read": cr_obs},
                )
        # v1.4.4 task B: item-lifecycle counts always rendered when present.
        if primary_runtime is not None:
            lc = primary_runtime.get("lifecycle")
            if isinstance(lc, dict) and (
                lc.get("started") or lc.get("completed") or lc.get("active")
            ):
                s_overview.ok(
                    "stats.lifecycle",
                    f"items: started={lc.get('started') or 0} "
                    f"completed={lc.get('completed') or 0} "
                    f"active={lc.get('active') or 0}",
                    data=lc,
                )
        # v1.4.4 task E: queue/concurrency one-liner (only when we actually
        # parsed events for this run). Surfaces queue latency separately
        # from model latency.
        qs = log_parsed.get("queue_summary") if log_parsed else None
        if qs:
            s_overview.ok(
                "stats.queue",
                f"queue: max wait={qs['max_wait_ms']}ms, "
                f"max queueSize={qs['max_queue_size']}, "
                f"max concurrentRuns={qs['max_concurrent_runs']}",
                data=qs,
            )
        # v1.4.4 task G: precheck route + estimated tokens (latest one in
        # the run wins — that's the value the model actually saw).
        prechecks = (log_parsed or {}).get("context_prechecks") or []
        if prechecks:
            last_pc = prechecks[-1]
            route = last_pc.get("route")
            est = last_pc.get("estimated_prompt_tokens") or 0
            verdict_warn = (route or "").lower() in _COMPACTION_ROUTES
            line = (
                f"context precheck: route={route} estPromptTokens={est:,}"
            )
            if verdict_warn:
                s_overview.warn(
                    "stats.precheck", line, data=last_pc,
                )
            else:
                s_overview.ok(
                    "stats.precheck", line, data=last_pc,
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

        # ── Runtime Context (folded into Session Overview) ──────────────
        # The v1.4.x standalone "Panorama · Runtime Context" section was
        # removed in v1.4.3. Its fields live here as additional Overview
        # lines. report.data["runtime_context"] is unchanged so JSON
        # consumers keep the full per-run snapshot.
        if primary_runtime:
            rt = primary_runtime
            hv = rt.get("harness_version")
            node = rt.get("node")
            if hv or node:
                s_overview.ok(
                    "runtime.harness",
                    f"harness: {hv or '?'}"
                    + (f" | node: {node}" if node else ""),
                )
            pa = rt.get("plugins_activated") or []
            if pa:
                s_overview.ok(
                    "runtime.plugins",
                    f"plugins activated: {', '.join(pa)}",
                    data={"plugins_activated": pa},
                )
            for pe in (rt.get("plugin_errors") or [])[:5]:
                s_overview.warn(
                    f"runtime.plugin_error.{pe.get('id','?')}",
                    f"plugin error: {pe.get('id')} — "
                    f"{(pe.get('error') or '?')[:120]}",
                    data=pe,
                )
            sk_count = rt.get("skill_count")
            if sk_count is not None:
                sn = rt.get("skill_names") or []
                preview = ", ".join(sn[:8])
                more = f" (+{len(sn) - 8} more)" if len(sn) > 8 else ""
                s_overview.ok(
                    "runtime.skills",
                    f"skills: {sk_count}"
                    + (f" — {preview}{more}" if preview else ""),
                    data={"skill_count": sk_count, "skill_names": sn},
                )
            sp_chars = rt.get("system_prompt_chars")
            pc_chars = rt.get("project_context_chars")
            np_chars = rt.get("non_project_context_chars")
            ts_chars = rt.get("tools_schema_chars")
            if sp_chars or ts_chars:
                pc_s = f" project={pc_chars:,}" if pc_chars else ""
                np_s = f" nonProject={np_chars:,}" if np_chars else ""
                ts_s = f" | tools_schema={ts_chars:,}" if ts_chars else ""
                s_overview.ok(
                    "runtime.prompt_budget",
                    f"system prompt: {sp_chars or 0:,} chars{pc_s}{np_s}{ts_s}",
                    data={
                        "system_prompt_chars": sp_chars,
                        "project_context_chars": pc_chars,
                        "non_project_context_chars": np_chars,
                        "tools_schema_chars": ts_chars,
                    },
                )
            bt = rt.get("bootstrap_truncation")
            if isinstance(bt, dict) and (
                bt.get("truncated_files") or bt.get("near_limit_files")
            ):
                s_overview.warn(
                    "runtime.bootstrap",
                    f"bootstrap: truncated={bt.get('truncated_files')} "
                    f"nearLimit={bt.get('near_limit_files')} "
                    f"totalNearLimit={bt.get('total_near_limit')}",
                    data=bt,
                )
            ctc = rt.get("compiled_tool_count")
            cmc = rt.get("compiled_messages_count")
            ss = rt.get("stream_strategy")
            tp = rt.get("transport")
            parts: List[str] = []
            if ctc is not None:
                parts.append(f"tools={ctc}")
            if cmc is not None:
                parts.append(f"messages={cmc}")
            if ss:
                parts.append(f"stream={ss}")
            if tp:
                parts.append(f"transport={tp}")
            if parts:
                s_overview.ok(
                    "runtime.compiled",
                    "compiled: " + " | ".join(parts),
                    data={
                        "compiled_tool_count": ctc,
                        "compiled_messages_count": cmc,
                        "stream_strategy": ss,
                        "transport": tp,
                    },
                )
        else:
            s_overview.warn(
                "runtime.missing", "no trajectory runs available",
            )

        # 2. Timeline — show key moments, not just count
        s_timeline = report.section("Panorama · Timeline")
        if not timeline:
            s_timeline.warn("timeline.empty", "no timeline events")
        else:
            span_ms = timeline[-1]["ts_ms"] - timeline[0]["ts_ms"]
            dropped_middle = timeline_stats.get("dropped_middle") or 0
            cap_note = (
                f" ({dropped_middle} dropped by cap)" if dropped_middle else ""
            )
            s_timeline.ok(
                "timeline.window",
                f"{len(timeline)} events{cap_note} over "
                f"{fmt_duration(span_ms / 1000)} "
                f"({fmt_epoch_local(timeline[0]['ts_ms'])} → "
                f"{fmt_epoch_local(timeline[-1]['ts_ms'])})",
                data={"count": len(timeline), **timeline_stats},
            )
            if timeline_stats.get("truncated"):
                s_timeline.warn(
                    "timeline.truncated",
                    f"timeline truncated: kept "
                    f"{len(timeline)}/{timeline_stats['total_before_cap']} events; "
                    f"{dropped_middle} dropped from middle "
                    f"(head + tail preserved)",
                    data=timeline_stats,
                )
            skipped = timeline_stats.get("skipped_no_ts") or 0
            if skipped:
                s_timeline.warn(
                    "timeline.skipped_no_ts",
                    f"skipped {skipped} record(s) with no parseable timestamp",
                    data={"skipped_no_ts": skipped},
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
            # v1.4.10: representative middle-of-the-run sample. Earlier
            # versions only rendered first/last/key moments, hiding the
            # shape of the run itself. The sample is deduped against the
            # already-rendered anchors and capped at TIMELINE_RENDER_SAMPLE.
            already_rendered_ts = {timeline[0]["ts_ms"], timeline[-1]["ts_ms"]}
            for keymom in (
                timeline_keys.get("first_error"),
                timeline_keys.get("first_warn"),
                timeline_keys.get("first_stall"),
            ):
                if keymom and keymom.get("ts_ms"):
                    already_rendered_ts.add(keymom["ts_ms"])
            sample = _timeline_sample(
                timeline,
                skip_ts_ms=already_rendered_ts,
                cap=TIMELINE_RENDER_SAMPLE,
            )
            for idx, entry in enumerate(sample, 1):
                summary = (entry.get("summary") or "")[:100]
                ts = entry.get("ts_ms") or 0
                src = entry.get("source") or "?"
                s_timeline.ok(
                    f"timeline.sample.{idx:02d}",
                    f"[{fmt_epoch_local(ts)}] {src} · {summary}",
                    data=entry,
                )

        # (Standalone "Runtime Context" section removed in v1.4.3 — its
        # fields were folded into Session Overview above. The per-run
        # block lives on under report.data["runtime_context"] for JSON
        # consumers.)

        # 3. Model Calls — per-call token usage + per-model performance.
        # NOTE: per-call duration was removed in v1.4.7 — it came from the
        # session.jsonl message gap (round-trip wall-clock), not a real API
        # timing channel, so it was not trustworthy as model latency. The
        # authoritative gateway-log run wall time below IS a real measurement
        # and is kept.
        s_model = report.section("Panorama · Model Calls")
        # v1.4.4 task F: surface the gateway-log run wall time when we
        # parsed it (authoritative, captures everything the gateway saw).
        if primary_runtime and primary_runtime.get("log_run_duration_ms"):
            log_dur = primary_runtime["log_run_duration_ms"]
            s_model.ok(
                "model.run_wall_time",
                f"run wall time: {fmt_duration(log_dur / 1000)} "
                f"({log_dur}ms, from gateway log)",
                data={"log_run_duration_ms": log_dur},
            )
        if not model_calls:
            s_model.ok("model.none", "no model calls in selected run")
        else:
            cost_s = (f" | cost=${total_cost_usd:.4f}"
                      if isinstance(total_cost_usd, (int, float)) else "")
            s_model.ok(
                "model.summary",
                f"{len(model_calls)} calls | in={total_input:,} | "
                f"out={total_output:,} tok | "
                f"cache_read={total_cache_read:,} | cache_write={total_cache_write:,}"
                + cost_s,
                data={
                    "count": len(model_calls), "input": total_input,
                    "output": total_output, "cache_read": total_cache_read,
                    "cache_write": total_cache_write,
                    "cost_usd": total_cost_usd,
                },
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
                # avg_dur removed from display in v1.4.7 (derived from the
                # unreliable per-call message-gap proxy); kept in JSON data.
                avg_dur_s = ""
                s_model.ok(
                    f"model.by.{m['model']}",
                    f"{m['model']}: {m['calls']} calls | "
                    f"avg_out={m['avg_output']} tok{avg_dur_s} | "
                    f"stop[{stop_summary}]",
                    data=m,
                )
            # Per-call detail (input/output tokens + stop reason).
            # Per-call duration was removed in v1.4.7 (message-gap proxy, not
            # real model latency); duration_ms stays in JSON data.
            for idx, c in enumerate(model_calls, 1):
                tools_s = ",".join(c["tools"][:3]) if c["tools"] else "→ final"
                s_model.ok(
                    f"model.call.{idx}",
                    f"#{idx} in={c.get('input', 0)} out={c['output']} "
                    f"({c['stopReason']}) "
                    f"[{tools_s}] cr={c['cacheRead']} cw={c['cacheWrite']}",
                    data=c,
                )

        # Surface model-call errors from runtime_context. promptErrorSource
        # / aborted / timed_out / idle_timed_out come from trace.artifacts;
        # if any are set, the model call returned an error or never finished.
        # Best-effort: if runtime says something failed but model_calls is
        # empty, we emit a "no usage record" hint — the trajectory loader
        # doesn't always surface usage for failed completions.
        for blk in runtime_blocks:
            err_flags: List[str] = []
            for k in (
                "aborted", "external_abort", "timed_out", "idle_timed_out",
                "timed_out_during_compaction",
                "timed_out_during_tool_execution",
            ):
                if blk.get(k):
                    err_flags.append(k)
            pes = blk.get("prompt_error_source")
            fs = (blk.get("final_status") or "").lower()
            if err_flags or pes or fs in ("error", "errored", "failed"):
                rid_s = (blk.get("runId") or "?")[:8]
                bits: List[str] = []
                if pes:
                    bits.append(f"promptErrorSource={pes}")
                if err_flags:
                    bits.append(", ".join(f"{k}=true" for k in err_flags))
                if fs and fs not in ("ok", "completed", "success"):
                    bits.append(f"finalStatus={fs}")
                s_model.fail(
                    f"model.error.{rid_s}",
                    f"run {rid_s} errored: " + " | ".join(bits),
                    data=blk,
                )
                # If we have nothing at all, the failed call left no usage
                # record — say so explicitly so readers don't think it
                # silently succeeded.
                if not model_calls:
                    s_model.warn(
                        f"model.error.no_usage.{rid_s}",
                        "model call failed with no usage record "
                        "(see Correlated Logs & Signals)",
                    )

        # 5. Tool Execution — args + result summary inline
        s_tools = report.section("Panorama · Tool Execution")
        if not waterfall:
            s_tools.ok("tools.none", "no tool calls in this run")
        else:
            if wf_stats["completed"]:
                summary_parts = [
                    f"{wf_stats['total']} calls",
                    f"{wf_stats['errors']} err",
                    f"avg {_fmt_tool_dur(int(round(wf_stats['avg_ms'])))}",
                    f"p50 {_fmt_tool_dur(wf_stats['p50_ms'])}",
                    f"p95 {_fmt_tool_dur(wf_stats['p95_ms'])}",
                    f"max {_fmt_tool_dur(wf_stats['max_ms'])}",
                ]
                slow = wf_stats.get("slowest")
                if slow and slow.get("name"):
                    summary_parts.append(
                        f"slowest {slow['name']}"
                        f"({_fmt_tool_dur(slow.get('duration_ms'))})"
                    )
                s_tools.ok(
                    "tools.timing",
                    " · ".join(summary_parts),
                    data=wf_stats,
                )
            # Column widths: keep #idx and name padded so the eye can
            # stripe down the duration column. Names longer than the pad
            # render flush — better than wrapping a column we don't own.
            IDX_W = 4
            NAME_W = 16
            DUR_W = 6
            INLINE_RESULT_MAX = 48
            for idx, t in enumerate(waterfall, 1):
                name = t.get("name", "?")
                dur = t.get("duration_ms")
                dur_s = _fmt_tool_dur(dur)
                is_err = t.get("is_error")
                v = Verdict.WARN if is_err else Verdict.OK
                args_s = _format_args_inline(t.get("args"))
                result_s = _format_result_inline(t)
                idx_f = f"#{idx}".ljust(IDX_W)
                name_f = name if len(name) > NAME_W else name.ljust(NAME_W)
                dur_f = dur_s.rjust(DUR_W)
                header = f"{idx_f} {name_f} {dur_f}"
                if args_s:
                    header += f"  {args_s}"
                detail: Optional[str] = None
                if result_s:
                    arrow = "⇒ ERR " if is_err else "→ "
                    snippet = arrow + result_s
                    # Short snippets ride along on the header; long ones
                    # drop to a continuation line so each call stays
                    # vertically scannable.
                    if (
                        len(result_s) <= INLINE_RESULT_MAX
                        and len(header) + 2 + len(snippet) <= 110
                    ):
                        header += f"  {snippet}"
                    else:
                        detail = snippet
                s_tools.add(
                    f"tools.call.{idx}", v, header,
                    detail=detail, data=t,
                )

        # 5. Correlated Logs & Signals — v1.4.11 merges the standalone
        # Health Signals section into Correlated Logs because the two were
        # always read together (signals explain WHY the logs say what they
        # say). The render order is intentional:
        #   1. summary line (or "no logs" notice)
        #   2. positive ✓ confirmations (so a clean run is visibly clean)
        #   3. problem ⚠/✗ signals (the diagnostic core)
        #   4. raw ERROR log lines (head-200 + "+N more")
        #   5. raw WARN log lines (head-10 + "+N more")
        # Verdict-driving severity calls (.fail/.warn) are unchanged — only
        # the section that hosts them moved. The cherry-picked
        # "representative INFO" block was removed (v1.4.11): on a clean
        # run, positives carry the affirmation, and a single summary
        # number is honest about the rest.
        s_logs = report.section("Panorama · Correlated Logs & Signals")

        # 1. Summary / no-logs notice ────────────────────────────────────
        error_entries: List[Dict[str, Any]] = []
        warn_entries: List[Dict[str, Any]] = []
        if not log_files:
            s_logs.warn("logs.missing", "no app log files found in log_dir")
        elif not correlated_logs:
            # v1.4.18: distinguish three empty-correlation states so
            # operators can tell "logs were rotated away" from "logs are
            # there but nothing carries this sessionId" — the former is
            # an environment/retention issue, the latter is a real
            # correlation miss worth investigating.
            present_dates, missing_dates, avail_dates = window_log_dates(
                str(ctx.log_dir), window_start, window_end,
            )
            if not window_start and not window_end:
                # Unknown window — can't classify; preserve the original
                # honest fallback.
                s_logs.ok("logs.none", "no correlated log entries found")
            elif missing_dates and not present_dates:
                # Every date the session window touches is missing from
                # log_dir → the log was rotated/deleted before we got
                # here. Not a session bug.
                avail_preview = avail_dates[-5:] if avail_dates else []
                avail_note = (
                    f" (log_dir has: {', '.join(avail_preview)})"
                    if avail_preview else " (log_dir has none)"
                )
                s_logs.ok(
                    "logs.not_retained",
                    f"session-window log not retained: "
                    f"{', '.join(missing_dates)}{avail_note} — "
                    f"app_log correlation/timeline unavailable for "
                    f"this window",
                    data={
                        "window_dates_missing": missing_dates,
                        "window_dates_present": present_dates,
                        "available_log_dates": avail_dates,
                    },
                )
            else:
                # Window dates present but no lines correlated — could
                # be a real bug (sessionId/runId not being logged) or
                # a strict-correlation miss.
                s_logs.ok(
                    "logs.uncorrelated",
                    f"no correlated entries — session-date log(s) "
                    f"present ({', '.join(present_dates)}) but no "
                    f"lines carry this sessionId/runId",
                    data={
                        "window_dates_missing": missing_dates,
                        "window_dates_present": present_dates,
                        "available_log_dates": avail_dates,
                    },
                )
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
            window_note = ""
            if logs_dropped_oow or logs_ts_less:
                bits = []
                if logs_dropped_oow:
                    bits.append(f"{logs_dropped_oow} out-of-window")
                if logs_ts_less:
                    bits.append(f"{logs_ts_less} ts-less kept")
                window_note = " | window-filter: " + ", ".join(bits)
            s_logs.ok(
                "logs.summary",
                f"{len(correlated_logs)} correlated entries: "
                f"{len(error_entries)} ERROR, {len(warn_entries)} WARN, "
                f"{info_count} INFO" + window_note,
                data={
                    "total": len(correlated_logs),
                    "error": len(error_entries),
                    "warn": len(warn_entries),
                    "info": info_count,
                    "out_of_window_dropped": logs_dropped_oow,
                    "ts_less_kept": logs_ts_less,
                },
            )

        # 2. Positive (OK) signals — additive, never affect verdict. ─────
        for psig in positive_signals:
            kind = psig.get("kind") or "ok"
            s_logs.ok(
                f"health.{kind}",
                psig.get("summary") or "",
                data=psig,
            )
        # When neither positive nor problem signals were computed, surface
        # an explicit "clean" line so the section never goes silent on a
        # truly minimal run (e.g. no trajectory + no logs).
        if not signals and not positive_signals:
            s_logs.ok(
                "health.clean",
                "no abort/timeout/leak/stall signals",
            )

        # 3. Problem signals — same vocabulary as the old Health Signals
        # section. The verdict-driving fail()/warn() calls are unchanged.
        for sig in signals:
            kind = sig.get("kind", "?")
            ts_ms = sig.get("ts_ms") or 0
            tsfx = f"[{fmt_epoch_local(ts_ms)}] " if ts_ms else ""
            if kind == "trajectory_artifact":
                # v1.4.4 task C: render the human-friendly "where it
                # hung" line; raw flags stay on the data block for
                # JSON consumers.
                human = sig.get("human_summary") or []
                detail = " | ".join(human) if human else (
                    ",".join(sig.get("flags") or []) or "?"
                )
                s_logs.fail(
                    f"health.{kind}.{(sig.get('runId') or '?')[:8]}",
                    f"{tsfx}run {(sig.get('runId') or '?')[:8]}: "
                    f"{detail} | finalStatus={sig.get('final_status')}",
                    data=sig,
                )
            elif kind == "tool_call_leak":
                s_logs.warn(
                    f"health.{kind}.{(sig.get('runId') or '?')[:8]}",
                    f"{tsfx}tool-call leak: active={sig.get('active')} "
                    f"started={sig.get('started')} "
                    f"completed={sig.get('completed')}",
                    data=sig,
                )
            elif kind == "log_stall":
                s_logs.warn(
                    f"health.{kind}.{ts_ms}",
                    f"{tsfx}stall: {sig.get('summary')}",
                    data=sig,
                )
            elif kind == "gateway_pid_change":
                s_logs.warn(
                    f"health.{kind}",
                    f"gateway pid change: {sig.get('pids')}",
                    data=sig,
                )
            elif kind == "long_tool_call":
                # v1.4.10: render the call's args + (when present) an
                # error or result snippet, so the line names WHICH
                # invocation hung — e.g. "cron(action=update,
                # jobId=0cdb2836) 2.4m → error: patch required"
                # — instead of just "cron 2.4m (error)".
                name = sig.get("name") or "?"
                args_s = sig.get("args_summary") or ""
                # Convert the waterfall's {k=v, k=v} curly format to
                # paren-wrapped (k=v, k=v) so the rendering reads like
                # a function call. The signal itself preserves the
                # original {} string for any consumer that already
                # depends on _format_args_inline output shape.
                if (args_s.startswith("{") and args_s.endswith("}")):
                    paren_args = "(" + args_s[1:-1] + ")"
                else:
                    paren_args = f"({args_s})" if args_s else ""
                name_call = f"{name}{paren_args}" if paren_args else name
                dur_s = fmt_duration(
                    (sig.get("duration_ms") or 0) / 1000
                )
                snippet = sig.get("snippet") or ""
                if sig.get("is_error"):
                    tail = (
                        f" → error: {snippet}" if snippet else " (error)"
                    )
                else:
                    tail = f" → {snippet}" if snippet else ""
                s_logs.warn(
                    f"health.{kind}.{ts_ms}",
                    f"{tsfx}long tool call: {name_call} {dur_s}{tail}",
                    data=sig,
                )
            elif kind == "last_tool_error":
                s_logs.warn(
                    f"health.{kind}.{(sig.get('runId') or '?')[:8]}",
                    f"{tsfx}last tool error: {sig.get('summary')}",
                    data=sig,
                )
            elif kind == "child_task_failed":
                s_logs.warn(
                    f"health.{kind}.{(sig.get('task_id') or '?')[:8]}",
                    f"{tsfx}child task failed: "
                    f"{sig.get('agent_id')}/{sig.get('runtime')} — "
                    f"{sig.get('error')}",
                    data=sig,
                )
            elif kind == "cron_delivery_failed":
                s_logs.fail(
                    f"health.{kind}."
                    f"{(str(sig.get('jobId')) or '?')[:8]}.{ts_ms}",
                    f"{tsfx}cron delivery failed: "
                    f"job={sig.get('jobId')} action={sig.get('action')} "
                    f"status={sig.get('status')} "
                    f"deliveryStatus={sig.get('deliveryStatus')}",
                    data=sig,
                )
            elif kind == "log_decision":
                s_logs.warn(
                    f"health.{kind}.{ts_ms}",
                    f"{tsfx}decision: {sig.get('summary')}",
                    data=sig,
                )
            elif kind == "items_incomplete":
                s_logs.warn(
                    f"health.{kind}.{(sig.get('runId') or '?')[:8]}",
                    f"{tsfx}incomplete items: "
                    f"{sig.get('started')} started, "
                    f"{sig.get('completed')} completed "
                    f"({sig.get('dropped')} dropped/errored)",
                    data=sig,
                )
            elif kind == "prompt_cache_broke":
                lost = sig.get("lost_tokens")
                lost_s = (
                    f", lost ~{lost:,} tokens"
                    if isinstance(lost, int) and lost > 0 else ""
                )
                s_logs.warn(
                    f"health.{kind}.{(sig.get('runId') or '?')[:8]}",
                    f"{tsfx}prompt cache broke{lost_s} "
                    f"(prev cacheRead={sig.get('previous_cache_read')} "
                    f"→ {sig.get('cache_read')})",
                    data=sig,
                )
            elif kind == "queue_wait_slow":
                s_logs.warn(
                    f"health.{kind}.{ts_ms}",
                    f"{tsfx}queued {sig.get('wait_ms')}ms behind "
                    f"other turns "
                    f"(queueSize={sig.get('queue_size')}, "
                    f"lane={(sig.get('lane') or '?')[:80]})",
                    data=sig,
                )
            elif kind == "context_precheck_overflow":
                s_logs.warn(
                    f"health.{kind}.{ts_ms}",
                    f"{tsfx}context precheck route="
                    f"{sig.get('route')} "
                    f"estTokens={sig.get('estimated_prompt_tokens')}",
                    data=sig,
                )
            elif kind == "state_transition_abnormal":
                s_logs.warn(
                    f"health.{kind}.{ts_ms}",
                    f"{tsfx}abnormal state transition: "
                    f"{sig.get('prev')} → {sig.get('new')}"
                    + (
                        f' reason="{sig.get("reason")}"'
                        if sig.get("reason") else ""
                    ),
                    data=sig,
                )
            elif kind == "config_reload_failed":
                s_logs.warn(
                    f"health.{kind}.{ts_ms}",
                    f"{tsfx}config reload FAILED (invalid): "
                    f"{(sig.get('reason') or '')[:160]}",
                    data=sig,
                )
            elif kind == "retried_after_failure":
                # v1.4.5: hidden failed-then-recovered attempts. WARN
                # when the final attempt succeeded (recovered), FAIL
                # when the final attempt also failed. The existing
                # trajectory_artifact signal still fires for whichever
                # attempt is the final one and degrades verdict
                # independently — this signal is purely additive.
                rid_s = (sig.get("runId") or "?")[:8]
                n = sig.get("attempt_count") or 0
                failed = sig.get("failed_count") or 0
                final_status = sig.get("final_status") or "?"
                chain = " | ".join(sig.get("per_attempt") or [])
                line = (
                    f"{tsfx}run {rid_s} had {n} attempts "
                    f"({failed} failed): {chain} → final {final_status}"
                )
                if sig.get("final_failed"):
                    s_logs.fail(
                        f"health.{kind}.{rid_s}", line, data=sig,
                    )
                else:
                    s_logs.warn(
                        f"health.{kind}.{rid_s}", line, data=sig,
                    )
            else:
                s_logs.warn(f"health.{kind}", str(sig), data=sig)

        # 4. Raw ERROR log lines (cap + "+N more"). ──────────────────────
        if error_entries:
            shown_errors = error_entries[:MAX_RENDERED_ERROR_LINES]
            for idx, rec in enumerate(shown_errors, 1):
                msg_s = parse_log_msg(rec) or "?"
                msg_s = _maybe_sanitize(msg_s, mask=mask)
                sub = get_log_subsystem(rec) or "?"
                ts = _log_ts_ms(rec)
                tsfx = f"[{fmt_epoch_local(ts)}] " if ts else ""
                s_logs.fail(
                    f"logs.error.{idx:04d}.{sub}",
                    f"{tsfx}[ERROR] [{sub}] {msg_s[:200]}",
                    data={"correlation": rec.get("correlation")},
                )
            if len(error_entries) > MAX_RENDERED_ERROR_LINES:
                more = len(error_entries) - MAX_RENDERED_ERROR_LINES
                s_logs.fail(
                    "logs.error.more",
                    f"+{more} more ERROR entries not shown "
                    f"(safety cap {MAX_RENDERED_ERROR_LINES})",
                    data={"more": more, "cap": MAX_RENDERED_ERROR_LINES},
                )

        # 5. Raw WARN log lines (head + "+N more"). ──────────────────────
        if warn_entries:
            shown_warns = warn_entries[:MAX_RENDERED_WARN_LINES]
            for idx, rec in enumerate(shown_warns, 1):
                msg_s = parse_log_msg(rec) or "?"
                msg_s = _maybe_sanitize(msg_s, mask=mask)
                sub = get_log_subsystem(rec) or "?"
                ts = _log_ts_ms(rec)
                tsfx = f"[{fmt_epoch_local(ts)}] " if ts else ""
                s_logs.warn(
                    f"logs.warn.{idx:04d}.{sub}",
                    f"{tsfx}[WARN] [{sub}] {msg_s[:200]}",
                    data={"correlation": rec.get("correlation")},
                )
            if len(warn_entries) > MAX_RENDERED_WARN_LINES:
                more = len(warn_entries) - MAX_RENDERED_WARN_LINES
                s_logs.warn(
                    "logs.warn.more",
                    f"+{more} more WARN entries not shown",
                    data={"more": more, "cap": MAX_RENDERED_WARN_LINES},
                )
        # 6. Representative INFO lines — only when the window had NO
        # ERROR/WARN entries, so a quiet/clean window still shows concrete
        # lifecycle/tool/model evidence instead of just a count. Cap is
        # REPRESENTATIVE_INFO_LINES (=20); OK-level (✓), no verdict effect.
        # Sits at else-body indent (sibling of the `if error_entries:` /
        # `if warn_entries:` blocks) so it fires precisely when both are empty.
        if correlated_logs and not error_entries and not warn_entries:
            info_only = [
                rec for rec in correlated_logs
                if (_log_level(rec) or "").upper() not in ("ERROR", "WARN")
            ]
            reps = _representative_logs(
                info_only, limit=REPRESENTATIVE_INFO_LINES)
            for idx, rec in enumerate(reps, 1):
                msg_s = parse_log_msg(rec) or "?"
                msg_s = _maybe_sanitize(msg_s, mask=mask)
                sub = get_log_subsystem(rec) or "?"
                ts = _log_ts_ms(rec)
                tsfx = f"[{fmt_epoch_local(ts)}] " if ts else ""
                s_logs.ok(
                    f"logs.info.{idx:02d}",
                    f"{tsfx}[INFO] [{sub}] {msg_s[:160]}",
                    data={"correlation": rec.get("correlation")},
                )

        # (Standalone "Model Decisions" section removed in v1.4.3 — log-marker
        #  decisions are routed under "health" entries as kind=log_decision;
        #  the duplicate trajectory model_select entry is gone because the
        #  model identity is already shown in Session Overview.)

        # 6. Child Tasks
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

        # (Standalone "Delivery" section removed in v1.4.3 — cron-run and
        #  messaging-tool send events are emitted directly into the Timeline
        #  with source="delivery" / event_type="delivery". Failed cron
        #  deliveries surface as a kind=cron_delivery_failed health signal so
        #  the verdict still degrades correctly.)

        # (Standalone "Panorama · Health Signals" section was REMOVED in
        #  v1.4.11. Its rendering now lives in the merged
        #  "Panorama · Correlated Logs & Signals" section above; the
        #  underlying signals are still computed and the JSON envelope
        #  keys `health_signals` / `positive_health_signals` are
        #  unchanged for downstream consumers.)

        if duration_ms > SLOW_E2E_MS:
            s_overview.warn(
                "window.slow",
                f"E2E elapsed {fmt_duration(duration_ms / 1000)} "
                f"> {SLOW_E2E_MS // 1000}s",
                data={"duration_ms": duration_ms,
                      "threshold_ms": SLOW_E2E_MS},
            )

        # ── 0. Panorama · Findings (v1.4.13) ─────────────────────────────
        # Built LAST so the verdict and every problem signal are fully
        # known, then moved to position 0 so it renders FIRST. Strictly
        # objective: the verdict is read from the already-computed
        # ``report.verdict``; lines re-state fields already on the source
        # signal. No inference, no recommendation. The same deterministic
        # severity key (fail-class > warn-class, rank desc, ts asc, kind
        # asc) applied above to ``signals`` orders the lines here too.
        s_findings = report.section("Panorama · Findings")
        verdict_value = report.verdict.value  # "ok" | "warn" | "fail"
        fail_count = sum(
            1 for s in signals if _signal_severity(s)[0] == "fail"
        )
        warn_count = sum(
            1 for s in signals if _signal_severity(s)[0] == "warn"
        )
        verdict_line = f"verdict: {verdict_value.upper()}"
        if fail_count or warn_count:
            verdict_line += (
                f" — {fail_count} fail, {warn_count} warn signals"
            )
        else:
            verdict_line += " — 0 problem signals"
        s_findings.ok(
            "findings.verdict",
            verdict_line,
            data={
                "verdict": verdict_value,
                "fail_count": fail_count,
                "warn_count": warn_count,
                "total_problem_signals": len(signals),
            },
        )
        if not signals:
            s_findings.ok(
                "findings.none",
                "no problem signals",
            )
        else:
            for idx, f in enumerate(findings_list, 1):
                ts_local = (
                    f"@{fmt_epoch_local(f['ts_ms'])} "
                    if f.get("ts_ms") else ""
                )
                line = (
                    f"{f['summary']} {ts_local}"
                    f"(see {f['ref']})"
                )
                if f["severity"] == "fail":
                    s_findings.fail(
                        f"findings.{idx:02d}",
                        line, data=f,
                    )
                else:
                    s_findings.warn(
                        f"findings.{idx:02d}",
                        line, data=f,
                    )
            if findings_more:
                s_findings.warn(
                    "findings.more",
                    f"+{findings_more} more "
                    f"(see Correlated Logs & Signals)",
                    data={
                        "more": findings_more,
                        "cap": FINDINGS_TOP_N,
                    },
                )

        # Move Findings to position 0 so it renders before Session Overview.
        # Built last so the verdict + signals are fully resolved.
        if report.sections and report.sections[-1] is s_findings:
            report.sections.insert(0, report.sections.pop())

        report.data["verdict"] = report.verdict.value
        report.elapsed_ms = (time.time() - t0) * 1000
        return report
