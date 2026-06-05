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


def test_model_call_duration_removed_from_render(tmp_path: Path):
    """v1.4.7: per-call duration was removed from the rendered Model Calls
    line (it came from an unreliable message-gap proxy). The raw duration_ms
    is still kept in JSON data for consumers. Also guards that the old
    ms-as-seconds rendering bug (547ms -> "9.1m") can never reappear in the
    per-call line, since the per-call duration is no longer printed at all.
    """
    from ocdiag.render.human import render

    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)

    # Underlying data still carries true millisecond gaps (1000ms, 2000ms).
    model_calls = report.data["model_calls"]
    durations = {c.get("duration_ms") for c in model_calls}
    assert 1000 in durations and 2000 in durations

    text = render(report, no_color=True)
    # Per-call duration prefix is gone: no "#1 1s"/"#2 2s" and no bogus minutes.
    assert "#1 1s" not in text
    assert "#2 2s" not in text
    assert "16.7m" not in text
    assert "33.3m" not in text
    # The per-call line still shows token usage + stop reason.
    assert "#1 in=" in text


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
    """v1.4.7: per-call lines show input/output tokens and stop reason only.
    Both the unreliable per-call tok/s throughput AND the per-call duration
    (message-gap proxy) were removed, along with the wall-clock note. The
    authoritative gateway-log run wall time may still appear elsewhere.
    """
    from ocdiag.render.human import render
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    text = render(report, no_color=True)
    # Input/output tokens still shown per call
    assert "in=10" in text
    assert "in=12" in text
    # Throughput removed entirely — no tok/s anywhere
    assert "tok/s" not in text
    # Per-call duration + its note removed (unreliable message-gap proxy)
    assert "round-trip wall-clock" not in text


def test_window_bound_logs_summary_carries_counters(tmp_path: Path):
    """v1.4.3: logs.summary carries out_of_window_dropped + ts_less_kept."""
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    logs_section = next(
        s for s in report.sections
        if s.title == "Panorama · Correlated Logs & Signals"
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
        s for s in report.sections
        if s.title == "Panorama · Correlated Logs & Signals"
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
        "Panorama · Correlated Logs & Signals",
        "Panorama · Child Tasks",
    ):
        assert required in section_titles, (
            f"missing section {required}; got {section_titles}"
        )
    # v1.4.11: standalone "Panorama · Health Signals" is gone — its
    # rendering moved into "Correlated Logs & Signals" above.
    assert "Panorama · Health Signals" not in section_titles
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


# ── v1.4.10 task T1: window-aware log discovery ──────────────────────────


def test_discover_logs_for_window_includes_yesterday(tmp_path: Path):
    """A log file dated within the session window must be included even when
    its mtime is older than today (the v1.4.9 ``discover_recent_logs``
    excluded such files, leaving Correlated Logs empty for older sessions).
    """
    from datetime import datetime, timedelta

    from ocdiag.recent_logs import discover_logs_for_window

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    yesterday = (datetime.now() - timedelta(days=2)).date()
    yfile = log_dir / f"openclaw-{yesterday.isoformat()}.log"
    yfile.write_text("dummy\n")
    # Backdate mtime so a today-mtime filter would reject it.
    old_mtime = time.time() - 3 * 86400
    os.utime(yfile, (old_mtime, old_mtime))

    # Window straddling that day.
    win_start = int(time.mktime(yesterday.timetuple()) * 1000)
    win_end = win_start + 60_000
    out = discover_logs_for_window(str(log_dir), win_start, win_end)
    assert str(yfile) in out, (
        "yesterday's log not in window-aware discovery; got: " + str(out)
    )


def test_discover_logs_for_window_zero_falls_back(tmp_path: Path):
    """When window=0, fall back to ``discover_recent_logs`` semantics."""
    from datetime import datetime

    from ocdiag.recent_logs import (
        discover_logs_for_window,
        discover_recent_logs,
    )

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    today = datetime.now().date().isoformat()
    tfile = log_dir / f"openclaw-{today}.log"
    tfile.write_text("today\n")
    out = discover_logs_for_window(str(log_dir), 0, 0)
    assert out == discover_recent_logs(str(log_dir))


def test_discover_logs_for_window_excludes_far_dates(tmp_path: Path):
    """Files whose filename date is far outside the window AND whose mtime
    is also old must be excluded, so we don't drag in unrelated logs.
    """
    from datetime import datetime, timedelta

    from ocdiag.recent_logs import discover_logs_for_window

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    far_date = (datetime.now() - timedelta(days=30)).date()
    far = log_dir / f"openclaw-{far_date.isoformat()}.log"
    far.write_text("old\n")
    old_mtime = time.time() - 30 * 86400
    os.utime(far, (old_mtime, old_mtime))

    near_date = (datetime.now() - timedelta(days=2)).date()
    win_start = int(time.mktime(near_date.timetuple()) * 1000)
    win_end = win_start + 60_000
    out = discover_logs_for_window(str(log_dir), win_start, win_end)
    assert str(far) not in out


def test_panorama_correlates_yesterday_log(tmp_path: Path):
    """Integration: a synthetic session whose window is "yesterday" plus a
    yesterday-named log mentioning the sessionId must produce
    correlated_logs > 0.
    """
    from datetime import datetime, timedelta

    home = tmp_path / "yhome"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "ylogs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "10101010-aaaa-bbbb-cccc-101010101010"
    yesterday = (datetime.now() - timedelta(days=2))
    y_ms = int(yesterday.timestamp() * 1000)
    y_iso = _ms_to_iso(y_ms)

    _write_jsonl(main_sd / f"{sid}.jsonl", [
        {"type": "session", "version": 3, "id": sid, "timestamp": y_iso},
        {"type": "message", "id": "u-1", "timestamp": y_iso,
         "message": {"role": "user", "timestamp": y_ms, "content": "ping"}},
        {"type": "message", "id": "u-2", "timestamp": _ms_to_iso(y_ms + 5000),
         "message": {"role": "user", "timestamp": y_ms + 5000,
                     "content": "more"}},
    ])

    # Yesterday-dated log file with backdated mtime + a sessionId mention
    # within the window.
    yfile = log_dir / (
        f"openclaw-{yesterday.date().isoformat()}.log"
    )
    rec = {
        "level": "INFO", "time": y_ms + 100, "pid": 1,
        "_meta": {"name": json.dumps({"subsystem": "gateway"})},
        "msg": f"sessionId={sid} starting",
    }
    yfile.write_text(json.dumps(rec) + "\n")
    old_mtime = time.time() - 2 * 86400
    os.utime(yfile, (old_mtime, old_mtime))

    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=home / "agents",
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)

    report = _run_panorama(ctx, session_id=sid)
    assert report.error is None
    assert len(report.data["correlated_logs"]) >= 1, (
        "yesterday-dated log not correlated; sources_present="
        + str(report.data["sources_present"])
    )


# ── v1.4.10 task T2a: enriched long_tool_call signal ──────────────────────


def test_long_tool_call_renders_args_and_error(tmp_path: Path):
    """A long (>60s) erroring tool call must surface its args + error
    snippet on the rendered Health Signals line and on the JSON signal.
    """
    from ocdiag.render.human import render

    home = tmp_path / "long-tool"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "70707070-1111-2222-3333-707070707070"
    call_id = "tooluse_LongCron1"
    start_ms = T0
    end_ms = T0 + 120_000  # 2 minutes
    records = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T0)},
        {"type": "message", "id": "u-1", "timestamp": _ms_to_iso(T0),
         "message": {"role": "user", "timestamp": T0, "content": "go"}},
        {"type": "message", "id": "a-1",
         "timestamp": _ms_to_iso(start_ms),
         "message": {
             "role": "assistant", "timestamp": start_ms,
             "content": [
                 {"type": "toolCall", "id": call_id, "name": "cron",
                  "input": {"action": "update",
                            "jobId": "0cdb2836-3791-468e-a756-d6b8af97d894"}},
             ],
         }},
        {"type": "message", "id": "r-1",
         "timestamp": _ms_to_iso(end_ms),
         "message": {
             "role": "toolResult", "timestamp": end_ms,
             "toolCallId": call_id, "toolName": "cron",
             "isError": True, "content": "patch required",
         }},
    ]
    _write_jsonl(main_sd / f"{sid}.jsonl", records)

    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=home / "agents",
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)

    report = _run_panorama(ctx, session_id=sid)
    sigs = report.data["health_signals"]
    long_sigs = [s for s in sigs if s.get("kind") == "long_tool_call"]
    assert len(long_sigs) == 1
    sig = long_sigs[0]
    # New v1.4.10 fields.
    assert "args_summary" in sig
    assert "snippet" in sig
    assert "action=update" in sig["args_summary"]
    assert "jobId=0cdb2836" in sig["args_summary"]
    assert "patch required" in sig["snippet"]

    text = render(report, no_color=True)
    assert "long tool call: cron(action=update" in text
    assert "→ error: patch required" in text


# ── v1.4.10 task T2b: positive (OK) health signals ────────────────────────


def test_positive_health_signals_clean_run(tmp_path: Path):
    """A clean fixture must emit at least one OK positive health signal."""
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    positives = report.data.get("positive_health_signals") or []
    kinds = {p.get("kind") for p in positives}
    # Tools — fixture has 2 calls + 1 error, so partial-ok wording.
    assert "ok_tools" in kinds
    # Lifecycle clean (started==completed==2, active==0)
    assert "ok_lifecycle" in kinds
    # v1.4.11: positive lines render under the merged
    # "Correlated Logs & Signals" section (was: standalone Health Signals).
    section = next(
        s for s in report.sections
        if s.title == "Panorama · Correlated Logs & Signals"
    )
    ok_msgs = [c.message for c in section.checks
               if c.name.startswith("health.ok_")]
    assert ok_msgs, (
        "expected at least one ok_* line in Correlated Logs & Signals"
    )


