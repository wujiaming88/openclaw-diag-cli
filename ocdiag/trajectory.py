"""Trajectory loader — single source of truth for `<uuid>.trajectory.jsonl`.

OpenClaw 2026.5.x writes one trajectory file per session, containing 7 events
per run grouped by ``runId``. This module is the only place that opens those
files; every diag collector that wants run-level signal goes through
``iter_runs`` / ``summarize_trajectory`` / ``load_run_full``.

Implementation rules (DESIGN.md axioms #1, #2, #4, #5, #6):
- Stream line-by-line — no ``f.read()`` on multi-MB files.
- Tolerate truncated final line (writer may not flush on crash).
- Skip malformed lines but count them so callers can warn.
- Group events by runId; do not assume strict event-type order within a file.
- Schema drift (``schemaVersion`` not in ``SUPPORTED_SCHEMA_VERSIONS``) yields
  a warning string from ``detect_schema_drift`` rather than a crash.
- Files larger than ``max_size_mb`` are skipped with a stub warning record.
- ``Run`` is "complete" only when both ``session.started`` and
  ``trace.artifacts`` are present. Incomplete runs (mid-flight, crashed, or
  lost) surface explicitly via ``incomplete=True`` (axiom #5).

Sanitization helper ``sanitize_field`` is the deliberate departure from
DESIGN.md axiom #7 for trajectory-sourced free-form content (assistantTexts,
messagingToolSentTexts, prompt, finalPromptText, toolMetas[].meta): default is
*plaintext* for diagnostic value; ``--mask`` opts INTO sanitization. README
and CHANGELOG must call this out clearly. Non-trajectory free-form content is
unchanged — still default-mask.
"""

from __future__ import annotations

import glob
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from . import paths
from .sensitive import sanitize_text


TRAJECTORY_SCHEMA = "openclaw-trajectory"
SUPPORTED_SCHEMA_VERSIONS = (1,)

# Files larger than this are skipped with an explicit warning. Real-world
# trajectories sit well below this (typical 200KB–3MB; max ~4MB observed in
# our reference dataset). 50MB protects against pathological growth or a
# misidentified file type.
DEFAULT_MAX_FILE_MB = 50

# Threadpool worker count for multi-file scans. Keep this low — these are
# I/O-bound JSONL reads that compete for disk cache, not compute. Override
# with ``OCDIAG_TRAJECTORY_WORKERS=N``.
DEFAULT_WORKERS = 4

EVENT_TYPES = (
    "session.started",
    "trace.metadata",
    "context.compiled",
    "prompt.submitted",
    "model.completed",
    "trace.artifacts",
    "session.ended",
)


# ── public dataclass ──

