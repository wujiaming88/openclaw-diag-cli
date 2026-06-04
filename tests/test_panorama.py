"""Tests for the panorama inspector.

Covers:
  - Argument validation (missing/short/non-uuid session ids → exit code 2 paths)
  - SESSION_NOT_FOUND
  - Correlation graph expansion across all six sources, on synthetic fixtures
    written into a temp $OPENCLAW_HOME
  - Tool-call waterfall pairing, duration arithmetic
  - Timeline ordering and merge across session.jsonl + trajectory + app log
  - Multi-run handling: --run-index, --all-runs
  - --strict-correlation drops sessionKey-only matches
  - Verdict logic (FAIL on artifact abort, WARN on slow E2E, OK on clean)
  - JSON envelope round-trip

The fixture is fully synthetic — no dependency on a live OpenClaw install.
A 100k-line synthetic log is generated on the fly to verify the correlation
scan stays under 5s.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag.core import registry  # noqa: E402
from ocdiag.core.context import DiagContext  # noqa: E402
from ocdiag.core.types import Verdict  # noqa: E402
from ocdiag.render.json_renderer import to_envelope  # noqa: E402


# Force registry to import every inspector before any test collects.
registry.discover()


SESSION_ID = "11111111-2222-3333-4444-555555555555"
RUN_ID_A = "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"
RUN_ID_B = "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb"
SESSION_KEY = "agent:main:feishu:user:U-test"
CHILD_TASK_ID = "child-task-1234"
CHILD_RUN_ID = "child-run-5678"
TOOL_CALL_ID_1 = "tooluse_TestCallId01"
TOOL_CALL_ID_2 = "tooluse_TestCallId02"

# Window: run A starts at this ts; run B picks up later. Session events
# straddle both. Numbers in ms since epoch (an arbitrary 2026-06-01 anchor).
T0 = 1780000000000   # window start (run A start)
T1 = T0 + 1000       # session.jsonl session record
T2 = T0 + 2000       # assistant: toolCall #1
T3 = T0 + 5000       # toolResult #1 (3s tool)
T4 = T0 + 7000       # assistant: toolCall #2
T5 = T0 + 9000       # toolResult #2 (2s tool, error)
T6 = T0 + 10000      # run A ends
T7 = T0 + 60000      # run B starts (1 min later, same session)
T8 = T0 + 70000      # run B ends


def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _ms_to_iso(ms: int) -> str:
    """Trajectory uses ISO timestamps. Build one from ms."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )[:-4] + "Z"


