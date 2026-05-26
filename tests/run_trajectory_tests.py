#!/usr/bin/env python3
"""Synthetic-fixture tests for ``ocdiag.trajectory``.

Generates 10 hand-crafted trajectory JSONL files covering the canonical
edge cases (silent cron, tool leak, idle timeout, schema drift, truncated
last line, multi-run-per-file, ...), feeds each through ``iter_runs`` /
``summarize_trajectory``, and asserts the surfaced fields match the
expected JSON checked into ``tests/expected/trajectory/``.

Designed to run as a plain script — no pytest / no test framework — so it
respects axiom #2 (zero runtime deps).

Usage:
    python3 tests/run_trajectory_tests.py
    python3 tests/run_trajectory_tests.py --regenerate-expected
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ocdiag import trajectory as traj  # noqa: E402

from tests.fixtures.trajectory._helpers import (  # noqa: E402
    SESSION_ID, make_events, write_fixture,
)


FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "trajectory"
EXPECTED_DIR = REPO_ROOT / "tests" / "expected" / "trajectory"


def _ts(seconds_offset: int = 0) -> str:
    """Deterministic ISO timestamp generator (anchored on a fixed date so
    expected JSON is stable)."""
    base = _dt.datetime(2026, 5, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    t = base + _dt.timedelta(seconds=seconds_offset)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def gen_fixtures() -> Dict[str, Path]:
    """Write all 10 fixture files. Returns {name: path}."""
    out: Dict[str, Path] = {}

    # 1. complete_user_run — happy path
    name = "complete_user_run"
    evs = make_events(
        run_id="run-complete-user",
        started_ts=_ts(0), ended_ts=_ts(45),
        trigger="user", final_status="success",
        item_lifecycle={"startedCount": 2, "completedCount": 2, "activeCount": 0},
        tool_metas=[{"toolName": "read"}, {"toolName": "write"}],
        usage={"input": 200, "output": 150, "cacheRead": 5000,
               "cacheWrite": 1000, "total": 6350},
        cache_broke=False,
    )
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), evs)
    out[name] = p

    # 2. aborted_run — user Ctrl-C
    name = "aborted_run"
    evs = make_events(
        run_id="run-aborted",
        started_ts=_ts(0), ended_ts=_ts(5),
        trigger="user", final_status="error",
        abort_flags={"aborted": True, "externalAbort": True},
        prompt_error_source="prompt",
    )
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), evs)
    out[name] = p

    # 3. idle_timeout_run
    name = "idle_timeout_run"
    evs = make_events(
        run_id="run-idle-timeout",
        started_ts=_ts(0), ended_ts=_ts(120),
        trigger="cron", final_status="error",
        abort_flags={"idleTimedOut": True, "timedOut": True},
    )
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), evs)
    out[name] = p

    # 4. tool_leak_run — active_count > 0
    name = "tool_leak_run"
    evs = make_events(
        run_id="run-leak",
        started_ts=_ts(0), ended_ts=_ts(60),
        trigger="user", final_status="success",
        item_lifecycle={"startedCount": 5, "completedCount": 3, "activeCount": 2},
        tool_metas=[{"toolName": "exec"}, {"toolName": "exec"}],
    )
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), evs)
    out[name] = p

    # 5. silent_cron_run — the 5.7 bug pattern
    name = "silent_cron_run"
    evs = make_events(
        run_id="run-silent-cron",
        started_ts=_ts(0), ended_ts=_ts(15),
        trigger="cron", final_status="success",
        did_send=False, successful_cron_adds=0,
        assistant_texts=[],
    )
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), evs)
    out[name] = p

    # 6. cache_break_run — promptCache.observation.broke=true
    name = "cache_break_run"
    evs = make_events(
        run_id="run-cache-break",
        started_ts=_ts(0), ended_ts=_ts(30),
        trigger="user", final_status="success",
        cache_broke=True,
        usage={"input": 10000, "output": 1000, "cacheRead": 500,
               "cacheWrite": 9000, "total": 20500},
    )
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), evs)
    out[name] = p

    # 7. incomplete_run — only session.started + trace.metadata
    name = "incomplete_run"
    evs = make_events(
        run_id="run-incomplete",
        started_ts=_ts(0), trigger="user",
        incomplete="only_started",
    )
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), evs)
    out[name] = p

    # 8. multiline_run — 5 separate runIds in one file
    name = "multiline_run"
    all_evs: List[Dict[str, Any]] = []
    for i in range(5):
        all_evs.extend(make_events(
            run_id=f"run-multi-{i}",
            started_ts=_ts(60 * i),
            ended_ts=_ts(60 * i + 30),
            trigger=("user" if i % 2 else "cron"),
            final_status="success",
        ))
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), all_evs)
    out[name] = p

    # 9. bad_schema_run — schemaVersion=99
    name = "bad_schema_run"
    evs = make_events(
        run_id="run-schema-drift",
        started_ts=_ts(0), ended_ts=_ts(20),
        trigger="user", final_status="success",
        schema_version=99,
    )
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), evs)
    out[name] = p

    # 10. truncated_run — last line truncated mid-record
    name = "truncated_run"
    evs = make_events(
        run_id="run-truncated",
        started_ts=_ts(0), ended_ts=_ts(40),
        trigger="user", final_status="success",
    )
    p = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    write_fixture(str(p), evs)
    # Append a truncated final line — simulate writer crash
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"traceSchema":"openclaw-trajectory","schemaVersion":1,'
                '"type":"trace.metadata","runId":"run-trunc')
    out[name] = p

    return out


def summarize_run(run: traj.Run) -> Dict[str, Any]:
    """Project a Run to a stable, comparable subset."""
    return {
        "session_id": run.session_id,
        "run_id": run.run_id,
        "trigger": run.trigger,
        "final_status": run.final_status,
        "incomplete": run.incomplete,
        "aborted": run.aborted,
        "external_abort": run.external_abort,
        "timed_out": run.timed_out,
        "idle_timed_out": run.idle_timed_out,
        "prompt_error_source": run.prompt_error_source,
        "usage": {
            "input": run.usage_input, "output": run.usage_output,
            "cacheRead": run.usage_cache_read, "cacheWrite": run.usage_cache_write,
            "total": run.usage_total,
        },
        "cache_broke": run.cache_broke,
        "lifecycle": {
            "started": run.started_count, "completed": run.completed_count,
            "active": run.active_count,
        },
        "did_send_via_messaging_tool": run.did_send_via_messaging_tool,
        "messaging_text_count": run.messaging_text_count,
        "successful_cron_adds": run.successful_cron_adds,
        "tool_metas": [m.get("toolName") for m in run.tool_metas],
        "schema_version_seen": run.schema_version_seen,
        "harness_version": run.harness_version,
        "system_prompt_chars": run.system_prompt_chars,
        "tools_schema_chars": run.tools_schema_chars,
    }


def expected_for(name: str, paths: Dict[str, Path]) -> Dict[str, Any]:
    """Read the trajectory file via the production parser and project it."""
    p = paths[name]
    runs = list(traj.iter_runs(str(p)))
    summary = traj.summarize_trajectory(str(p))
    drift = traj.detect_schema_drift(str(p))
    return {
        "fixture": name,
        "schema_drift": drift,
        "summary": {
            k: summary[k] for k in (
                "total_runs", "incomplete_runs", "by_trigger",
                "by_final_status", "by_abort_flag", "active_leak_runs",
                "schema_version_seen", "schema_drift",
            )
        },
        "runs": [summarize_run(r) for r in runs],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regenerate-expected", action="store_true",
                    help="Overwrite expected/*.json with current parser output.")
    args = ap.parse_args()

    print("[1/3] generating synthetic fixtures...", flush=True)
    paths = gen_fixtures()
    print(f"      wrote {len(paths)} files under {FIXTURE_DIR}")

    print("[2/3] parsing each fixture and computing observed output...",
          flush=True)
    EXPECTED_DIR.mkdir(parents=True, exist_ok=True)
    failures: List[str] = []
    for name in sorted(paths.keys()):
        observed = expected_for(name, paths)
        exp_path = EXPECTED_DIR / f"{name}.json"
        observed_str = json.dumps(observed, indent=2, ensure_ascii=False,
                                   sort_keys=True)
        if args.regenerate_expected or not exp_path.exists():
            exp_path.write_text(observed_str + "\n", encoding="utf-8")
            action = "REGEN" if exp_path.exists() else "WRITE"
            print(f"      [{action}] {exp_path.name}")
            continue
        expected_str = exp_path.read_text(encoding="utf-8").rstrip("\n")
        if observed_str != expected_str:
            failures.append(name)
            print(f"      [FAIL] {name}", flush=True)
        else:
            print(f"      [OK]   {name}")

    print("[3/3] verdict assertions...", flush=True)
    # Cross-cutting assertions — verdict logic, schema drift, leak detection.
    by_name = {name: expected_for(name, paths) for name in paths}

    def expect(label: str, cond: bool) -> None:
        if cond:
            print(f"      [OK]   {label}")
        else:
            failures.append(label)
            print(f"      [FAIL] {label}")

    expect("complete_user_run -> 1 complete success run",
           by_name["complete_user_run"]["summary"]["by_final_status"].get("success") == 1
           and by_name["complete_user_run"]["summary"]["incomplete_runs"] == 0)
    expect("aborted_run -> aborted=1, externalAbort=1, prompt_error_source=prompt",
           by_name["aborted_run"]["summary"]["by_abort_flag"]["aborted"] == 1
           and by_name["aborted_run"]["runs"][0]["prompt_error_source"] == "prompt")
    expect("idle_timeout_run -> idleTimedOut=1, timedOut=1",
           by_name["idle_timeout_run"]["summary"]["by_abort_flag"]["idleTimedOut"] == 1
           and by_name["idle_timeout_run"]["summary"]["by_abort_flag"]["timedOut"] == 1)
    expect("tool_leak_run -> active_leak_runs=1",
           by_name["tool_leak_run"]["summary"]["active_leak_runs"] == 1)
    expect("silent_cron_run -> trigger=cron, final_status=success, did_send=False",
           by_name["silent_cron_run"]["runs"][0]["trigger"] == "cron"
           and by_name["silent_cron_run"]["runs"][0]["final_status"] == "success"
           and by_name["silent_cron_run"]["runs"][0]["did_send_via_messaging_tool"] is False)
    expect("cache_break_run -> cache_broke=True",
           by_name["cache_break_run"]["runs"][0]["cache_broke"] is True)
    expect("incomplete_run -> incomplete=True, total_runs=1",
           by_name["incomplete_run"]["runs"][0]["incomplete"] is True
           and by_name["incomplete_run"]["summary"]["total_runs"] == 1)
    expect("multiline_run -> 5 distinct runIds, all complete",
           by_name["multiline_run"]["summary"]["total_runs"] == 5
           and by_name["multiline_run"]["summary"]["incomplete_runs"] == 0)
    expect("bad_schema_run -> schema_drift detected",
           by_name["bad_schema_run"]["summary"]["schema_drift"] == "99")
    expect("truncated_run -> still parses 1 complete run, no crash",
           by_name["truncated_run"]["summary"]["total_runs"] == 1
           and by_name["truncated_run"]["summary"]["incomplete_runs"] == 0)

    if failures:
        print(f"\n{len(failures)} failure(s):", file=sys.stderr)
        for fname in failures:
            print(f"  - {fname}", file=sys.stderr)
        return 1
    print("\nAll trajectory fixture tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