@dataclass
class Run:
    """One agent run distilled from 7 trajectory events.

    Provenance: every field below is sourced from a specific event; comments
    name the event so callers can audit. ``raw_events`` is empty unless the
    Run was loaded via ``load_run_full``.
    """

    session_id: str
    run_id: str
    session_key: str = ""
    workspace_dir: str = ""
    provider: str = ""
    model_id: str = ""
    model_api: str = ""
    # session.started
    started_ts_ms: int = 0
    trigger: str = "unknown"
    agent_id: str = ""
    message_provider: str = ""
    message_channel: str = ""
    tool_count: int = 0
    client_tool_count: int = 0
    # session.ended (None when run is incomplete / crashed)
    ended_ts_ms: Optional[int] = None
    # trace.artifacts
    final_status: Optional[str] = None
    aborted: bool = False
    external_abort: bool = False
    timed_out: bool = False
    idle_timed_out: bool = False
    timed_out_during_compaction: bool = False
    timed_out_during_tool_execution: bool = False
    prompt_error_source: Optional[str] = None
    usage_input: int = 0
    usage_output: int = 0
    usage_cache_read: int = 0
    usage_cache_write: int = 0
    usage_total: int = 0
    cache_broke: Optional[bool] = None
    compaction_count: int = 0
    started_count: int = 0
    completed_count: int = 0
    active_count: int = 0
    did_send_via_messaging_tool: bool = False
    messaging_targets: List[Any] = field(default_factory=list)
    messaging_text_count: int = 0
    messaging_texts: List[str] = field(default_factory=list)
    successful_cron_adds: int = 0
    tool_metas: List[Dict[str, Any]] = field(default_factory=list)
    # Fallback when tool_metas is empty (typical for stuck/aborted runs whose
    # toolCalls never produced a meta payload): the most recent unmatched
    # toolCall names extracted from model.completed.messagesSnapshot. v0.6.1.
    last_tool_call_names: List[str] = field(default_factory=list)
    assistant_texts: List[str] = field(default_factory=list)
    final_prompt_text: Optional[str] = None
    # trace.metadata.prompting.systemPromptReport
    system_prompt_chars: int = 0
    system_prompt_project_chars: int = 0
    system_prompt_non_project_chars: int = 0
    skills_prompt_chars: int = 0
    tools_schema_chars: int = 0
    bootstrap_truncated_files: int = 0
    bootstrap_near_limit_files: int = 0
    injected_workspace_files: List[Dict[str, Any]] = field(default_factory=list)
    skills_top_entries: List[Dict[str, Any]] = field(default_factory=list)
    tools_top_entries: List[Dict[str, Any]] = field(default_factory=list)
    skill_count: int = 0
    skill_ids: List[str] = field(default_factory=list)
    # trace.metadata.plugins
    plugin_entries: List[Dict[str, Any]] = field(default_factory=list)
    imported_runtime_plugin_ids: List[str] = field(default_factory=list)
    # trace.metadata.harness / runtime
    harness_version: str = ""
    harness_node: str = ""
    invocation: List[str] = field(default_factory=list)
    redaction_modes: Dict[str, str] = field(default_factory=dict)
    runtime_config: Dict[str, Any] = field(default_factory=dict)
    skills_snapshot_version: Any = None
    # bookkeeping
    incomplete: bool = True   # True until both session.started + trace.artifacts seen
    schema_version_seen: int = 0
    source_file: str = ""
    raw_events: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Convenience
    @property
    def duration_ms(self) -> Optional[int]:
        """Wall-clock duration in ms, or None if either bound is missing."""
        if self.started_ts_ms and self.ended_ts_ms:
            d = self.ended_ts_ms - self.started_ts_ms
            return d if d >= 0 else None
        return None


# ── parsing helpers ──

def _iso_to_ms(iso: str) -> int:
    """Convert an ISO-8601 trajectory timestamp to epoch ms. 0 on error."""
    if not iso:
        return 0
    try:
        s = iso.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except (ValueError, AttributeError):
        return 0


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _safe_int(v: Any) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        try:
            return int(v)
        except (ValueError, OverflowError):
            return 0
    return 0


def _safe_bool(v: Any) -> bool:
    return bool(v) if isinstance(v, (bool, int, float, str)) else False


# ── discovery ──

def discover_trajectory_files(
    sessions_base: str = paths.SESSIONS_BASE,
) -> List[str]:
    """Return absolute paths of every ``*.trajectory.jsonl`` under any agent.

    Returns paths sorted by mtime (newest first) so windowed callers can
    stop early. Missing ``sessions_base`` returns an empty list — that is a
    routine "no data" condition, not an error (axiom #5).
    """
    if not sessions_base or not os.path.isdir(sessions_base):
        return []
    files: List[tuple] = []
    for agent_dir in os.listdir(sessions_base):
        sd = os.path.join(sessions_base, agent_dir, "sessions")
        if not os.path.isdir(sd):
            continue
        try:
            for entry in os.listdir(sd):
                if not entry.endswith(".trajectory.jsonl"):
                    continue
                p = os.path.join(sd, entry)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                files.append((mt, p))
        except OSError:
            continue
    files.sort(reverse=True)
    return [p for _, p in files]


def trajectory_file_for_session(session_file: str) -> Optional[str]:
    """Given ``/.../<uuid>.jsonl``, return the sibling
    ``/.../<uuid>.trajectory.jsonl`` if it exists, else None."""
    if not session_file:
        return None
    d = os.path.dirname(session_file)
    base = os.path.basename(session_file).split(".jsonl", 1)[0]
    candidate = os.path.join(d, f"{base}.trajectory.jsonl")
    return candidate if os.path.isfile(candidate) else None