def _build_session_records() -> List[Dict[str, Any]]:
    """Two assistant turns + two tool results spanning ~9 seconds."""
    return [
        {
            "type": "session", "version": 3, "id": SESSION_ID,
            "timestamp": _ms_to_iso(T1),
        },
        {
            "type": "message",
            "id": "user-1",
            "timestamp": _ms_to_iso(T1),
            "message": {
                "role": "user",
                "timestamp": T1,
                "content": "hello",
            },
        },
        {
            "type": "message",
            "id": "asst-1",
            "timestamp": _ms_to_iso(T2),
            "message": {
                "role": "assistant",
                "timestamp": T2,
                "model": "test-model",
                "provider": "test",
                "stopReason": "toolUse",
                "usage": {"input": 10, "output": 5,
                          "cacheRead": 0, "cacheWrite": 0},
                "content": [
                    {"type": "text", "text": "running"},
                    {"type": "toolCall", "id": TOOL_CALL_ID_1,
                     "name": "Bash", "input": {"cmd": "ls"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "tr-1",
            "timestamp": _ms_to_iso(T3),
            "message": {
                "role": "toolResult",
                "timestamp": T3,
                "toolCallId": TOOL_CALL_ID_1,
                "toolName": "Bash",
                "isError": False,
                "content": "stdout output",
            },
        },
        {
            "type": "message",
            "id": "asst-2",
            "timestamp": _ms_to_iso(T4),
            "message": {
                "role": "assistant",
                "timestamp": T4,
                "model": "test-model",
                "provider": "test",
                "stopReason": "toolUse",
                "usage": {"input": 12, "output": 7,
                          "cacheRead": 0, "cacheWrite": 0},
                "content": [
                    {"type": "toolCall", "id": TOOL_CALL_ID_2,
                     "name": "Read", "input": {"path": "/etc/hosts"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "tr-2",
            "timestamp": _ms_to_iso(T5),
            "message": {
                "role": "toolResult",
                "timestamp": T5,
                "toolCallId": TOOL_CALL_ID_2,
                "toolName": "Read",
                "isError": True,
                "content": "ENOENT",
            },
        },
    ]


def _build_trajectory(*, run_id: str, started_ms: int, ended_ms: int,
                     final_status: str = "ok",
                     aborted: bool = False, timed_out: bool = False) -> List[Dict[str, Any]]:
    base = {
        "schemaVersion": 1,
        "traceSchema": "openclaw-trajectory",
        "runId": run_id,
        "sessionId": SESSION_ID,
        "sessionKey": SESSION_KEY,
        "provider": "amazon-bedrock",
        "modelId": "global.anthropic.claude-opus-4-7-v1",
    }
    return [
        {
            **base, "type": "session.started",
            "ts": _ms_to_iso(started_ms),
            "data": {
                "trigger": "user",
                "agentId": "main",
                "messageChannel": "feishu",
                "messageProvider": "feishu",
                "toolCount": 12,
                "clientToolCount": 3,
            },
        },
        {
            **base, "type": "trace.metadata",
            "ts": _ms_to_iso(started_ms + 1),
            "data": {
                "harness": {"version": "0.42.0",
                            "runtime": {"node": "v20.10.0"}},
                "model": {"provider": "amazon-bedrock",
                          "name": "claude-opus-4-7", "api": "messages"},
                "plugins": {"entries": [
                    {"id": "p1", "activated": True,
                     "status": "ok", "error": None},
                    {"id": "p2", "activated": True,
                     "status": "error",
                     "error": "init failed"},
                ]},
                "skills": {"entries": [{"id": "skill1"}]},
                "prompting": {"systemPromptReport": {
                    "systemPrompt": {"chars": 12345,
                                     "projectContextChars": 1000,
                                     "nonProjectContextChars": 11345},
                    "tools": {"schemaChars": 5000, "entries": []},
                    "skills": {"promptChars": 800, "entries": []},
                }},
            },
        },
        {
            **base, "type": "trace.artifacts",
            "ts": _ms_to_iso(ended_ms - 100),
            "data": {
                "finalStatus": final_status,
                "aborted": aborted,
                "externalAbort": False,
                "timedOut": timed_out,
                "idleTimedOut": False,
                "timedOutDuringCompaction": False,
                "timedOutDuringToolExecution": False,
                "promptErrorSource": None,
                "usage": {"input": 100, "output": 200,
                          "cacheRead": 50, "cacheWrite": 30,
                          "total": 380},
                "promptCache": {"observation": {"broke": False,
                                                "cacheRead": 50}},
                "compactionCount": 0,
                "itemLifecycle": {"startedCount": 2,
                                  "completedCount": 2,
                                  "activeCount": 0},
                "didSendViaMessagingTool": False,
                "messagingToolSentTargets": [],
                "messagingToolSentTexts": [],
                "successfulCronAdds": 0,
                "toolMetas": [],
            },
        },
        {
            **base, "type": "session.ended",
            "ts": _ms_to_iso(ended_ms),
            "data": {"status": final_status},
        },
    ]


def _build_runs_sqlite(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("""
            CREATE TABLE task_runs (
              task_id TEXT PRIMARY KEY,
              runtime TEXT NOT NULL,
              task_kind TEXT,
              source_id TEXT,
              requester_session_key TEXT,
              owner_key TEXT NOT NULL,
              scope_kind TEXT NOT NULL,
              child_session_key TEXT,
              parent_flow_id TEXT,
              parent_task_id TEXT,
              agent_id TEXT,
              run_id TEXT,
              label TEXT,
              task TEXT NOT NULL,
              status TEXT NOT NULL,
              delivery_status TEXT NOT NULL,
              notify_policy TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              started_at INTEGER,
              ended_at INTEGER,
              last_event_at INTEGER,
              cleanup_after INTEGER,
              error TEXT,
              progress_summary TEXT,
              terminal_summary TEXT,
              terminal_outcome TEXT
            )
        """)
        con.execute("""
            INSERT INTO task_runs
              (task_id, runtime, requester_session_key, owner_key, scope_kind,
               child_session_key, agent_id, run_id, task, status,
               delivery_status, notify_policy, created_at, started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            CHILD_TASK_ID, "subagent", SESSION_KEY, "owner-1", "session",
            "agent:main:subagent:" + CHILD_TASK_ID, "research", CHILD_RUN_ID,
            "research-task", "completed", "delivered", "always",
            T0, T0 + 100, T0 + 5000,
        ))
        con.commit()
    finally:
        con.close()


def _build_app_log(path: Path, *, lines: int = 50,
                   include_session: bool = True,
                   include_runid: bool = True,
                   include_warn_unrelated: bool = True) -> None:
    """Emit a synthetic openclaw-*.log with structured JSON lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(lines):
            ts = T0 + i * 100
            level = "INFO"
            text = f"unrelated event {i}"
            if include_session and i == 5:
                text = f"sessionId={SESSION_ID} starting"
            if include_runid and i == 10:
                text = f"sessionId={SESSION_ID} runId={RUN_ID_A} working"
            if include_warn_unrelated and i == 20:
                level = "WARN"
                text = "uncorrelated warning, no correlation key"
            rec = {
                "level": level,
                "time": ts,
                "pid": 12345,
                "_meta": {"name": json.dumps({"subsystem": "gateway"})},
                "msg": text,
            }
            f.write(json.dumps(rec) + "\n")


def _build_fixture_home(tmp: Path, *,
                        run_b: bool = False,
                        artifact_failure: bool = False) -> DiagContext:
    """Lay down a synthetic OPENCLAW_HOME with all six sources populated."""
    home = tmp / "ocdiag-pano-home"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # ① session.jsonl
    session_file = main_sd / f"{SESSION_ID}.jsonl"
    _write_jsonl(session_file, _build_session_records())

    # ② trajectory.jsonl
    traj_file = main_sd / f"{SESSION_ID}.trajectory.jsonl"
    traj_lines = _build_trajectory(
        run_id=RUN_ID_A, started_ms=T0, ended_ms=T6,
        final_status="failed" if artifact_failure else "ok",
        aborted=artifact_failure,
    )
    if run_b:
        traj_lines += _build_trajectory(
            run_id=RUN_ID_B, started_ms=T7, ended_ms=T8,
        )
    _write_jsonl(traj_file, traj_lines)

    # ③ sessions.json
    store_path = main_sd / "sessions.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store = {
        SESSION_KEY: {
            "sessionId": SESSION_ID,
            "systemPromptReport": {
                "systemPrompt": {"chars": 12345},
                "tools": {"entries": []},
                "skills": {"entries": []},
                "source": "test",
            },
        }
    }
    with open(store_path, "w") as f:
        json.dump(store, f)

    # ④ openclaw-*.log — write today-dated to ride discover_recent_logs
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / f"openclaw-{today}.log"
    _build_app_log(log_path)

    # ⑤ runs.sqlite
    sqlite_path = home / "tasks" / "runs.sqlite"
    _build_runs_sqlite(sqlite_path)

    cfg = home / "openclaw.json"
    cfg.write_text(json.dumps({"gateway": {"port": 18789}}))

    ctx = DiagContext(
        openclaw_home=home,
        config_path=cfg,
        log_dir=log_dir,
        sessions_base=agents,
    )
    # Redirect ocdiag.paths so build_graph picks up our temp runs.sqlite/cron.
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    paths_mod.CRON_RUNS_DIR = str(home / "cron" / "runs")
    return ctx


# ── tests ─────────────────────────────────────────────────────────────────


def _run_panorama(ctx: DiagContext, **kwargs) -> Any:
    inspector = registry.get("panorama")
    assert inspector is not None, "panorama not registered"
    return inspector.collect(ctx, **kwargs)


def test_panorama_registered():
    assert registry.get("panorama") is not None
    assert registry.get("panorama").kind == "inspector"


def test_missing_session_id():
    home = tempfile.mkdtemp(prefix="ocdiag-pano-")
    try:
        ctx = DiagContext(
            openclaw_home=Path(home),
            config_path=Path(home) / "openclaw.json",
            log_dir=Path(home),
            sessions_base=Path(home) / "agents",
        )
        report = _run_panorama(ctx)
        assert report.error == "missing session_id"
        assert report.diag_error is not None
        assert report.diag_error.code == "MISSING_ARGUMENT"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_invalid_query_short():
    home = tempfile.mkdtemp(prefix="ocdiag-pano-")
    try:
        ctx = DiagContext(
            openclaw_home=Path(home),
            config_path=Path(home) / "openclaw.json",
            log_dir=Path(home),
            sessions_base=Path(home) / "agents",
        )
        report = _run_panorama(ctx, session_id="abcd")
        assert report.diag_error is not None
        assert report.diag_error.code == "INVALID_QUERY"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_session_not_found():
    home = tempfile.mkdtemp(prefix="ocdiag-pano-")
    try:
        Path(home, "agents").mkdir(parents=True)
        ctx = DiagContext(
            openclaw_home=Path(home),
            config_path=Path(home) / "openclaw.json",
            log_dir=Path(home),
            sessions_base=Path(home) / "agents",
        )
        report = _run_panorama(ctx, session_id="abcdefab")
        assert report.diag_error is not None
        assert report.diag_error.code == "SESSION_NOT_FOUND"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_correlation_graph_full(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    assert report.error is None, f"unexpected error: {report.error}"
    graph = report.data["correlation_graph"]
    assert graph["sessionId"] == SESSION_ID
    assert graph["sessionKey"] == SESSION_KEY
    assert RUN_ID_A in graph["runIds"]
    assert TOOL_CALL_ID_1 in graph["toolCallIds"]
    assert TOOL_CALL_ID_2 in graph["toolCallIds"]
    # Child task surfaces from runs.sqlite
    assert any(CHILD_TASK_ID in c for c in graph["childSessionIds"])
    # Every source seen
    assert "session.jsonl" in graph["sources_seen"]
    assert "trajectory.jsonl" in graph["sources_seen"]
    assert "sessions.json" in graph["sources_seen"]
    assert "runs.sqlite" in graph["sources_seen"]
    assert "app_log" in graph["sources_seen"]


def test_tool_waterfall_and_stats(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    waterfall = report.data["tool_waterfall"]
    assert len(waterfall) == 2
    # Calls were paired with results → durations populated
    by_id = {w["callId"]: w for w in waterfall}
    assert by_id[TOOL_CALL_ID_1]["duration_ms"] == 3000
    assert by_id[TOOL_CALL_ID_2]["duration_ms"] == 2000
    assert by_id[TOOL_CALL_ID_2]["is_error"] is True
    stats = report.data["tool_stats"]
    assert stats["total"] == 2
    assert stats["completed"] == 2
    assert stats["errors"] == 1
    assert stats["max_ms"] == 3000


def test_model_call_duration_renders_in_seconds(tmp_path: Path):
    """Regression: model-call durations are stored in ms but fmt_duration()
    expects seconds. A missing `/1000` rendered a 1-2s call as ~17-33 minutes
    (e.g. 547ms gap → "9.1m"). Lock the unit so it never regresses.
    """
    from ocdiag.render.human import render

    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)

    # Underlying data carries true millisecond gaps (1000ms, 2000ms).
    model_calls = report.data["model_calls"]
    durations = {c.get("duration_ms") for c in model_calls}
    assert 1000 in durations and 2000 in durations

    text = render(report, no_color=True)
    # Buggy unit-as-seconds would have printed minutes for sub-3s calls.
    assert "16.7m" not in text  # would be fmt_duration(1000) == 16.7m
    assert "33.3m" not in text  # would be fmt_duration(2000) == 33.3m
    # Correct rendering keeps these in the seconds bucket.
    assert "#1 1s" in text
    assert "#2 2s" in text


def test_model_call_throughput_removed(tmp_path: Path):
    """v1.4.6: per-call tok/s throughput was REMOVED. It was derived from a
    round-trip wall-clock gap (previous message -> assistant message), not
    real API latency; in multi-step runs consecutive messages are ms apart,
    so a large output over a ~6ms gap implied physically impossible rates
    (e.g. 4096 tok / 0.006s = 682,666 tok/s). Rather than show an unreliable
    number, throughput is no longer displayed at all.
    """
    import json as _json
    from ocdiag.render.human import render

    ctx = _build_fixture_home(tmp_path)
    sid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    base = T0
    records = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(base)},
        {"type": "message", "id": "u1", "timestamp": _ms_to_iso(base),
         "message": {"role": "user", "timestamp": base, "content": "go"}},
        {"type": "message", "id": "a1", "timestamp": _ms_to_iso(base + 6),
         "message": {"role": "assistant", "timestamp": base + 6,
                     "model": "test-model", "provider": "test",
                     "stopReason": "length",
                     "usage": {"input": 2, "output": 4096,
                               "cacheRead": 0, "cacheWrite": 0},
                     "content": [{"type": "text", "text": "x"}]}},
    ]
    sess_file = ctx.sessions_base / "main" / "sessions" / f"{sid}.jsonl"
    with open(sess_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(_json.dumps(r) + "\n")

    report = _run_panorama(ctx, session_id=sid)
    calls = report.data["model_calls"]
    # Data still carries the raw gap + tokens (consumers can derive if needed).
    assert any(c.get("duration_ms") == 6 and c.get("output") == 4096
               for c in calls)

    text = render(report, no_color=True)
    # No throughput shown anywhere; no bogus value leaks.
    assert "tok/s" not in text
    assert "682666" not in text
    assert "avg output rate" not in text
    # The per-call line still shows out tokens + stop reason.
    assert "out=4096" in text
    assert "(length)" in text


def test_timeline_ordering_and_sources(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    tl = report.data["timeline"]
    # Strictly chronological
    for a, b in zip(tl, tl[1:]):
        assert a["ts_ms"] <= b["ts_ms"]
    # Each entry carries a known source
    sources = {e["source"] for e in tl}
    assert "session.jsonl" in sources
    assert "trajectory.jsonl" in sources
    # app_log entries that match correlation should appear too
    assert "app_log" in sources


def test_correlated_logs_have_path_annotations(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    logs = report.data["correlated_logs"]
    assert len(logs) >= 1
    for rec in logs:
        assert "correlation" in rec
        assert "primary" in rec["correlation"]
        assert "path" in rec["correlation"]


def test_strict_correlation_excludes_session_key_only(tmp_path: Path):
    """In strict mode, lines that match only sessionKey should be dropped.

    Our synthetic log has sessionId / runId hits, no sessionKey-only hits, so
    the strict count must be ≤ non-strict count.
    """
    ctx = _build_fixture_home(tmp_path)
    full = _run_panorama(ctx, session_id=SESSION_ID)
    strict = _run_panorama(ctx, session_id=SESSION_ID,
                           strict_correlation=True)
    assert len(strict.data["correlated_logs"]) <= \
        len(full.data["correlated_logs"])


def test_run_index_and_all_runs(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path, run_b=True)
    latest = _run_panorama(ctx, session_id=SESSION_ID)
    # default run_index → latest run only
    assert latest.data["selected_runs"] == [RUN_ID_B]

    first = _run_panorama(ctx, session_id=SESSION_ID, run_index=0)
    assert first.data["selected_runs"] == [RUN_ID_A]

    everything = _run_panorama(ctx, session_id=SESSION_ID, all_runs=True)
    assert set(everything.data["selected_runs"]) == {RUN_ID_A, RUN_ID_B}


def test_verdict_fail_on_artifact_abort(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path, artifact_failure=True)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    assert report.verdict == Verdict.FAIL
    signals = report.data["health_signals"]
    assert any(s.get("kind") == "trajectory_artifact" for s in signals)


def test_verdict_ok_on_clean(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    # Our fixture's app log has no WARN/ERROR-with-correlation, no aborts.
    # Verdict should be OK or WARN (depending on plugin_errors). Plugin p2
    # has activated+error → WARN under runtime_context.
    assert report.verdict in (Verdict.OK, Verdict.WARN)


def test_child_tasks_included(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    children = report.data["child_tasks"]
    assert len(children) == 1
    c = children[0]
    assert c["task_id"] == CHILD_TASK_ID
    assert c["agent_id"] == "research"
    assert c["status"] == "completed"
    assert c["duration_ms"] == 4900


def test_envelope_round_trip(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    env = to_envelope(report)
    assert env["ok"] is True
    assert env["data"]["module"] == "panorama"
    assert env["data"]["verdict"] in ("ok", "warn", "fail")
    # Must JSON-serialize cleanly.
    json.dumps(env, ensure_ascii=False)


def test_mask_sanitizes_tool_args(tmp_path: Path):
    """With --mask, secret-looking args should be scrubbed in the waterfall.
    """
    home = tmp_path / "with-secret"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "ffffffff-1111-2222-3333-444444444444"
    secret_call = "tooluse_SecretCall1"
    records = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T1)},
        {"type": "message", "id": "u-1", "timestamp": _ms_to_iso(T1),
         "message": {"role": "user", "timestamp": T1, "content": "go"}},
        {"type": "message", "id": "a-1", "timestamp": _ms_to_iso(T2),
         "message": {"role": "assistant", "timestamp": T2,
                     "content": [
                         {"type": "toolCall", "id": secret_call, "name": "Run",
                          "input": {"env": "API_KEY=sk-ant-abcdef0123456789"}},
                     ]}},
        {"type": "message", "id": "r-1", "timestamp": _ms_to_iso(T3),
         "message": {"role": "toolResult", "timestamp": T3,
                     "toolCallId": secret_call, "toolName": "Run",
                     "isError": False, "content": "ok"}},
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

    masked = _run_panorama(ctx, session_id=sid, mask=True)
    raw_text = json.dumps(masked.data["tool_waterfall"])
    assert "sk-ant-abcdef0123456789" not in raw_text

    unmasked = _run_panorama(ctx, session_id=sid, mask=False)
    assert "sk-ant-abcdef0123456789" in json.dumps(unmasked.data["tool_waterfall"])


def test_missing_sources_degrade_gracefully(tmp_path: Path):
    """Only session.jsonl present — every other source missing.

    This is the worst-case real-world scenario: trajectory.jsonl never wrote
    out, no app log for today, no runs.sqlite, no sessions.json store. The
    inspector must still produce a Report.
    """
    home = tmp_path / "minimal"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp_path / "logs"  # empty
    log_dir.mkdir(parents=True, exist_ok=True)
    main_sd.mkdir(parents=True, exist_ok=True)

    sid = "99999999-1111-1111-1111-999999999999"
    records = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T1)},
        {"type": "message", "id": "u-1", "timestamp": _ms_to_iso(T1),
         "message": {"role": "user", "timestamp": T1, "content": "ping"}},
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

    report = _run_panorama(ctx, session_id=sid)
    assert report.error is None
    sp = report.data["sources_present"]
    assert sp["session.jsonl"] is True
    assert sp["trajectory.jsonl"] is False
    assert sp["sessions.json"] is False
    assert sp["app_log"] is False
    assert sp["runs.sqlite"] is False
    # Should still build a (mostly empty) timeline.
    assert isinstance(report.data["timeline"], list)


def test_runtime_context_data_kept_section_removed(tmp_path: Path):
    """v1.4.3: runtime_context data is unchanged on the JSON envelope but
    the standalone "Panorama · Runtime Context" pretty section is gone —
    folded into Session Overview.
    """
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    # data still populated for JSON consumers
    assert isinstance(report.data["runtime_context"], list)
    assert report.data["runtime_context"], "runtime_context should be non-empty"
    blk = report.data["runtime_context"][0]
    assert blk["harness_version"] == "0.42.0"

    section_titles = [s.title for s in report.sections]
    assert "Panorama · Runtime Context" not in section_titles
    # Session Overview now carries runtime fields (harness, plugins, etc).
    overview = next(
        s for s in report.sections if s.title == "Panorama · Session Overview"
    )
    keys = [c.name for c in overview.checks]
    assert any(k.startswith("runtime.") for k in keys), (
        "expected runtime.* lines folded into overview, got: " + str(keys)
    )


def test_model_decisions_section_removed_data_dropped(tmp_path: Path):
    """v1.4.3: Model Decisions section is gone; report.data['model_decisions']
    is no longer populated. Log-marker decisions live under health_signals.
    """
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    assert "model_decisions" not in report.data
    section_titles = [s.title for s in report.sections]
    assert "Panorama · Model Decisions" not in section_titles


def test_delivery_section_removed_data_dropped(tmp_path: Path):
    """v1.4.3: Delivery section is removed; delivery events live in the
    timeline with event_type="delivery".
    """
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    assert "delivery" not in report.data
    section_titles = [s.title for s in report.sections]
    assert "Panorama · Delivery" not in section_titles


def test_model_call_input_and_throughput_fields(tmp_path: Path):
    """v1.4.6: per-call lines show input/output tokens and stop reason. The
    unreliable per-call tok/s throughput was removed (round-trip wall-clock
    gaps are not real generation time), so no tok/s must appear. The
    wall-clock duration note must still be present.
    """
    from ocdiag.render.human import render
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    text = render(report, no_color=True)
    # Wall-clock note still present (durations are still shown)
    assert "round-trip wall-clock" in text
    # Input/output tokens still shown per call
    assert "in=10" in text
    assert "in=12" in text
    # Throughput removed entirely — no tok/s anywhere
    assert "tok/s" not in text


def test_window_bound_logs_summary_carries_counters(tmp_path: Path):
    """v1.4.3: logs.summary carries out_of_window_dropped + ts_less_kept."""
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    logs_section = next(
        s for s in report.sections if s.title == "Panorama · Correlated Logs"
    )
    summary = next(
        (c for c in logs_section.checks if c.name == "logs.summary"), None,
    )
    assert summary is not None
    assert "out_of_window_dropped" in summary.data
    assert "ts_less_kept" in summary.data


def test_window_bound_drops_far_future_log(tmp_path: Path):
    """A log entry whose timestamp falls far outside the session window must
    be dropped from correlated_logs (and hence from the timeline).
    """
    ctx = _build_fixture_home(tmp_path)
    log_dir = ctx.log_dir
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / f"openclaw-{today}.log"
    # Append a sessionId-bearing entry far past the window end (T8 + 1 day).
    far_ts = T8 + 24 * 3600 * 1000
    rec = {
        "level": "INFO", "time": far_ts, "pid": 12345,
        "_meta": {"name": json.dumps({"subsystem": "gateway"})},
        "msg": f"sessionId={SESSION_ID} reused-key noise",
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

    report = _run_panorama(ctx, session_id=SESSION_ID)
    logs_section = next(
        s for s in report.sections if s.title == "Panorama · Correlated Logs"
    )
    summary = next(
        c for c in logs_section.checks if c.name == "logs.summary"
    )
    assert summary.data["out_of_window_dropped"] >= 1
    # Timeline should not contain the far-future entry
    for entry in report.data["timeline"]:
        assert entry["ts_ms"] < far_ts - 1000


def test_timeline_truncation_records_dropped_middle():
    """When _build_timeline exceeds its cap, dropped_middle is non-zero and
    truncated is True. We exercise the helper directly to avoid building a
    50k-record fixture.
    """
    from ocdiag.inspectors.panorama import _build_timeline
    # Synthetic session_records that all carry timestamps; trigger the cap.
    cap = 20
    big = []
    for i in range(cap * 3):  # 60 events → far above cap
        big.append({
            "type": "message",
            "timestamp": _ms_to_iso(T0 + i),
            "message": {"role": "user", "timestamp": T0 + i, "content": "x"},
        })
    timeline, stats = _build_timeline(
        session_records=big,
        trajectory_runs=[],
        correlated_logs=[],
        cap=cap,
    )
    assert stats["truncated"] is True
    assert stats["dropped_middle"] > 0
    assert stats["total_before_cap"] == cap * 3
    assert len(timeline) == cap


def test_timeline_skipped_no_ts_counted():
    """Records with no timestamp must be counted, not silently swallowed."""
    from ocdiag.inspectors.panorama import _build_timeline
    bad = [
        {"type": "message", "message": {"role": "user", "content": "no ts"}},
        {"type": "message", "timestamp": _ms_to_iso(T0),
         "message": {"role": "user", "timestamp": T0, "content": "ok"}},
    ]
    timeline, stats = _build_timeline(
        session_records=bad,
        trajectory_runs=[],
        correlated_logs=[],
    )
    assert stats["skipped_no_ts"] == 1
    assert len(timeline) == 1


def test_delivery_in_timeline_via_messaging_tool(tmp_path: Path):
    """When the run sent via messaging tool, the timeline gains a
    source="delivery" entry.
    """
    home = tmp_path / "with-msg"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "deadbeef-1111-2222-3333-444444444444"
    _write_jsonl(main_sd / f"{sid}.jsonl", [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T1)},
        {"type": "message", "id": "u-1", "timestamp": _ms_to_iso(T1),
         "message": {"role": "user", "timestamp": T1, "content": "go"}},
    ])

    # Trajectory with messaging-tool send recorded in artifacts.
    base = {
        "schemaVersion": 1, "traceSchema": "openclaw-trajectory",
        "runId": RUN_ID_A, "sessionId": sid, "sessionKey": SESSION_KEY,
        "provider": "test", "modelId": "test-model",
    }
    traj = [
        {**base, "type": "session.started", "ts": _ms_to_iso(T0),
         "data": {"trigger": "user", "agentId": "main",
                  "messageChannel": "feishu"}},
        {**base, "type": "trace.metadata", "ts": _ms_to_iso(T0 + 1),
         "data": {}},
        {**base, "type": "trace.artifacts", "ts": _ms_to_iso(T6 - 1),
         "data": {
             "finalStatus": "ok", "didSendViaMessagingTool": True,
             "messagingToolSentTargets": ["user-A"],
             "messagingToolSentTexts": ["hi"],
         }},
        {**base, "type": "session.ended", "ts": _ms_to_iso(T6),
         "data": {"status": "ok"}},
    ]
    _write_jsonl(main_sd / f"{sid}.trajectory.jsonl", traj)

    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=agents,
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    paths_mod.CRON_RUNS_DIR = str(home / "cron" / "runs")

    report = _run_panorama(ctx, session_id=sid)
    delivery_entries = [
        e for e in report.data["timeline"] if e.get("event_type") == "delivery"
    ]
    assert len(delivery_entries) >= 1
    assert delivery_entries[0]["source"] == "delivery"
    assert "messaging-tool" in delivery_entries[0]["summary"]


def test_cron_delivery_failure_routes_to_health_signals(tmp_path: Path):
    """A failed cron delivery must surface as a health signal (and therefore
    influence verdict) even though the standalone Delivery section is gone.
    """
    # Build a session whose sessionKey carries cron:<jobId> and a cron-runs
    # file with a failed delivery line.
    home = tmp_path / "with-cron"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "ccccccc1-1111-2222-3333-444444444444"
    job_id = "cccccccc-9999-9999-9999-999999999999"
    cron_session_key = f"agent:main:cron:{job_id}"

    _write_jsonl(main_sd / f"{sid}.jsonl", [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T1)},
        {"type": "message", "id": "u-1", "timestamp": _ms_to_iso(T1),
         "message": {"role": "user", "timestamp": T1, "content": "ping"}},
    ])
    # sessions.json so build_graph discovers sessionKey → cron jobId
    store = {cron_session_key: {"sessionId": sid}}
    with open(main_sd / "sessions.json", "w") as f:
        json.dump(store, f)

    # Cron run record with an explicit failure.
    cron_dir = home / "cron" / "runs"
    cron_dir.mkdir(parents=True, exist_ok=True)
    cron_path = cron_dir / f"{job_id}.jsonl"
    with open(cron_path, "w") as f:
        f.write(json.dumps({
            "ts": T0 + 5000, "jobId": job_id, "action": "finished",
            "status": "ok", "deliveryStatus": "failed",
            "summary": "delivery exploded",
        }) + "\n")

    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=agents,
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    paths_mod.CRON_RUNS_DIR = str(cron_dir)

    report = _run_panorama(ctx, session_id=sid)
    signals = report.data["health_signals"]
    assert any(s.get("kind") == "cron_delivery_failed" for s in signals)
    # Verdict should degrade to FAIL (cron_delivery_failed routes via fail()).
    assert report.verdict in (Verdict.FAIL, Verdict.WARN)


def test_log_decision_routed_to_health_signals(tmp_path: Path):
    """A correlated log line containing a known decision marker must surface
    under health_signals as kind="log_decision" — there is no separate
    Model Decisions section anymore.
    """
    ctx = _build_fixture_home(tmp_path)
    log_dir = ctx.log_dir
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / f"openclaw-{today}.log"
    # Append an in-window line that will be correlated AND mentions the marker
    rec = {
        "level": "INFO", "time": T0 + 4000, "pid": 12345,
        "_meta": {"name": json.dumps({"subsystem": "gateway"})},
        "msg": f"sessionId={SESSION_ID} model_fallback_decision: fallback to alt",
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

    report = _run_panorama(ctx, session_id=SESSION_ID)
    signals = report.data["health_signals"]
    assert any(s.get("kind") == "log_decision" for s in signals)


def test_perf_100k_lines_under_5s(tmp_path: Path):
    """A 100k-line app log should still be filterable in well under 5s."""
    ctx = _build_fixture_home(tmp_path)
    log_dir = ctx.log_dir
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / f"openclaw-{today}.log"
    # Append 100k more lines, of which ~1% mention sessionId.
    with open(log_path, "a", encoding="utf-8") as f:
        for i in range(100_000):
            ts = T0 + i
            if i % 100 == 0:
                line = {"level": "INFO", "time": ts, "pid": 12345,
                        "msg": f"sessionId={SESSION_ID} step {i}"}
            else:
                line = {"level": "INFO", "time": ts, "pid": 12345,
                        "msg": f"unrelated step {i}"}
            f.write(json.dumps(line) + "\n")

    t0 = time.time()
    report = _run_panorama(ctx, session_id=SESSION_ID)
    elapsed = time.time() - t0
    assert report.error is None
    # CI machines vary; 5s is generous given the synthetic content.
    assert elapsed < 5.0, f"panorama took {elapsed:.1f}s on 100k lines"
    # We capped log records; should be at most DEFAULT_LOG_RECORD_CAP.
    assert len(report.data["correlated_logs"]) <= 5000


# ── v1.4.4 task A: prompt-cache breakage ──────────────────────────────────


def _trajectory_with_artifact_overrides(
    sid: str, run_id: str, started_ms: int, ended_ms: int,
    *, artifacts_data_overrides: Dict[str, Any],
    final_status: str = "ok",
) -> List[Dict[str, Any]]:
    """Build a trajectory whose trace.artifacts.data field can be
    surgically overridden by the test. Used by the v1.4.4 tests to inject
    promptCache.observation broke states, itemLifecycle counts, and
    timeout-flag combinations without touching the shared helper.
    """
    base = {
        "schemaVersion": 1, "traceSchema": "openclaw-trajectory",
        "runId": run_id, "sessionId": sid, "sessionKey": SESSION_KEY,
        "provider": "test", "modelId": "test-model",
    }
    artifacts_data = {
        "finalStatus": final_status,
        "aborted": False, "externalAbort": False,
        "timedOut": False, "idleTimedOut": False,
        "timedOutDuringCompaction": False,
        "timedOutDuringToolExecution": False,
        "promptErrorSource": None,
        "usage": {"input": 0, "output": 0,
                  "cacheRead": 0, "cacheWrite": 0, "total": 0},
        "compactionCount": 0,
        "didSendViaMessagingTool": False,
        "messagingToolSentTargets": [],
        "messagingToolSentTexts": [],
        "successfulCronAdds": 0,
    }
    artifacts_data.update(artifacts_data_overrides)
    return [
        {**base, "type": "session.started", "ts": _ms_to_iso(started_ms),
         "data": {"trigger": "user", "agentId": "main",
                  "messageChannel": "feishu"}},
        {**base, "type": "trace.metadata", "ts": _ms_to_iso(started_ms + 1),
         "data": {}},
        {**base, "type": "trace.artifacts",
         "ts": _ms_to_iso(ended_ms - 100), "data": artifacts_data},
        {**base, "type": "session.ended", "ts": _ms_to_iso(ended_ms),
         "data": {"status": final_status}},
    ]


def _make_minimal_session_jsonl(main_sd: Path, sid: str) -> None:
    _write_jsonl(main_sd / f"{sid}.jsonl", [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T1)},
        {"type": "message", "id": "u-1", "timestamp": _ms_to_iso(T1),
         "message": {"role": "user", "timestamp": T1, "content": "go"}},
    ])


def _ctx_for_session(home: Path, log_dir: Path) -> DiagContext:
    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=home / "agents",
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    paths_mod.CRON_RUNS_DIR = str(home / "cron" / "runs")
    return ctx


def _bare_home(tmp_path: Path, sid: str, traj: List[Dict[str, Any]]
               ) -> DiagContext:
    home = tmp_path / "home"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _make_minimal_session_jsonl(main_sd, sid)
    _write_jsonl(main_sd / f"{sid}.trajectory.jsonl", traj)
    return _ctx_for_session(home, log_dir)


def test_prompt_cache_broke_extracted_and_warns(tmp_path: Path):
    """task A: broke=True with prev/cur cacheRead emits a warn signal +
    overview line, and the lost-token math is correct.
    """
    sid = "11111111-aaaa-aaaa-aaaa-111111111aaa"
    rid = "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        artifacts_data_overrides={
            "promptCache": {
                "observation": {
                    "broke": True,
                    "previousCacheRead": 1_000_000,
                    "cacheRead": 250_000,
                },
            },
        },
    )
    ctx = _bare_home(tmp_path, sid, traj)
    report = _run_panorama(ctx, session_id=sid)
    rt = report.data["runtime_context"][0]
    assert rt["cache_broke"] is True
    assert rt["cache_read_observed"] == 250_000
    assert rt["cache_read_previous"] == 1_000_000
    assert rt["cache_read_lost"] == 750_000  # exact math
    sigs = report.data["health_signals"]
    cache_sigs = [s for s in sigs if s.get("kind") == "prompt_cache_broke"]
    assert len(cache_sigs) == 1
    assert cache_sigs[0]["lost_tokens"] == 750_000
    # Verdict should be at least WARN, since broke routed via warn().
    assert report.verdict in (Verdict.WARN, Verdict.FAIL)
    # Overview emits the warn line with a recognizable message.
    overview = next(
        s for s in report.sections if s.title == "Panorama · Session Overview"
    )
    assert any(
        "cache broke" in c.message and "750,000" in c.message
        for c in overview.checks
    )


def test_prompt_cache_hit_renders_ok_line(tmp_path: Path):
    """task A: broke=False with non-zero cacheRead emits an OK overview
    line and no health signal (cache is healthy).
    """
    sid = "22222222-bbbb-bbbb-bbbb-222222222bbb"
    rid = "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        artifacts_data_overrides={
            "promptCache": {
                "observation": {
                    "broke": False,
                    "cacheRead": 480_000,
                },
            },
        },
    )
    ctx = _bare_home(tmp_path, sid, traj)
    report = _run_panorama(ctx, session_id=sid)
    sigs = report.data["health_signals"]
    assert not any(s.get("kind") == "prompt_cache_broke" for s in sigs)
    overview = next(
        s for s in report.sections if s.title == "Panorama · Session Overview"
    )
    assert any(
        "cache hit" in c.message and "480,000" in c.message
        for c in overview.checks
    )


def test_prompt_cache_observation_missing_no_render(tmp_path: Path):
    """task A: older runs with no observation block must not crash and
    must not render a cache line.
    """
    sid = "33333333-cccc-cccc-cccc-333333333ccc"
    rid = "cccccccc-3333-3333-3333-cccccccccccc"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        artifacts_data_overrides={
            "promptCache": {
                "lastCallUsage": {
                    "input": 1, "output": 2, "cacheRead": 0,
                    "cacheWrite": 100, "total": 103,
                },
            },
        },
    )
    ctx = _bare_home(tmp_path, sid, traj)
    report = _run_panorama(ctx, session_id=sid)
    rt = report.data["runtime_context"][0]
    assert "cache_broke" not in rt or rt["cache_broke"] is None
    assert "cache_read_lost" not in rt
    overview = next(
        s for s in report.sections if s.title == "Panorama · Session Overview"
    )
    assert not any(
        "cache broke" in c.message or "cache hit" in c.message
        for c in overview.checks
    )


# ── v1.4.4 task B: itemLifecycle render + incomplete ─────────────────────


def test_item_lifecycle_renders_in_overview(tmp_path: Path):
    sid = "44444444-dddd-dddd-dddd-444444444ddd"
    rid = "dddddddd-4444-4444-4444-dddddddddddd"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        artifacts_data_overrides={
            "itemLifecycle": {
                "startedCount": 8, "completedCount": 8, "activeCount": 0,
            },
        },
    )
    ctx = _bare_home(tmp_path, sid, traj)
    report = _run_panorama(ctx, session_id=sid)
    overview = next(
        s for s in report.sections if s.title == "Panorama · Session Overview"
    )
    line = next(
        (c for c in overview.checks if c.name == "stats.lifecycle"), None,
    )
    assert line is not None
    assert "started=8" in line.message and "completed=8" in line.message