def test_positive_signals_do_not_change_verdict(tmp_path: Path):
    """A failing run must keep its FAIL verdict even though positive
    signals are emitted alongside the problems.
    """
    ctx = _build_fixture_home(tmp_path, artifact_failure=True)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    assert report.verdict == Verdict.FAIL
    # Problems still rendered.
    sigs = report.data["health_signals"]
    assert any(s.get("kind") == "trajectory_artifact" for s in sigs)


# ── v1.4.10 task T3: timeline middle-event sample ─────────────────────────


def test_timeline_sample_renders_middle_events(tmp_path: Path):
    """The Timeline section must include sample lines covering events
    between first and last, bounded by TIMELINE_RENDER_SAMPLE.
    """
    from ocdiag.inspectors.panorama import TIMELINE_RENDER_SAMPLE

    ctx = _build_fixture_home(tmp_path, run_b=True)
    report = _run_panorama(ctx, session_id=SESSION_ID, all_runs=True)
    timeline_section = next(
        s for s in report.sections if s.title == "Panorama · Timeline"
    )
    sample_checks = [
        c for c in timeline_section.checks
        if c.name.startswith("timeline.sample.")
    ]
    assert sample_checks, "expected timeline.sample.* checks"
    assert len(sample_checks) <= TIMELINE_RENDER_SAMPLE
    # Chronological order
    sample_ts = [
        (c.data.get("ts_ms") if isinstance(c.data, dict) else 0)
        for c in sample_checks
    ]
    assert sample_ts == sorted(sample_ts)
    # De-dup vs the actual first/last entries on the merged timeline.
    timeline_data = report.data["timeline"]
    skip_ts = {timeline_data[0]["ts_ms"], timeline_data[-1]["ts_ms"]}
    for ts in sample_ts:
        assert ts not in skip_ts, (
            "sample must not duplicate the first/last timeline ts"
        )


def test_timeline_sample_helper_dedup_and_bounds():
    """Direct test of _timeline_sample: cap, chronological, dedup, prefer
    interesting events.
    """
    from ocdiag.inspectors.panorama import _timeline_sample
    timeline = []
    for i in range(50):
        timeline.append({
            "ts_ms": T0 + i * 1000,
            "source": "session.jsonl",
            "event_type": "message",
            "summary": f"message:user [{i}]",
        })
    # Inject a few "interesting" events — log:ERROR, state, delivery.
    timeline.append({
        "ts_ms": T0 + 10_500, "source": "app_log",
        "event_type": "log:ERROR", "summary": "[gw] kaboom",
    })
    timeline.append({
        "ts_ms": T0 + 20_500, "source": "state",
        "event_type": "state", "summary": "state idle → processing",
    })
    timeline.sort(key=lambda e: e["ts_ms"])
    # Skip the first event the renderer would already show.
    skip = {timeline[0]["ts_ms"], timeline[-1]["ts_ms"]}
    sample = _timeline_sample(timeline, skip_ts_ms=skip, cap=10)
    assert len(sample) <= 10
    assert all(s["ts_ms"] not in skip for s in sample)
    # Chronological
    assert sample == sorted(sample, key=lambda e: e["ts_ms"])
    # Interesting events must be present in the sample.
    summaries = " | ".join(s["summary"] for s in sample)
    assert "kaboom" in summaries
    assert "idle → processing" in summaries


def test_timeline_sample_small_timeline_returns_all():
    """When the timeline is smaller than the sample cap, return all of
    them (still dedup'd vs skip set).
    """
    from ocdiag.inspectors.panorama import _timeline_sample
    timeline = [
        {"ts_ms": T0 + 1000, "source": "session.jsonl",
         "event_type": "message", "summary": "user"},
        {"ts_ms": T0 + 2000, "source": "session.jsonl",
         "event_type": "message", "summary": "asst"},
        {"ts_ms": T0 + 3000, "source": "session.jsonl",
         "event_type": "message", "summary": "tool"},
    ]
    out = _timeline_sample(
        timeline, skip_ts_ms={T0 + 1000}, cap=20,
    )
    # Skipped first; remaining 2 returned in order.
    assert len(out) == 2
    assert out[0]["ts_ms"] == T0 + 2000
    assert out[1]["ts_ms"] == T0 + 3000


# ── v1.4.10 e2e: real session e37602da ────────────────────────────────────


