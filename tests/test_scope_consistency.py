"""data_scope zero-drift consistency tests.

Owner-mandated: the displayed ``data_scope`` MUST equal the actual scan,
with zero deviation. Two failure modes guarded against here:

(A) DRIFT — the displayed window token is a hardcoded literal that can
    diverge from the ms value passed into ``ctx.collect_runs``. We pin the
    ``window_token()`` helper as the single source of truth.

(B) MISLABEL — the displayed detail count is NOT the actual scanned count
    (e.g. plugin_diag previously reported the filtered top-30 sample as
    if it were the scope). We independently recompute the scan and assert
    equality.

Pure stdlib + pytest. The trajectory-touching collector tests stage an
isolated ``$HOME`` with a copied fixture file whose timestamps are rewritten
to "just now", so windowed scans (24h/7d/14d) hit the run.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ocdiag import trajectory as traj_mod  # noqa: E402
from ocdiag.core import registry  # noqa: E402
from ocdiag.core.context import DiagContext  # noqa: E402
from ocdiag.core.types import Report, ScopeItem  # noqa: E402
from ocdiag.timeutil import window_token  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "trajectory"

STUB_CONFIG: Dict = {
    "gateway": {"port": 18789},
    "agents": {"defaults": {}, "list": [{"id": "main", "workspace": ""}]},
    "plugins": {"installs": {}},
    "models": {"providers": {}},
    "channels": {},
}


# ── window_token: pin the canonical mapping ──

def test_window_token_known_windows():
    assert window_token(24 * 3600 * 1000) == "24h"
    assert window_token(7 * 86400 * 1000) == "7d"
    assert window_token(14 * 86400 * 1000) == "14d"
    assert window_token(30 * 86400 * 1000) == "30d"


def test_window_token_fallbacks():
    # 5d (divisible by a day) → "5d"
    assert window_token(5 * 86400 * 1000) == "5d"
    # 6h (divisible by an hour, not a day) → "6h"
    assert window_token(6 * 3600 * 1000) == "6h"
    # arbitrary ms (neither) → "<ms>ms"
    assert window_token(123) == "123ms"
    assert window_token(0) == "0ms"


def test_window_token_handles_garbage():
    assert window_token("nope") == "?"  # type: ignore[arg-type]


# ── fixture staging ──

def _iso_utc(t_epoch: float) -> str:
    return _dt.datetime.fromtimestamp(
        t_epoch, tz=_dt.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _rewrite_fixture(src: Path, dst: Path, anchor_offset_seconds: int) -> None:
    """Copy a trajectory fixture to ``dst`` with each event's ``ts`` rewritten
    so the run lands inside recent windows. Mirrors run_collector_tests.py.
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