# ── streaming parser ──

def _parse_lines(traj_path: str, max_size_mb: int):
    """Yield (record_dict_or_None, parse_warn_count) for each line in file.

    Returns a generator. Streams line-by-line; tolerates truncated final
    line, malformed JSON, and missing top-level fields. Parse warnings are
    counted but suppressed — caller can fold them into a summary.
    """
    try:
        size = os.path.getsize(traj_path)
    except OSError:
        return
    if size > max_size_mb * 1024 * 1024:
        # Surface oversize as a synthetic record so callers can flag it.
        yield {
            "_oversize": True,
            "_path": traj_path,
            "_size_mb": size / (1024 * 1024),
        }
        return
    try:
        f = open(traj_path, "rb")
    except OSError:
        return
    try:
        for raw in f:
            try:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            except Exception:  # noqa: BLE001 — defensive on weird encodings
                continue
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Truncated last line or stray garbage — skip silently. The
                # caller can detect via summary.parse_warnings if desired.
                continue
            if not isinstance(rec, dict):
                continue
            yield rec
    finally:
        try:
            f.close()
        except OSError:
            pass


def detect_schema_drift(traj_path: str) -> Optional[str]:
    """Peek at the first record. If ``schemaVersion`` is not in
    ``SUPPORTED_SCHEMA_VERSIONS``, return its string form so callers can warn
    rather than crash. Returns None when supported / unreadable / empty.
    """
    for rec in _parse_lines(traj_path, DEFAULT_MAX_FILE_MB):
        if rec.get("_oversize"):
            return None
        sv = rec.get("schemaVersion")
        if isinstance(sv, int) and sv not in SUPPORTED_SCHEMA_VERSIONS:
            return str(sv)
        return None
    return None