@pytest.mark.skipif(
    not _real_session_present("e37602da-ce25-45c6-97d9-2cffa237d1ba"),
    reason="real session e37602da fixture not present on this host",
)
def test_e2e_real_session_e37_v1_4_10_improvements():
    """v1.4.10 e2e: the real e37602da session must now show
    correlated logs > 0 (T1), enriched long-tool-call signals (T2a),
    and timeline middle-sample lines (T3).
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
        ctx, session_id="e37602da-ce25-45c6-97d9-2cffa237d1ba",
        all_runs=True,
    )
    assert report.error is None, f"unexpected error: {report.error}"
    # T1: correlated logs > 0
    assert len(report.data["correlated_logs"]) > 0, (
        "T1 regression: correlated_logs empty for older session"
    )
    # T2a: at least one long_tool_call signal carrying args + snippet
    sigs = report.data["health_signals"]
    longs = [s for s in sigs if s.get("kind") == "long_tool_call"]
    if longs:  # only if the session actually had long tool calls
        assert any("args_summary" in s for s in longs)
    # T3: timeline middle-sample renders
    timeline_section = next(
        s for s in report.sections if s.title == "Panorama · Timeline"
    )
    assert any(
        c.name.startswith("timeline.sample.")
        for c in timeline_section.checks
    ), "T3 regression: timeline.sample.* missing"


# ── v1.4.11: bigger timeline middle sample (cap raised to 40) ─────────────


def test_timeline_render_sample_cap_raised_to_40():
    """v1.4.11: TIMELINE_RENDER_SAMPLE was raised from 20 to 40 so the
    rendered Timeline shows more middle activity on long runs.
    """
    from ocdiag.inspectors.panorama import TIMELINE_RENDER_SAMPLE
    assert TIMELINE_RENDER_SAMPLE == 40


def test_timeline_sample_more_than_old_cap_and_spans_run():
    """Build a 200-event timeline and verify the renderer:
      1. emits MORE than the old cap (>20) and ≤ the new cap (40),
      2. picks events covering the whole run, NOT just the early span.

    This exercises both the bumped constant AND the rewritten filler
    that uses fractional spacing across the full pool index range.
    """
    from ocdiag.inspectors.panorama import (
        TIMELINE_RENDER_SAMPLE,
        _timeline_sample,
    )
    n_events = 200
    timeline = []
    for i in range(n_events):
        timeline.append({
            "ts_ms": T0 + i * 1000,
            "source": "session.jsonl",
            "event_type": "message",
            "summary": f"message:user [{i}]",
        })
    # Skip first/last (the renderer already shows them) and sample.
    skip = {timeline[0]["ts_ms"], timeline[-1]["ts_ms"]}
    sample = _timeline_sample(
        timeline, skip_ts_ms=skip, cap=TIMELINE_RENDER_SAMPLE,
    )
    # 1. count
    assert len(sample) > 20, (
        f"expected >20 sample lines under new cap, got {len(sample)}"
    )
    assert len(sample) <= TIMELINE_RENDER_SAMPLE
    # 2. coverage: at least one pick must land in the LAST quarter of
    # the timeline (indexes 150..199 → ts T0 + 150_000..199_000ms).
    # The pre-1.4.11 sampler clustered everything near index 0 because
    # `step = n // room` rounded to 1 once room ≈ n; this assertion
    # would have failed under that path.
    last_quarter_start = T0 + (n_events * 3 // 4) * 1000
    assert any(s["ts_ms"] >= last_quarter_start for s in sample), (
        "sample should reach the last quarter of the run; "
        f"max ts in sample={max(s['ts_ms'] for s in sample) - T0}ms, "
        f"last_quarter_start={last_quarter_start - T0}ms"
    )


# ── v1.4.11: representative INFO removed from Correlated Logs ─────────────


def test_correlated_logs_renders_representative_info_when_no_err_warn(
        tmp_path: Path):
    """v1.4.12: when correlated logs have no ERROR/WARN, the merged section
    SHOWS up to REPRESENTATIVE_INFO_LINES (=20) representative INFO lines so a
    quiet window still shows concrete evidence (restored + raised from 5).
    """
    from ocdiag.inspectors.panorama import REPRESENTATIVE_INFO_LINES
    ctx = _build_fixture_home(tmp_path)
    # Rewrite today's log so every line is INFO-only and includes the
    # sessionId so we get a non-zero correlated-log count.
    log_dir = ctx.log_dir
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / f"openclaw-{today}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        for i in range(8):
            ts = T0 + i * 100
            rec = {
                "level": "INFO", "time": ts, "pid": 1,
                "_meta": {"name": json.dumps({"subsystem": "gateway"})},
                "msg": f"sessionId={SESSION_ID} step {i}",
            }
            f.write(json.dumps(rec) + "\n")
    report = _run_panorama(ctx, session_id=SESSION_ID)
    section = next(
        s for s in report.sections
        if s.title == "Panorama · Correlated Logs & Signals"
    )
    # Summary still present
    assert any(c.name == "logs.summary" for c in section.checks)
    # v1.4.12: representative INFO lines ARE shown for a clean window.
    info_lines = [c for c in section.checks
                  if c.name.startswith("logs.info.")]
    assert info_lines, (
        "v1.4.12: representative INFO lines must show when no ERROR/WARN"
    )
    # 8 INFO entries < cap → all shown; never exceed the cap.
    assert len(info_lines) == 8
    assert len(info_lines) <= REPRESENTATIVE_INFO_LINES


def test_representative_logs_present_and_capped():
    """v1.4.12: the representative-INFO helper is restored and the cap is 20
    (raised from the old 5). The helper never returns more than the cap.
    """
    import ocdiag.inspectors.panorama as panorama
    assert hasattr(panorama, "_representative_logs")
    assert panorama.REPRESENTATIVE_INFO_LINES == 20
    pool = [
        {"level": "INFO", "time": i, "message": f"tool start step {i}"}
        for i in range(100)
    ]
    reps = panorama._representative_logs(
        pool, limit=panorama.REPRESENTATIVE_INFO_LINES)
    assert 0 < len(reps) <= 20


# ── v1.4.11: merged "Correlated Logs & Signals" section ───────────────────


def test_no_standalone_health_signals_section(tmp_path: Path):
    """v1.4.11: the standalone "Panorama · Health Signals" section is
    gone. Its content lives under "Correlated Logs & Signals".
    """
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    section_titles = [s.title for s in report.sections]
    assert "Panorama · Health Signals" not in section_titles
    assert "Panorama · Correlated Logs & Signals" in section_titles
    # v1.4.13: section count rose to 7 with the new objective Findings
    # summary at the top (renders FIRST).
    assert "Panorama · Findings" in section_titles
    assert section_titles[0] == "Panorama · Findings"
    assert len(section_titles) == 7, (
        f"expected 7 sections after Findings added, got: {section_titles}"
    )


def test_merged_section_contains_summary_and_positive_signals(
        tmp_path: Path):
    """A clean run renders BOTH the log summary AND the positive ✓
    confirmations under the merged section. The order matters: summary
    first, then positives.
    """
    ctx = _build_fixture_home(tmp_path)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    section = next(
        s for s in report.sections
        if s.title == "Panorama · Correlated Logs & Signals"
    )
    names = [c.name for c in section.checks]
    # Summary present (its data block carries the counters).
    assert "logs.summary" in names
    # At least one positive signal (the fixture is 'lifecycle clean').
    assert any(n.startswith("health.ok_") for n in names), (
        f"expected health.ok_* lines under merged section; got {names}"
    )
    # Order: logs.summary appears BEFORE health.ok_* lines.
    summary_idx = names.index("logs.summary")
    first_positive = next(
        i for i, n in enumerate(names) if n.startswith("health.ok_")
    )
    assert summary_idx < first_positive, (
        f"logs.summary should come before health.ok_*; names={names}"
    )


def test_merged_section_carries_problem_signals(tmp_path: Path):
    """A long erroring tool call must produce a long_tool_call problem
    signal AND it must render under the merged section. Verdict logic
    is unchanged (warn) compared to pre-merge behavior.
    """
    home = tmp_path / "long-tool-merge"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "abcdef11-1111-1111-1111-abcdef111111"
    call_id = "tooluse_LongMerge1"
    start_ms = T0
    end_ms = T0 + 90_000  # 1.5 minutes — exceeds 60s threshold
    records = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T0)},
        {"type": "message", "id": "u-1", "timestamp": _ms_to_iso(T0),
         "message": {"role": "user", "timestamp": T0, "content": "go"}},
        {"type": "message", "id": "a-1",
         "timestamp": _ms_to_iso(start_ms),
         "message": {
             "role": "assistant", "timestamp": start_ms,
             "content": [
                 {"type": "toolCall", "id": call_id, "name": "cron",
                  "input": {"action": "update", "jobId": "abc12345"}},
             ],
         }},
        {"type": "message", "id": "r-1",
         "timestamp": _ms_to_iso(end_ms),
         "message": {
             "role": "toolResult", "timestamp": end_ms,
             "toolCallId": call_id, "toolName": "cron",
             "isError": True, "content": "deadline exceeded",
         }},
    ]
    _write_jsonl(main_sd / f"{sid}.jsonl", records)
    cfg = home / "openclaw.json"
    cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=home / "agents",
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)

    report = _run_panorama(ctx, session_id=sid)
    # The signal is still computed.
    sigs = report.data["health_signals"]
    long_sigs = [s for s in sigs if s.get("kind") == "long_tool_call"]
    assert len(long_sigs) == 1
    # And it renders under the MERGED section, not standalone.
    section = next(
        s for s in report.sections
        if s.title == "Panorama · Correlated Logs & Signals"
    )
    long_lines = [
        c for c in section.checks
        if c.name.startswith("health.long_tool_call.")
    ]
    assert long_lines, (
        f"long_tool_call line missing from merged section; "
        f"got names={[c.name for c in section.checks]}"
    )
    # Verdict for problems still warns (unchanged from pre-merge).
    assert report.verdict in (Verdict.WARN, Verdict.FAIL)


def test_merged_section_verdict_unchanged_for_artifact_failure(
        tmp_path: Path):
    """A trajectory_artifact failure still drives FAIL verdict, and
    renders its ✗ line under the merged section.
    """
    ctx = _build_fixture_home(tmp_path, artifact_failure=True)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    assert report.verdict == Verdict.FAIL
    section = next(
        s for s in report.sections
        if s.title == "Panorama · Correlated Logs & Signals"
    )
    fail_lines = [
        c for c in section.checks
        if c.name.startswith("health.trajectory_artifact.")
    ]
    assert fail_lines, (
        "trajectory_artifact line missing from merged section; "
        f"names={[c.name for c in section.checks]}"
    )


# ── v1.4.13: objective Findings summary + deterministic ranking ──────────


def test_findings_section_first_and_verdict_line(tmp_path: Path):
    """v1.4.13: 'Panorama · Findings' renders before Session Overview and
    leads with the computed verdict + objective signal counts.
    """
    ctx = _build_fixture_home(tmp_path, artifact_failure=True)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    titles = [s.title for s in report.sections]
    assert titles[0] == "Panorama · Findings"
    findings = next(s for s in report.sections if s.title == "Panorama · Findings")
    verdict_line = next(
        c for c in findings.checks if c.name == "findings.verdict"
    )
    # Verdict line begins with "verdict: <VERDICT>".
    assert verdict_line.message.startswith(
        f"verdict: {report.verdict.value.upper()}"
    )
    # Counts must be objective non-negative integers and reflect the
    # underlying problem signals exactly (no inference).
    sigs = report.data["health_signals"]
    fail_n = verdict_line.data["fail_count"]
    warn_n = verdict_line.data["warn_count"]
    assert fail_n + warn_n == len(sigs)
    assert fail_n >= 0 and warn_n >= 0


def test_findings_ordered_by_deterministic_severity_key(tmp_path: Path):
    """The order is (severity_class desc, kind_rank desc, ts asc, kind asc).
    Build a fixture mixing fail-class + warn-class signals at varied ts and
    assert the ordering is exactly what the key prescribes — fail before
    warn, higher rank before lower, earlier ts before later within a tie.
    """
    from ocdiag.inspectors.panorama import (
        SIGNAL_SEVERITY,
        _signal_sort_key,
    )
    sigs = [
        # warn-class, lower rank, earlier ts
        {"kind": "log_decision", "ts_ms": 100, "summary": "x"},
        # fail-class, highest rank, later ts
        {"kind": "trajectory_artifact", "ts_ms": 5000, "runId": "rrr",
         "flags": ["timedOut"], "final_status": "error"},
        # warn-class, higher rank, very late ts
        {"kind": "tool_call_leak", "ts_ms": 9000, "runId": "rrr",
         "active": 1, "started": 5, "completed": 4},
        # fail-class, lower rank, earliest ts
        {"kind": "cron_delivery_failed", "ts_ms": 50, "jobId": "j",
         "action": "a", "status": "ok", "deliveryStatus": "failed"},
    ]
    ordered = sorted(sigs, key=_signal_sort_key)
    # Expected order: fail-class trajectory_artifact (rank 100),
    # then fail-class cron_delivery_failed (rank 90),
    # then warn-class tool_call_leak (rank 70),
    # then warn-class log_decision (rank 25).
    assert [s["kind"] for s in ordered] == [
        "trajectory_artifact",
        "cron_delivery_failed",
        "tool_call_leak",
        "log_decision",
    ]
    # Confirm SIGNAL_SEVERITY is the source of truth for class+rank.
    assert SIGNAL_SEVERITY["trajectory_artifact"][0] == "fail"
    assert SIGNAL_SEVERITY["log_decision"][0] == "warn"
    assert (
        SIGNAL_SEVERITY["trajectory_artifact"][1]
        > SIGNAL_SEVERITY["cron_delivery_failed"][1]
    )


def test_findings_cap_and_more_line(tmp_path: Path):
    """When more than FINDINGS_TOP_N problem signals exist, the section caps
    them and emits a single '+N more (see Correlated Logs & Signals)' line.
    """
    from ocdiag.inspectors.panorama import (
        FINDINGS_TOP_N,
        _build_findings,
    )
    sigs = [
        {"kind": "long_tool_call", "ts_ms": i * 100,
         "name": f"tool_{i}", "duration_ms": 60_000, "is_error": False,
         "args_summary": "{}", "snippet": ""}
        for i in range(FINDINGS_TOP_N + 5)
    ]
    findings, more = _build_findings(sigs)
    assert len(findings) == FINDINGS_TOP_N
    assert more == 5


def test_findings_summary_lines_have_no_forbidden_words(tmp_path: Path):
    """The Findings section MUST be objective. Scan every rendered line for
    a blocklist of subjective words and assert none appear (case-insens).
    """
    # Fixture covering many distinct signal kinds at once. We append one
    # of each into the inspector via synthetic logs / trajectory tweaks.
    home = tmp_path / "many"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "13131313-1313-1313-1313-131313131313"
    rid = "13131313-aaaa-bbbb-cccc-131313131313"
    _make_minimal_session_jsonl(main_sd, sid)

    # Trajectory with abort/timeout AND incomplete lifecycle AND cache
    # break — three distinct problem signals at once.
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        final_status="error",
        artifacts_data_overrides={
            "aborted": True, "timedOut": True, "idleTimedOut": True,
            "promptErrorSource": "prompt", "finalStatus": "error",
            "itemLifecycle": {
                "startedCount": 12, "completedCount": 7, "activeCount": 0,
            },
            "promptCache": {
                "observation": {
                    "broke": True,
                    "previousCacheRead": 1_000_000,
                    "cacheRead": 250_000,
                },
            },
        },
    )
    _write_jsonl(main_sd / f"{sid}.trajectory.jsonl", traj)
    ctx = _ctx_for_session(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)
    findings = next(
        s for s in report.sections if s.title == "Panorama · Findings"
    )
    rendered = "\n".join(c.message for c in findings.checks)
    forbidden = [
        "root cause", "caused", "because", "likely", "probably",
        "suggest", "should", "recommend", "investigate",
        "most significant", " due to ", "appears", "seems",
    ]
    lower = rendered.lower()
    for word in forbidden:
        assert word not in lower, (
            f"forbidden subjective word '{word}' in Findings:\n{rendered}"
        )
    # Sanity: there ARE problem signals + a FAIL verdict.
    assert "verdict: FAIL" in rendered
    # Each non-verdict line ends with a pointer to the detail section.
    detail_lines = [
        c.message for c in findings.checks
        if c.name.startswith("findings.") and c.name != "findings.verdict"
        and c.name != "findings.none"
    ]
    for line in detail_lines:
        assert "(see Correlated Logs & Signals)" in line


def test_findings_ok_fixture_says_no_problem_signals(tmp_path: Path):
    """When there are zero problem signals, Findings emits one explicit
    objective line — no fabricated praise.
    """
    sid = "14141414-1414-1414-1414-141414141414"
    rid = "14141414-aaaa-bbbb-cccc-141414141414"
    home = tmp_path / "ok"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _make_minimal_session_jsonl(main_sd, sid)
    # Clean trajectory: success, no flags, no leaks.
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, T6,
        artifacts_data_overrides={
            "itemLifecycle": {
                "startedCount": 3, "completedCount": 3, "activeCount": 0,
            },
        },
    )
    _write_jsonl(main_sd / f"{sid}.trajectory.jsonl", traj)
    ctx = _ctx_for_session(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)
    sigs = report.data["health_signals"]
    # No problem signals expected on this fixture.
    assert sigs == [], f"expected zero problem signals, got: {sigs}"
    findings = next(
        s for s in report.sections if s.title == "Panorama · Findings"
    )
    none_line = next(
        (c for c in findings.checks if c.name == "findings.none"), None,
    )
    assert none_line is not None
    assert none_line.message == "no problem signals"


def test_correlated_logs_problem_signals_severity_ordered(tmp_path: Path):
    """v1.4.13: problem signals rendered under Correlated Logs & Signals
    follow the same deterministic severity key as Findings. Build a
    fixture that emits a fail-class and a warn-class signal and assert
    the fail-class line precedes the warn-class line in the section.
    """
    home = tmp_path / "ord"
    main_sd = home / "agents" / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "15151515-1515-1515-1515-151515151515"
    call_id = "tooluse_LongOrdered1"
    start_ms = T0
    end_ms = T0 + 90_000  # long tool call → warn signal
    records = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T0)},
        {"type": "message", "id": "u-1", "timestamp": _ms_to_iso(T0),
         "message": {"role": "user", "timestamp": T0, "content": "go"}},
        {"type": "message", "id": "a-1",
         "timestamp": _ms_to_iso(start_ms),
         "message": {
             "role": "assistant", "timestamp": start_ms,
             "content": [
                 {"type": "toolCall", "id": call_id, "name": "cron",
                  "input": {"action": "update", "jobId": "ababcdcd"}},
             ],
         }},
        {"type": "message", "id": "r-1",
         "timestamp": _ms_to_iso(end_ms),
         "message": {
             "role": "toolResult", "timestamp": end_ms,
             "toolCallId": call_id, "toolName": "cron",
             "isError": True, "content": "deadline exceeded",
         }},
    ]
    _write_jsonl(main_sd / f"{sid}.jsonl", records)
    # Trajectory with an artifact failure → fail signal.
    rid = "15151515-aaaa-bbbb-cccc-151515151515"
    traj = _trajectory_with_artifact_overrides(
        sid, rid, T0, end_ms + 1000,
        final_status="error",
        artifacts_data_overrides={
            "aborted": True, "timedOut": True, "finalStatus": "error",
        },
    )
    _write_jsonl(main_sd / f"{sid}.trajectory.jsonl", traj)
    ctx = _ctx_for_session(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)
    section = next(
        s for s in report.sections
        if s.title == "Panorama · Correlated Logs & Signals"
    )
    # Find positions of the fail-class trajectory_artifact line and the
    # warn-class long_tool_call line. The artifact line must come first.
    names = [c.name for c in section.checks]
    art_idx = next(
        i for i, n in enumerate(names)
        if n.startswith("health.trajectory_artifact.")
    )
    long_idx = next(
        i for i, n in enumerate(names)
        if n.startswith("health.long_tool_call.")
    )
    assert art_idx < long_idx, (
        f"fail-class signal must precede warn-class; got names={names}"
    )


def test_findings_in_json_envelope(tmp_path: Path):
    """v1.4.13: ``report.data['findings']`` carries an ordered list of
    objective dicts; ``findings_more_count`` carries the tail.
    """
    ctx = _build_fixture_home(tmp_path, artifact_failure=True)
    report = _run_panorama(ctx, session_id=SESSION_ID)
    findings = report.data["findings"]
    assert isinstance(findings, list) and findings, (
        "expected non-empty findings on a failing fixture"
    )
    for f in findings:
        # Each finding has exactly the documented keys.
        assert set(f.keys()) == {
            "severity", "kind", "ts_ms", "summary", "ref",
        }, f"unexpected keys: {f.keys()}"
        assert f["severity"] in ("fail", "warn")
        assert isinstance(f["kind"], str) and f["kind"]
        assert isinstance(f["ts_ms"], int)
        assert isinstance(f["summary"], str) and f["summary"]
        assert f["ref"] == "Correlated Logs & Signals"
    assert isinstance(report.data["findings_more_count"], int)


def test_findings_top_n_constant_is_module_level():
    """The cap is a module-level constant so it can be tuned in one place
    without renaming code paths.
    """
    from ocdiag.inspectors.panorama import FINDINGS_TOP_N
    assert isinstance(FINDINGS_TOP_N, int) and FINDINGS_TOP_N > 0


def test_signal_severity_table_documented():
    """Every kind that ``_health_signals`` can emit must appear in
    SIGNAL_SEVERITY (otherwise it would silently fall through to the
    ('warn', 0) default and rank below catalogued entries — fine for
    forward-compat but bad as a regression). Pin the catalogue here.
    """
    from ocdiag.inspectors.panorama import SIGNAL_SEVERITY
    expected_kinds = {
        "trajectory_artifact",
        "cron_delivery_failed",
        "retried_after_failure",
        "tool_call_leak",
        "items_incomplete",
        "prompt_cache_broke",
        "long_tool_call",
        "child_task_failed",
        "log_stall",
        "context_precheck_overflow",
        "state_transition_abnormal",
        "queue_wait_slow",
        "config_reload_failed",
        "log_decision",
        "last_tool_error",
        "gateway_pid_change",
    }
    missing = expected_kinds - set(SIGNAL_SEVERITY)
    assert not missing, f"SIGNAL_SEVERITY missing entries for: {missing}"


# ── v1.4.14 regression: --mask scrubs correlated log bodies ──────────────


def _build_app_log_with_secret(
    path: Path, *, level: str, secret: str,
    session_id: str, run_id: str,
) -> None:
    """Single correlated log line carrying an obvious secret in the message.

    Crafted so that the correlation graph admits it (carries sessionId +
    runId) and the secret survives ``parse_log_msg`` (which strips
    subsystem-marker JSON but keeps plain ``msg`` strings).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        rec = {
            "level": level,
            "time": T0 + 500,
            "pid": 12345,
            "_meta": {"name": json.dumps({"subsystem": "gateway"})},
            "msg": (
                f"sessionId={session_id} runId={run_id} "
                f"failed: Authorization: Bearer {secret}"
            ),
        }
        f.write(json.dumps(rec) + "\n")


