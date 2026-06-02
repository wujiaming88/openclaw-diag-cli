#!/usr/bin/env python3
"""Fixture-driven E2E tests for the 9 trajectory-touching collectors.

Spawns ``bin/ocdiag <module> --json`` against an isolated temp ``$HOME``
that contains:

  - a minimal stub ``openclaw.json``
  - just the trajectory fixture(s) needed for the test, copied with their
    event timestamps rewritten to "just now" so 24h / 7d / 30d windows hit

Then asserts the collector's exit code, ``verdict`` field, and a small
set of critical ``data.*`` paths. Pure stdlib, same style as
``tests/run_trajectory_tests.py`` — no pytest, zero deps.

Usage:
    python3 tests/run_collector_tests.py

Prerequisite: trajectory fixtures must already be on disk under
``tests/fixtures/trajectory/`` (they are git-tracked; if a fresh checkout
is missing them, run ``python3 tests/run_trajectory_tests.py`` first).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "trajectory"
OCDIAG = REPO_ROOT / "bin" / "ocdiag"

# Stub openclaw.json — minimal but valid. Collectors that need fields
# beyond these treat absence as "data missing" (axiom #5) and degrade
# gracefully, so we keep the surface area tiny.
STUB_CONFIG: Dict[str, Any] = {
    "gateway": {"port": 18789},
    "agents": {"defaults": {}, "list": [{"id": "main", "workspace": ""}]},
    "plugins": {"installs": {}},
    "models": {"providers": {}},
}


# ── timestamp rewriting ──

def _iso_utc(t_epoch: float) -> str:
    return _dt.datetime.fromtimestamp(
        t_epoch, tz=_dt.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _rewrite_fixture_timestamps(
    src: Path, dst: Path, anchor_offset_seconds: int,
) -> None:
    """Copy a fixture JSONL to ``dst`` and rewrite each event's ``ts`` so the
    run lands inside recent time windows.

    Within a single ``runId`` the original event ordering is preserved by
    spreading events 1s apart starting from ``now + anchor_offset_seconds``.
    Truncated final lines (no closing ``}``) are passed through verbatim
    so the loader's truncation tolerance is still exercised.
    """
    base_now = time.time() + anchor_offset_seconds
    run_seq: Dict[str, int] = {}

    out_lines: List[str] = []
    with open(src, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                out_lines.append("")
                continue
            try:
                ev = json.loads(line)
            except Exception:
                # Truncated/garbage trailing line — keep as-is.
                out_lines.append(line)
                continue
            rid = ev.get("runId")
            if rid:
                n = run_seq.get(rid, 0)
                run_seq[rid] = n + 1
                ev["ts"] = _iso_utc(base_now + n)
            out_lines.append(json.dumps(ev, ensure_ascii=False))

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        for ln in out_lines:
            f.write(ln + "\n")


# ── env staging ──

def _stage_env(
    fixtures: List[str],
    anchor_offset_seconds: int = -3600,
) -> Tuple[Path, Dict[str, str]]:
    """Build a temp HOME tree and the env vars that point all OPENCLAW_*
    paths into it. Returns ``(tmpdir, env)``; caller is responsible for
    cleanup."""
    tmpdir = Path(tempfile.mkdtemp(prefix="ocdiag-collector-test-"))
    home = tmpdir / "home"
    oc_home = home / ".openclaw"
    oc_home.mkdir(parents=True, exist_ok=True)

    (oc_home / "openclaw.json").write_text(
        json.dumps(STUB_CONFIG, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    log_dir = tmpdir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    sessions_dir = oc_home / "agents" / "main" / "sessions"
    for name in fixtures:
        src = FIXTURE_DIR / f"{name}.trajectory.jsonl"
        if not src.is_file():
            raise RuntimeError(
                f"fixture missing: {src} — run "
                f"`python3 tests/run_trajectory_tests.py` first"
            )
        dst = sessions_dir / f"{name}.trajectory.jsonl"
        _rewrite_fixture_timestamps(src, dst, anchor_offset_seconds)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["OPENCLAW_HOME"] = str(oc_home)
    env["OPENCLAW_CONFIG"] = str(oc_home / "openclaw.json")
    env["OPENCLAW_SESSIONS"] = str(oc_home / "agents")
    env["OPENCLAW_LOG_DIR"] = str(log_dir)
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    )
    # Force non-tty so colors / progress spinners stay off in subprocess.
    env.pop("FORCE_COLOR", None)
    return tmpdir, env


# ── subprocess invocation ──

def _run_collector(
    module: str, env: Dict[str, str], timeout: float = 20.0,
) -> Tuple[int, Dict[str, Any], str]:
    """Spawn the collector and parse its stdout JSON envelope.

    Returns the unwrapped report dict (envelope's ``data`` field) so test
    expressions like ``data.windows.24h…`` continue to work against the
    v1.1+ ``{ok, data, error}`` envelope.
    """
    cmd = [sys.executable, str(OCDIAG), module, "--json", "--no-color"]
    r = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout,
    )
    out = r.stdout.strip()
    if not out:
        raise RuntimeError(
            f"empty stdout (rc={r.returncode}, stderr: {r.stderr[:300]!r})"
        )
    last_line = out.splitlines()[-1]
    try:
        envelope = json.loads(last_line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"stdout not JSON ({e}): {out[:500]!r}")
    # Unwrap v1.1+ envelope for test backward compat.
    if isinstance(envelope, dict) and "ok" in envelope and "data" in envelope:
        if envelope["ok"] and isinstance(envelope["data"], dict):
            payload = envelope["data"]
        else:
            payload = envelope
    else:
        payload = envelope
    return r.returncode, payload, r.stderr


# ── assertion helpers ──

def _path(payload: Dict[str, Any], dotted: str) -> Any:
    cur: Any = payload
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            raise AssertionError(
                f"path {dotted}: expected dict at {part!r}, got "
                f"{type(cur).__name__}"
            )
        if part not in cur:
            raise AssertionError(
                f"missing key: {dotted} (stopped at {part!r}, "
                f"available: {sorted(cur.keys())[:8]})"
            )
        cur = cur[part]
    return cur


# ── test definitions ──

CheckFn = Callable[[Dict[str, Any]], bool]
TestSpec = Dict[str, Any]
TESTS: List[TestSpec] = []


def _add(label: str, module: str, fixtures: List[str], **kw: Any) -> None:
    TESTS.append({"label": label, "module": module, "fixtures": fixtures, **kw})


# ─── run_health (most coverage — verdict + windowed counts) ─────────────────
_add(
    "run_health: tool_leak → verdict=fail + 24h.active_leak_count>=1",
    "run_health", ["tool_leak_run"],
    verdict="fail",
    checks=[
        ("24h.active_leak_count>=1",
         lambda p: _path(p, "data.windows.24h.active_leak_count") >= 1),
    ],
)
# v0.6.1: stuck run with empty toolMetas — verify last_tool_call_names
# fallback is surfaced in the active_leak_samples payload (was rendering
# as `[?]` in v0.6.0).
_add(
    "run_health: tool_leak_no_meta → last_tool_call_names fallback populated",
    "run_health", ["tool_leak_no_meta_run"],
    verdict="fail",
    checks=[
        ("24h.active_leak_count>=1",
         lambda p: _path(p, "data.windows.24h.active_leak_count") >= 1),
        ("first sample tool_metas empty",
         lambda p: len(_path(p, "data.windows.24h.active_leak_samples")[0]["tool_metas"]) == 0),
        ("first sample last_tool_call_names == ['read']",
         lambda p: _path(p, "data.windows.24h.active_leak_samples")[0]["last_tool_call_names"] == ["read"]),
    ],
)
_add(
    "run_health: complete_user → verdict=ok + 7d.error_rate_pct==0",
    "run_health", ["complete_user_run"],
    verdict="ok",
    checks=[
        ("7d.error_rate_pct==0",
         lambda p: _path(p, "data.windows.7d.error_rate_pct") == 0),
    ],
)
_add(
    "run_health: aborted → 7d.abort_breakdown.aborted==1",
    "run_health", ["aborted_run"],
    checks=[
        ("aborted=1",
         lambda p: _path(p, "data.windows.7d.abort_breakdown").get("aborted") == 1),
        ("externalAbort=1",
         lambda p: _path(p, "data.windows.7d.abort_breakdown").get("externalAbort") == 1),
    ],
)
_add(
    "run_health: idle_timeout → 7d.abort_breakdown.idleTimedOut==1",
    "run_health", ["idle_timeout_run"],
    checks=[
        ("idleTimedOut=1",
         lambda p: _path(p, "data.windows.7d.abort_breakdown").get("idleTimedOut") == 1),
        ("timedOut=1",
         lambda p: _path(p, "data.windows.7d.abort_breakdown").get("timedOut") == 1),
    ],
)
_add(
    "run_health: silent_cron → 7d.by_trigger.cron==1",
    "run_health", ["silent_cron_run"],
    checks=[
        ("by_trigger.cron=1",
         lambda p: _path(p, "data.windows.7d.by_trigger").get("cron") == 1),
    ],
)
_add(
    "run_health: multiline → runs_total_all_time==5",
    "run_health", ["multiline_run"],
    checks=[
        ("runs_total_all_time=5",
         lambda p: _path(p, "data.runs_total_all_time") == 5),
    ],
)
_add(
    "run_health: incomplete → 7d.by_final_status.incomplete>=1",
    "run_health", ["incomplete_run"],
    checks=[
        ("by_final_status.incomplete>=1",
         lambda p: _path(p, "data.windows.7d.by_final_status").get("incomplete", 0) >= 1),
    ],
)

# ─── cron_jobs (silent-cron detection) ─────────────────────────────────────
_add(
    "cron_jobs: silent_cron → verdict=fail + silent_cron_runs non-empty",
    "cron_jobs", ["silent_cron_run"],
    verdict="fail",
    checks=[
        ("silent_cron_runs len>=1",
         lambda p: len(_path(p, "data.trajectory_cron").get("silent_cron_runs") or []) >= 1),
    ],
)
_add(
    "cron_jobs: complete_user → silent_cron_runs empty",
    "cron_jobs", ["complete_user_run"],
    checks=[
        ("silent_cron_runs empty",
         lambda p: len((_path(p, "data.trajectory_cron") or {}).get("silent_cron_runs") or []) == 0),
    ],
)

# ─── sessions (active-leak detection) ──────────────────────────────────────
_add(
    "sessions: tool_leak → trajectory.runs_with_active_leaks>=1",
    "sessions", ["tool_leak_run"],
    verdict="fail",
    checks=[
        ("runs_with_active_leaks>=1",
         lambda p: _path(p, "data.trajectory.runs_with_active_leaks") >= 1),
    ],
)
_add(
    "sessions: complete_user → trajectory.runs_with_active_leaks==0",
    "sessions", ["complete_user_run"],
    checks=[
        ("runs_with_active_leaks==0",
         lambda p: _path(p, "data.trajectory.runs_with_active_leaks") == 0),
    ],
)

# ─── recent_errors (abort breakdown) ───────────────────────────────────────
_add(
    "recent_errors: aborted → trajectory_errors.abort_breakdown_7d.aborted==1",
    "recent_errors", ["aborted_run"],
    checks=[
        ("abort_breakdown_7d.aborted==1",
         lambda p: _path(p, "data.trajectory_errors.abort_breakdown_7d").get("aborted") == 1),
        ("prompt_error_sources.prompt==1",
         lambda p: _path(p, "data.trajectory_errors.prompt_error_sources").get("prompt") == 1),
    ],
)
_add(
    "recent_errors: idle_timeout → abort_breakdown_7d.idleTimedOut==1",
    "recent_errors", ["idle_timeout_run"],
    checks=[
        ("idleTimedOut==1",
         lambda p: _path(p, "data.trajectory_errors.abort_breakdown_7d").get("idleTimedOut") == 1),
    ],
)

# ─── performance (cache_health) ────────────────────────────────────────────
_add(
    "performance: cache_break → cache_broke_pct>0",
    "performance", ["cache_break_run"],
    checks=[
        ("cache_broke_pct>0",
         lambda p: _path(p, "data.trajectory_cache_health.cache_broke_pct") > 0),
    ],
)
_add(
    "performance: complete_user → cache_broke_pct==0",
    "performance", ["complete_user_run"],
    checks=[
        ("cache_broke_pct==0",
         lambda p: _path(p, "data.trajectory_cache_health.cache_broke_pct") == 0),
    ],
)

# ─── plugin_diag (drift detection — fixture has empty plugin_entries) ──────
_add(
    "plugin_diag: complete_user → no plugin samples (drift section reports empty)",
    "plugin_diag", ["complete_user_run"],
    checks=[
        ("trajectory_plugins.samples==0 OR no drift",
         lambda p: (
             (_path(p, "data.trajectory_plugins") or {}).get("samples") == 0
             or len(((_path(p, "data.trajectory_plugins") or {})
                     .get("plugin_drift") or {})
                    .get("config_enabled_runtime_disabled") or []) == 0
         )),
    ],
)

# ─── environment / configuration / gateway (smoke only) ────────────────────
_add(
    "environment: complete_user → runs cleanly, no JSON parse error",
    "environment", ["complete_user_run"], smoke=True,
)
_add(
    "configuration: complete_user → runs cleanly, parses stub config",
    "configuration", ["complete_user_run"], smoke=True,
    checks=[
        ("data.json_valid==True",
         lambda p: _path(p, "data.json_valid") is True),
    ],
)
_add(
    "gateway: complete_user → runs cleanly",
    "gateway", ["complete_user_run"], smoke=True,
)


# ── runner ──

def main() -> int:
    if not OCDIAG.is_file():
        print(f"FATAL: dispatcher not found at {OCDIAG}", file=sys.stderr)
        return 2

    print(f"[1/2] running {len(TESTS)} collector tests...", flush=True)
    failures: List[str] = []
    skipped: List[str] = []
    t0 = time.time()

    for tc in TESTS:
        label: str = tc["label"]
        module: str = tc["module"]
        fixtures: List[str] = tc["fixtures"]
        smoke: bool = tc.get("smoke", False)
        expected_verdict: Optional[str] = tc.get("verdict")
        checks: List[Tuple[str, CheckFn]] = tc.get("checks", [])

        tmpdir: Optional[Path] = None
        try:
            tmpdir, env = _stage_env(fixtures)
            try:
                rc, payload, _stderr = _run_collector(module, env)
            except subprocess.TimeoutExpired:
                failures.append(f"{label}: TIMEOUT")
                print(f"  [FAIL] {label}: TIMEOUT", flush=True)
                continue
            except RuntimeError as e:
                failures.append(f"{label}: {e}")
                print(f"  [FAIL] {label}: {e}", flush=True)
                continue

            # rc 0 = ok/warn, 1 = fail, 2 = crash. Anything else is unexpected.
            if rc not in (0, 1):
                failures.append(f"{label}: rc={rc} (collector crashed)")
                print(f"  [FAIL] {label}: rc={rc} (collector crashed)",
                      flush=True)
                continue

            local_failures: List[str] = []

            if expected_verdict is not None:
                actual = payload.get("verdict")
                if actual != expected_verdict:
                    local_failures.append(
                        f"verdict={actual!r} want {expected_verdict!r}"
                    )

            for check_label, fn in checks:
                try:
                    ok = bool(fn(payload))
                except AssertionError as e:
                    local_failures.append(f"{check_label} ({e})")
                    continue
                except Exception as e:
                    local_failures.append(
                        f"{check_label} (exception: {type(e).__name__}: {e})"
                    )
                    continue
                if not ok:
                    local_failures.append(check_label)

            if local_failures:
                msg = " | ".join(local_failures)
                failures.append(f"{label}: {msg}")
                print(f"  [FAIL] {label}: {msg}", flush=True)
            else:
                tag = "[OK]   " if not smoke else "[OK]   "
                print(f"  {tag}{label}", flush=True)
        finally:
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)

    elapsed = time.time() - t0
    passed = len(TESTS) - len(failures) - len(skipped)
    print(
        f"\n[2/2] done in {elapsed:.1f}s | {passed} passed, "
        f"{len(failures)} failed, {len(skipped)} skipped",
        flush=True,
    )
    if failures:
        print(f"\n{len(failures)} failure(s):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\nAll collector integration tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