def _merge_event(run: Run, ev: Dict[str, Any], populate_raw: bool) -> None:
    """Fold one trajectory event into the running ``Run`` snapshot.

    Best-effort: each section is wrapped in a try/except that swallows so a
    single weird event cannot abort the whole run extraction (axiom #6).
    """
    etype = ev.get("type") or ""
    data = ev.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    sv = ev.get("schemaVersion")
    if isinstance(sv, int):
        run.schema_version_seen = max(run.schema_version_seen, sv)

    if not run.session_id:
        run.session_id = _safe_str(ev.get("sessionId"))
    if not run.session_key:
        run.session_key = _safe_str(ev.get("sessionKey"))
    if not run.workspace_dir:
        run.workspace_dir = _safe_str(ev.get("workspaceDir"))
    if not run.provider:
        run.provider = _safe_str(ev.get("provider"))
    if not run.model_id:
        run.model_id = _safe_str(ev.get("modelId"))
    if not run.model_api:
        run.model_api = _safe_str(ev.get("modelApi"))

    if populate_raw:
        run.raw_events[etype] = ev

    try:
        if etype == "session.started":
            run.started_ts_ms = _iso_to_ms(_safe_str(ev.get("ts")))
            run.trigger = _safe_str(data.get("trigger")) or "unknown"
            run.agent_id = _safe_str(data.get("agentId"))
            run.message_provider = _safe_str(data.get("messageProvider"))
            run.message_channel = _safe_str(data.get("messageChannel"))
            run.tool_count = _safe_int(data.get("toolCount"))
            run.client_tool_count = _safe_int(data.get("clientToolCount"))
        elif etype == "trace.metadata":
            harness = data.get("harness") or {}
            if isinstance(harness, dict):
                run.harness_version = _safe_str(harness.get("version"))
                rt = harness.get("runtime") or {}
                if isinstance(rt, dict):
                    run.harness_node = _safe_str(rt.get("node"))
                inv = harness.get("invocation")
                if isinstance(inv, list):
                    run.invocation = [_safe_str(x) for x in inv]
            redaction = data.get("redaction") or {}
            if isinstance(redaction, dict):
                run.redaction_modes = {
                    k: _safe_str(v) for k, v in redaction.items()
                    if isinstance(v, str)
                }
            cfg = data.get("config") or {}
            if isinstance(cfg, dict):
                rt_cfg = cfg.get("runtime")
                if isinstance(rt_cfg, dict):
                    # Copy only scalar values to keep memory bounded.
                    run.runtime_config = {
                        k: v for k, v in rt_cfg.items()
                        if isinstance(v, (str, int, float, bool, list)) and
                           (not isinstance(v, list) or len(v) < 50)
                    }
            plugins = data.get("plugins") or {}
            if isinstance(plugins, dict):
                ents = plugins.get("entries")
                if isinstance(ents, list):
                    # Keep minimal copy; full entries available via raw_events.
                    run.plugin_entries = [
                        {
                            "id": _safe_str(e.get("id")),
                            "name": _safe_str(e.get("name")),
                            "version": _safe_str(e.get("version")),
                            "enabled": _safe_bool(e.get("enabled")),
                            "activated": _safe_bool(e.get("activated")),
                            "status": _safe_str(e.get("status")),
                            "error": e.get("error") if e.get("error") else None,
                            "activationSource": _safe_str(e.get("activationSource")),
                            "activationReason": _safe_str(e.get("activationReason")),
                        }
                        for e in ents if isinstance(e, dict)
                    ]
                imp = plugins.get("importedRuntimePluginIds")
                if isinstance(imp, list):
                    run.imported_runtime_plugin_ids = [_safe_str(x) for x in imp]
            skills = data.get("skills") or {}
            if isinstance(skills, dict):
                run.skills_snapshot_version = skills.get("snapshotVersion")
                ents = skills.get("entries")
                if isinstance(ents, list):
                    run.skill_count = len(ents)
                    run.skill_ids = [
                        _safe_str(e.get("id"))
                        for e in ents if isinstance(e, dict)
                    ]
            prompting = data.get("prompting") or {}
            if isinstance(prompting, dict):
                spr = prompting.get("systemPromptReport") or {}
                if isinstance(spr, dict):
                    sp = spr.get("systemPrompt") or {}
                    if isinstance(sp, dict):
                        run.system_prompt_chars = _safe_int(sp.get("chars"))
                        run.system_prompt_project_chars = _safe_int(
                            sp.get("projectContextChars"))
                        run.system_prompt_non_project_chars = _safe_int(
                            sp.get("nonProjectContextChars"))
                    sk = spr.get("skills") or {}
                    if isinstance(sk, dict):
                        run.skills_prompt_chars = _safe_int(sk.get("promptChars"))
                        ents = sk.get("entries") or []
                        if isinstance(ents, list):
                            ents_sorted = sorted(
                                (e for e in ents if isinstance(e, dict)),
                                key=lambda e: -_safe_int(e.get("blockChars")),
                            )
                            run.skills_top_entries = [
                                {
                                    "name": _safe_str(e.get("name")),
                                    "blockChars": _safe_int(e.get("blockChars")),
                                }
                                for e in ents_sorted[:20]
                            ]
                    tl = spr.get("tools") or {}
                    if isinstance(tl, dict):
                        run.tools_schema_chars = _safe_int(tl.get("schemaChars"))
                        ents = tl.get("entries") or []
                        if isinstance(ents, list):
                            ents_sorted = sorted(
                                (e for e in ents if isinstance(e, dict)),
                                key=lambda e: -_safe_int(e.get("schemaChars")),
                            )
                            run.tools_top_entries = [
                                {
                                    "name": _safe_str(e.get("name")),
                                    "schemaChars": _safe_int(e.get("schemaChars")),
                                    "summaryChars": _safe_int(e.get("summaryChars")),
                                    "propertiesCount": _safe_int(
                                        e.get("propertiesCount")),
                                }
                                for e in ents_sorted[:20]
                            ]
                    bt = spr.get("bootstrapTruncation") or {}
                    if isinstance(bt, dict):
                        run.bootstrap_truncated_files = _safe_int(
                            bt.get("truncatedFiles"))
                        run.bootstrap_near_limit_files = _safe_int(
                            bt.get("nearLimitFiles"))
                inj = spr.get("injectedWorkspaceFiles") or []
                if isinstance(inj, list):
                    run.injected_workspace_files = [
                        {
                            "name": _safe_str(f.get("name")),
                            "rawChars": _safe_int(f.get("rawChars")),
                            "injectedChars": _safe_int(f.get("injectedChars")),
                            "truncated": _safe_bool(f.get("truncated")),
                            "missing": _safe_bool(f.get("missing")),
                        }
                        for f in inj if isinstance(f, dict)
                    ]
        elif etype == "model.completed":
            usage = data.get("usage") or {}
            if isinstance(usage, dict):
                run.usage_input = _safe_int(usage.get("input"))
                run.usage_output = _safe_int(usage.get("output"))
                run.usage_cache_read = _safe_int(usage.get("cacheRead"))
                run.usage_cache_write = _safe_int(usage.get("cacheWrite"))
                run.usage_total = _safe_int(usage.get("total"))
            pc = data.get("promptCache") or {}
            if isinstance(pc, dict):
                obs = pc.get("observation") or {}
                if isinstance(obs, dict):
                    broke = obs.get("broke")
                    if isinstance(broke, bool):
                        run.cache_broke = broke
            run.compaction_count = _safe_int(data.get("compactionCount"))
            ats = data.get("assistantTexts")
            if isinstance(ats, list):
                run.assistant_texts = [_safe_str(t) for t in ats if t]
            fpt = data.get("finalPromptText")
            if isinstance(fpt, str):
                run.final_prompt_text = fpt
            # Extract toolCall names from messagesSnapshot for the
            # `last_tool_call_names` fallback. Used by run_health when
            # tool_metas is empty (e.g. stuck/aborted runs whose tools
            # never finished and therefore never wrote a meta entry).
            # We collect ALL toolCall.name in order; the consumer caps
            # display length. Unmatched (no toolResult) ones are kept;
            # already-matched ones are filtered to surface signal of
            # what's actually pending. v0.6.1.
            ms = data.get("messagesSnapshot")
            if isinstance(ms, list) and ms:
                tc_seq: List[Dict[str, str]] = []  # [{id, name}]
                seen_results: set = set()
                for msg in ms:
                    if not isinstance(msg, dict):
                        continue
                    # Shape B: top-level toolResult message
                    # ({role: "toolResult", toolCallId|toolUseId|id: "..."}).
                    # This is the shape used elsewhere in this repo (e.g.
                    # 07_performance.py reads msg.toolCallId directly).
                    role = msg.get("role")
                    if role == "toolResult":
                        rid = (msg.get("toolCallId") or msg.get("toolUseId")
                               or msg.get("id"))
                        if rid:
                            seen_results.add(_safe_str(rid))
                        # Note: don't continue — some shapes also have a
                        # content list mirroring the same id; harmless to
                        # double-record into a set.
                    cnt = msg.get("content")
                    if not isinstance(cnt, list):
                        continue
                    for c in cnt:
                        if not isinstance(c, dict):
                            continue
                        ct = c.get("type")
                        if ct == "toolCall":
                            tc_seq.append({
                                "id": _safe_str(c.get("id") or c.get("toolUseId")),
                                "name": _safe_str(c.get("name")),
                            })
                        elif ct == "toolResult":
                            # Shape A: toolResult inside content list
                            # ({type: "toolResult", toolUseId: "..."}).
                            rid = (c.get("toolUseId") or c.get("id")
                                   or c.get("toolCallId"))
                            if rid:
                                seen_results.add(_safe_str(rid))
                # Prefer unmatched (still-active) toolCalls; if all matched
                # (i.e. snapshot taken after a normal turn), fall back to the
                # last few toolCall names regardless.
                unmatched = [
                    tc["name"] for tc in tc_seq
                    if tc["name"] and tc["id"] and tc["id"] not in seen_results
                ]
                if unmatched:
                    run.last_tool_call_names = unmatched[-10:]
                else:
                    names = [tc["name"] for tc in tc_seq if tc["name"]]
                    if names:
                        run.last_tool_call_names = names[-10:]
        elif etype == "trace.artifacts":
            run.final_status = _safe_str(data.get("finalStatus")) or None
            run.aborted = _safe_bool(data.get("aborted"))
            run.external_abort = _safe_bool(data.get("externalAbort"))
            run.timed_out = _safe_bool(data.get("timedOut"))
            run.idle_timed_out = _safe_bool(data.get("idleTimedOut"))
            run.timed_out_during_compaction = _safe_bool(
                data.get("timedOutDuringCompaction"))
            run.timed_out_during_tool_execution = _safe_bool(
                data.get("timedOutDuringToolExecution"))
            pes = data.get("promptErrorSource")
            run.prompt_error_source = _safe_str(pes) if pes else None
            usage = data.get("usage") or {}
            if isinstance(usage, dict):
                # trace.artifacts overrides model.completed (it is the final)
                run.usage_input = _safe_int(usage.get("input"))
                run.usage_output = _safe_int(usage.get("output"))
                run.usage_cache_read = _safe_int(usage.get("cacheRead"))
                run.usage_cache_write = _safe_int(usage.get("cacheWrite"))
                run.usage_total = _safe_int(usage.get("total"))
            pc = data.get("promptCache") or {}
            if isinstance(pc, dict):
                obs = pc.get("observation") or {}
                if isinstance(obs, dict):
                    broke = obs.get("broke")
                    if isinstance(broke, bool):
                        run.cache_broke = broke
            il = data.get("itemLifecycle") or {}
            if isinstance(il, dict):
                run.started_count = _safe_int(il.get("startedCount"))
                run.completed_count = _safe_int(il.get("completedCount"))
                run.active_count = _safe_int(il.get("activeCount"))
            tm = data.get("toolMetas") or []
            if isinstance(tm, list):
                run.tool_metas = [
                    {
                        "toolName": _safe_str(m.get("toolName")),
                        "meta": m.get("meta"),
                    }
                    for m in tm if isinstance(m, dict)
                ]
            run.did_send_via_messaging_tool = _safe_bool(
                data.get("didSendViaMessagingTool"))
            mtt = data.get("messagingToolSentTargets") or []
            if isinstance(mtt, list):
                run.messaging_targets = list(mtt)
            mts = data.get("messagingToolSentTexts") or []
            if isinstance(mts, list):
                run.messaging_text_count = len(mts)
                run.messaging_texts = [_safe_str(t) for t in mts if t]
            run.successful_cron_adds = _safe_int(data.get("successfulCronAdds"))
            run.compaction_count = _safe_int(data.get("compactionCount"))
            ats = data.get("assistantTexts")
            if isinstance(ats, list) and not run.assistant_texts:
                run.assistant_texts = [_safe_str(t) for t in ats if t]
        elif etype == "session.ended":
            run.ended_ts_ms = _iso_to_ms(_safe_str(ev.get("ts")))
            if not run.final_status:
                fs = data.get("status")
                if fs:
                    run.final_status = _safe_str(fs)
            # session.ended also re-asserts abort flags; trust the artifact
            # values when they're already set, else fold these in.
            if not run.aborted:
                run.aborted = _safe_bool(data.get("aborted"))
            if not run.external_abort:
                run.external_abort = _safe_bool(data.get("externalAbort"))
            if not run.timed_out:
                run.timed_out = _safe_bool(data.get("timedOut"))
            if not run.idle_timed_out:
                run.idle_timed_out = _safe_bool(data.get("idleTimedOut"))
    except Exception:  # noqa: BLE001 — never crash run extraction
        # Each event section is best-effort; one weird shape must not abort
        # the whole run. Caller still sees whatever fields we did manage to
        # populate.
        return