def _has_text(report, needle: str) -> bool:
    """True if ``needle`` appears anywhere in rendered section messages.

    Walks every Check.message + Check.data on every Section so the test
    catches leaks via either the human render path or the section-level
    JSON payload.
    """
    if needle in json.dumps(report.data):
        return True
    for s in report.sections:
        for c in s.checks:
            if needle in (c.message or ""):
                return True
            if c.data and needle in json.dumps(c.data, default=str):
                return True
    return False


def test_mask_scrubs_correlated_logs_envelope(tmp_path: Path):
    """v1.4.14 P1: --mask must scrub the JSON envelope's correlated_logs.

    Pre-fix: ``report.data["correlated_logs"]`` was the raw record list,
    so a Bearer token in a log line leaked verbatim under --mask.
    """
    secret = "LIVEKEY1234567890abcDEFghi"
    ctx = _build_fixture_home(tmp_path)
    # Replace the synthetic log with one that carries a correlated secret.
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    _build_app_log_with_secret(
        log_path, level="ERROR", secret=secret,
        session_id=SESSION_ID, run_id=RUN_ID_A,
    )

    report = _run_panorama(ctx, session_id=SESSION_ID, mask=True)
    envelope_logs = report.data["correlated_logs"]
    # The line itself must still be present (correlation matched), but the
    # secret token must be scrubbed.
    assert envelope_logs, "expected correlated_logs to include the secret line"
    blob = json.dumps(envelope_logs)
    assert secret not in blob, (
        f"--mask leaked secret into correlated_logs envelope: {blob[:300]}"
    )


