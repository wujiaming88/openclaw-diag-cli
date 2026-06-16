#!/usr/bin/env python3
"""Smoke tests for migrated v2 collectors.

These run each collector against an isolated temp $HOME with a stub
openclaw.json, then assert basic invariants on the produced Report:
- Report has the expected module_id
- Has at least one section with at least one check
- Verdict is one of {ok, warn, fail}
- JSON envelope round-trips through to_envelope()

This is a structural smoke test — it does NOT pin specific verdicts
against external tools, since whether `iostat`/`free`/`dig` are installed
varies by machine.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag.core import registry  # noqa: E402
from ocdiag.core.context import DiagContext  # noqa: E402
from ocdiag.core.types import Report, Verdict  # noqa: E402
from ocdiag.render.json_renderer import to_envelope  # noqa: E402


_failures = []


def _check(name, ok, detail=""):
    if ok:
        print(f"  [OK]   {name}")
    else:
        _failures.append(name)
        print(f"  [FAIL] {name}: {detail}")


STUB_CONFIG = {
    "gateway": {"port": 18789},
    "agents": {"defaults": {}, "list": [{"id": "main", "workspace": ""}]},
    "plugins": {"installs": {}},
    "models": {"providers": {}},
    "channels": {},
}


def _make_temp_ctx() -> tuple:
    home = tempfile.mkdtemp(prefix="ocdiag-v2-test-")
    home_path = Path(home)
    (home_path / "agents").mkdir(parents=True, exist_ok=True)
    config_path = home_path / "openclaw.json"
    with open(config_path, "w") as f:
        json.dump(STUB_CONFIG, f)
    log_dir = home_path / "logs"
    log_dir.mkdir(exist_ok=True)
    ctx = DiagContext(
        openclaw_home=home_path,
        config_path=config_path,
        log_dir=log_dir,
        sessions_base=home_path / "agents",
    )
    return ctx, home


def _run_collector_smoke(mid: str):
    ctx, tmp = _make_temp_ctx()
    try:
        registry.discover()
        c = registry.get(mid)
        _check(f"{mid}: registered", c is not None)
        if c is None:
            return
        report = c.collect(ctx)
        _check(f"{mid}: returns Report", isinstance(report, Report))
        _check(f"{mid}: module_id matches", report.module_id == mid)
        _check(f"{mid}: has at least one section", len(report.sections) >= 1)
        all_checks = [chk for s in report.sections for chk in s.checks]
        _check(f"{mid}: produced at least one check", len(all_checks) >= 1)
        _check(
            f"{mid}: verdict is valid",
            report.verdict in (Verdict.OK, Verdict.WARN, Verdict.FAIL),
        )
        env = to_envelope(report)
        _check(f"{mid}: envelope ok=True", env["ok"] is True)
        _check(f"{mid}: envelope.data has module", env["data"]["module"] == mid)
        _check(
            f"{mid}: envelope summary keys",
            set(env["data"]["summary"].keys()) == {"pass", "warn", "fail", "total"},
        )
        json.dumps(env, ensure_ascii=False)  # must serialize cleanly
        _check(f"{mid}: envelope JSON-serializable", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_configuration_missing_config():
    """configuration with non-existent config path should FAIL explicitly."""
    registry.discover()
    ctx = DiagContext(
        openclaw_home=Path("/nonexistent-ocdiag-test"),
        config_path=Path("/nonexistent-ocdiag-test/openclaw.json"),
        log_dir=Path("/tmp"),
        sessions_base=Path("/nonexistent-ocdiag-test/agents"),
    )
    c = registry.get("configuration")
    report = c.collect(ctx)
    _check(
        "configuration: missing config -> FAIL",
        report.verdict == Verdict.FAIL,
    )
    _check(
        "configuration: error message set",
        report.error == "配置文件未找到",
    )


class _FakeRun:
    def __init__(
        self,
        started_ts_ms: int,
        plugin_entries=None,
        session_id: str = "sssssss-1234",
        run_id: str = "rrrrrrr-5678",
    ):
        self.started_ts_ms = started_ts_ms
        self.plugin_entries = list(plugin_entries or [])
        self.imported_runtime_plugin_ids = []
        self.session_id = session_id
        self.run_id = run_id


class _FakePluginCtx:
    """Minimal stand-in for DiagContext so we can drive plugin_diag's
    layered scan deterministically without writing trajectory files."""

    def __init__(self, runs_by_window):
        self._runs_by_window = runs_by_window
        self.calls = []

    def trajectory_files(self):
        return ["fake-traj.jsonl"]

    def collect_runs(self, *, since_ms=None, mtime_prefilter=False, **_):
        self.calls.append((since_ms, mtime_prefilter))
        if since_ms is None:
            return list(self._runs_by_window.get("full", []))
        # Map back to a label by matching the requested ms-floor against the
        # 7d / 30d bands (same windows as the implementation).
        from ocdiag import trajectory
        seven = trajectory.ms_ago(7 * 86400 * 1000)
        thirty = trajectory.ms_ago(30 * 86400 * 1000)
        if since_ms >= seven - 1:
            return list(self._runs_by_window.get("7d", []))
        if since_ms >= thirty - 1:
            return list(self._runs_by_window.get("30d", []))
        return list(self._runs_by_window.get("full", []))


def _drive_plugin_trajectory(runs_by_window):
    from ocdiag.collectors.plugin_diag import _section_trajectory
    from ocdiag.core.types import Section
    section = Section(title="t")
    ctx = _FakePluginCtx(runs_by_window)
    payload = _section_trajectory(section, ctx, configured={})
    return payload, ctx


def test_plugin_diag_scope_7d_when_recent_runs_exist():
    fresh_run = _FakeRun(
        started_ts_ms=10**13,
        plugin_entries=[{"id": "p1", "activated": True}],
    )
    payload, ctx = _drive_plugin_trajectory({"7d": [fresh_run]})
    summary = payload["trajectory_plugins"]
    _check(
        "plugin_diag: 7d scope when recent runs exist",
        summary.get("trajectory_scan_scope") == "7d",
        repr(summary),
    )
    _check(
        "plugin_diag: only 7d window probed",
        ctx.calls and ctx.calls[0][1] is True,
    )


def test_plugin_diag_scope_30d_when_only_30d_has_runs():
    older_run = _FakeRun(
        started_ts_ms=10**13,
        plugin_entries=[{"id": "p1", "activated": True}],
    )
    payload, _ = _drive_plugin_trajectory({"7d": [], "30d": [older_run]})
    summary = payload["trajectory_plugins"]
    _check(
        "plugin_diag: 30d scope when only 30d hits",
        summary.get("trajectory_scan_scope") == "30d",
        repr(summary),
    )


def test_plugin_diag_scope_full_fallback_when_only_old_runs():
    very_old = _FakeRun(
        started_ts_ms=1,
        plugin_entries=[{"id": "p1", "activated": True}],
    )
    payload, _ = _drive_plugin_trajectory({
        "7d": [], "30d": [], "full": [very_old],
    })
    summary = payload["trajectory_plugins"]
    _check(
        "plugin_diag: full_fallback scope when only old runs",
        summary.get("trajectory_scan_scope") == "full_fallback",
        repr(summary),
    )


def test_plugin_diag_scope_full_fallback_when_no_metadata():
    bare = _FakeRun(started_ts_ms=10**13, plugin_entries=[])
    payload, _ = _drive_plugin_trajectory({
        "7d": [bare], "30d": [bare], "full": [bare],
    })
    summary = payload["trajectory_plugins"]
    _check(
        "plugin_diag: full_fallback when no plugin metadata anywhere",
        summary.get("trajectory_scan_scope") == "full_fallback",
        repr(summary),
    )
    _check(
        "plugin_diag: samples=0 when no metadata",
        summary.get("samples") == 0,
        repr(summary),
    )


def test_plugin_diag_scope_none_when_no_trajectory_files():
    from ocdiag.collectors.plugin_diag import _section_trajectory
    from ocdiag.core.types import Section

    class _NoFilesCtx(_FakePluginCtx):
        def trajectory_files(self):
            return []

    section = Section(title="t")
    payload = _section_trajectory(section, _NoFilesCtx({}), configured={})
    summary = payload["trajectory_plugins"]
    _check(
        "plugin_diag: scope=none when no trajectory files",
        summary.get("trajectory_scan_scope") == "none",
        repr(summary),
    )


def test_configuration_invalid_json():
    """configuration with malformed JSON should FAIL with parse_error."""
    registry.discover()
    home = Path(tempfile.mkdtemp(prefix="ocdiag-v2-bad-"))
    try:
        cfg = home / "openclaw.json"
        cfg.write_text("{not valid json")
        ctx = DiagContext(
            openclaw_home=home,
            config_path=cfg,
            log_dir=home,
            sessions_base=home,
        )
        c = registry.get("configuration")
        report = c.collect(ctx)
        _check(
            "configuration: invalid JSON -> FAIL",
            report.verdict == Verdict.FAIL,
        )
        _check(
            "configuration: parse_error in data",
            "parse_error" in report.data,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main():
    print("[1/3] collector smoke tests...")
    for mid in ("sys_health", "environment", "configuration", "shell_history"):
        _run_collector_smoke(mid)
    print()
    print("[2/3] configuration: missing config path...")
    test_configuration_missing_config()
    print()
    print("[3/3] configuration: invalid JSON...")
    test_configuration_invalid_json()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} test(s)")
        for n in _failures:
            print(f"  - {n}")
        return 1
    print("All v2 collector smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