# ── primary stream API ──

def iter_runs(
    traj_path: str,
    *,
    max_size_mb: int = DEFAULT_MAX_FILE_MB,
    populate_raw: bool = False,
) -> Iterator[Run]:
    """Yield one ``Run`` per distinct ``runId`` in the file.

    Events are merged in arrival order (writer guarantees seq order in
    practice, but we tolerate reordering). A run is marked ``incomplete`` if
    either ``session.started`` or ``trace.artifacts`` is missing.

    ``populate_raw=True`` keeps full event records on each Run for callers
    that need to render them (e.g. ``trace`` command). Default False to
    bound peak memory.
    """
    if not traj_path or not os.path.isfile(traj_path):
        return
    runs: Dict[str, Run] = {}
    seen_started: set = set()
    seen_artifacts: set = set()

    for ev in _parse_lines(traj_path, max_size_mb):
        if ev.get("_oversize"):
            # Synthesize a single "oversize" sentinel run — caller can detect
            # it via run.incomplete and source_file but we still don't crash.
            run = Run(session_id="", run_id="oversize",
                      source_file=traj_path, incomplete=True)
            yield run
            return
        if ev.get("traceSchema") and ev.get("traceSchema") != TRAJECTORY_SCHEMA:
            # Stray sibling artifact — skip.
            continue
        rid = _safe_str(ev.get("runId"))
        if not rid:
            continue
        run = runs.get(rid)
        if run is None:
            run = Run(session_id=_safe_str(ev.get("sessionId")), run_id=rid,
                      source_file=traj_path)
            runs[rid] = run
        _merge_event(run, ev, populate_raw)
        et = ev.get("type")
        if et == "session.started":
            seen_started.add(rid)
        elif et == "trace.artifacts":
            seen_artifacts.add(rid)

    for rid, run in runs.items():
        run.incomplete = not (rid in seen_started and rid in seen_artifacts)
        yield run


