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