def _make_ctx_with_fixtures(
    fixtures: List[str], anchor_offset_seconds: int = -3600,
) -> Tuple[DiagContext, str]:
    tmp = tempfile.mkdtemp(prefix="ocdiag-scope-test-")
    home = Path(tmp) / "home"
    oc_home = home / ".openclaw"
    oc_home.mkdir(parents=True, exist_ok=True)
    (oc_home / "openclaw.json").write_text(
        json.dumps(STUB_CONFIG, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log_dir = Path(tmp) / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = oc_home / "agents" / "main" / "sessions"
    for name in fixtures:
        src = FIXTURE_DIR / f"{name}.trajectory.jsonl"
        if not src.is_file():
            pytest.skip(
                f"fixture missing: {src} — run "
                f"`python3 tests/run_trajectory_tests.py` first",
            )
        _rewrite_fixture(
            src, sessions_dir / f"{name}.trajectory.jsonl",
            anchor_offset_seconds,
        )
    ctx = DiagContext(
        openclaw_home=oc_home,
        config_path=oc_home / "openclaw.json",
        log_dir=log_dir,
        sessions_base=oc_home / "agents",
    )
    return ctx, tmp


def _scope_items(report: Report, source: str) -> List[ScopeItem]:
    return [si for si in report.data_scope if si.source == source]


def _scope_one(report: Report, source: str, window: str) -> ScopeItem:
    matches = [
        si for si in report.data_scope
        if si.source == source and si.window == window
    ]
    assert len(matches) == 1, (
        f"expected exactly one scope item for ({source!r}, {window!r}); "
        f"got {[(s.source, s.window) for s in report.data_scope]}"
    )
    return matches[0]


def _run(module_id: str, ctx: DiagContext) -> Report:
    registry.discover()
    coll = registry.get(module_id)
    assert coll is not None, f"collector not registered: {module_id}"
    return coll.collect(ctx)


# ── windowed-trajectory collectors: displayed count == independent recompute ──
#
# Each test stages the same fixture under a fresh $HOME, runs the collector,
# then independently recomputes ``len(ctx.collect_runs(since_ms=ms_ago(W)))``
# (with the same prefilter setting) and asserts the displayed scope detail
# embeds that exact count. The window token must be the canonical
# ``window_token(ms)`` — never a parallel literal.


def test_gateway_trajectory_24h_scope_matches_real_scan():
    ctx, tmp = _make_ctx_with_fixtures(["complete_user_run"])
    try:
        report = _run("gateway", ctx)
        # Recompute with the same args gateway uses.
        # NB: a fresh ctx isn't strictly needed — the cache is keyed on
        # (since_ms, limit, raw, prefilter) so this lookup is a hit on the
        # collector's earlier scan.
        runs = ctx.collect_runs(
            since_ms=traj_mod.ms_ago(24 * 3600 * 1000),
            mtime_prefilter=True,
        )
        si = _scope_one(report, "trajectory", "24h")
        assert si.window == window_token(24 * 3600 * 1000)
        assert si.detail is not None
        assert f"{len(runs)} runs" in si.detail, (
            f"scope detail {si.detail!r} does not contain "
            f"the real scanned count {len(runs)}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cron_jobs_trajectory_7d_scope_matches_real_scan():
    ctx, tmp = _make_ctx_with_fixtures(["complete_user_run"])
    try:
        report = _run("cron_jobs", ctx)
        runs = ctx.collect_runs(since_ms=traj_mod.ms_ago(7 * 86400 * 1000))
        si = _scope_one(report, "trajectory", "7d")
        assert si.window == window_token(7 * 86400 * 1000)
        assert si.detail is not None
        assert f"{len(runs)} runs" in si.detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_environment_trajectory_14d_scope_matches_real_scan():
    ctx, tmp = _make_ctx_with_fixtures(["complete_user_run"])
    try:
        report = _run("environment", ctx)
        runs = ctx.collect_runs(since_ms=traj_mod.ms_ago(14 * 86400 * 1000))
        si = _scope_one(report, "trajectory", "14d")
        assert si.window == window_token(14 * 86400 * 1000)
        assert si.detail is not None
        assert f"{len(runs)} runs" in si.detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recent_errors_trajectory_7d_scope_matches_real_scan():
    ctx, tmp = _make_ctx_with_fixtures(["complete_user_run"])
    try:
        report = _run("recent_errors", ctx)
        runs = ctx.collect_runs(since_ms=traj_mod.ms_ago(7 * 86400 * 1000))
        si = _scope_one(report, "trajectory", "7d")
        assert si.window == window_token(7 * 86400 * 1000)
        assert si.detail is not None
        assert f"{len(runs)} runs" in si.detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── plugin_diag: scan-vs-sample mislabel guard ──


def test_plugin_diag_scope_reports_runs_scanned_not_sample_count():
    ctx, tmp = _make_ctx_with_fixtures(["complete_user_run"])
    try:
        report = _run("plugin_diag", ctx)
        traj_summary = (
            report.data.get("trajectory_plugins") or {}
        )
        # Both numbers must be present in the JSON payload.
        assert "trajectory_runs_scanned" in traj_summary, (
            "summary missing trajectory_runs_scanned"
        )
        assert "samples" in traj_summary, "summary missing samples"
        runs_scanned = traj_summary["trajectory_runs_scanned"]
        samples = traj_summary["samples"]

        si_list = _scope_items(report, "trajectory")
        assert len(si_list) == 1, (
            f"plugin_diag should emit exactly one trajectory scope; "
            f"got {[(s.window, s.detail) for s in si_list]}"
        )
        si = si_list[0]
        assert si.detail is not None
        # Detail must reference the SCANNED count, not just the sample.
        assert f"{runs_scanned} runs scanned" in si.detail, (
            f"scope detail {si.detail!r} does not embed runs_scanned="
            f"{runs_scanned}"
        )
        assert f"{samples} sampled" in si.detail, (
            f"scope detail {si.detail!r} does not embed samples={samples}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── performance: latest-20 + 7d split, real file counts ──


def test_performance_emits_two_sessions_scope_items_with_real_counts():
    ctx, tmp = _make_ctx_with_fixtures(["complete_user_run"])
    try:
        report = _run("performance", ctx)
        sess_items = _scope_items(report, "sessions")
        windows = sorted(si.window for si in sess_items)
        assert windows == ["7d", "latest-20"], (
            f"expected exactly two sessions scope items "
            f"(latest-20 + 7d); got {windows}"
        )
        latest = next(si for si in sess_items if si.window == "latest-20")
        seven_d = next(si for si in sess_items if si.window == "7d")
        assert latest.detail is not None
        assert seven_d.detail is not None

        sess_files = report.data.get("session_files_analyzed")
        trend_files = report.data.get("trend_files_analyzed")
        assert sess_files is not None
        assert trend_files is not None
        assert f"{sess_files} files" in latest.detail
        assert f"{trend_files} files" in seven_d.detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── analysis-threshold guard: "active threshold 7d" never lives in window ──


def test_sessions_diag_active_threshold_lives_in_detail_not_window():
    ctx, tmp = _make_ctx_with_fixtures(["complete_user_run"])
    try:
        report = _run("sessions_diag", ctx)
        # The sessions scan is full-disk; the 7d active threshold is an
        # ANALYSIS slice and must show up in detail text, not as a window
        # token. (Brief: "Getting this wrong is itself a deviation.")
        sess_items = _scope_items(report, "sessions")
        assert sess_items, "sessions_diag must emit a sessions scope item"
        for si in sess_items:
            assert si.window == "full", (
                f"sessions window must be 'full' (analysis threshold "
                f"belongs in detail). Got window={si.window!r}"
            )
            assert "7d" in (si.detail or ""), (
                f"sessions detail should mention '7d active threshold'; "
                f"got {si.detail!r}"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_task_health_orphan_cutoff_in_detail_not_window():
    # task_health emits its scope statically (no scan needed for the
    # window token); the analysis threshold ``orphan cutoff 24h`` belongs
    # in detail, never in window. This is purely a guard on the literal.
    ctx, tmp = _make_ctx_with_fixtures([])
    try:
        report = _run("task_health", ctx)
        ti = _scope_items(report, "tasks")
        assert ti, "task_health must emit a tasks scope item"
        for si in ti:
            assert si.window == "current", (
                f"tasks window must be 'current'; got {si.window!r}"
            )
            assert "24h" in (si.detail or ""), (
                f"tasks detail should mention orphan cutoff 24h; "
                f"got {si.detail!r}"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── universal: every scope item's window != raw ms literal ──


def test_no_collector_emits_a_raw_ms_literal_as_window():
    """A regression guard: window tokens should be human-readable
    (24h/7d/14d/30d/full/current/today/...). Catching a raw ms-as-string
    means a collector forgot to route through window_token().
    """
    ctx, tmp = _make_ctx_with_fixtures(["complete_user_run"])
    try:
        registry.discover()
        for module_id in (
            "configuration", "cron_jobs", "doctor", "environment",
            "gateway", "performance", "plugin_diag", "recent_errors",
            "run_health", "sessions_diag", "shell_history",
            "sys_health", "task_health",
        ):
            coll = registry.get(module_id)
            if coll is None:
                continue
            try:
                report = coll.collect(ctx)
            except Exception:
                continue
            for si in report.data_scope:
                assert not si.window.isdigit(), (
                    f"{module_id}: scope window {si.window!r} looks like a "
                    f"raw integer — should route through window_token()"
                )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