def load_run_full(traj_path: str, run_id: str) -> Optional[Run]:
    """Return the named run with ``raw_events`` populated, or None on miss."""
    if not run_id:
        return None
    for run in iter_runs(traj_path, populate_raw=True):
        if run.run_id == run_id:
            return run
    return None


def summarize_trajectory(traj_path: str) -> Dict[str, Any]:
    """Return per-file aggregate counts. Cheap; designed for collectors that
    only need totals (sessions, recent_errors, cron_jobs).

    Streams once; never holds raw events.
    """
    summary: Dict[str, Any] = {
        "path": traj_path,
        "size_bytes": 0,
        "schema_version_seen": 0,
        "schema_drift": None,
        "total_runs": 0,
        "incomplete_runs": 0,
        "by_trigger": {},
        "by_final_status": {},
        "by_abort_flag": {
            "aborted": 0,
            "externalAbort": 0,
            "timedOut": 0,
            "idleTimedOut": 0,
            "timedOutDuringCompaction": 0,
            "timedOutDuringToolExecution": 0,
        },
        "active_leak_runs": 0,
        "first_started_ts_ms": 0,
        "last_started_ts_ms": 0,
    }
    try:
        summary["size_bytes"] = os.path.getsize(traj_path)
    except OSError:
        return summary
    drift = detect_schema_drift(traj_path)
    if drift is not None:
        summary["schema_drift"] = drift

    for run in iter_runs(traj_path):
        summary["total_runs"] += 1
        if run.schema_version_seen:
            summary["schema_version_seen"] = max(
                summary["schema_version_seen"], run.schema_version_seen)
        if run.incomplete:
            summary["incomplete_runs"] += 1
        bt = summary["by_trigger"]
        bt[run.trigger] = bt.get(run.trigger, 0) + 1
        fs = run.final_status or "incomplete"
        bs = summary["by_final_status"]
        bs[fs] = bs.get(fs, 0) + 1
        abort_map = summary["by_abort_flag"]
        if run.aborted:
            abort_map["aborted"] += 1
        if run.external_abort:
            abort_map["externalAbort"] += 1
        if run.timed_out:
            abort_map["timedOut"] += 1
        if run.idle_timed_out:
            abort_map["idleTimedOut"] += 1
        if run.timed_out_during_compaction:
            abort_map["timedOutDuringCompaction"] += 1
        if run.timed_out_during_tool_execution:
            abort_map["timedOutDuringToolExecution"] += 1
        if run.active_count > 0:
            summary["active_leak_runs"] += 1
        if run.started_ts_ms:
            if not summary["first_started_ts_ms"] or \
                    run.started_ts_ms < summary["first_started_ts_ms"]:
                summary["first_started_ts_ms"] = run.started_ts_ms
            if run.started_ts_ms > summary["last_started_ts_ms"]:
                summary["last_started_ts_ms"] = run.started_ts_ms
    return summary