def test_item_lifecycle_incomplete_warns(tmp_path: Path):
    """task B: started>completed and active==0 → items_incomplete signal.
    """
    sid = "55555555-eeee-eeee-eeee-555555555eee"
    rid = "eeeeeeee-5555-5555-5555-eeeeeeeeeeee"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        artifacts_data_overrides={
            "itemLifecycle": {
                "startedCount": 12, "completedCount": 7, "activeCount": 0,
            },
        },
    )
    ctx = _bare_home(tmp_path, sid, traj)
    report = _run_panorama(ctx, session_id=sid)
    sigs = report.data["health_signals"]
    inc = next(
        (s for s in sigs if s.get("kind") == "items_incomplete"), None,
    )
    assert inc is not None
    assert inc["dropped"] == 5
    assert inc["started"] == 12 and inc["completed"] == 7
    assert report.verdict in (Verdict.WARN, Verdict.FAIL)


def test_item_lifecycle_active_leak_still_warns(tmp_path: Path):
    """task B regression: active>0 still produces the existing tool_call_leak
    signal even with the new incomplete code path. Both can fire for the
    same run if completed<started AND active>0 — the active leak takes
    precedence (we only emit the incomplete signal when active==0).
    """
    sid = "66666666-ffff-ffff-ffff-666666666fff"
    rid = "ffffffff-6666-6666-6666-ffffffffffff"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        artifacts_data_overrides={
            "itemLifecycle": {
                "startedCount": 27, "completedCount": 25, "activeCount": 2,
            },
        },
    )
    ctx = _bare_home(tmp_path, sid, traj)
    report = _run_panorama(ctx, session_id=sid)
    sigs = report.data["health_signals"]
    leak = next(
        (s for s in sigs if s.get("kind") == "tool_call_leak"), None,
    )
    assert leak is not None and leak["active"] == 2
    assert not any(
        s.get("kind") == "items_incomplete" for s in sigs
    ), "incomplete should be suppressed when active>0"


