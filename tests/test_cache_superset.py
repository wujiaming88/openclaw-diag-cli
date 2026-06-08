#!/usr/bin/env python3
"""Golden tests for ``DiagContext.collect_runs`` v1.5.2 cache changes.

Two invariants are proved here:

1. Superset reuse path — when a windowed query (``since_ms`` set,
   ``limit_per_file is None``, ``mtime_prefilter=False``) is served from a
   cached full scan, the returned set is IDENTICAL (same ``Run`` objects,
   set-equal) to a fresh ``trajectory.collect_runs(files, since_ms=...)``.
   Critically that includes keeping undated runs (``started_ts_ms == 0``)
   regardless of the window.

2. Gateway output neutrality of ``mtime_prefilter`` — the prefiltered
   24h scan may omit undated runs in old files, but those runs would have
   been excluded from the gateway count and histogram anyway (the fix in
   ``_section_run_frequency`` already restricts to dated runs). So the
   prefiltered scan and the full scan produce the same dated-runs-in-window
   set, which is what gateway reports.

Stdlib only; pure-script style matching the rest of ``tests/``.

Usage:
    python3 tests/test_cache_superset.py
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ocdiag import trajectory as traj  # noqa: E402
from ocdiag.core.context import DiagContext  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "trajectory"


def _iso_utc(t_epoch: float) -> str:
    return _dt.datetime.fromtimestamp(
        t_epoch, tz=_dt.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write_undated_fixture(target_dir: Path, name: str) -> None:
    """Write a JSONL where session.started has an empty ``ts`` and no
    other event provides one. This makes ``Run.started_ts_ms`` stay 0,
    exercising the predicate's "keep undated regardless of window" branch.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    p = target_dir / f"{name}.trajectory.jsonl"
    rid = "run-undated-1"
    sid = "22222222-2222-2222-2222-222222222222"
    base = {
        "traceSchema": "openclaw-trajectory",
        "schemaVersion": 1,
        "traceId": sid,
        "source": "runtime",
        "sessionId": sid,
        "sessionKey": "agent:test:cli:direct:test-user",
        "runId": rid,
        "workspaceDir": "/tmp/test-workspace",
        "provider": "test-provider",
        "modelId": "test-model",
        "modelApi": "test-api",
        "ts": "",  # ← this is the point: parser yields started_ts_ms = 0
    }
    events = [
        {**base, "type": "session.started", "seq": 1, "sourceSeq": 1,
         "data": {"trigger": "user"}},
        {**base, "type": "trace.artifacts", "seq": 2, "sourceSeq": 2,
         "data": {"finalStatus": "success"}},
    ]
    with open(p, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    # Pin mtime to ~recent so mtime_prefilter doesn't drop it inadvertently.
    now = time.time() - 600
    os.utime(p, (now, now))


def _stage_fixtures(
    tmpdir: Path,
    fixtures: List[Tuple[str, int]],
) -> Path:
    """Copy fixtures into a fake ``$OPENCLAW_HOME/agents/main/sessions/``,
    rewriting each event ``ts`` to ``now + offset_seconds`` so the runs
    land at known relative ages. Returns the ``sessions_base`` path.

    ``fixtures`` is a list of ``(fixture_name, offset_seconds)`` pairs.
    Negative offsets put the run in the past; e.g. ``-3600`` = 1 hour ago.
    """
    sessions_base = tmpdir / "agents"
    target = sessions_base / "main" / "sessions"
    target.mkdir(parents=True, exist_ok=True)

    for name, offset in fixtures:
        src = FIXTURE_DIR / f"{name}.trajectory.jsonl"
        if not src.is_file():
            raise RuntimeError(
                f"fixture missing: {src} — run "
                f"`python3 tests/run_trajectory_tests.py` first"
            )
        dst = target / f"{name}.trajectory.jsonl"
        base_now = time.time() + offset
        run_seq: dict = {}
        with open(src, "r", encoding="utf-8") as f, \
                open(dst, "w", encoding="utf-8") as out:
            for raw in f:
                line = raw.rstrip("\n")
                if not line.strip():
                    out.write("\n")
                    continue
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001 — keep truncated lines
                    out.write(line + "\n")
                    continue
                rid = ev.get("runId")
                if rid:
                    n = run_seq.get(rid, 0)
                    run_seq[rid] = n + 1
                    ev["ts"] = _iso_utc(base_now + n)
                out.write(json.dumps(ev, ensure_ascii=False) + "\n")
        # Set the file mtime to match the run's anchor so mtime-prefilter
        # tests are deterministic.
        os.utime(dst, (base_now, base_now))
    return sessions_base


def _key(run) -> Tuple[str, str, int]:
    """Stable identity for a Run within one ctx. ``id()`` would also work
    for the superset path (same object), but ``(source_file, run_id,
    started_ts_ms)`` lets us cross-check freshly-scanned Run objects too."""
    return (run.source_file, run.run_id, run.started_ts_ms)


def _make_ctx(sessions_base: Path) -> DiagContext:
    # Construct directly rather than via ``default`` so we can pin
    # ``sessions_base`` regardless of any caller env vars.
    return DiagContext(
        openclaw_home=sessions_base.parent,
        config_path=sessions_base.parent / "openclaw.json",
        log_dir=sessions_base.parent / "log",
        sessions_base=sessions_base,
    )


def _assert(label: str, cond: bool, failures: List[str]) -> None:
    if cond:
        print(f"      [OK]   {label}")
    else:
        failures.append(label)
        print(f"      [FAIL] {label}")


def main() -> int:
    failures: List[str] = []

    # Stage a mix: very recent (1h ago, in 24h), older (3d ago, in 7d not 24h),
    # much older (10d ago, in 14d not 7d), ancient (30d ago, outside all),
    # plus an INCOMPLETE run (started but no trace.artifacts → undated_count
    # contribution comes from the fixture's missing finalization). We add
    # one fixture per window AND the incomplete fixture so the predicate's
    # "keep undated regardless of window" branch is exercised.
    fixtures = [
        ("complete_user_run",      -1 * 3600),       # 1h ago
        ("aborted_run",            -3 * 86400),      # 3d ago
        ("idle_timeout_run",       -10 * 86400),     # 10d ago
        ("cache_break_run",        -30 * 86400),     # 30d ago — outside all
        ("incomplete_run",         -2 * 86400),      # 2d ago, but undated
    ]

    tmpdir = Path(tempfile.mkdtemp(prefix="ocdiag-cache-superset-"))
    try:
        sessions_base = _stage_fixtures(tmpdir, fixtures)
        # Add a synthetic UNDATED run (started_ts_ms = 0) so we can prove
        # the "keep undated regardless of window" predicate branch.
        _write_undated_fixture(
            sessions_base / "main" / "sessions",
            "synthetic_undated",
        )
        files = traj.discover_trajectory_files(str(sessions_base))
        if not files:
            print("[FAIL] no fixture files staged", file=sys.stderr)
            return 1

        windows = {
            "24h": 24 * 3600 * 1000,
            "7d":  7 * 86400 * 1000,
            "14d": 14 * 86400 * 1000,
        }

        # ─── Test 1: superset path is set-equal to fresh scan ───
        print("[1/3] superset reuse vs fresh scan, per window...", flush=True)
        for label, win in windows.items():
            ctx_super = _make_ctx(sessions_base)
            # Prime full-scan cache (key: None, None, False, False).
            full = ctx_super.collect_runs()
            since = traj.now_ms() - win
            # This call MUST hit the superset path, NOT re-scan disk. We
            # verify that by snapshotting the full-scan cache key and
            # checking ``_trajectory_cache`` contents after.
            superset_runs = ctx_super.collect_runs(since_ms=since)

            # Fresh scan in an isolated ctx with no cache.
            ctx_fresh = _make_ctx(sessions_base)
            fresh_runs = traj.collect_runs(
                ctx_fresh.trajectory_files(), since_ms=since,
            )

            ks_super: Set = {_key(r) for r in superset_runs}
            ks_fresh: Set = {_key(r) for r in fresh_runs}
            _assert(
                f"{label}: superset == fresh (n_super={len(ks_super)}, "
                f"n_fresh={len(ks_fresh)})",
                ks_super == ks_fresh,
                failures,
            )

            # Predicate spot-check: at every window, the undated run MUST
            # appear (started_ts_ms == 0 → kept).
            undated_super = [r for r in superset_runs if not r.started_ts_ms]
            _assert(
                f"{label}: undated runs preserved by superset filter "
                f"(n={len(undated_super)})",
                len(undated_super) >= 1,
                failures,
            )

            # The cache MUST contain BOTH the full-scan key and the
            # windowed key after the superset call. If the disk path had
            # been taken instead, only the windowed key would exist and
            # the full-scan key would not have been read.
            assert ctx_super._trajectory_cache is not None
            full_key = (None, None, False, False)
            win_key = (since, None, False, False)
            _assert(
                f"{label}: full-scan cache hit and windowed key cached "
                f"(no disk re-scan)",
                full_key in ctx_super._trajectory_cache
                and win_key in ctx_super._trajectory_cache,
                failures,
            )

            # Object-identity check: superset Runs must be the SAME
            # Python objects as the cached full-scan list (we hand back a
            # subset, not copies).
            full_id_set = {id(r) for r in full}
            super_id_set = {id(r) for r in superset_runs}
            _assert(
                f"{label}: superset reuses full-scan Run objects "
                f"(no re-parse)",
                super_id_set.issubset(full_id_set),
                failures,
            )

        # ─── Test 2: mtime_prefilter is set-equal on dated runs ───
        print("[2/3] mtime_prefilter vs full scan, dated runs in 24h...",
              flush=True)
        ctx_pf = _make_ctx(sessions_base)
        since_24h = traj.now_ms() - windows["24h"]
        prefilter_runs = ctx_pf.collect_runs(
            since_ms=since_24h, mtime_prefilter=True,
        )

        ctx_full = _make_ctx(sessions_base)
        full_24h = ctx_full.collect_runs(since_ms=since_24h)

        # The prefilter is a strict subset (it can drop undated runs whose
        # files predate the floor). For dated runs in window, the two MUST
        # agree. That dated-only invariant is exactly what gateway reports.
        ks_pf_dated: Set = {_key(r) for r in prefilter_runs if r.started_ts_ms}
        ks_full_dated: Set = {_key(r) for r in full_24h if r.started_ts_ms}
        _assert(
            f"24h: prefilter dated == full dated "
            f"(n_pf={len(ks_pf_dated)}, n_full={len(ks_full_dated)})",
            ks_pf_dated == ks_full_dated,
            failures,
        )

        # The prefilter result MUST be a (non-strict) subset of the full
        # 24h result — never adds, only drops.
        ks_pf: Set = {_key(r) for r in prefilter_runs}
        ks_full: Set = {_key(r) for r in full_24h}
        _assert(
            "24h: prefilter ⊆ full (subset only — no spurious additions)",
            ks_pf.issubset(ks_full),
            failures,
        )

        # ─── Test 3: cache key isolation between prefilter and full ───
        print("[3/3] cache-key isolation: prefilter MUST NOT pollute "
              "full-set keys...", flush=True)
        ctx_iso = _make_ctx(sessions_base)
        # Prefilter call first — caches under (since, None, False, True).
        ctx_iso.collect_runs(since_ms=since_24h, mtime_prefilter=True)
        assert ctx_iso._trajectory_cache is not None
        pf_key = (since_24h, None, False, True)
        full_key = (since_24h, None, False, False)
        _assert(
            "prefilter key cached under mtime_prefilter=True",
            pf_key in ctx_iso._trajectory_cache,
            failures,
        )
        _assert(
            "prefilter call did NOT pollute non-prefilter window key",
            full_key not in ctx_iso._trajectory_cache,
            failures,
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} failure(s):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\nAll cache-superset tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