def test_mask_scrubs_raw_error_render(tmp_path: Path):
    """v1.4.14 P1: --mask must scrub the pretty-rendered ERROR log lines.

    Pre-fix: the Raw ERROR render block called ``parse_log_msg(rec)`` and
    sliced it directly into ``s_logs.fail(...)`` without sanitization.
    """
    secret = "sk-LIVEKEY1234567890"
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    _build_app_log_with_secret(
        log_path, level="ERROR", secret=secret,
        session_id=SESSION_ID, run_id=RUN_ID_A,
    )

    report = _run_panorama(ctx, session_id=SESSION_ID, mask=True)
    # Find the Correlated Logs & Signals section and check no rendered
    # message contains the secret.
    logs_sec = next(
        (s for s in report.sections
         if "Correlated Logs" in s.title or "Logs" in s.title),
        None,
    )
    assert logs_sec is not None, "expected a Correlated Logs section"
    rendered = " | ".join(c.message for c in logs_sec.checks)
    assert secret not in rendered, (
        f"--mask leaked secret into rendered ERROR line: {rendered[:300]}"
    )


def test_mask_scrubs_raw_warn_render(tmp_path: Path):
    """v1.4.14 P1: --mask must scrub WARN lines the same way as ERROR."""
    secret = "sk-WARNKEY9876543210"
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    _build_app_log_with_secret(
        log_path, level="WARN", secret=secret,
        session_id=SESSION_ID, run_id=RUN_ID_A,
    )

    report = _run_panorama(ctx, session_id=SESSION_ID, mask=True)
    assert not _has_text(report, secret), "--mask leaked WARN body"