# ── v1.4.4 task C: timeout/abort classification ──────────────────────────


def test_timeout_classification_idle(tmp_path: Path):
    """task C: idleTimedOut maps to 'went idle...' and suppresses the
    bare timedOut message even when timedOut is also true (real data has
    both flags set in idle-timeout cases).
    """
    sid = "77777777-1234-5678-9abc-77777777aaaa"
    rid = "77777777-1111-2222-3333-77777777aaaa"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        final_status="error",
        artifacts_data_overrides={
            "aborted": True, "timedOut": True, "idleTimedOut": True,
            "promptErrorSource": "prompt", "finalStatus": "error",
        },
    )
    ctx = _bare_home(tmp_path, sid, traj)
    report = _run_panorama(ctx, session_id=sid)
    sigs = report.data["health_signals"]
    art = next(s for s in sigs if s.get("kind") == "trajectory_artifact")
    human = art.get("human_summary") or []
    assert any("went idle" in h for h in human)
    assert not any("exceeded turn timeout" in h for h in human)
    assert any("prompt error source: prompt" in h for h in human)
    # Raw flags still kept on JSON envelope.
    assert "idleTimedOut" in art["flags"]
    assert "timedOut" in art["flags"]


def test_timeout_classification_during_tool_execution(tmp_path: Path):
    sid = "77777777-2222-2222-2222-77777777bbbb"
    rid = "77777777-3333-3333-3333-77777777bbbb"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        final_status="error",
        artifacts_data_overrides={
            "timedOut": True, "timedOutDuringToolExecution": True,
            "finalStatus": "error",
        },
    )
    ctx = _bare_home(tmp_path, sid, traj)
    report = _run_panorama(ctx, session_id=sid)
    art = next(
        s for s in report.data["health_signals"]
        if s.get("kind") == "trajectory_artifact"
    )
    human = art.get("human_summary") or []
    assert any("hung during tool execution" in h for h in human)
    assert not any("exceeded turn timeout" in h for h in human)


