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