# ── multi-file scanning ──

def get_workers() -> int:
    """Resolve OCDIAG_TRAJECTORY_WORKERS env override."""
    try:
        n = int(os.environ.get("OCDIAG_TRAJECTORY_WORKERS", DEFAULT_WORKERS))
        return max(1, min(n, 32))
    except (ValueError, TypeError):
        return DEFAULT_WORKERS


def collect_runs(
    traj_paths: List[str],
    *,
    since_ms: Optional[int] = None,
    limit_per_file: Optional[int] = None,
    populate_raw: bool = False,
) -> List[Run]:
    """Scan multiple trajectory files in parallel and return a flat list of
    Runs.

    - ``since_ms``: drop runs whose ``started_ts_ms`` is older than this.
      0/missing started_ts is kept (incomplete runs deserve to be surfaced).
    - ``limit_per_file``: cap runs returned per file, newest first. Useful
      for very large files where only the tail is interesting.
    """
    if not traj_paths:
        return []
    workers = get_workers()

    def _one(path: str) -> List[Run]:
        rs: List[Run] = []
        for run in iter_runs(path, populate_raw=populate_raw):
            if since_ms is not None and run.started_ts_ms and \
                    run.started_ts_ms < since_ms:
                continue
            rs.append(run)
        if limit_per_file is not None and len(rs) > limit_per_file:
            rs.sort(key=lambda r: r.started_ts_ms, reverse=True)
            rs = rs[:limit_per_file]
        return rs

    out: List[Run] = []
    if workers <= 1 or len(traj_paths) <= 1:
        for p in traj_paths:
            out.extend(_one(p))
        return out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for chunk in ex.map(_one, traj_paths):
            out.extend(chunk)
    return out