def test_timeout_classification_external_abort_only(tmp_path: Path):
    sid = "77777777-4444-4444-4444-77777777cccc"
    rid = "77777777-5555-5555-5555-77777777cccc"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        final_status="error",
        artifacts_data_overrides={
            "aborted": True, "externalAbort": True, "finalStatus": "error",
        },
    )
    ctx = _bare_home(tmp_path, sid, traj)
    report = _run_panorama(ctx, session_id=sid)
    art = next(
        s for s in report.data["health_signals"]
        if s.get("kind") == "trajectory_artifact"
    )
    human = art.get("human_summary") or []
    assert any("cancelled externally" in h for h in human)
    assert not any("aborted (internal)" in h for h in human)


# ── v1.4.4 task D: OTel traceId correlation ──────────────────────────────


def test_otel_trace_id_correlation_pulls_in_orphan_lines(tmp_path: Path):
    """A log line that shares the OTel traceId but does NOT mention
    sessionId/runId must be admitted by the second pass and tagged
    with path=otel-trace.
    """
    ctx = _build_fixture_home(tmp_path)
    log_dir = ctx.log_dir
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / f"openclaw-{today}.log"
    # Append a sessionId-bearing line WITH a traceId, then an orphan that
    # only carries the same traceId — the orphan must be admitted via OTel.
    trace_id = "abcdef1234567890" + "0" * 16  # exactly 32 hex
    sid_line = {
        "level": "INFO", "time": T0 + 4500, "pid": 1,
        "traceId": trace_id, "spanId": "1111111111111111", "traceFlags": "01",
        "_meta": {"name": json.dumps({"subsystem": "agent/embedded"})},
        "msg": f"sessionId={SESSION_ID} provider=bedrock",
    }
    orphan = {
        "level": "INFO", "time": T0 + 4600, "pid": 1,
        "traceId": trace_id, "spanId": "2222222222222222", "traceFlags": "01",
        "_meta": {"name": json.dumps({"subsystem": "harness/internal"})},
        "msg": "deep stack frame: bedrock-converse adapter started",
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(sid_line) + "\n")
        f.write(json.dumps(orphan) + "\n")
    report = _run_panorama(ctx, session_id=SESSION_ID)
    logs = report.data["correlated_logs"]
    paths = [
        rec.get("correlation", {}).get("path") for rec in logs
    ]
    # The orphan must show up with an otel-trace path.
    otel_only = [p for p in paths if p and p.startswith("otel-trace:")]
    assert otel_only, (
        f"orphan line via OTel traceId not admitted; paths={paths}"
    )
    assert report.data.get("otel_trace_ids") == [trace_id]


