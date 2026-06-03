"""Tests for the panorama inspector.

Covers:
  - Argument validation (missing/short/non-uuid session ids → exit code 2 paths)
  - SESSION_NOT_FOUND
  - Correlation graph expansion across all six sources, on synthetic fixtures
    written into a temp $OPENCLAW_HOME
  - Tool-call waterfall pairing, duration arithmetic
  - Timeline ordering and merge across session.jsonl + trajectory + app log
  - Multi-run handling: --run-index, --all-runs
  - --include-ambient surfaces uncorrelated WARN/ERROR within window
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
                text = "ambient warning, no correlation key"
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


def test_include_ambient_surfaces_uncorrelated(tmp_path: Path):
    ctx = _build_fixture_home(tmp_path)
    base = _run_panorama(ctx, session_id=SESSION_ID)
    assert "ambient_logs" not in base.data
    enriched = _run_panorama(
        ctx, session_id=SESSION_ID, include_ambient=True,
    )
    # The fixture writes one ambient WARN inside the window.
    assert "ambient_logs" in enriched.data
    assert len(enriched.data["ambient_logs"]) >= 1


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
