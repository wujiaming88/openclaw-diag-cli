#!/usr/bin/env python3
"""Pytest: prefilter requests reuse an existing full-scan cache (v1.10.2).

Proves the ``all``-scenario optimization in ``DiagContext.collect_runs``:

1. When a full scan is ALREADY cached (e.g. ``configuration`` ran first in
   ``all``), a windowed ``mtime_prefilter=True`` request is served from that
   cache IN MEMORY — same ``Run`` objects, no disk re-scan — yielding the
   superset (undated runs kept).
2. When NO full scan is cached (standalone ``plugin_diag``), a prefilter
   request falls through to the real prefilter DISK scan: it does NOT
   fabricate a full-scan cache entry, and caches only under its prefilter key.

Stdlib only; matches the rest of ``tests/``.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Set

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ocdiag import trajectory as traj  # noqa: E402
from ocdiag.core.context import DiagContext  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "trajectory"


def _iso_utc(t_epoch: float) -> str:
    return _dt.datetime.fromtimestamp(
        t_epoch, tz=_dt.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _stage(tmp: Path, name: str, offset_s: int) -> Path:
    """Copy one fixture into a fake sessions tree, rewriting ts to now+offset
    and pinning mtime to the anchor. Returns sessions_base."""
    sessions_base = tmp / "agents"
    target = sessions_base / "main" / "sessions"
    target.mkdir(parents=True, exist_ok=True)
    src = FIXTURE_DIR / f"{name}.trajectory.jsonl"
    dst = target / f"{name}.trajectory.jsonl"
    base_now = time.time() + offset_s
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
            except Exception:
                out.write(line + "\n")
                continue
            rid = ev.get("runId")
            if rid:
                n = run_seq.get(rid, 0)
                run_seq[rid] = n + 1
                ev["ts"] = _iso_utc(base_now + n)
            out.write(json.dumps(ev, ensure_ascii=False) + "\n")
    import os
    os.utime(dst, (base_now, base_now))
    return sessions_base


def _ctx(sessions_base: Path) -> DiagContext:
    return DiagContext(
        openclaw_home=sessions_base.parent,
        config_path=sessions_base.parent / "openclaw.json",
        log_dir=sessions_base.parent / "log",
        sessions_base=sessions_base,
    )


def test_prefilter_reuses_full_cache_when_present():
    tmp = Path(tempfile.mkdtemp(prefix="ocdiag-pf-reuse-"))
    try:
        sb = _stage(tmp, "complete_user_run", -3600)  # 1h ago
        ctx = _ctx(sb)
        # Prime full-scan cache (configuration-style).
        full = ctx.collect_runs()
        assert (None, None, False, False) in ctx._trajectory_cache
        since = traj.now_ms() - 7 * 86400 * 1000
        # plugin_diag-style prefilter request — must be served from full cache.
        pf = ctx.collect_runs(since_ms=since, mtime_prefilter=True)
        # Served from full cache → same Run objects (no re-parse).
        full_ids = {id(r) for r in full}
        assert {id(r) for r in pf}.issubset(full_ids), (
            "prefilter result should reuse full-scan Run objects, not re-scan"
        )
        # Cached under the prefilter key for repeat-lookup dedup.
        assert (since, None, False, True) in ctx._trajectory_cache
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prefilter_disk_scans_when_no_full_cache():
    tmp = Path(tempfile.mkdtemp(prefix="ocdiag-pf-disk-"))
    try:
        sb = _stage(tmp, "complete_user_run", -3600)
        ctx = _ctx(sb)
        since = traj.now_ms() - 7 * 86400 * 1000
        # Standalone: no full cache primed → real prefilter disk scan.
        pf = ctx.collect_runs(since_ms=since, mtime_prefilter=True)
        # Must NOT fabricate a full-scan cache entry.
        assert (None, None, False, False) not in ctx._trajectory_cache, (
            "prefilter standalone path must not create a full-scan cache key"
        )
        # Caches only under its prefilter key.
        assert (since, None, False, True) in ctx._trajectory_cache
        # And it returns the in-window run.
        ks: Set = {(r.run_id, r.started_ts_ms) for r in pf}
        assert len(ks) >= 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── conclusion equivalence: top-30 selection is mode-invariant ──

def _write_runs(path: Path, runs, mtime_epoch: float):
    """Write a trajectory file holding many runs. ``runs`` is a list of
    ``(run_id, ts_iso_or_empty)``; empty ts → undated run (started_ts_ms 0)."""
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rid, ts in runs:
            sid = f"sess-{rid}"
            base = {
                "traceSchema": "openclaw-trajectory", "schemaVersion": 1,
                "traceId": sid, "source": "runtime", "sessionId": sid,
                "sessionKey": f"agent:test:cli:direct:{rid}", "runId": rid,
                "workspaceDir": "/tmp/ws", "provider": "p",
                "modelId": "m", "modelApi": "a", "ts": ts,
            }
            f.write(json.dumps({**base, "type": "session.started", "seq": 1,
                                "sourceSeq": 1, "data": {"trigger": "user"}}) + "\n")
            f.write(json.dumps({**base, "type": "trace.artifacts", "seq": 2,
                                "sourceSeq": 2,
                                "data": {"finalStatus": "success"}}) + "\n")
    os.utime(path, (mtime_epoch, mtime_epoch))


def _select_top30(runs):
    runs = sorted(runs, key=lambda r: r.started_ts_ms or 0, reverse=True)
    top30 = runs[:30]
    return [r.run_id for r in top30]


def test_top30_selection_identical_across_modes_when_enough_dated():
    """When a window has >=30 dated runs, the undated runs that the superset
    keeps (but the prefilter disk scan drops) must NEVER enter the top-30 —
    so plugin_diag's latest/recent selection is byte-identical in both modes.
    """
    tmp = Path(tempfile.mkdtemp(prefix="ocdiag-concl-"))
    try:
        sb = tmp / "agents"
        target = sb / "main" / "sessions"
        now = time.time()
        # 35 DATED runs within 7d (1..35 hours ago), recent file mtime.
        dated = [
            (f"dated-{i:02d}", _iso_utc(now - (i + 1) * 3600))
            for i in range(35)
        ]
        _write_runs(target / "dated.trajectory.jsonl", dated, now - 1800)
        # 5 UNDATED runs in an OLD-mtime file (60d ago) → prefilter drops it,
        # superset (full scan) keeps it.
        undated = [(f"undated-{i}", "") for i in range(5)]
        _write_runs(target / "undated.trajectory.jsonl", undated, now - 60 * 86400)

        since = traj.now_ms() - 7 * 86400 * 1000

        ctxA = _ctx(sb)
        disk = ctxA.collect_runs(since_ms=since, mtime_prefilter=True)
        ctxB = _ctx(sb)
        ctxB.collect_runs()  # prime full cache
        sup = ctxB.collect_runs(since_ms=since, mtime_prefilter=True)

        # The superset must carry the undated runs; the disk prefilter must not.
        sup_undated = [r for r in sup if not r.started_ts_ms]
        disk_undated = [r for r in disk if not r.started_ts_ms]
        assert len(sup_undated) == 5, f"superset should keep 5 undated, got {len(sup_undated)}"
        assert len(disk_undated) == 0, f"prefilter should drop old-file undated, got {len(disk_undated)}"

        # Dated-in-window sets identical.
        dated_disk = sorted(r.run_id for r in disk if r.started_ts_ms)
        dated_sup = sorted(r.run_id for r in sup if r.started_ts_ms)
        assert dated_disk == dated_sup, "dated-in-window sets must be identical"

        # THE KEY ASSERTION: top-30 selection identical → latest/recent invariant.
        top_disk = _select_top30(disk)
        top_sup = _select_top30(sup)
        assert top_disk == top_sup, (
            f"top-30 selection diverged between modes:\n"
            f"  disk={top_disk}\n  sup ={top_sup}"
        )
        # And no undated run reached the top-30 in either mode.
        assert all(rid.startswith("dated-") for rid in top_sup), (
            f"undated run leaked into top-30: {top_sup}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
