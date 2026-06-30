"""Tests for the trace inspector --all-messages flag.

Covers:
  - Single-turn path (default): byte-identical Section/Check shape vs.
    pre-1.11.0 output. Locked via the legacy section titles + report.data
    keys (timeline, model_calls, tool_execs, summary, user_message_*).
  - --all-messages path: produces N turn blocks for a fixture with N user
    messages, each carrying its own timeline/summary, plus a final
    aggregate section. report.data["all_messages"] / ["aggregate"] are
    the new keys.
  - Mutual exclusion: --all-messages combined with any single-turn
    selector returns INVALID_ARGUMENT (the inspector path validates too,
    not just the CLI parser).
  - JSON envelope and NDJSON serialize cleanly in both modes.

The fixture is fully synthetic — three user turns, each with a single
assistant reply, written to a temp $OPENCLAW_HOME just like
test_panorama.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag.core import registry  # noqa: E402
from ocdiag.core.context import DiagContext  # noqa: E402
from ocdiag.core.types import Verdict  # noqa: E402
from ocdiag.render.json_renderer import to_envelope  # noqa: E402
from ocdiag.render.ndjson import NdjsonRenderer  # noqa: E402

# Force registry to import every inspector before any test collects.
registry.discover()


SESSION_ID = "33333333-4444-5555-6666-777777777777"

# 2026-06-01 anchor + 1s steps so each turn covers a clean ~2-second window.
T0 = 1780000000000


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )[:-4] + "Z"


def _user_msg(uid: str, ts: int, text: str) -> Dict[str, Any]:
    return {
        "type": "message",
        "id": uid,
        "timestamp": _ms_to_iso(ts),
        "message": {
            "role": "user",
            "timestamp": ts,
            "content": text,
        },
    }


def _assistant_msg(
    aid: str, ts: int, record_ts: int, *,
    out_tok: int = 5, in_tok: int = 10,
) -> Dict[str, Any]:
    return {
        "type": "message",
        "id": aid,
        "timestamp": _ms_to_iso(record_ts),
        "message": {
            "role": "assistant",
            "timestamp": ts,
            "model": "test-model",
            "provider": "test",
            "stopReason": "stop",
            "usage": {
                "input": in_tok, "output": out_tok,
                "cacheRead": 0, "cacheWrite": 0,
            },
            "content": [{"type": "text", "text": "ack"}],
        },
    }


def _build_session_records(num_turns: int) -> List[Dict[str, Any]]:
    """Build a session.jsonl with ``num_turns`` user/assistant pairs.

    Each turn is 2 seconds wide; turns are separated by 5 seconds. So 3
    turns spans ~21s. analyze_phases will compute per-turn total_ms ~= 1s
    each (record-vs-message ts gap), well below SLOW_THRESHOLD_MS so the
    turns stay verdict=OK.
    """
    records: List[Dict[str, Any]] = [
        {"type": "session", "version": 3, "id": SESSION_ID,
         "timestamp": _ms_to_iso(T0)},
    ]
    for i in range(num_turns):
        u_ts = T0 + i * 5000
        a_msg_ts = u_ts + 1000
        a_rec_ts = u_ts + 2000
        records.append(_user_msg(f"user-{i}", u_ts, f"hello turn {i}"))
        records.append(_assistant_msg(f"asst-{i}", a_msg_ts, a_rec_ts))
    return records


def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _build_fixture(tmp: Path, *, num_turns: int = 3) -> DiagContext:
    home = tmp / "ocdiag-trace-home"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    session_file = main_sd / f"{SESSION_ID}.jsonl"
    _write_jsonl(session_file, _build_session_records(num_turns))

    cfg = home / "openclaw.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=agents,
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    return ctx


def _run_trace(ctx: DiagContext, **kwargs):
    inspector = registry.get("trace")
    assert inspector is not None, "trace inspector not registered"
    return inspector.collect(ctx, **kwargs)


# ── single-turn (default) — locks pre-1.11.0 contract ──────────────────────


def test_single_turn_default_uses_last_message(tmp_path: Path):
    ctx = _build_fixture(tmp_path, num_turns=3)
    report = _run_trace(ctx, session_id=SESSION_ID)
    assert report.error is None, f"unexpected error: {report.error}"
    # default = last user message (index 2 of 3)
    assert report.data["user_message_index"] == 2
    assert report.data["user_message_id"] == "user-2"
    # legacy single-turn keys must still be present
    for k in ("timeline", "model_calls", "tool_execs", "summary",
              "base_epoch_ms"):
        assert k in report.data, f"missing legacy key {k}"
    # The new --all-messages keys must NOT be set in single-turn mode.
    assert "all_messages" not in report.data
    assert "aggregate" not in report.data


def test_single_turn_section_titles_unchanged(tmp_path: Path):
    """Pin the pre-1.11.0 section titles for the single-turn path."""
    ctx = _build_fixture(tmp_path, num_turns=2)
    report = _run_trace(ctx, session_id=SESSION_ID)
    titles = [s.title for s in report.sections]
    # Must include the legacy titles, in order, with no "Turn #" prefix.
    assert "Trace · 元信息" in titles
    assert "Trace · 时间轴" in titles
    assert "Trace · 汇总" in titles
    # No turn-prefixed sections leak in
    assert not any("Turn #" in t for t in titles), titles
    # No --all-messages aggregate section
    assert "Trace · 跨消息汇总" not in titles


def test_single_turn_msg_index_selects(tmp_path: Path):
    ctx = _build_fixture(tmp_path, num_turns=3)
    report = _run_trace(ctx, session_id=SESSION_ID, msg_index=0)
    assert report.data["user_message_index"] == 0
    assert report.data["user_message_id"] == "user-0"


# ── --all-messages — every user turn in one run ────────────────────────────


def test_all_messages_produces_n_turn_blocks(tmp_path: Path):
    ctx = _build_fixture(tmp_path, num_turns=3)
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    assert report.error is None, f"unexpected error: {report.error}"

    # Per-turn payload reaches report.data
    all_msgs = report.data["all_messages"]
    assert len(all_msgs) == 3
    for i, t in enumerate(all_msgs):
        assert t["index"] == i
        assert t["user_message_id"] == f"user-{i}"
        assert "timeline" in t
        assert "summary" in t
        # Each turn should have produced exactly one model call.
        assert t["summary"]["model_count"] == 1
        # Snippet preview is populated
        assert t["user_message_snippet"].startswith("hello turn")

    # legacy single-turn keys are NOT set in this mode
    assert "timeline" not in report.data
    assert "user_message_index" not in report.data

    # aggregate carries cumulative numbers
    agg = report.data["aggregate"]
    assert agg["turns"] == 3
    assert agg["total_input_tokens"] == 30  # 3 × in=10
    assert agg["total_output_tokens"] == 15  # 3 × out=5

    # Per-turn sections exist in section list with the "Turn #i/N · " prefix.
    titles = [s.title for s in report.sections]
    for i in range(3):
        prefix = f"Turn #{i + 1}/3 · "
        assert any(t.startswith(prefix + "Trace · 时间轴") for t in titles), (
            f"missing timeline for turn {i}: {titles}"
        )
    # final aggregate section
    assert "Trace · 跨消息汇总" in titles


def test_all_messages_zero_turns_does_not_crash(tmp_path: Path):
    """Empty user-messages list should still yield a clean error path."""
    ctx = _build_fixture(tmp_path, num_turns=0)
    # We still need a session.jsonl even with no message records — the
    # fixture gives one. analyze_phases needs at least one user msg, so
    # zero turns is currently surfaced as "no message records in session".
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    assert report.error is not None
    # Must be a structured error, not a Python exception
    assert report.diag_error is not None


def test_all_messages_one_turn_yields_one_block(tmp_path: Path):
    """1 user msg in --all-messages mode → 1 turn block, no crash."""
    ctx = _build_fixture(tmp_path, num_turns=1)
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    assert report.error is None
    assert len(report.data["all_messages"]) == 1
    assert report.data["aggregate"]["turns"] == 1


# ── mutual exclusion ──────────────────────────────────────────────────────


@pytest.mark.parametrize("kw", [
    {"msg_index": 0},
    {"msg_id": "user-1"},
    {"msg_match": "hello"},
])
def test_mutex_all_messages_with_single_turn_selector(
    tmp_path: Path, kw: Dict[str, Any],
):
    ctx = _build_fixture(tmp_path, num_turns=3)
    report = _run_trace(
        ctx, session_id=SESSION_ID, all_messages=True, **kw,
    )
    assert report.error is not None
    assert report.diag_error is not None
    assert report.diag_error.code == "INVALID_ARGUMENT"


# ── render contracts ──────────────────────────────────────────────────────


def test_json_envelope_roundtrips_in_all_messages_mode(tmp_path: Path):
    ctx = _build_fixture(tmp_path, num_turns=3)
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    env = to_envelope(report)
    assert env["ok"] is True
    # Whole envelope must JSON-serialize cleanly (no datetime / set / etc.).
    json.dumps(env, ensure_ascii=False)
    data = env["data"]
    # Top-level data carries the new keys
    assert "all_messages" in data["data"]
    assert "aggregate" in data["data"]
    # And does NOT carry the legacy single-turn keys
    assert "timeline" not in data["data"]


def test_json_envelope_single_turn_keeps_legacy_data_keys(tmp_path: Path):
    ctx = _build_fixture(tmp_path, num_turns=2)
    report = _run_trace(ctx, session_id=SESSION_ID)
    env = to_envelope(report)
    data = env["data"]["data"]
    for k in ("timeline", "model_calls", "tool_execs", "summary",
              "user_message_index", "user_message_id"):
        assert k in data, f"single-turn JSON missing legacy key {k}"


def test_ndjson_emits_one_line_per_section_in_all_messages(tmp_path: Path):
    ctx = _build_fixture(tmp_path, num_turns=2)
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    buf = io.StringIO()
    NdjsonRenderer(stream=buf).write(report)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    # At minimum: scope + overview + (per-turn × 2) + aggregate
    assert len(lines) >= 4
    objs = [json.loads(ln) for ln in lines]
    # Every line must carry the trace module id.
    for o in objs:
        assert o["module"] == "trace"


def test_aggregate_flags_slow_turns(tmp_path: Path):
    """A turn whose record_ts gap exceeds 30s WARNS in the aggregate."""
    home = tmp_path / "slow"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    main_sd.mkdir(parents=True, exist_ok=True)
    sid = "44444444-1111-2222-3333-555555555555"

    # 1 fast turn + 1 slow turn (> 30s).
    SLOW_GAP = 35_000
    records: List[Dict[str, Any]] = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T0)},
        _user_msg("user-0", T0, "fast"),
        _assistant_msg("asst-0", T0 + 1000, T0 + 2000),
        _user_msg("user-1", T0 + 100_000, "slow"),
        # A turn whose record_ts is 35s after the message ts → analyze_phases
        # computes total_ms ~ 35s, which exceeds SLOW_THRESHOLD_MS=30s.
        _assistant_msg(
            "asst-1", T0 + 100_000 + 1_000, T0 + 100_000 + SLOW_GAP,
        ),
    ]
    _write_jsonl(main_sd / f"{sid}.jsonl", records)
    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=agents,
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)

    report = _run_trace(ctx, session_id=sid, all_messages=True)
    # The aggregate section must carry a WARN line for the slow turn.
    agg_section = next(
        s for s in report.sections if s.title == "Trace · 跨消息汇总"
    )
    warns = [c for c in agg_section.checks if c.verdict == Verdict.WARN]
    assert any("Turn #2" in c.message for c in warns), [
        c.message for c in agg_section.checks
    ]


def test_session_level_enrichment_emitted_once(tmp_path: Path):
    """Trajectory/SystemPrompt/Gateway are session-scoped; even with N
    turns they appear once. We don't have trajectory/log fixtures here,
    so only assert the section list does NOT carry per-turn duplicates of
    Session · titles.
    """
    ctx = _build_fixture(tmp_path, num_turns=3)
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    titles = [s.title for s in report.sections]
    # No per-turn duplicates of session-scoped titles
    assert sum(t == "Session · Trajectory" for t in titles) <= 1
    assert sum(t == "Session · System Prompt" for t in titles) <= 1
    assert sum(t == "Session · Gateway 计时" for t in titles) <= 1


# ── richer-fixture coverage: tool execs / cache tokens / snippets / enrichment ─


def _tool_call_assistant_msg(
    aid: str, ts: int, record_ts: int, *,
    tool_call_id: str, tool_name: str = "Bash",
    in_tok: int = 10, out_tok: int = 5,
    cache_read: int = 0, cache_write: int = 0,
) -> Dict[str, Any]:
    """Assistant message that emits a toolCall (stopReason=toolUse).

    Used to seed analyze_phases with a real model_call → tool_exec batch
    so the per-turn tool-breakdown section is rendered.
    """
    return {
        "type": "message",
        "id": aid,
        "timestamp": _ms_to_iso(record_ts),
        "message": {
            "role": "assistant",
            "timestamp": ts,
            "model": "test-model",
            "provider": "test",
            "stopReason": "toolUse",
            "usage": {
                "input": in_tok, "output": out_tok,
                "cacheRead": cache_read, "cacheWrite": cache_write,
            },
            "content": [
                {"type": "toolCall", "id": tool_call_id,
                 "name": tool_name, "input": {"cmd": "ls"}},
            ],
        },
    }


def _tool_result_msg(rid: str, ts: int, *, tool_call_id: str,
                     tool_name: str = "Bash",
                     is_error: bool = False) -> Dict[str, Any]:
    return {
        "type": "message",
        "id": rid,
        "timestamp": _ms_to_iso(ts),
        "message": {
            "role": "toolResult",
            "timestamp": ts,
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "isError": is_error,
            "content": "stdout output",
        },
    }


def _final_assistant_msg(
    aid: str, ts: int, record_ts: int, *,
    in_tok: int = 5, out_tok: int = 3,
    cache_read: int = 0, cache_write: int = 0,
) -> Dict[str, Any]:
    """Assistant turn that closes the loop with stopReason=stop."""
    msg = _assistant_msg(
        aid, ts, record_ts, in_tok=in_tok, out_tok=out_tok,
    )
    msg["message"]["usage"]["cacheRead"] = cache_read
    msg["message"]["usage"]["cacheWrite"] = cache_write
    return msg


def _build_rich_session_records(
    *, num_turns: int, with_tools: bool = True,
    cache_read: int = 0, cache_write: int = 0,
) -> List[Dict[str, Any]]:
    """Turns with: long user text + (optional) toolCall→toolResult batch +
    a final assistant. Cache tokens propagate to summary aggregates so the
    aggregate cache_read/cache_write lines fire.
    """
    records: List[Dict[str, Any]] = [
        {"type": "session", "version": 3, "id": SESSION_ID,
         "timestamp": _ms_to_iso(T0)},
    ]
    long_text = (
        "this is a deliberately long user message "
        + "x" * 200  # > 80 chars triggers the snippet truncation branch
    )
    for i in range(num_turns):
        u_ts = T0 + i * 10_000
        records.append(_user_msg(f"user-{i}", u_ts, long_text))
        if with_tools:
            tcall_id = f"tcall-{i}"
            a1_ts = u_ts + 500
            a1_rec_ts = u_ts + 1_000
            records.append(_tool_call_assistant_msg(
                f"asst-{i}-a", a1_ts, a1_rec_ts,
                tool_call_id=tcall_id,
                cache_read=cache_read, cache_write=cache_write,
            ))
            tr_ts = u_ts + 2_000
            records.append(_tool_result_msg(
                f"tr-{i}", tr_ts, tool_call_id=tcall_id,
            ))
            a2_ts = u_ts + 2_500
            a2_rec_ts = u_ts + 3_000
            records.append(_final_assistant_msg(
                f"asst-{i}-b", a2_ts, a2_rec_ts,
                cache_read=cache_read, cache_write=cache_write,
            ))
        else:
            a_ts = u_ts + 1_000
            a_rec_ts = u_ts + 2_000
            records.append(_final_assistant_msg(
                f"asst-{i}", a_ts, a_rec_ts,
                cache_read=cache_read, cache_write=cache_write,
            ))
    return records


def _build_rich_fixture(
    tmp: Path, *, num_turns: int = 2, with_tools: bool = True,
    cache_read: int = 0, cache_write: int = 0,
) -> DiagContext:
    home = tmp / "ocdiag-trace-rich"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    session_file = main_sd / f"{SESSION_ID}.jsonl"
    _write_jsonl(
        session_file,
        _build_rich_session_records(
            num_turns=num_turns, with_tools=with_tools,
            cache_read=cache_read, cache_write=cache_write,
        ),
    )

    cfg = home / "openclaw.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=agents,
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    return ctx


def test_user_snippet_truncates_long_text(tmp_path: Path):
    """A user message longer than the snippet max_chars (80) is truncated
    with an ellipsis. Locks _user_text_snippet's truncation branch.
    """
    ctx = _build_rich_fixture(tmp_path, num_turns=1, with_tools=False)
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    assert report.error is None
    snip = report.data["all_messages"][0]["user_message_snippet"]
    # 80-char default → truncated to 79 chars + "…"
    assert snip.endswith("…")
    assert len(snip) == 80


def test_per_turn_tool_breakdown_section_emitted(tmp_path: Path):
    """Each --all-messages turn whose analysis carries tool_execs renders
    its own ``Trace · 工具拆解`` section under the turn label.
    """
    ctx = _build_rich_fixture(tmp_path, num_turns=2, with_tools=True)
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    assert report.error is None
    titles = [s.title for s in report.sections]
    # Each of the 2 turns gets its own per-turn tool-breakdown section.
    tool_sections = [t for t in titles if "Trace · 工具拆解" in t]
    assert len(tool_sections) == 2, titles
    # And both are turn-labelled (not the legacy single-turn title).
    assert all(t.startswith("Turn #") for t in tool_sections)
    # Per-turn payload also surfaces the tool exec for downstream consumers.
    for t in report.data["all_messages"]:
        assert t["tool_execs"], "expected tool_execs in turn payload"


def test_aggregate_cache_token_lines_emitted(tmp_path: Path):
    """Aggregate prints cache_read=/cache_write= only when any turn carries
    cache tokens. Pin those two branches.
    """
    ctx = _build_rich_fixture(
        tmp_path, num_turns=2, with_tools=False,
        cache_read=42, cache_write=7,
    )
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    assert report.error is None
    assert report.data["aggregate"]["total_cache_read"] == 84
    assert report.data["aggregate"]["total_cache_write"] == 14
    agg_section = next(
        s for s in report.sections if s.title == "Trace · 跨消息汇总"
    )
    tok_check = next(c for c in agg_section.checks if c.name == "agg.tokens")
    assert "cache_read=84" in tok_check.message
    assert "cache_write=14" in tok_check.message


# ── enrichment branches: trajectory + system_prompt + gateway + slow E2E ───


def _build_traj_records(sid: str, started_ms: int, ended_ms: int) -> List[Dict[str, Any]]:
    base = {
        "schemaVersion": 1, "traceSchema": "openclaw-trajectory",
        "runId": "run-aaaa", "sessionId": sid,
    }
    return [
        {**base, "type": "session.started",
         "ts": _ms_to_iso(started_ms),
         "data": {"trigger": "user", "toolCount": 5}},
        {**base, "type": "trace.metadata",
         "ts": _ms_to_iso(started_ms + 1),
         "data": {
            "model": {"provider": "test", "name": "test-model"},
            "plugins": {"entries": []},
         }},
        {**base, "type": "model.completed",
         "ts": _ms_to_iso(ended_ms - 200),
         "data": {"compactionCount": 0,
                  "promptCache": {"observation": {
                      "broke": False, "cacheRead": 50}}}},
        {**base, "type": "trace.artifacts",
         "ts": _ms_to_iso(ended_ms - 100),
         "data": {
            "finalStatus": "ok",
            "aborted": False, "externalAbort": False,
            "timedOut": False, "idleTimedOut": False,
            "timedOutDuringCompaction": False,
            "timedOutDuringToolExecution": False,
            "promptErrorSource": None,
            "usage": {"input": 100, "output": 200,
                      "cacheRead": 50, "cacheWrite": 30, "total": 380},
            "itemLifecycle": {"startedCount": 1,
                              "completedCount": 1, "activeCount": 0},
            "didSendViaMessagingTool": False,
            "messagingToolSentTargets": [],
            "messagingToolSentTexts": [],
            "successfulCronAdds": 0,
            "toolMetas": [],
         }},
        {**base, "type": "session.ended",
         "ts": _ms_to_iso(ended_ms),
         "data": {"status": "ok"}},
    ]


def _build_enriched_fixture(
    tmp: Path, *, slow: bool = False, with_traj: bool = True,
    with_sp: bool = True, with_gw: bool = True,
) -> DiagContext:
    """Lay down session.jsonl + trajectory + sessions.json + a same-day
    gateway log so trace's enrichment helpers (load_trajectory_info /
    sessions.lookup_system_prompt_report / load_gateway_timing) all fire.

    ``slow=True`` widens one turn's record_ts gap above SLOW_THRESHOLD_MS so
    the slow-E2E WARN branch is exercised.
    """
    home = tmp / "ocdiag-trace-enriched"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    main_sd.mkdir(parents=True, exist_ok=True)
    sid = SESSION_ID

    # session.jsonl — 2 turns, second turn made slow when slow=True.
    records: List[Dict[str, Any]] = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T0)},
    ]
    # turn 0: ordinary
    records.append(_user_msg("user-0", T0, "first"))
    records.append(_assistant_msg("asst-0", T0 + 1000, T0 + 2000))
    # turn 1: optionally slow
    u1_ts = T0 + 100_000
    records.append(_user_msg("user-1", u1_ts, "second"))
    a1_msg_ts = u1_ts + 1_000
    a1_rec_ts = u1_ts + (35_000 if slow else 2_000)
    records.append(_assistant_msg("asst-1", a1_msg_ts, a1_rec_ts))
    _write_jsonl(main_sd / f"{sid}.jsonl", records)

    # ② trajectory.jsonl — same dir, <uuid>.trajectory.jsonl
    if with_traj:
        traj_path = main_sd / f"{sid}.trajectory.jsonl"
        _write_jsonl(traj_path, _build_traj_records(
            sid, started_ms=T0, ended_ms=T0 + 5_000,
        ))

    # ③ sessions.json — sibling system-prompt store
    if with_sp:
        store = {
            "agent:main:test:user:U-1": {
                "sessionId": sid,
                "systemPromptReport": {
                    "systemPrompt": {
                        "chars": 4321,
                        "projectContextChars": 1000,
                        "nonProjectContextChars": 3321,
                    },
                    "tools": {"entries": [], "schemaChars": 200},
                    "skills": {"entries": []},
                    "source": "test",
                },
            }
        }
        with open(main_sd / "sessions.json", "w", encoding="utf-8") as f:
            json.dump(store, f)

    # ④ openclaw-<base-date>.log — gateway timing
    # base_date is derived via local epoch_ms_to_iso(base_epoch_ms)[:10],
    # which means we have to write a log that the helper will accept. The
    # safest deterministic path is to monkeypatch the log discovery in the
    # test that needs it — see test_single_turn_enrichment_runs.
    # We still write a file so find_gateway_logs has something to match.
    if with_gw:
        # Use the actual local-tz date prefix the loader will compute.
        from ocdiag.tracing import epoch_ms_to_iso
        base_date = epoch_ms_to_iso(T0)[:10]
        gw_path = log_dir / f"openclaw-{base_date}.log"
        # Build records the loader recognises.
        run_start_iso = datetime.fromtimestamp(
            T0 / 1000, tz=timezone.utc,
        ).isoformat()
        prompt_start_iso = datetime.fromtimestamp(
            (T0 + 500) / 1000, tz=timezone.utc,
        ).isoformat()
        prompt_end_iso = datetime.fromtimestamp(
            (T0 + 4000) / 1000, tz=timezone.utc,
        ).isoformat()
        with open(gw_path, "w", encoding="utf-8") as f:
            for ts_iso, msg in [
                (run_start_iso,
                 f"agent/embedded embedded run start: sessionId={sid}"),
                (prompt_start_iso,
                 f"agent/embedded embedded run prompt start: sessionId={sid}"),
                (prompt_end_iso,
                 f"agent/embedded embedded run prompt end: "
                 f"sessionId={sid} durationMs=3500"),
            ]:
                f.write(json.dumps({"time": ts_iso, "1": msg}) + "\n")

    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=agents,
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    return ctx


def test_single_turn_tool_breakdown_section_emitted(tmp_path: Path):
    """Single-turn path with tool_execs renders ``Trace · 工具拆解``.

    The legacy single-turn path is byte-identical to pre-1.11.0 output,
    so this also pins the section title (no Turn # prefix) and the
    presence of the per-tool aggregate line.
    """
    ctx = _build_rich_fixture(tmp_path, num_turns=1, with_tools=True)
    report = _run_trace(ctx, session_id=SESSION_ID)
    assert report.error is None
    titles = [s.title for s in report.sections]
    assert "Trace · 工具拆解" in titles
    tools_section = next(
        s for s in report.sections if s.title == "Trace · 工具拆解"
    )
    # The synthetic batch only ever uses Bash → exactly one aggregate line.
    assert any(c.name == "tool.Bash" for c in tools_section.checks), [
        c.name for c in tools_section.checks
    ]


def test_single_turn_enrichment_renders_all_three_sections(tmp_path: Path):
    """Single-turn path with trajectory + system-prompt + gateway fixtures
    on disk renders Trace · Trajectory / Trace · System Prompt /
    Trace · Gateway 计时 sections, AND attaches firstCallInputTokens to the
    system-prompt info (covers the firstCallInputTokens branch).

    We pick ``msg_index=0`` so the first turn's base_epoch_ms (T0) is the
    anchor — matching the trajectory's session.started ts. (The default
    last-user-msg pick would anchor at T0+100s and load_trajectory_info
    drops candidates whose session.started is more than 60s away.)
    """
    ctx = _build_enriched_fixture(tmp_path)
    report = _run_trace(ctx, session_id=SESSION_ID, msg_index=0)
    assert report.error is None
    titles = [s.title for s in report.sections]
    assert "Trace · Trajectory" in titles
    assert "Trace · System Prompt" in titles
    assert "Trace · Gateway 计时" in titles
    # firstCallInputTokens: derived from the first model call's tokens_in
    # + cache_read + cache_write — for our fixture that's 10 + 0 + 0 = 10.
    sp = report.data["systemPrompt"]
    assert sp["firstCallInputTokens"] == 10
    # trajectory + gateway data are wired in too.
    assert report.data["trajectory"]["runId"] == "run-aaaa"
    assert "prompt_duration_ms" in report.data["gateway"]


def test_single_turn_slow_e2e_emits_warn(tmp_path: Path):
    """Single-turn path with E2E > 30s appends a slow-E2E WARN to the
    summary section. Locks the warn branch at the bottom of the
    single-turn render path.
    """
    home = tmp_path / "slow-single"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    main_sd.mkdir(parents=True, exist_ok=True)
    sid = "55555555-1111-2222-3333-444444444444"

    SLOW_GAP = 35_000
    records: List[Dict[str, Any]] = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T0)},
        _user_msg("user-0", T0, "slow turn"),
        _assistant_msg("asst-0", T0 + 1000, T0 + SLOW_GAP),
    ]
    _write_jsonl(main_sd / f"{sid}.jsonl", records)
    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=agents,
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)

    report = _run_trace(ctx, session_id=sid)
    assert report.error is None
    summary = next(s for s in report.sections if s.title == "Trace · 汇总")
    warns = [c for c in summary.checks if c.verdict == Verdict.WARN]
    assert any(c.name == "trace.slow" for c in warns), [
        c.name for c in summary.checks
    ]


def test_all_messages_session_level_enrichment_sections(tmp_path: Path):
    """--all-messages + on-disk trajectory/sp/gateway → ONE Session · *
    block at the top-level (covers the 887-894 enrichment branch).
    """
    ctx = _build_enriched_fixture(tmp_path)
    report = _run_trace(ctx, session_id=SESSION_ID, all_messages=True)
    assert report.error is None, f"unexpected error: {report.error}"
    titles = [s.title for s in report.sections]
    assert titles.count("Session · Trajectory") == 1
    assert titles.count("Session · System Prompt") == 1
    assert titles.count("Session · Gateway 计时") == 1
    # And NOT the single-turn "Trace · " versions (those are single-turn-only).
    assert "Trace · Trajectory" not in titles
    assert "Trace · System Prompt" not in titles
    assert "Trace · Gateway 计时" not in titles


def test_turn_index_falls_back_to_ordinal_when_lookup_fails(
    tmp_path: Path, monkeypatch,
):
    """Covers the StopIteration fallback in TraceInspector.collect.

    select_user_message normally returns a (rec_idx, rec) pair pointing
    into the user_msgs list; the inspector then re-derives ``turn_index``
    from that ordinal. Pre-1.11.0 it was guaranteed to find a match, but
    a hypothetical caller (extension, future SDK) could synthesise a
    selector that produces a rec_idx absent from user_msgs. We pin the
    fallback so the inspector still names the turn deterministically
    instead of crashing.

    To force the rare branch without altering production code we
    monkeypatch find_user_messages so its (rec_idx, _) ordinals don't
    contain the rec_idx that select_user_message returned. The actual
    record at that rec_idx is still a real user message, so
    extract_trace_records / analyze_phases run normally.
    """
    ctx = _build_fixture(tmp_path, num_turns=2)
    from ocdiag.inspectors import trace as trace_mod

    real_find = trace_mod.find_user_messages

    def _shifted_find(records):
        # Return a list whose rec_idx values are deliberately wrong so
        # the lookup ``ri == rec_idx`` never matches.
        msgs = real_find(records)
        return [(ri + 100, r) for (ri, r) in msgs]

    monkeypatch.setattr(trace_mod, "find_user_messages", _shifted_find)
    # Default (msg_index=None) → select_user_message picks the LAST user
    # msg from its OWN call to find_user_messages; inside ocdiag.tracing
    # that import is unaffected by our patch, so it returns the real
    # rec_idx. The inspector then walks our shifted list and the lookup
    # falls into the StopIteration branch (turn_index = ordinal = 0).
    report = _run_trace(ctx, session_id=SESSION_ID)
    assert report.error is None
    assert report.data["user_message_index"] == 0


# ── skill-load detection + privacy-preserving rendering ──────────────────


def _assistant_with_toolcalls(
    aid: str, ts: int, record_ts: int, *,
    tool_calls: List[Dict[str, Any]],
    in_tok: int = 10, out_tok: int = 5,
) -> Dict[str, Any]:
    """Assistant message that emits one or more toolCalls (stopReason=toolUse).

    ``tool_calls`` is a list of dicts with keys: ``id``, ``name``,
    ``arguments`` (the dict the inspector reads via _tool_call_args).
    """
    content = []
    for tc in tool_calls:
        content.append({
            "type": "toolCall",
            "id": tc["id"],
            "name": tc["name"],
            "arguments": tc.get("arguments", {}),
        })
    return {
        "type": "message",
        "id": aid,
        "timestamp": _ms_to_iso(record_ts),
        "message": {
            "role": "assistant",
            "timestamp": ts,
            "model": "test-model",
            "provider": "test",
            "stopReason": "toolUse",
            "usage": {
                "input": in_tok, "output": out_tok,
                "cacheRead": 0, "cacheWrite": 0,
            },
            "content": content,
        },
    }


def _tool_result(
    rid: str, ts: int, *, tool_call_id: str, tool_name: str,
    is_error: bool = False,
) -> Dict[str, Any]:
    return {
        "type": "message",
        "id": rid,
        "timestamp": _ms_to_iso(ts),
        "message": {
            "role": "toolResult",
            "timestamp": ts,
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "isError": is_error,
            "content": "ok",
        },
    }


def _build_one_turn_fixture(
    tmp: Path, *, tool_calls_in_assistant: List[Dict[str, Any]],
) -> DiagContext:
    """One user turn + assistant(toolCalls) + matching toolResults + final
    assistant. Each entry in ``tool_calls_in_assistant`` carries the toolCall
    content dict (id/name/arguments)."""
    home = tmp / "ocdiag-trace-skill"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    main_sd.mkdir(parents=True, exist_ok=True)
    sid = SESSION_ID

    u_ts = T0
    a1_ts = u_ts + 500
    a1_rec_ts = u_ts + 1_000
    tr_ts = u_ts + 2_000
    a2_ts = u_ts + 2_500
    a2_rec_ts = u_ts + 3_000

    records: List[Dict[str, Any]] = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T0)},
        _user_msg("user-0", u_ts, "do the thing"),
        _assistant_with_toolcalls(
            "asst-0a", a1_ts, a1_rec_ts,
            tool_calls=tool_calls_in_assistant,
        ),
    ]
    for tc in tool_calls_in_assistant:
        records.append(_tool_result(
            f"tr-{tc['id']}", tr_ts,
            tool_call_id=tc["id"], tool_name=tc["name"],
        ))
    records.append(_final_assistant_msg("asst-0b", a2_ts, a2_rec_ts))

    _write_jsonl(main_sd / f"{sid}.jsonl", records)
    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=agents,
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    return ctx


def _section_text(report) -> str:
    """Flatten every section title + check message + detail into one string,
    for substring assertions (privacy / leak checks)."""
    parts: List[str] = []
    for s in report.sections:
        parts.append(s.title)
        for c in s.checks:
            parts.append(c.message)
            if c.detail:
                parts.append(c.detail)
            if c.evidence:
                parts.append(c.evidence)
    return "\n".join(parts)


def test_skill_name_from_path_unit():
    """Direct unit asserts on _skill_name_from_path."""
    from ocdiag.tracing import _skill_name_from_path
    assert _skill_name_from_path(
        "/home/u/.claude/skills/web-search-plus/SKILL.md",
    ) == "web-search-plus"
    # case-insensitive on the SKILL.md basename
    assert _skill_name_from_path("/x/skill.md") == "x"
    # non-SKILL.md basename → not a skill load
    assert _skill_name_from_path("/x/bar.py") is None
    # bare SKILL.md with no parent directory → no skill name resolvable
    assert _skill_name_from_path("SKILL.md") is None
    # empty path
    assert _skill_name_from_path("") is None


def test_skill_load_detected_from_skill_md_read(tmp_path: Path):
    """A read of .../web-search-plus/SKILL.md is flagged as a skill load,
    surfaces in analysis["tool_execs"], and renders in the Trace · Skill
    section with 'web-search-plus' + 'loaded at'."""
    skill_path = "/home/u/.claude/skills/web-search-plus/SKILL.md"
    ctx = _build_one_turn_fixture(
        tmp_path,
        tool_calls_in_assistant=[
            {"id": "tcall-skill", "name": "read",
             "arguments": {"path": skill_path}},
        ],
    )
    report = _run_trace(ctx, session_id=SESSION_ID)
    assert report.error is None, f"unexpected error: {report.error}"

    # 1) analysis output carries the skill-load flags
    execs = report.data["tool_execs"]
    assert len(execs) == 1
    te = execs[0]
    assert te["is_skill_load"] is True
    assert te["skill_name"] == "web-search-plus"
    assert te["skill_path"] == skill_path
    assert te.get("completed_epoch_ms")  # populated, non-zero

    # 2) the Trace · Skill section renders the skill load
    titles = [s.title for s in report.sections]
    assert any("Trace · Skill" in t for t in titles), titles
    skill_section = next(
        s for s in report.sections if "Trace · Skill" in s.title
    )
    msgs = [c.message for c in skill_section.checks]
    assert any(
        "web-search-plus" in m and "loaded at" in m for m in msgs
    ), msgs


def test_non_skill_read_is_not_skill_load(tmp_path: Path):
    """A read of a normal file path is NOT flagged is_skill_load and the
    Skill section reports 'no skill was loaded'."""
    ctx = _build_one_turn_fixture(
        tmp_path,
        tool_calls_in_assistant=[
            {"id": "tcall-read", "name": "read",
             "arguments": {"path": "/tmp/foo.py"}},
        ],
    )
    report = _run_trace(ctx, session_id=SESSION_ID)
    assert report.error is None, f"unexpected error: {report.error}"

    execs = report.data["tool_execs"]
    assert len(execs) == 1
    assert not execs[0].get("is_skill_load")
    assert execs[0].get("skill_name") in (None, "")

    skill_section = next(
        s for s in report.sections if "Trace · Skill" in s.title
    )
    msgs = [c.message for c in skill_section.checks]
    assert any("no skill was loaded" in m for m in msgs), msgs


def test_exec_command_not_leaked_in_trace(tmp_path: Path):
    """An exec toolCall's command string and a write toolCall's content
    payload must NEVER appear in the rendered trace output. Privacy contract:
    only counts/char-lengths are surfaced for these tools."""
    secret_cmd = "echo SUPERSECRET_TOKEN_12345"
    secret_content = "PASSWORD_NEVER_PRINTED_67890"
    ctx = _build_one_turn_fixture(
        tmp_path,
        tool_calls_in_assistant=[
            {"id": "tcall-exec", "name": "exec",
             "arguments": {"command": secret_cmd}},
            {"id": "tcall-write", "name": "write",
             "arguments": {"path": "/tmp/out.txt",
                           "content": secret_content}},
        ],
    )
    report = _run_trace(ctx, session_id=SESSION_ID)
    assert report.error is None, f"unexpected error: {report.error}"

    # exec is still counted as a tool exec
    execs = report.data["tool_execs"]
    names = [e["name"] for e in execs]
    assert "exec" in names, names
    assert "write" in names, names

    # Neither the exec command nor the write content may appear anywhere
    # in the rendered section text.
    rendered = _section_text(report)
    assert "SUPERSECRET_TOKEN_12345" not in rendered, (
        "exec command leaked into trace output"
    )
    assert secret_cmd not in rendered
    assert "PASSWORD_NEVER_PRINTED_67890" not in rendered, (
        "write content leaked into trace output"
    )
    assert secret_content not in rendered

    # And not via the JSON envelope either — covers any downstream JSON
    # consumer that might serialize a hidden field.
    env_str = json.dumps(to_envelope(report), ensure_ascii=False)
    assert "SUPERSECRET_TOKEN_12345" not in env_str
    assert "PASSWORD_NEVER_PRINTED_67890" not in env_str


# ── pytest entry ──


def main():
    """Allow running this file directly for ad-hoc smoke runs."""
    import subprocess
    rc = subprocess.call([
        sys.executable, "-m", "pytest", __file__, "-v",
    ])
    return rc


if __name__ == "__main__":
    sys.exit(main())
