"""Correlation-graph expansion for the panorama inspector.

Given a ``sessionId``, walk the standard OpenClaw data sources and discover
every additional identifier that is provably correlated with it:

  - ``sessionKey`` from ``sessions.json``
  - ``runIds`` from trajectory events and app logs
  - ``toolCallIds`` from session.jsonl
  - ``childSessionIds`` from ``runs.sqlite`` (children whose
    ``requester_session_key`` matches our session's key)
  - ``cronJobId`` parsed from a ``cron:<jobId>`` segment of the session key

Each identifier is recorded with a ``path`` describing how it was discovered
from ``sessionId``. A log entry is then "correlated" if it contains ANY of
these identifiers; the same path can be attached to the entry as proof of
relevance — no whitelist, no subjective judgment.

The module is intentionally pure: nothing here writes, nothing here calls
external services, and every loader degrades silently when its source is
missing or unparseable.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# ID expansion ──────────────────────────────────────────────────────────────


@dataclass
class CorrelationGraph:
    """Set of identifiers transitively reachable from a session.

    ``paths`` maps each discovered identifier to the chain of edges that
    proved its relevance, e.g. ``"sessionId → sessions.json → sessionKey"``.
    The first key (``sessionId``) seeds the graph with the trivial path.
    """

    session_id: str
    session_key: Optional[str] = None
    run_ids: Set[str] = field(default_factory=set)
    tool_call_ids: Set[str] = field(default_factory=set)
    child_session_ids: Set[str] = field(default_factory=set)
    cron_job_id: Optional[str] = None
    paths: Dict[str, str] = field(default_factory=dict)
    sources_seen: Set[str] = field(default_factory=set)

    def all_ids(self) -> Set[str]:
        ids: Set[str] = {self.session_id}
        if self.session_key:
            ids.add(self.session_key)
        ids.update(self.run_ids)
        ids.update(self.tool_call_ids)
        ids.update(self.child_session_ids)
        if self.cron_job_id:
            ids.add(self.cron_job_id)
        return ids

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "sessionKey": self.session_key,
            "runIds": sorted(self.run_ids),
            "toolCallIds": sorted(self.tool_call_ids),
            "childSessionIds": sorted(self.child_session_ids),
            "cronJobId": self.cron_job_id,
            "paths": self.paths,
            "sources_seen": sorted(self.sources_seen),
        }


def _record_path(graph: CorrelationGraph, ident: str, path: str) -> None:
    if ident and ident not in graph.paths:
        graph.paths[ident] = path


def expand_from_sessions_json(
    graph: CorrelationGraph, sessions_json_path: str,
) -> None:
    """Resolve ``sessionKey`` from the per-agent ``sessions.json`` store."""
    if not os.path.isfile(sessions_json_path):
        return
    try:
        with open(sessions_json_path, "r", encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(store, dict):
        return
    graph.sources_seen.add("sessions.json")
    for key, entry in store.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("sessionId") == graph.session_id:
            graph.session_key = key
            _record_path(graph, key, "sessionId → sessions.json → sessionKey")
            break


def expand_from_trajectory(
    graph: CorrelationGraph, trajectory_path: str,
) -> None:
    """Pick up every ``runId`` referenced in the trajectory file."""
    if not trajectory_path or not os.path.isfile(trajectory_path):
        return
    graph.sources_seen.add("trajectory.jsonl")
    try:
        with open(trajectory_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = rec.get("runId")
                if isinstance(rid, str) and rid:
                    graph.run_ids.add(rid)
                    _record_path(
                        graph, rid,
                        "sessionId → trajectory.jsonl → runId",
                    )
    except OSError:
        return


def expand_from_session_jsonl(
    graph: CorrelationGraph, session_file: str,
) -> None:
    """Collect every ``toolCallId`` mentioned in the conversation file."""
    if not session_file or not os.path.isfile(session_file):
        return
    graph.sources_seen.add("session.jsonl")
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                # Tool result message: callId on the outer message.
                tcid = msg.get("toolCallId")
                if isinstance(tcid, str) and tcid:
                    graph.tool_call_ids.add(tcid)
                    _record_path(
                        graph, tcid,
                        "sessionId → session.jsonl → toolCallId",
                    )
                # Assistant message: scan content for embedded toolCall blocks.
                content = msg.get("content")
                if isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "toolCall":
                            cid = c.get("id")
                            if isinstance(cid, str) and cid:
                                graph.tool_call_ids.add(cid)
                                _record_path(
                                    graph, cid,
                                    "sessionId → session.jsonl → toolCallId",
                                )
    except OSError:
        return


_RUN_ID_PAT = re.compile(r"\brunId=([0-9a-fA-F-]{8,})")


def expand_from_app_logs(
    graph: CorrelationGraph,
    log_files: Iterable[str],
    session_id: str,
) -> None:
    """Pull additional ``runId`` mentions out of OpenClaw app logs.

    A line qualifies when it contains the literal ``sessionId``; from those
    lines we extract every ``runId=...`` token. This is a superset of what the
    trajectory provides — short-lived runs that crashed before flushing
    trajectory events still leave a trace here.
    """
    sid_token = session_id
    if not sid_token:
        return
    for lf in log_files:
        if not os.path.isfile(lf):
            continue
        try:
            with open(lf, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if sid_token not in line:
                        continue
                    for match in _RUN_ID_PAT.finditer(line):
                        rid = match.group(1)
                        if rid and rid not in graph.run_ids:
                            graph.run_ids.add(rid)
                            _record_path(
                                graph, rid,
                                "sessionId → app log → runId",
                            )
        except OSError:
            continue
    graph.sources_seen.add("app_log")


_CRON_KEY_PAT = re.compile(r":cron:([0-9a-fA-F-]{8,})")


def expand_from_session_key(graph: CorrelationGraph) -> None:
    """If sessionKey carries a ``cron:<jobId>`` segment, surface it."""
    sk = graph.session_key
    if not sk:
        return
    m = _CRON_KEY_PAT.search(sk)
    if m:
        job_id = m.group(1)
        graph.cron_job_id = job_id
        _record_path(
            graph, job_id,
            "sessionId → sessionKey → cron:jobId",
        )


def expand_from_runs_sqlite(
    graph: CorrelationGraph, runs_sqlite_path: str,
) -> List[Dict[str, Any]]:
    """Discover child tasks where ``requester_session_key`` matches.

    Returns the list of raw row dicts so callers can build a child-tasks
    section without re-opening the database.
    """
    if not graph.session_key:
        return []
    if not runs_sqlite_path or not os.path.isfile(runs_sqlite_path):
        return []
    rows: List[Dict[str, Any]] = []
    try:
        # ``mode=ro`` keeps us from acquiring write locks on a live db.
        uri = f"file:{runs_sqlite_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute(
                "SELECT * FROM task_runs WHERE requester_session_key = ?",
                (graph.session_key,),
            )
            for r in cur.fetchall():
                row = {k: r[k] for k in r.keys()}
                rows.append(row)
                child_key = row.get("child_session_key")
                if isinstance(child_key, str) and child_key:
                    graph.child_session_ids.add(child_key)
                    _record_path(
                        graph, child_key,
                        "sessionId → sessionKey → runs.sqlite → childSessionKey",
                    )
                # task_id is itself a child-task identifier worth correlating.
                tid = row.get("task_id")
                if isinstance(tid, str) and tid:
                    graph.child_session_ids.add(tid)
                    _record_path(
                        graph, tid,
                        "sessionId → sessionKey → runs.sqlite → taskId",
                    )
                run_id = row.get("run_id")
                if isinstance(run_id, str) and run_id:
                    graph.run_ids.add(run_id)
                    _record_path(
                        graph, run_id,
                        "sessionId → sessionKey → runs.sqlite → runId",
                    )
        finally:
            con.close()
    except sqlite3.Error:
        return rows
    graph.sources_seen.add("runs.sqlite")
    return rows


def build_graph(
    session_id: str,
    *,
    session_file: Optional[str] = None,
    sessions_json_path: Optional[str] = None,
    trajectory_path: Optional[str] = None,
    app_log_files: Optional[Iterable[str]] = None,
    runs_sqlite_path: Optional[str] = None,
) -> Tuple[CorrelationGraph, List[Dict[str, Any]]]:
    """Build the full correlation graph from ``session_id``.

    Returns ``(graph, child_task_rows)``. The child-task rows are useful for
    callers building per-child sections without re-querying ``runs.sqlite``.
    Sources that are ``None`` or missing are quietly skipped — the caller
    decides whether to surface a "missing source" warning.
    """
    graph = CorrelationGraph(session_id=session_id)
    _record_path(graph, session_id, "seed")

    if sessions_json_path:
        expand_from_sessions_json(graph, sessions_json_path)
    if session_file:
        expand_from_session_jsonl(graph, session_file)
    if trajectory_path:
        expand_from_trajectory(graph, trajectory_path)
    if app_log_files:
        expand_from_app_logs(graph, app_log_files, session_id)
    expand_from_session_key(graph)
    rows: List[Dict[str, Any]] = []
    if runs_sqlite_path:
        rows = expand_from_runs_sqlite(graph, runs_sqlite_path)
    return graph, rows


# Log filtering ─────────────────────────────────────────────────────────────


def matched_ids(line: str, ids: Iterable[str]) -> List[str]:
    """Cheap pre-filter: which ids appear as substrings of ``line``?

    Operating on the raw line (not parsed JSON) is the fast path for huge log
    files. Callers JSON-parse only the lines that survive this check.
    """
    return [i for i in ids if i and i in line]


def annotate_with_correlation(
    record: Dict[str, Any],
    graph: CorrelationGraph,
    matched: Iterable[str],
) -> Dict[str, Any]:
    """Attach a ``correlation`` block listing every matched id and its path.

    The first matched id (in iteration order) wins ``primary``; the rest sit
    in ``additional`` so a renderer can show "matched by N ids" without
    digging through the whole list.
    """
    matched_list = list(matched)
    paths = {mid: graph.paths.get(mid, "?") for mid in matched_list}
    record = dict(record)
    record["correlation"] = {
        "ids": matched_list,
        "primary": matched_list[0] if matched_list else None,
        "path": paths.get(matched_list[0]) if matched_list else None,
        "paths": paths,
    }
    return record


def filter_log_file(
    log_file: str,
    graph: CorrelationGraph,
    *,
    strict: bool = False,
    max_records: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return every parsed log record whose raw line mentions a graph id.

    ``strict`` restricts matching to ``sessionId`` or any ``runId`` — useful
    for noisy multi-tenant log files where ``sessionKey`` would otherwise
    drag in unrelated traffic from the same channel.
    """
    if strict:
        ids: Set[str] = {graph.session_id, *graph.run_ids}
    else:
        ids = graph.all_ids()
    ids = {i for i in ids if i}
    if not ids:
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if max_records is not None and len(out) >= max_records:
                    break
                hits = matched_ids(line, ids)
                if not hits:
                    continue
                try:
                    rec = json.loads(line.strip())
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                out.append(annotate_with_correlation(rec, graph, hits))
    except OSError:
        return out
    return out