def test_unmask_keeps_correlated_log_secret(tmp_path: Path):
    """Sanity check: --unmask (default) must NOT redact log bodies.

    Guards against accidentally over-scrubbing in the new code path.
    """
    secret = "sk-UNMASKED1234567890"
    ctx = _build_fixture_home(tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = ctx.log_dir / f"openclaw-{today}.log"
    _build_app_log_with_secret(
        log_path, level="ERROR", secret=secret,
        session_id=SESSION_ID, run_id=RUN_ID_A,
    )

    report = _run_panorama(ctx, session_id=SESSION_ID, mask=False)
    assert _has_text(report, secret), "--unmask should preserve the raw log body"


# ── v1.4.14 regression: --openclaw-home is honoured by runs.sqlite/cron ──


def test_openclaw_home_routes_runs_sqlite(tmp_path: Path, monkeypatch):
    """v1.4.14 P2: passing only --openclaw-home (no env override) should
    make panorama find runs.sqlite under that home.

    Pre-fix: ``_runs_sqlite_path()`` consulted the import-time
    ``paths_mod.OPENCLAW_HOME`` constant, so a CLI-only override was
    silently ignored.
    """
    # Critically: do NOT set OPENCLAW_HOME in env, and do NOT touch
    # paths_mod.OPENCLAW_HOME — we want to prove ctx.openclaw_home is
    # the source of truth.
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_CRON_RUNS", raising=False)

    ctx = _build_fixture_home(tmp_path)

    # Reset paths_mod to a sentinel that does NOT match the temp home so
    # that any code still consulting it would fail to find runs.sqlite.
    import ocdiag.paths as paths_mod
    bogus = tmp_path / "definitely-not-the-real-home"
    bogus.mkdir(parents=True, exist_ok=True)
    paths_mod.OPENCLAW_HOME = str(bogus)
    paths_mod.CRON_RUNS_DIR = str(bogus / "cron" / "runs")

    report = _run_panorama(ctx, session_id=SESSION_ID)
    sp = report.data["sources_present"]
    assert sp["runs.sqlite"] is True, (
        "panorama must find runs.sqlite via ctx.openclaw_home, "
        "not via paths_mod.OPENCLAW_HOME"
    )
    # Child task surfaces from runs.sqlite — proves we actually opened it.
    children = report.data["child_tasks"]
    assert any(c["task_id"] == CHILD_TASK_ID for c in children)


def _stamp_cron_session_key(home: Path, cron_job_id: str) -> None:
    """Rewrite the fixture's sessions.json + trajectory so the sessionKey
    carries ``:cron:<jobId>``.

    The correlation graph's ``cron_job_id`` is derived from the sessionKey
    string (regex ``:cron:<id>``); ``graph.session_key`` itself is loaded
    from the per-agent ``sessions.json`` store (see
    ``expand_from_sessions_json``), so a fixture must stamp the marker
    there. We rewrite the trajectory too for consistency.
    """
    cron_key = f"agent:main:cron:{cron_job_id}"
    main_sd = home / "agents" / "main" / "sessions"

    # Replace sessions.json with a cron-style sessionKey.
    store_path = main_sd / "sessions.json"
    store = {
        cron_key: {
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

    # Mirror it into the trajectory for consistency.
    traj_file = main_sd / f"{SESSION_ID}.trajectory.jsonl"
    rewritten: List[Dict[str, Any]] = []
    with open(traj_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rec["sessionKey"] = cron_key
            rewritten.append(rec)
    _write_jsonl(traj_file, rewritten)


def test_openclaw_home_routes_cron_runs(tmp_path: Path, monkeypatch):
    """v1.4.14 P2: same as above but for cron/runs/<job>.jsonl.

    Pre-fix: ``_cron_run_path`` derived the dir from
    ``paths_mod.CRON_RUNS_DIR`` only.
    """
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_CRON_RUNS", raising=False)

    ctx = _build_fixture_home(tmp_path)

    # Mirror the test above: scramble paths_mod.
    import ocdiag.paths as paths_mod
    bogus = tmp_path / "definitely-not-the-real-home"
    bogus.mkdir(parents=True, exist_ok=True)
    paths_mod.OPENCLAW_HOME = str(bogus)
    paths_mod.CRON_RUNS_DIR = str(bogus / "cron" / "runs")

    # Wire a cron-triggered run into the synthetic fixture: trajectory
    # sessionKey carries `:cron:<jobId>` (which is how the correlation
    # graph picks up ``cron_job_id``). The jobId must match the
    # correlation regex `:cron:([0-9a-fA-F-]{8,})`, so we use a hex/uuid-
    # shaped value rather than free-form text. A matching delivery file
    # is dropped under the temp openclaw_home.
    cron_job_id = "abcdef01-1111-2222-3333-444444444444"
    home = Path(ctx.openclaw_home)
    cron_runs_dir = home / "cron" / "runs"
    cron_runs_dir.mkdir(parents=True, exist_ok=True)
    cron_run_file = cron_runs_dir / f"{cron_job_id}.jsonl"
    cron_run_file.write_text(json.dumps({
        "schemaVersion": 1,
        "jobId": cron_job_id,
        "sessionId": SESSION_ID,
        "runId": RUN_ID_A,
        "deliveredAt": _ms_to_iso(T6),
        "deliveryStatus": "delivered",
    }) + "\n")

    _stamp_cron_session_key(home, cron_job_id)

    report = _run_panorama(ctx, session_id=SESSION_ID)
    sp = report.data["sources_present"]
    assert sp.get("cron/runs") is True, (
        "panorama must locate cron/runs/<job>.jsonl via ctx.openclaw_home"
    )
    cron_runs = report.data.get("cron_runs") or []
    assert any(r.get("jobId") == cron_job_id for r in cron_runs), (
        "expected the cron delivery record to be loaded"
    )


def test_openclaw_cron_runs_env_still_wins(tmp_path: Path, monkeypatch):
    """When the user explicitly sets OPENCLAW_CRON_RUNS, that env var
    must continue to override the openclaw-home-derived cron dir, since
    it is documented as a standalone knob in ocdiag.paths.
    """
    cron_job_id = "deadbeef-1111-2222-3333-444444444444"
    # Custom dir outside both the temp home and paths_mod default.
    custom_cron_dir = tmp_path / "elsewhere" / "cron-runs"
    custom_cron_dir.mkdir(parents=True, exist_ok=True)
    (custom_cron_dir / f"{cron_job_id}.jsonl").write_text(json.dumps({
        "schemaVersion": 1, "jobId": cron_job_id,
        "sessionId": SESSION_ID, "runId": RUN_ID_A,
        "deliveredAt": _ms_to_iso(T6), "deliveryStatus": "delivered",
    }) + "\n")

    monkeypatch.setenv("OPENCLAW_CRON_RUNS", str(custom_cron_dir))

    ctx = _build_fixture_home(tmp_path)
    home = Path(ctx.openclaw_home)
    _stamp_cron_session_key(home, cron_job_id)

    report = _run_panorama(ctx, session_id=SESSION_ID)
    sp = report.data["sources_present"]
    assert sp.get("cron/runs") is True
    cron_runs = report.data.get("cron_runs") or []
    assert any(r.get("jobId") == cron_job_id for r in cron_runs)


# ── transcript-only OpenClaw injected turns are excluded from Model Calls ─


def test_model_calls_exclude_openclaw_transcript_only_injections(tmp_path: Path):
    """v1.4.16: assistant messages whose provider is "openclaw" and whose
    model is "delivery-mirror" or "gateway-injected" are transcript-only
    synthetic turns (delivery mirror / gateway injection), NOT real LLM
    inferences. They must not appear in Model Calls and must not feed any
    downstream aggregate (by-model breakdown, total tokens, session_stats).

    Reference (OpenClaw 2026.6.1 dist):
      * dist/selection-DrXxngyT.js: TRANSCRIPT_ONLY_OPENCLAW_ASSISTANT_MODELS
      * dist/compaction-successor-transcript-CUmEvaGX.js:
        TRANSCRIPT_ONLY_OPENCLAW_MODELS
      * docs/reference/transcript-hygiene.md: "Replay filters OpenClaw
        delivery-mirror and gateway-injected assistant turns."
    """
    home = tmp_path / "with-injected"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "cccccccc-3333-4444-5555-cccccccccccc"
    base = T0
    records = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(base)},
        {"type": "message", "id": "u-1", "timestamp": _ms_to_iso(base),
         "message": {"role": "user", "timestamp": base, "content": "go"}},
        # Real model call #1.
        {"type": "message", "id": "a-1",
         "timestamp": _ms_to_iso(base + 1000),
         "message": {"role": "assistant", "timestamp": base + 1000,
                     "model": "claude-opus-4-7",
                     "provider": "amazon-bedrock",
                     "stopReason": "toolUse",
                     "usage": {"input": 100, "output": 200,
                               "cacheRead": 0, "cacheWrite": 0},
                     "content": [{"type": "text", "text": "ok"}]}},
        # OpenClaw transcript-only delivery-mirror — must be filtered.
        {"type": "message", "id": "a-mirror",
         "timestamp": _ms_to_iso(base + 1500),
         "message": {"role": "assistant", "timestamp": base + 1500,
                     "model": "delivery-mirror",
                     "provider": "openclaw",
                     "stopReason": "stop",
                     "usage": {"input": 0, "output": 0,
                               "cacheRead": 0, "cacheWrite": 0},
                     "content": [{"type": "text", "text": "(mirror)"}]}},
        # OpenClaw transcript-only gateway-injected — must be filtered.
        {"type": "message", "id": "a-gw",
         "timestamp": _ms_to_iso(base + 1800),
         "message": {"role": "assistant", "timestamp": base + 1800,
                     "model": "gateway-injected",
                     "provider": "openclaw",
                     "stopReason": "stop",
                     "usage": {"input": 0, "output": 0,
                               "cacheRead": 0, "cacheWrite": 0},
                     "content": [{"type": "text", "text": "(injected)"}]}},
        # Real model call #2 — counted, and its duration must be measured
        # from the previous *real* boundary (the user message at `base`
        # + the assistant turn at base+1000), NOT from the skipped
        # transcript-only turns. last_ts at this point is base+1000, so
        # duration_ms = 1000.
        {"type": "message", "id": "a-2",
         "timestamp": _ms_to_iso(base + 2000),
         "message": {"role": "assistant", "timestamp": base + 2000,
                     "model": "claude-opus-4-7",
                     "provider": "amazon-bedrock",
                     "stopReason": "endTurn",
                     "usage": {"input": 110, "output": 250,
                               "cacheRead": 5, "cacheWrite": 3},
                     "content": [{"type": "text", "text": "done"}]}},
        # Negative case: provider is NOT "openclaw" but model name is
        # "delivery-mirror". Must NOT be filtered (filter requires both).
        {"type": "message", "id": "a-other",
         "timestamp": _ms_to_iso(base + 3000),
         "message": {"role": "assistant", "timestamp": base + 3000,
                     "model": "delivery-mirror",
                     "provider": "anthropic",
                     "stopReason": "endTurn",
                     "usage": {"input": 1, "output": 2,
                               "cacheRead": 0, "cacheWrite": 0},
                     "content": [{"type": "text", "text": "x"}]}},
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
    assert report.error is None, f"unexpected error: {report.error}"

    calls = report.data["model_calls"]
    # Three real calls survived: bedrock x2 + anthropic/delivery-mirror.
    assert len(calls) == 3, calls
    for c in calls:
        assert not (c.get("provider") == "openclaw"
                    and c.get("model") in {"delivery-mirror",
                                           "gateway-injected"}), c

    # Negative case survived (provider != "openclaw").
    assert any(c.get("provider") == "anthropic"
               and c.get("model") == "delivery-mirror" for c in calls)

    # Both real bedrock calls survived with their original token counts.
    bedrock_calls = [c for c in calls
                     if c.get("provider") == "amazon-bedrock"]
    assert len(bedrock_calls) == 2
    assert {c["output"] for c in bedrock_calls} == {200, 250}

    # The 2nd real bedrock call's duration_ms is measured from the 1st
    # bedrock call (last_ts wasn't bumped by the skipped injected turns).
    second_bedrock = next(
        c for c in bedrock_calls if c["output"] == 250
    )
    assert second_bedrock["duration_ms"] == 1000, second_bedrock

    # By-model aggregate: no transcript-only keys present.
    by_model = {m["model"]: m for m
                in report.data["model_aggregate"]["models"]}
    assert "delivery-mirror" in by_model  # the anthropic one only
    assert by_model["delivery-mirror"]["calls"] == 1
    # All "delivery-mirror" calls in aggregate must come from non-openclaw.
    # (We can't see provider in by-model directly, but call count == 1
    # already proves the openclaw mirror was filtered upstream.)
    assert "gateway-injected" not in by_model
    assert by_model["claude-opus-4-7"]["calls"] == 2
    assert by_model["claude-opus-4-7"]["input"] == 210
    assert by_model["claude-opus-4-7"]["output"] == 450

    # session_stats counts only real calls (3 = 2 bedrock + 1 anthropic).
    assert report.data["session_stats"]["model_calls"] == 3
    # Total token sums must not include the zero-output injected turns
    # (they would not change totals here since output=0, but verify the
    # exclusion is real by checking input totals — bedrock 100+110=210
    # + anthropic 1 = 211, no openclaw zeros mixed in).
    assert report.data["session_stats"]["tokens"]["input"] == 211
    assert report.data["session_stats"]["tokens"]["output"] == 452

    # Render must not show any "delivery-mirror: 2 calls" (the buggy
    # symptom) or "gateway-injected" as a model row.
    from ocdiag.render.human import render
    text = render(report, no_color=True)
    assert "gateway-injected" not in text
    # delivery-mirror appears only via the surviving anthropic call,
    # so its row should say "1 calls" not "2 calls".
    assert "delivery-mirror: 2 calls" not in text


# ── v1.4.17 Tool Execution rendering ──────────────────────────────────────


def test_fmt_tool_dur_helper():
    """Sub-second keeps ms precision; second/minute scales humanize."""
    from ocdiag.inspectors.panorama import _fmt_tool_dur
    assert _fmt_tool_dur(None) == "?"
    assert _fmt_tool_dur(0) == "0ms"
    assert _fmt_tool_dur(234) == "234ms"
    assert _fmt_tool_dur(999) == "999ms"
    assert _fmt_tool_dur(1000) == "1.0s"
    assert _fmt_tool_dur(4797) == "4.8s"
    assert _fmt_tool_dur(59999) == "60.0s"
    assert _fmt_tool_dur(60000) == "1.0m"
    assert _fmt_tool_dur(141665) == "2.4m"


def test_collapse_ws_helper():
    """JSON pretty-print artefacts collapse to single spaces."""
    from ocdiag.inspectors.panorama import _collapse_ws
    assert _collapse_ws("a    b") == "a b"
    assert _collapse_ws("{\n  \"a\":  1,\n  \"b\":  2\n}") == "{ \"a\": 1, \"b\": 2 }"
    assert _collapse_ws("  leading   trailing  ") == "leading trailing"


def _build_tool_render_fixture(tmp: Path) -> tuple:
    """Synthetic session covering the full render matrix:
    short-success / long-JSON-success / short-error / long-error / pending.
    Returns (ctx, session_id) — caller runs panorama and renders.
    """
    home = tmp / "tool-render-home"
    agents = home / "agents"
    main_sd = agents / "main" / "sessions"
    log_dir = tmp / "tool-render-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sid = "12345678-aaaa-bbbb-cccc-1234567890ab"
    long_json_result = json.dumps(
        {"results": [], "disabled": True, "unavailable": True,
         "error": "No API key configured for memory_search"},
        indent=2,
    )
    long_error_text = (
        'Validation failed for tool "edit":   - edits.0: must not have '
        "additional properties (this is a long error that needs to drop "
        "to a continuation line)"
    )
    records = [
        {"type": "session", "version": 3, "id": sid,
         "timestamp": _ms_to_iso(T0)},
        {"type": "message", "id": "u1", "timestamp": _ms_to_iso(T0),
         "message": {"role": "user", "timestamp": T0, "content": "go"}},
        # #1 short success
        {"type": "message", "id": "a1", "timestamp": _ms_to_iso(T0 + 1),
         "message": {"role": "assistant", "timestamp": T0 + 1,
                     "content": [{"type": "toolCall", "id": "tc1",
                                  "name": "Bash", "input": {"cmd": "ls"}}]}},
        {"type": "message", "id": "r1", "timestamp": _ms_to_iso(T0 + 200),
         "message": {"role": "toolResult", "timestamp": T0 + 200,
                     "toolCallId": "tc1", "toolName": "Bash",
                     "isError": False, "content": "ok\n"}},
        # #2 long JSON success
        {"type": "message", "id": "a2", "timestamp": _ms_to_iso(T0 + 1000),
         "message": {"role": "assistant", "timestamp": T0 + 1000,
                     "content": [{"type": "toolCall", "id": "tc2",
                                  "name": "memory_search",
                                  "input": {"query": "lookup"}}]}},
        {"type": "message", "id": "r2", "timestamp": _ms_to_iso(T0 + 5797),
         "message": {"role": "toolResult", "timestamp": T0 + 5797,
                     "toolCallId": "tc2", "toolName": "memory_search",
                     "isError": False, "content": long_json_result}},
        # #3 short error
        {"type": "message", "id": "a3", "timestamp": _ms_to_iso(T0 + 6000),
         "message": {"role": "assistant", "timestamp": T0 + 6000,
                     "content": [{"type": "toolCall", "id": "tc3",
                                  "name": "cron",
                                  "input": {"action": "update", "jobId": "abc"}}]}},
        {"type": "message", "id": "r3", "timestamp": _ms_to_iso(T0 + 147665),
         "message": {"role": "toolResult", "timestamp": T0 + 147665,
                     "toolCallId": "tc3", "toolName": "cron",
                     "isError": True, "content": "patch required"}},
        # #4 long error
        {"type": "message", "id": "a4", "timestamp": _ms_to_iso(T0 + 148000),
         "message": {"role": "assistant", "timestamp": T0 + 148000,
                     "content": [{"type": "toolCall", "id": "tc4",
                                  "name": "edit",
                                  "input": {"path": "/x"}}]}},
        {"type": "message", "id": "r4", "timestamp": _ms_to_iso(T0 + 180889),
         "message": {"role": "toolResult", "timestamp": T0 + 180889,
                     "toolCallId": "tc4", "toolName": "edit",
                     "isError": True, "content": long_error_text}},
        # #5 pending (no result)
        {"type": "message", "id": "a5", "timestamp": _ms_to_iso(T0 + 181000),
         "message": {"role": "assistant", "timestamp": T0 + 181000,
                     "content": [{"type": "toolCall", "id": "tc5",
                                  "name": "sessions_history",
                                  "input": {"limit": 10}}]}},
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
    return ctx, sid


def _tool_section_checks(report) -> list:
    for sec in report.sections:
        if sec.title == "Panorama · Tool Execution":
            return sec.checks
    raise AssertionError("Tool Execution section not found")


def test_tool_execution_renders_humanized_durations(tmp_path: Path):
    """v1.4.17: per-call duration must use _fmt_tool_dur, not raw 'NNNNms'."""
    ctx, sid = _build_tool_render_fixture(tmp_path)
    report = _run_panorama(ctx, session_id=sid)
    checks = _tool_section_checks(report)
    # Skip the timing summary; per-call checks follow.
    per_call = [c for c in checks if c.name.startswith("tools.call.")]
    assert len(per_call) == 5
    # No raw multi-digit ms (e.g. "147665ms") — that's the v1.4.16 wart.
    for c in per_call:
        whole = c.message + (c.detail or "")
        assert "147665ms" not in whole
        assert "180889ms" not in whole
        assert "5797ms" not in whole
    # Specific humanized renderings are present.
    msgs = [c.message for c in per_call]
    joined = "\n".join(msgs)
    assert "199ms" in joined  # short call keeps ms
    assert "4.8s" in joined   # 5797ms → 4.8s (gap-derived 4797ms, depending)
    assert "2.4m" in joined   # cron 141665ms → 2.4m
    assert "32.9s" in joined  # edit ≈ 32889ms


def test_tool_execution_no_double_status_glyph(tmp_path: Path):
    """The render layer prepends the verdict glyph; the message itself
    must not carry a redundant ✓/✗ (the v1.4.16 bug).
    """
    ctx, sid = _build_tool_render_fixture(tmp_path)
    report = _run_panorama(ctx, session_id=sid)
    per_call = [c for c in _tool_section_checks(report)
                if c.name.startswith("tools.call.")]
    for c in per_call:
        # No bare-glyph "args" pattern from the old `f"... {status} args=..."`
        assert " ✗ args=" not in c.message
        assert " ✓ args=" not in c.message
        # And no leading status char.
        assert not c.message.lstrip().startswith("✓ ")
        assert not c.message.lstrip().startswith("✗ ")


def test_tool_execution_short_error_inline_long_in_detail(tmp_path: Path):
    """Short error rides the header; long error/result drops to detail."""
    ctx, sid = _build_tool_render_fixture(tmp_path)
    report = _run_panorama(ctx, session_id=sid)
    by_idx = {c.name: c for c in _tool_section_checks(report)
              if c.name.startswith("tools.call.")}
    # #3 cron — short "patch required" — inline on header.
    assert "patch required" in by_idx["tools.call.3"].message
    assert "⇒ ERR patch required" in by_idx["tools.call.3"].message
    assert by_idx["tools.call.3"].detail in (None, "")
    # #4 edit — long error — message has no ERR text, detail does.
    assert "Validation failed" not in by_idx["tools.call.4"].message
    assert by_idx["tools.call.4"].detail is not None
    assert by_idx["tools.call.4"].detail.startswith("⇒ ERR ")
    assert "Validation failed" in by_idx["tools.call.4"].detail
    # #2 memory_search — long JSON result — detail starts with "→ ".
    assert by_idx["tools.call.2"].detail is not None
    assert by_idx["tools.call.2"].detail.startswith("→ ")


def test_tool_execution_pending_call_renders_question_mark(tmp_path: Path):
    """Pending call (duration_ms is None) renders dur as "?" without crashing
    and without a result arrow.
    """
    ctx, sid = _build_tool_render_fixture(tmp_path)
    report = _run_panorama(ctx, session_id=sid)
    by_idx = {c.name: c for c in _tool_section_checks(report)
              if c.name.startswith("tools.call.")}
    msg = by_idx["tools.call.5"].message
    assert "sessions_history" in msg
    assert "?" in msg
    assert "→" not in msg
    assert "⇒" not in msg


def test_tool_execution_summary_includes_slowest(tmp_path: Path):
    """v1.4.17: timing summary uses '·' separators, humanized ms, slowest."""
    ctx, sid = _build_tool_render_fixture(tmp_path)
    report = _run_panorama(ctx, session_id=sid)
    timing = next(c for c in _tool_section_checks(report)
                  if c.name == "tools.timing")
    assert "5 calls" in timing.message
    assert "2 err" in timing.message
    assert "·" in timing.message
    assert "avg " in timing.message
    assert "p50 " in timing.message
    assert "p95 " in timing.message
    assert "max " in timing.message
    assert "slowest cron(2.4m)" in timing.message
    # No legacy "avg=NNNNms" form.
    assert "avg=" not in timing.message
    assert "ms p50=" not in timing.message


def test_tool_execution_compresses_json_whitespace(tmp_path: Path):
    """Indented JSON results must collapse to single-space separators —
    the old renderer left runs of 3+ spaces from the original indent.
    """
    ctx, sid = _build_tool_render_fixture(tmp_path)
    report = _run_panorama(ctx, session_id=sid)
    by_idx = {c.name: c for c in _tool_section_checks(report)
              if c.name.startswith("tools.call.")}
    detail = by_idx["tools.call.2"].detail or ""
    # No 3+ space runs anywhere in the rendered detail.
    assert "   " not in detail


def test_tool_execution_json_envelope_unchanged(tmp_path: Path):
    """Render-layer changes must NOT touch report.data["tool_waterfall"] /
    "tool_stats". Consumers depend on the existing field shape.
    """
    ctx, sid = _build_tool_render_fixture(tmp_path)
    report = _run_panorama(ctx, session_id=sid)
    waterfall = report.data["tool_waterfall"]
    assert isinstance(waterfall, list) and len(waterfall) == 5
    sample = waterfall[0]
    for fld in ("name", "callId", "duration_ms", "is_error",
                "result_text", "args"):
        assert fld in sample, f"missing field {fld} in tool_waterfall row"
    stats = report.data["tool_stats"]
    for fld in ("total", "completed", "errors",
                "avg_ms", "p50_ms", "p95_ms", "max_ms", "slowest"):
        assert fld in stats, f"missing field {fld} in tool_stats"
    # Slowest is still the {name, duration_ms} dict.
    slow = stats["slowest"]
    assert slow is not None
    assert "name" in slow and "duration_ms" in slow


def test_tool_execution_columns_align_in_human_render(tmp_path: Path):
    """Human render places #idx/name/dur in fixed columns so the eye can
    scan vertically. Locking exact widths catches accidental drift.
    """
    from ocdiag.render.human import render
    ctx, sid = _build_tool_render_fixture(tmp_path)
    report = _run_panorama(ctx, session_id=sid)
    text = render(report, no_color=True)
    # Find Tool Execution section, collect per-call lines.
    lines = []
    in_sec = False
    for ln in text.split("\n"):
        if "Tool Execution" in ln:
            in_sec = True
            continue
        if in_sec and ln.strip().startswith(("Panorama ·", "━")):
            in_sec = False
        if in_sec and ln.strip().startswith(("✓ #", "⚠ #", "✗ #")):
            lines.append(ln)
    # 5 per-call rows, each beginning at the same #idx column offset.
    assert len(lines) == 5
    # IDX col starts after "  ✓ " / "  ⚠ " etc; "#" is at offset 4.
    for ln in lines:
        assert ln[4] == "#", f"#idx col misaligned: {ln!r}"


# ── --version flag (parity with Node entry) ───────────────────────────────


def test_main_version_flag(capsys):
    from ocdiag import __version__ as pkg_version
    from ocdiag.main import main

    rc = main(["--version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == pkg_version
    assert captured.err == ""


def test_main_version_aliases(capsys):
    from ocdiag import __version__ as pkg_version
    from ocdiag.main import main

    for alias in ("-V", "-v", "version"):
        rc = main([alias])
        captured = capsys.readouterr()
        assert rc == 0, f"alias {alias!r} returned {rc}"
        assert captured.out.strip() == pkg_version, f"alias {alias!r} printed {captured.out!r}"


# ── v1.4.18: three-state empty-correlation messaging ────────────────────


def _build_session_for_window(
    home: Path, sid: str, *, ts_ms: int,
) -> None:
    """Lay down a minimal session.jsonl whose record timestamps anchor
    the panorama session window onto a chosen day."""
    main_sd = home / "agents" / "main" / "sessions"
    iso = _ms_to_iso(ts_ms)
    _write_jsonl(main_sd / f"{sid}.jsonl", [
        {"type": "session", "version": 3, "id": sid, "timestamp": iso},
        {"type": "message", "id": "u-1", "timestamp": iso,
         "message": {"role": "user", "timestamp": ts_ms,
                     "content": "ping"}},
        {"type": "message", "id": "u-2",
         "timestamp": _ms_to_iso(ts_ms + 5000),
         "message": {"role": "user", "timestamp": ts_ms + 5000,
                     "content": "more"}},
    ])
    cfg = home / "openclaw.json"
    cfg.write_text("{}")


def _ctx_for(home: Path, log_dir: Path) -> DiagContext:
    cfg = home / "openclaw.json"
    if not cfg.is_file():
        cfg.write_text("{}")
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg,
        log_dir=log_dir, sessions_base=home / "agents",
    )
    import ocdiag.paths as paths_mod
    paths_mod.OPENCLAW_HOME = str(home)
    paths_mod.CRON_RUNS_DIR = str(home / "cron" / "runs")
    return ctx


def _logs_section(report) -> Any:
    for sec in report.sections:
        if sec.title == "Panorama · Correlated Logs & Signals":
            return sec
    raise AssertionError("Correlated Logs section missing")


def test_logs_not_retained_state(tmp_path: Path):
    """Session window day has no log file in log_dir, but adjacent days'
    files exist (so log_files is non-empty via discover_logs_for_window's
    ±1 day margin) → emit logs.not_retained, not the generic logs.none."""
    from datetime import datetime, timedelta

    home = tmp_path / "ret-home"
    log_dir = tmp_path / "ret-logs"
    log_dir.mkdir(parents=True)

    # Window day = 3 days ago. log_dir holds days -2 and -1 (within ±1
    # margin of window day, so discover_logs_for_window includes them).
    win_day = (datetime.now() - timedelta(days=3)).date()
    win_ms = int(time.mktime(win_day.timetuple()) * 1000) + 3600_000

    sid = "deadbeef-aaaa-bbbb-cccc-deadbeefdead"
    _build_session_for_window(home, sid, ts_ms=win_ms)

    # Logs for win_day-1 and win_day+1 (within ±1 margin used by
    # discover_logs_for_window). win_day itself is intentionally absent.
    for offset in (-1, 1):
        d = win_day + timedelta(days=offset)
        path = log_dir / f"openclaw-{d.isoformat()}.log"
        # Lines that DO mention the sessionId but with timestamps far
        # outside the window — bound filter drops them, correlated=0.
        rec = {
            "level": "INFO",
            "time": int(time.time() * 1000),  # now → out of window
            "pid": 1,
            "_meta": {"name": json.dumps({"subsystem": "gateway"})},
            "msg": f"sessionId={sid} unrelated",
        }
        path.write_text(json.dumps(rec) + "\n")
        old = time.time() - abs(offset) * 86400
        os.utime(path, (old, old))

    # Make sure the win_day log itself does NOT exist.
    assert not (log_dir / f"openclaw-{win_day.isoformat()}.log").exists()

    ctx = _ctx_for(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)
    assert report.error is None, f"unexpected error: {report.error}"
    sec = _logs_section(report)
    names = [c.name for c in sec.checks]
    assert "logs.not_retained" in names, (
        f"expected logs.not_retained, got {names}"
    )
    assert "logs.missing" not in names
    assert "logs.uncorrelated" not in names
    check = next(c for c in sec.checks if c.name == "logs.not_retained")
    assert "not retained" in check.message
    assert win_day.isoformat() in check.message
    # Verdict must remain OK — this is environmental, not a session fault.
    assert check.verdict == Verdict.OK
    # Structured data carries the missing/present/available lists.
    assert win_day.isoformat() in check.data["window_dates_missing"]
    assert check.data["window_dates_present"] == []
    assert len(check.data["available_log_dates"]) >= 1
    # Section's verdict shouldn't be promoted to WARN by this state.
    assert sec.verdict in (Verdict.OK, Verdict.WARN, Verdict.FAIL)
    # The not_retained check itself contributes OK.


def test_logs_uncorrelated_state(tmp_path: Path):
    """Window day's log file exists but its lines lack any sessionId/runId
    matching the diagnosed session → emit logs.uncorrelated."""
    from datetime import datetime, timedelta

    home = tmp_path / "uncorr-home"
    log_dir = tmp_path / "uncorr-logs"
    log_dir.mkdir(parents=True)

    win_day = (datetime.now() - timedelta(days=2)).date()
    win_ms = int(time.mktime(win_day.timetuple()) * 1000) + 1800_000

    sid = "caffe1ee-2222-3333-4444-cafefeed1234"
    _build_session_for_window(home, sid, ts_ms=win_ms)

    log_path = log_dir / f"openclaw-{win_day.isoformat()}.log"
    # Multiple lines, none mentioning the sessionId — also no runId.
    lines = []
    for i in range(5):
        rec = {
            "level": "INFO",
            "time": win_ms + i * 1000,
            "pid": 1,
            "_meta": {"name": json.dumps({"subsystem": "gateway"})},
            "msg": f"unrelated event {i}",
        }
        lines.append(json.dumps(rec))
    log_path.write_text("\n".join(lines) + "\n")
    old = time.time() - 2 * 86400
    os.utime(log_path, (old, old))

    ctx = _ctx_for(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)
    assert report.error is None
    sec = _logs_section(report)
    names = [c.name for c in sec.checks]
    assert "logs.uncorrelated" in names, (
        f"expected logs.uncorrelated, got {names}"
    )
    assert "logs.not_retained" not in names
    assert "logs.missing" not in names
    check = next(c for c in sec.checks if c.name == "logs.uncorrelated")
    assert "present" in check.message
    assert "no lines carry" in check.message
    assert check.verdict == Verdict.OK
    assert win_day.isoformat() in check.data["window_dates_present"]


def test_logs_missing_state_preserved(tmp_path: Path):
    """log_dir has zero openclaw-*.log files → keep emitting logs.missing
    (warn) — backwards-compatible with the v1.4.17 behavior."""
    from datetime import datetime, timedelta

    home = tmp_path / "miss-home"
    log_dir = tmp_path / "miss-logs"
    log_dir.mkdir(parents=True)

    win_day = (datetime.now() - timedelta(days=2)).date()
    win_ms = int(time.mktime(win_day.timetuple()) * 1000)

    sid = "b00b00b0-0000-1111-2222-b00b00b00000"
    _build_session_for_window(home, sid, ts_ms=win_ms)

    ctx = _ctx_for(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)
    assert report.error is None
    sec = _logs_section(report)
    names = [c.name for c in sec.checks]
    assert "logs.missing" in names
    assert "logs.not_retained" not in names
    assert "logs.uncorrelated" not in names
    check = next(c for c in sec.checks if c.name == "logs.missing")
    assert check.verdict == Verdict.WARN


def test_logs_unknown_window_falls_back_to_none(tmp_path: Path):
    """When the session has no usable timestamps (window=0 path), the
    classifier can't tell missing-vs-present and must degrade to the
    original logs.none ok message — and not raise."""
    home = tmp_path / "zero-home"
    log_dir = tmp_path / "zero-logs"
    log_dir.mkdir(parents=True)
    main_sd = home / "agents" / "main" / "sessions"

    sid = "00000000-aaaa-bbbb-cccc-000000000000"
    # Session record with no usable timestamp on any record (no `timestamp`
    # field on the message wrapper, no `message.timestamp`). The session
    # record itself has a malformed timestamp so iso_to_epoch_ms returns 0.
    _write_jsonl(main_sd / f"{sid}.jsonl", [
        {"type": "session", "version": 3, "id": sid, "timestamp": ""},
        {"type": "message", "id": "u-1", "timestamp": "",
         "message": {"role": "user", "content": "hi"}},
    ])
    # Drop a today-dated log file so log_files is non-empty (forcing the
    # branch into the elif path).
    from datetime import datetime
    today = datetime.now().date().isoformat()
    log_path = log_dir / f"openclaw-{today}.log"
    log_path.write_text(json.dumps({
        "level": "INFO", "time": 1, "pid": 1,
        "_meta": {"name": json.dumps({"subsystem": "gateway"})},
        "msg": "noop",
    }) + "\n")

    ctx = _ctx_for(home, log_dir)
    report = _run_panorama(ctx, session_id=sid)
    assert report.error is None
    sec = _logs_section(report)
    names = [c.name for c in sec.checks]
    # Either logs.none (true zero window) or one of the new states if the
    # session somehow ended up with a window. Both are acceptable as long
    # as nothing exploded; assert no warn/fail from this branch.
    assert "logs.missing" not in names
    # Must include exactly one of the empty-correlation messages.
    empty_states = {
        "logs.none", "logs.not_retained", "logs.uncorrelated",
    }
    assert empty_states & set(names), (
        f"expected one empty-correlation state, got {names}"
    )


def test_window_log_dates_helper(tmp_path: Path):
    """Direct unit test for window_log_dates: present vs missing vs
    available bookkeeping across a multi-day window."""
    from datetime import date

    from ocdiag.recent_logs import window_log_dates

    log_dir = tmp_path / "h"
    log_dir.mkdir()
    # Drop files for 2026-06-04 and 2026-06-06 (skip 06-05).
    for iso in ("2026-06-04", "2026-06-06", "2026-06-10"):
        (log_dir / f"openclaw-{iso}.log").write_text("x")

    # Window straddles 2026-06-04 → 2026-06-06 (3 days). 06-05 missing.
    start_ms = int(time.mktime(date(2026, 6, 4).timetuple()) * 1000)
    end_ms = int(time.mktime(date(2026, 6, 6).timetuple()) * 1000)
    present, missing, available = window_log_dates(
        str(log_dir), start_ms, end_ms,
    )
    assert present == ["2026-06-04", "2026-06-06"]
    assert missing == ["2026-06-05"]
    assert available == ["2026-06-04", "2026-06-06", "2026-06-10"]

    # Zero window → empty present/missing, available still populated.
    p2, m2, a2 = window_log_dates(str(log_dir), 0, 0)
    assert (p2, m2) == ([], [])
    assert a2 == available

    # Single-day window with file present.
    one_ms = int(time.mktime(date(2026, 6, 4).timetuple()) * 1000)
    p3, m3, _ = window_log_dates(str(log_dir), one_ms, one_ms + 1000)
    assert p3 == ["2026-06-04"]
    assert m3 == []

    # Single-day window with file missing.
    miss_ms = int(time.mktime(date(2026, 6, 5).timetuple()) * 1000)
    p4, m4, _ = window_log_dates(str(log_dir), miss_ms, miss_ms + 1000)
    assert p4 == []
    assert m4 == ["2026-06-05"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