def collect_summaries(traj_paths: List[str]) -> List[Dict[str, Any]]:
    """Parallel ``summarize_trajectory`` over many files."""
    if not traj_paths:
        return []
    workers = get_workers()
    if workers <= 1 or len(traj_paths) <= 1:
        return [summarize_trajectory(p) for p in traj_paths]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(summarize_trajectory, traj_paths))


# ── sanitization helper (deliberate axiom #7 departure) ──

def sanitize_field(value: Any, mask: bool = False) -> Any:
    """Return ``value`` plaintext (default) or scrubbed (``mask=True``).

    Trajectory-sourced fields — assistantTexts, messagingToolSentTexts,
    finalPromptText, toolMetas[].meta — are NOT masked by default. This is a
    deliberate departure from DESIGN.md axiom #7: the diagnostic value of
    seeing the actual content outweighs the leakage risk for trajectory
    files (which already live under user home + are gated by --json/--mask
    awareness in CLI usage).

    Non-trajectory free-form content (shell history, plugin error samples,
    systemd unit files, session bodies) does NOT route through this helper —
    it keeps using ``ocdiag.sensitive.sanitize_text`` with default-on
    masking.
    """
    if not mask:
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_field(v, mask=True) for v in value]
    if isinstance(value, dict):
        return {k: sanitize_field(v, mask=True) for k, v in value.items()}
    return value


# ── ergonomic helpers for collectors ──

def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def ms_ago(ms_window: int) -> int:
    return now_ms() - ms_window


__all__ = [
    "TRAJECTORY_SCHEMA",
    "SUPPORTED_SCHEMA_VERSIONS",
    "EVENT_TYPES",
    "Run",
    "discover_trajectory_files",
    "trajectory_file_for_session",
    "iter_runs",
    "load_run_full",
    "summarize_trajectory",
    "detect_schema_drift",
    "collect_runs",
    "collect_summaries",
    "sanitize_field",
    "get_workers",
    "now_ms",
    "ms_ago",
]