def filter_log_files(
    log_files: Iterable[str],
    graph: CorrelationGraph,
    *,
    strict: bool = False,
    max_records: Optional[int] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for lf in log_files:
        if not os.path.isfile(lf):
            continue
        remaining: Optional[int] = None
        if max_records is not None:
            remaining = max_records - len(out)
            if remaining <= 0:
                break
        out.extend(filter_log_file(
            lf, graph, strict=strict, max_records=remaining,
        ))
    return out


# OTel traceId expansion ────────────────────────────────────────────────────
#
# OpenClaw emits OTel ``traceId`` (32-hex) on every gateway log line. Lines
# that text-mention our sessionId/runId carry it, but ~70% of lines for the
# same trace (deep-stack provider/plugin/harness logs) do NOT mention the
# session text — they share only the traceId. To pull those in we run a two-
# pass scan: first the existing graph-id filter; then a second pass that re-
# admits any line whose traceId is in the set we harvested from pass 1.

# 32-hex matches OpenTelemetry W3C trace-ids. The all-zero id is OTel's
# "unset" sentinel — it must never count as a real trace.
_OTEL_TRACE_RE = re.compile(r'"traceId"\s*:\s*"([0-9a-fA-F]{32})"')
_OTEL_ALL_ZERO = "0" * 32


def _harvest_trace_ids_from_records(
    records: Iterable[Dict[str, Any]],
    *,
    strict: bool,
) -> Set[str]:
    """Collect non-zero ``traceId``s from already-correlated records.

    In ``strict`` mode we only trust traceIds that came from a line whose
    primary correlation match was the sessionId or a runId — because those
    are the only IDs we believe represent THIS run. (sessionKey can be reused
    across runs; toolCallId is per-call but rarely substring-unique.)
    """
    out: Set[str] = set()
    for rec in records:
        tid = rec.get("traceId")
        if not isinstance(tid, str):
            continue
        if len(tid) != 32 or tid == _OTEL_ALL_ZERO:
            continue
        if strict:
            corr = rec.get("correlation") or {}
            primary = corr.get("primary")
            if primary is None:
                continue
            # Accept only when primary id is the seed sessionId or a runId
            # discovered via the graph.
            # Caller passes ``strict_seed_ids``; we read it off the record's
            # correlation block. Keep API simple: if the primary id is
            # acceptable, the path will be an empty filter handled below.
            if not corr.get("_otel_seed_ok"):
                continue
        out.add(tid)
    return out


def _scan_file_for_otel_lines(
    log_file: str,
    trace_ids: Set[str],
    already_seen_keys: Set[Tuple[int, str]],
    graph: CorrelationGraph,
    *,
    max_records: Optional[int],
) -> List[Dict[str, Any]]:
    """Single linear pass over ``log_file`` returning lines whose ``traceId``
    is in ``trace_ids`` and which have NOT already been admitted by pass 1.

    Cheapness: we substring-test the line for any trace id before running
    a regex / JSON parse. Trace ids are 32-hex unique tokens, so a substring
    hit is overwhelmingly the actual field — we still parse the JSON to
    confirm and to attach the rich record to the output.

    Dedup uses ``already_seen_keys = {(time_ms, line_hash)}``-ish identity.
    We keep it as a content-key on the cheap: ``(traceId, spanId, time, ts)``.
    Anything matching pass 1 will reproduce the same key here and be skipped.
    """
    if not trace_ids:
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if max_records is not None and len(out) >= max_records:
                    break
                # Cheap substring pre-filter: any trace id present at all?
                hit_id: Optional[str] = None
                for tid in trace_ids:
                    if tid in line:
                        hit_id = tid
                        break
                if hit_id is None:
                    continue
                # Confirm via regex on the actual JSON field — keeps us from
                # admitting lines where a 32-hex appears in free text.
                m = _OTEL_TRACE_RE.search(line)
                if not m or m.group(1) != hit_id:
                    continue
                try:
                    rec = json.loads(line.strip())
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                # Skip lines pass 1 already collected (graph-id text hit).
                seen_key = _record_identity(rec)
                if seen_key in already_seen_keys:
                    continue
                # Skip if the line ALSO mentions any graph id — it would
                # have been collected by pass 1, just with a different
                # cap/order. Belt-and-braces.
                if matched_ids(line, graph.all_ids()):
                    continue
                rec = dict(rec)
                rec["correlation"] = {
                    "ids": [hit_id],
                    "primary": hit_id,
                    "path": f"otel-trace:{hit_id}",
                    "paths": {hit_id: f"otel-trace:{hit_id}"},
                }
                out.append(rec)
    except OSError:
        return out
    return out


def _record_identity(rec: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    """Cheap content-key for dedup between pass 1 and pass 2.

    OpenClaw lines carry ``time`` (ms epoch) plus a unique ``spanId`` —
    together they form a stable identity. ``traceId`` is included so even
    spanless lines collide consistently.
    """
    return (rec.get("traceId"), rec.get("spanId"), rec.get("time"))


def filter_log_files_with_otel(
    log_files: Iterable[str],
    graph: CorrelationGraph,
    *,
    strict: bool = False,
    max_records: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Two-pass log correlation: graph-id text match, then OTel-traceId expand.

    Returns ``(records, harvested_trace_ids)``. The records list contains all
    pass-1 hits followed by pass-2 OTel-only hits, each carrying a
    ``correlation`` block. Pass-2 entries have ``correlation.path`` shaped as
    ``otel-trace:<traceId>``.

    ``strict`` mode flows through to pass 1 (sessionId / runId only) AND
    restricts traceId harvesting: only traceIds observed on a line whose
    correlation primary is the seed sessionId or a known runId are trusted.
    Other IDs (sessionKey, toolCallId) can survive a non-session run reuse,
    so we don't expand from those even when they happen to carry a traceId.
    """
    primary = filter_log_files(
        log_files, graph, strict=strict, max_records=max_records,
    )
    # Tag each primary record with whether its primary id is acceptable as
    # an OTel seed under strict rules. Done here (not in filter_log_file)
    # so the existing API stays unchanged.
    seed_ids = {graph.session_id, *graph.run_ids}
    seed_ids.discard("")
    for rec in primary:
        corr = rec.get("correlation") or {}
        if corr.get("primary") in seed_ids:
            corr["_otel_seed_ok"] = True
            rec["correlation"] = corr
    harvested = _harvest_trace_ids_from_records(primary, strict=strict)
    if not harvested:
        # Drop the internal flag before returning to callers.
        for rec in primary:
            corr = rec.get("correlation") or {}
            corr.pop("_otel_seed_ok", None)
        return primary, []
    seen = {_record_identity(rec) for rec in primary}
    extras: List[Dict[str, Any]] = []
    for lf in log_files:
        if not os.path.isfile(lf):
            continue
        remaining: Optional[int] = None
        if max_records is not None:
            remaining = max_records - len(primary) - len(extras)
            if remaining <= 0:
                break
        extras.extend(_scan_file_for_otel_lines(
            lf, harvested, seen, graph, max_records=remaining,
        ))
    # Drop the internal flag before returning to callers.
    for rec in primary:
        corr = rec.get("correlation") or {}
        corr.pop("_otel_seed_ok", None)
    return primary + extras, sorted(harvested)