def test_otel_trace_id_correlation_strict_blocks_session_key_seed(
        tmp_path: Path):
    """In strict mode the harvest only accepts traceIds discovered on a
    sessionId/runId-seeded line — sessionKey-seeded lines must not expand.
    """
    home = tmp_path / "strict-otel"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "88888888-1111-1111-1111-888888888888"
    sk = "agent:main:feishu:user:U-strict"
    _make_minimal_session_jsonl(main_sd, sid)
    # sessions.json gives us the sessionKey for matching.
    with open(main_sd / "sessions.json", "w") as f:
        json.dump({sk: {"sessionId": sid}}, f)

    # Trajectory makes the run window valid.
    traj = _trajectory_with_artifact_overrides(
        sid, "88888888-aaaa-aaaa-aaaa-888888888888", T0, T6,
        artifacts_data_overrides={},
    )
    _write_jsonl(main_sd / f"{sid}.trajectory.jsonl", traj)

    # Build today-dated log: a sessionKey-only line carrying a traceId,
    # and an orphan sharing that traceId. Under strict the orphan must
    # NOT be admitted.
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / f"openclaw-{today}.log"
    trace_id = "deadbeef" + "0" * 24
    sk_line = {
        "level": "INFO", "time": T0 + 1000, "pid": 1,
        "traceId": trace_id, "spanId": "aaaaaaaaaaaaaaaa", "traceFlags": "01",
        "_meta": {"name": json.dumps({"subsystem": "channel/feishu"})},
        "msg": f"sessionKey={sk} arrived",
    }
    orphan = {
        "level": "INFO", "time": T0 + 2000, "pid": 1,
        "traceId": trace_id, "spanId": "bbbbbbbbbbbbbbbb", "traceFlags": "01",
        "_meta": {"name": json.dumps({"subsystem": "harness/internal"})},
        "msg": "deep stack: no key in this line",
    }
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(sk_line) + "\n")
        f.write(json.dumps(orphan) + "\n")

    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=agents,
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    paths_mod.CRON_RUNS_DIR = str(home / "cron" / "runs")

    strict = _run_panorama(ctx, session_id=sid, strict_correlation=True)
    paths = [
        rec.get("correlation", {}).get("path")
        for rec in strict.data["correlated_logs"]
    ]
    assert not any(p and p.startswith("otel-trace:") for p in paths), (
        f"strict mode wrongly expanded traceId via sessionKey seed: {paths}"
    )


# ── v1.4.4 task E: lane queue latency / concurrency ──────────────────────


def _append_log_lines(log_path: Path, lines: List[Dict[str, Any]]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec) + "\n")


def _diag_subsystem_log(text: str, ts_ms: int, sid: str = SESSION_ID
                        ) -> Dict[str, Any]:
    """OpenClaw shape: positional "0" carries the subsystem JSON, "1"
    carries the message string. We always include a sessionId substring
    so pass-1 correlation pulls it in.
    """
    return {
        "0": json.dumps({"subsystem": "diagnostic"}),
        "1": text + f" sessionId={sid}",
        "_meta": {"name": json.dumps({"subsystem": "diagnostic"})},
        "level": "INFO", "time": ts_ms, "pid": 1,
    }


def test_queue_events_parsed_and_summarized(tmp_path: Path):
    """task E: lane dequeue/enqueue and run-registered lines must be
    parsed into typed records, with the summary on session overview.
    """
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    lane = f"session:agent:main:feishu:run:{RUN_ID_A}"
    _append_log_lines(log_path, [
        _diag_subsystem_log(
            f"lane enqueue: lane={lane} queueSize=2", T0 + 100),
        _diag_subsystem_log(
            f"lane dequeue: lane={lane} waitMs=350 queueSize=1", T0 + 600),
        _diag_subsystem_log(
            f"run registered: sessionId={SESSION_ID} totalActive=3",
            T0 + 700),
    ])
    report = _run_panorama(ctx, session_id=SESSION_ID)
    parsed = report.data["log_parsed"]
    deq = [q for q in parsed["queue_events"] if q["kind"] == "dequeue"]
    assert len(deq) == 1
    assert deq[0]["wait_ms"] == 350  # exact value
    assert deq[0]["queue_size"] == 1
    assert parsed["queue_summary"]["max_concurrent_runs"] == 3
    assert parsed["queue_summary"]["max_queue_size"] == 2
    overview = next(
        s for s in report.sections if s.title == "Panorama · Session Overview"
    )
    queue_line = next(
        (c for c in overview.checks if c.name == "stats.queue"), None,
    )
    assert queue_line is not None
    assert "max wait=350ms" in queue_line.message
    assert "max concurrentRuns=3" in queue_line.message


def test_queue_wait_slow_warns(tmp_path: Path):
    """task E: a dequeue with waitMs above SLOW_QUEUE_WAIT_MS (>2000) must
    emit a queue_wait_slow signal and a 'queued <N>ms' warn line.
    """
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    lane = f"session:agent:main:feishu:run:{RUN_ID_A}"
    _append_log_lines(log_path, [
        _diag_subsystem_log(
            f"lane dequeue: lane={lane} waitMs=4500 queueSize=2", T0 + 800),
    ])
    report = _run_panorama(ctx, session_id=SESSION_ID)
    sigs = report.data["health_signals"]
    slow = [s for s in sigs if s.get("kind") == "queue_wait_slow"]
    assert len(slow) == 1
    assert slow[0]["wait_ms"] == 4500


# ── v1.4.4 task F: authoritative run duration from logs ──────────────────


def test_log_run_duration_extracted(tmp_path: Path):
    """task F: 'embedded run prompt end ... durationMs=N' for the primary
    run id must surface as runtime_context.log_run_duration_ms and as a
    Model Calls line.
    """
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    _append_log_lines(log_path, [{
        "0": json.dumps({"subsystem": "agent/embedded"}),
        "1": (
            f"embedded run prompt end: runId={RUN_ID_A} "
            f"sessionId={SESSION_ID} durationMs=236421"
        ),
        "_meta": {"name": json.dumps({"subsystem": "agent/embedded"})},
        "level": "INFO", "time": T0 + 9500, "pid": 1,
    }])
    report = _run_panorama(ctx, session_id=SESSION_ID)
    rt = report.data["runtime_context"][-1]  # primary run is last selected
    assert rt.get("log_run_duration_ms") == 236421
    section = next(
        s for s in report.sections if s.title == "Panorama · Model Calls"
    )
    line = next(
        (c for c in section.checks if c.name == "model.run_wall_time"),
        None,
    )
    assert line is not None
    assert "236421ms" in line.message


# ── v1.4.4 task G: context-overflow precheck ─────────────────────────────


def test_context_precheck_route_fits_renders_ok(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    _append_log_lines(log_path, [{
        "0": json.dumps({"subsystem": "agent/embedded"}),
        "1": (
            f"[context-overflow-precheck] pre-prompt check "
            f"sessionKey=agent:main:feishu:run:{RUN_ID_A} "
            f"provider=test/test-model route=fits "
            f"estimatedPromptTokens=17688 sessionId={SESSION_ID}"
        ),
        "_meta": {"name": json.dumps({"subsystem": "agent/embedded"})},
        "level": "INFO", "time": T0 + 1500, "pid": 1,
    }])
    report = _run_panorama(ctx, session_id=SESSION_ID)
    parsed = report.data["log_parsed"]
    assert parsed["context_prechecks"][0]["route"] == "fits"
    assert parsed["context_prechecks"][0]["estimated_prompt_tokens"] == 17688
    overview = next(
        s for s in report.sections if s.title == "Panorama · Session Overview"
    )
    line = next(
        (c for c in overview.checks if c.name == "stats.precheck"), None,
    )
    assert line is not None
    assert "route=fits" in line.message


def test_context_precheck_route_compact_warns(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    _append_log_lines(log_path, [{
        "0": json.dumps({"subsystem": "agent/embedded"}),
        "1": (
            f"[context-overflow-precheck] pre-prompt check "
            f"sessionKey=agent:main:feishu:run:{RUN_ID_A} "
            f"provider=test/test-model route=compact "
            f"estimatedPromptTokens=950000 sessionId={SESSION_ID}"
        ),
        "_meta": {"name": json.dumps({"subsystem": "agent/embedded"})},
        "level": "INFO", "time": T0 + 1500, "pid": 1,
    }])
    report = _run_panorama(ctx, session_id=SESSION_ID)
    sigs = report.data["health_signals"]
    pc = [s for s in sigs if s.get("kind") == "context_precheck_overflow"]
    assert len(pc) == 1
    assert pc[0]["route"] == "compact"
    assert pc[0]["estimated_prompt_tokens"] == 950000


# ── v1.4.4 task H: session state transitions ─────────────────────────────


def test_state_transitions_parsed_and_in_timeline(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    _append_log_lines(log_path, [{
        "0": json.dumps({"subsystem": "diagnostic"}),
        "1": (
            f"session state: sessionId={SESSION_ID} "
            f"sessionKey=agent:main:feishu prev=idle new=processing "
            f'reason="" queueDepth=1'
        ),
        "_meta": {"name": json.dumps({"subsystem": "diagnostic"})},
        "level": "INFO", "time": T0 + 200, "pid": 1,
    }, {
        "0": json.dumps({"subsystem": "diagnostic"}),
        "1": (
            f"session state: sessionId={SESSION_ID} "
            f"sessionKey=agent:main:feishu prev=processing new=aborted "
            f'reason="external" queueDepth=0'
        ),
        "_meta": {"name": json.dumps({"subsystem": "diagnostic"})},
        "level": "INFO", "time": T0 + 8000, "pid": 1,
    }])
    report = _run_panorama(ctx, session_id=SESSION_ID)
    parsed = report.data["log_parsed"]
    assert len(parsed["state_transitions"]) == 2
    assert parsed["state_transitions"][1]["new"] == "aborted"
    # Aborted transition surfaces under health.
    sigs = report.data["health_signals"]
    abnormal = [
        s for s in sigs if s.get("kind") == "state_transition_abnormal"
    ]
    assert len(abnormal) == 1
    assert abnormal[0]["new"] == "aborted"
    # Timeline carries source="state" entries.
    states = [e for e in report.data["timeline"] if e.get("source") == "state"]
    assert len(states) == 2


# ── v1.4.4 task I: config hot-reload events ──────────────────────────────


def test_config_reload_applied_in_timeline(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    # We need this line correlated, so include sessionId substring.
    # Real reload lines do NOT carry sessionId — but they share the OTel
    # traceId of an in-window run. For unit testing we cheat by appending
    # the sessionId to the message; the parser only cares about the
    # 'config hot reload applied (...)' substring.
    _append_log_lines(log_path, [{
        "0": json.dumps({"subsystem": "gateway/reload"}),
        "1": (
            "config hot reload applied (agents.list, plugins.allow) "
            f"sessionId={SESSION_ID}"
        ),
        "_meta": {"name": json.dumps({"subsystem": "gateway/reload"})},
        "level": "INFO", "time": T0 + 3000, "pid": 1,
    }])
    report = _run_panorama(ctx, session_id=SESSION_ID)
    parsed = report.data["log_parsed"]
    reloads = parsed["config_reloads"]
    assert len(reloads) == 1
    assert reloads[0]["outcome"] == "applied"
    assert "agents.list" in reloads[0]["keys"]
    timeline = report.data["timeline"]
    assert any(e.get("event_type") == "config_reload" for e in timeline)


def test_config_reload_skipped_warns(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    _append_log_lines(log_path, [{
        "0": json.dumps({"subsystem": "gateway/reload"}),
        "1": (
            "config reload skipped (invalid config): JSON5 parse error: "
            "trailing comma at line 23 "
            f"sessionId={SESSION_ID}"
        ),
        "_meta": {"name": json.dumps({"subsystem": "gateway/reload"})},
        "level": "WARN", "time": T0 + 4000, "pid": 1,
    }])
    report = _run_panorama(ctx, session_id=SESSION_ID)
    sigs = report.data["health_signals"]
    failed = [s for s in sigs if s.get("kind") == "config_reload_failed"]
    assert len(failed) == 1
    assert "JSON5 parse error" in failed[0]["reason"]


# ── v1.4.4 task J: window-bound + strict + mask compose ──────────────────


def test_log_parsed_inherits_window_bound(tmp_path: Path):
    """task J: log lines outside the session window must NOT appear in
    log_parsed buckets — they're filtered by the same window pass that
    produces correlated_logs.
    """
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    far_future = T8 + 24 * 3600 * 1000  # one day after the window
    # In-window queue dequeue (this should appear).
    _append_log_lines(log_path, [{
        "0": json.dumps({"subsystem": "diagnostic"}),
        "1": (
            f"lane dequeue: lane=session:agent:main:feishu "
            f"waitMs=42 queueSize=1 sessionId={SESSION_ID}"
        ),
        "_meta": {"name": json.dumps({"subsystem": "diagnostic"})},
        "level": "INFO", "time": T0 + 500, "pid": 1,
    }, {
        # Out-of-window queue dequeue (this must be dropped).
        "0": json.dumps({"subsystem": "diagnostic"}),
        "1": (
            f"lane dequeue: lane=session:agent:main:feishu "
            f"waitMs=999 queueSize=9 sessionId={SESSION_ID}"
        ),
        "_meta": {"name": json.dumps({"subsystem": "diagnostic"})},
        "level": "INFO", "time": far_future, "pid": 1,
    }])
    report = _run_panorama(ctx, session_id=SESSION_ID)
    deq = [
        q for q in report.data["log_parsed"]["queue_events"]
        if q["kind"] == "dequeue"
    ]
    assert len(deq) == 1
    assert deq[0]["wait_ms"] == 42, "out-of-window dequeue leaked through"


# ── v1.4.4: perf — two-pass traceId scan stays linear ────────────────────


def test_perf_100k_lines_with_otel_under_6s(tmp_path: Path):
    """Two-pass OTel correlation must be O(n) not O(n^2). Build a 100k-line
    log with ~1% sessionId mentions and ~5% extra trace-only siblings, then
    assert the panorama call stays under a generous wall-clock cap.
    """
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    trace_id = "f00ba7" + "0" * 26
    with open(log_path, "a", encoding="utf-8") as f:
        for i in range(100_000):
            ts = T0 + i
            if i % 100 == 0:
                rec = {
                    "level": "INFO", "time": ts, "pid": 1,
                    "traceId": trace_id, "spanId": f"{i:016x}",
                    "msg": f"sessionId={SESSION_ID} step {i}",
                }
            elif i % 20 == 0:
                # Sibling trace line — orphan that pass 2 will admit.
                rec = {
                    "level": "INFO", "time": ts, "pid": 1,
                    "traceId": trace_id, "spanId": f"{i:016x}",
                    "msg": f"deep stack step {i}",
                }
            else:
                rec = {
                    "level": "INFO", "time": ts, "pid": 1,
                    "msg": f"unrelated step {i}",
                }
            f.write(json.dumps(rec) + "\n")
    t0 = time.time()
    report = _run_panorama(ctx, session_id=SESSION_ID)
    elapsed = time.time() - t0
    assert report.error is None
    assert elapsed < 6.0, f"two-pass scan took {elapsed:.1f}s on 100k lines"
    # The cap stays in force.
    assert len(report.data["correlated_logs"]) <= 5000


# ── v1.4.4: end-to-end smoke against a real session ──────────────────────


REAL_SESSION_ID = "63d70b29-1a14-4a2b-83c5-9432f9987f40"
REAL_SESSIONS_DIR = Path("/root/.openclaw/agents/main/sessions")
REAL_LOG_DIR = Path("/tmp/openclaw")
REAL_HOME = Path("/root/.openclaw")


@pytest.mark.skipif(
    not (REAL_SESSIONS_DIR / f"{REAL_SESSION_ID}.jsonl").is_file(),
    reason="real session fixture not present on this host",
)
def test_e2e_real_session_smoke():
    """Smoke: run panorama against the actual session 63d70b29 and assert
    it produces a Report with the expected sections, no error, and a
    parsed log_parsed dict (queue/run-duration/etc may be empty depending
    on what's in today's log — we only check structure).
    """
    cfg = REAL_HOME / "config.json"
    ctx = DiagContext(
        openclaw_home=REAL_HOME,
        config_path=cfg if cfg.is_file() else REAL_HOME / "openclaw.json",
        log_dir=REAL_LOG_DIR,
        sessions_base=REAL_HOME / "agents",
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(REAL_HOME)
    paths_mod.CRON_RUNS_DIR = str(REAL_HOME / "cron" / "runs")
    report = _run_panorama(ctx, session_id=REAL_SESSION_ID)
    assert report.error is None, f"unexpected error: {report.error}"
    section_titles = [s.title for s in report.sections]
    for required in (
        "Panorama · Session Overview",
        "Panorama · Timeline",
        "Panorama · Model Calls",
        "Panorama · Tool Execution",
        "Panorama · Correlated Logs",
        "Panorama · Health Signals",
    ):
        assert required in section_titles, (
            f"missing section {required}; got {section_titles}"
        )
    assert isinstance(report.data.get("log_parsed"), dict)


# ── v1.4.5: multi-attempt-per-runId handling ─────────────────────────────


def _build_attempt_cycle(
    *, sid: str, run_id: str, started_ms: int, ended_ms: int,
    artifacts_overrides: Dict[str, Any], final_status: str = "success",
) -> List[Dict[str, Any]]:
    """One full attempt cycle (session.started → ... → session.ended) with
    a configurable trace.artifacts payload. seq counters reset to 1 per
    cycle, mirroring the real trajectory shape."""
    base = {
        "schemaVersion": 1, "traceSchema": "openclaw-trajectory",
        "runId": run_id, "sessionId": sid, "sessionKey": SESSION_KEY,
        "provider": "test", "modelId": "test-model",
    }
    artifacts_data = {
        "finalStatus": final_status,
        "aborted": False, "externalAbort": False,
        "timedOut": False, "idleTimedOut": False,
        "timedOutDuringCompaction": False,
        "timedOutDuringToolExecution": False,
        "promptErrorSource": None,
        "usage": {"input": 0, "output": 0,
                  "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }
    artifacts_data.update(artifacts_overrides)
    return [
        {**base, "type": "session.started", "ts": _ms_to_iso(started_ms),
         "seq": 1,
         "data": {"trigger": "user", "agentId": "main",
                  "messageChannel": "feishu"}},
        {**base, "type": "trace.metadata",
         "ts": _ms_to_iso(started_ms + 1), "seq": 2, "data": {}},
        {**base, "type": "context.compiled",
         "ts": _ms_to_iso(started_ms + 2), "seq": 3,
         "data": {"messages": [], "tools": []}},
        {**base, "type": "prompt.submitted",
         "ts": _ms_to_iso(started_ms + 3), "seq": 4, "data": {}},
        {**base, "type": "model.completed",
         "ts": _ms_to_iso(ended_ms - 200), "seq": 5, "data": {}},
        {**base, "type": "trace.artifacts",
         "ts": _ms_to_iso(ended_ms - 100), "seq": 6,
         "data": artifacts_data},
        {**base, "type": "session.ended",
         "ts": _ms_to_iso(ended_ms), "seq": 7,
         "data": {"status": final_status}},
    ]


def test_multi_attempt_failed_then_success_surfaces_hidden_failure(
        tmp_path: Path):
    """v1.4.5: mirror the real e37602da pattern. One runId, two cycles:
    attempt #1 idle-times out, attempt #2 succeeds. The pre-1.4.5 grouper
    overwrote attempt #1 with #2 → the failure was invisible. We now
    surface a retried_after_failure WARN signal carrying the classified
    reason for #1.
    """
    sid = "abcdef00-1111-2222-3333-444444444aaa"
    rid = "abcdef00-aaaa-bbbb-cccc-dddddddddddd"
    home = tmp_path / "home"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _make_minimal_session_jsonl(main_sd, sid)

    # Attempt #1: 5 minutes, idle-timed-out + aborted + promptErrorSource.
    cycle1 = _build_attempt_cycle(
        sid=sid, run_id=rid, started_ms=T0, ended_ms=T0 + 5 * 60 * 1000,
        final_status="error",
        artifacts_overrides={
            "aborted": True, "timedOut": True, "idleTimedOut": True,
            "promptErrorSource": "prompt", "finalStatus": "error",
        },
    )
    # Attempt #2: 52 seconds, success.
    cycle2_start = T0 + 5 * 60 * 1000 + 100
    cycle2 = _build_attempt_cycle(
        sid=sid, run_id=rid, started_ms=cycle2_start,
        ended_ms=cycle2_start + 52 * 1000,
        artifacts_overrides={"finalStatus": "success"},
    )
    _write_jsonl(main_sd / f"{sid}.trajectory.jsonl", cycle1 + cycle2)
    ctx = _ctx_for_session(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)

    # The runId still groups into ONE run; attempt_count reveals the truth.
    selected_runs = report.data["selected_runs"]
    assert selected_runs == [rid]
    run_attempts = report.data["run_attempts"]
    assert len(run_attempts) == 1
    rd = run_attempts[0]
    assert rd["attempt_count"] == 2
    assert rd["had_failed_attempt"] is True
    a1, a2 = rd["attempts"]
    # Per-attempt accuracy
    assert a1["index"] == 1 and a2["index"] == 2
    assert a1["failed"] is True and a2["failed"] is False
    assert a1["final_status"] == "error"
    assert a2["final_status"] == "success"
    assert a1["prompt_error_source"] == "prompt"
    assert "idleTimedOut" in (a1["failure_flags"] or [])
    assert "promptErrorSource" in (a1["failure_flags"] or [])
    # Per-attempt durations match what we wrote
    assert a1["duration_ms"] == 5 * 60 * 1000
    assert a2["duration_ms"] == 52 * 1000

    # Health Signals: a retried_after_failure entry must list cycle 1 as
    # idle and final as success.
    sigs = report.data["health_signals"]
    retry_sigs = [s for s in sigs if s.get("kind") == "retried_after_failure"]
    assert len(retry_sigs) == 1
    rs = retry_sigs[0]
    assert rs["attempt_count"] == 2
    assert rs["failed_count"] == 1
    assert rs["final_status"] == "success"
    assert rs["final_failed"] is False
    chain = " | ".join(rs["per_attempt"])
    assert "went idle" in chain
    assert "success" in chain

    # Verdict: WARN (recovered), not FAIL.
    assert report.verdict == Verdict.WARN

    # Session Overview must show the attempts line.
    overview = next(
        s for s in report.sections if s.title == "Panorama · Session Overview"
    )
    assert any(
        "attempts: 2" in c.message and "1 failed" in c.message
        and "final=success" in c.message
        for c in overview.checks
    )


def test_multi_attempt_both_failed_keeps_fail_verdict(tmp_path: Path):
    """v1.4.5: when the FINAL attempt also fails, verdict stays FAIL —
    the recovery WARN should not weaken what was already a hard failure.
    The retried_after_failure signal still fires and carries final_failed=True.
    """
    sid = "abcdef00-1111-2222-3333-444444444bbb"
    rid = "abcdef00-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    home = tmp_path / "home"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _make_minimal_session_jsonl(main_sd, sid)

    cycle1 = _build_attempt_cycle(
        sid=sid, run_id=rid, started_ms=T0, ended_ms=T0 + 30_000,
        final_status="error",
        artifacts_overrides={
            "aborted": True, "timedOut": True, "idleTimedOut": True,
            "finalStatus": "error",
        },
    )
    c2_start = T0 + 30_100
    cycle2 = _build_attempt_cycle(
        sid=sid, run_id=rid, started_ms=c2_start, ended_ms=c2_start + 5_000,
        final_status="error",
        artifacts_overrides={
            "aborted": True, "timedOut": True,
            "timedOutDuringToolExecution": True, "finalStatus": "error",
        },
    )
    _write_jsonl(main_sd / f"{sid}.trajectory.jsonl", cycle1 + cycle2)
    ctx = _ctx_for_session(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)

    sigs = report.data["health_signals"]
    retry_sigs = [s for s in sigs if s.get("kind") == "retried_after_failure"]
    assert len(retry_sigs) == 1
    assert retry_sigs[0]["final_failed"] is True
    assert retry_sigs[0]["failed_count"] == 2
    # Final attempt also failed → verdict FAIL.
    assert report.verdict == Verdict.FAIL


def test_single_cycle_no_regression(tmp_path: Path):
    """v1.4.5 control: a single-cycle run must yield attempt_count=1 and
    NO retried_after_failure signal. Existing tests already cover the
    rest of the output; this guards the structural fields specifically.
    """
    sid = "abcdef00-1111-2222-3333-444444444ccc"
    rid = "abcdef00-cccc-cccc-cccc-cccccccccccc"
    home = tmp_path / "home"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _make_minimal_session_jsonl(main_sd, sid)
    cycle = _build_attempt_cycle(
        sid=sid, run_id=rid, started_ms=T0, ended_ms=T0 + 10_000,
        artifacts_overrides={"finalStatus": "success"},
    )
    _write_jsonl(main_sd / f"{sid}.trajectory.jsonl", cycle)
    ctx = _ctx_for_session(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)

    rd = report.data["run_attempts"][0]
    assert rd["attempt_count"] == 1
    assert rd["had_failed_attempt"] is False
    assert len(rd["attempts"]) == 1
    assert rd["attempts"][0]["failed"] is False
    sigs = report.data["health_signals"]
    assert not any(
        s.get("kind") == "retried_after_failure" for s in sigs
    )
    # Overview must NOT show an attempts line for single-cycle runs.
    overview = next(
        s for s in report.sections if s.title == "Panorama · Session Overview"
    )
    assert not any(
        c.name.startswith("runs.attempts.") for c in overview.checks
    )


# ── v1.4.5 e2e: real session smokes ──────────────────────────────────────


REAL_SESSION_E37 = "e37602da-ce25-45c6-97d9-2cffa237d1ba"
REAL_HIDDEN_RID_PREFIX = "7b06f3d9"  # the runId whose attempt-1 was hidden


def _real_session_present(sid: str) -> bool:
    """The session.jsonl can have a normal name OR a .reset.* sibling
    (after a /reset). Either is enough for sessions.resolve to find it.
    """
    base = REAL_SESSIONS_DIR
    if not base.is_dir():
        return False
    if (base / f"{sid}.jsonl").is_file():
        return True
    # Check for any .reset.* variant
    return any(
        p.name.startswith(f"{sid}.jsonl")
        for p in base.glob(f"{sid}.jsonl*")
    )


@pytest.mark.skipif(
    not _real_session_present(REAL_SESSION_E37),
    reason="real session e37602da fixture not present on this host",
)
def test_e2e_real_session_e37_hidden_failure_surfaces():
    """v1.4.5 e2e: against the real e37602da session (run 7b06f3d9 had
    attempt-1 idle-time-out + attempt-2 success), --all-runs must surface
    the previously-hidden retry as a retried_after_failure signal.
    """
    cfg = REAL_HOME / "config.json"
    ctx = DiagContext(
        openclaw_home=REAL_HOME,
        config_path=cfg if cfg.is_file() else REAL_HOME / "openclaw.json",
        log_dir=REAL_LOG_DIR,
        sessions_base=REAL_HOME / "agents",
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(REAL_HOME)
    paths_mod.CRON_RUNS_DIR = str(REAL_HOME / "cron" / "runs")
    report = _run_panorama(
        ctx, session_id=REAL_SESSION_E37, all_runs=True,
    )
    assert report.error is None, f"unexpected error: {report.error}"

    # The targeted runId must show up in run_attempts with attempt_count==2.
    matches = [
        rd for rd in report.data["run_attempts"]
        if (rd.get("runId") or "").startswith(REAL_HIDDEN_RID_PREFIX)
    ]
    assert matches, (
        "runId 7b06f3d9 not found in run_attempts; got "
        + str([rd.get("runId") for rd in report.data["run_attempts"]])
    )
    rd = matches[0]
    assert rd["attempt_count"] >= 2, (
        f"expected at least 2 attempts on 7b06f3d9; got {rd}"
    )
    assert rd["had_failed_attempt"] is True

    # A retried_after_failure signal for that runId must be present.
    sigs = report.data["health_signals"]
    retry_sigs = [
        s for s in sigs
        if s.get("kind") == "retried_after_failure"
        and (s.get("runId") or "").startswith(REAL_HIDDEN_RID_PREFIX)
    ]
    assert retry_sigs, (
        "retried_after_failure signal for 7b06f3d9 missing; "
        f"signals seen: {[s.get('kind') for s in sigs]}"
    )
    rs = retry_sigs[0]
    # The first attempt was idle-timed-out, so the rendered chain must
    # include that classification.
    chain = " | ".join(rs["per_attempt"])
    assert "went idle" in chain, (
        f"idle-timeout classification missing from chain: {chain}"
    )


@pytest.mark.skipif(
    not _real_session_present(REAL_SESSION_ID),
    reason="real single-cycle session 63d70b29 not present on this host",
)
def test_e2e_real_session_63d_no_regression():
    """v1.4.5 e2e: 63d70b29 is single-cycle. Ensure no
    retried_after_failure signals fire and run_attempts shows count==1
    for every run."""
    cfg = REAL_HOME / "config.json"
    ctx = DiagContext(
        openclaw_home=REAL_HOME,
        config_path=cfg if cfg.is_file() else REAL_HOME / "openclaw.json",
        log_dir=REAL_LOG_DIR,
        sessions_base=REAL_HOME / "agents",
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(REAL_HOME)
    paths_mod.CRON_RUNS_DIR = str(REAL_HOME / "cron" / "runs")
    report = _run_panorama(
        ctx, session_id=REAL_SESSION_ID, all_runs=True,
    )
    assert report.error is None, f"unexpected error: {report.error}"
    for rd in report.data["run_attempts"]:
        assert rd["attempt_count"] == 1, (
            f"unexpected multi-attempt on 63d70b29 run "
            f"{rd.get('runId')}: {rd}"
        )
    sigs = report.data["health_signals"]
    assert not any(s.get("kind") == "retried_after_failure" for s in sigs), (
        "retried_after_failure unexpectedly fired on single-cycle session"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
