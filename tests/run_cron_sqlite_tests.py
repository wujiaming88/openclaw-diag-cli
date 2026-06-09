#!/usr/bin/env python3
"""Stdlib-only tests for the SQLite-aware cron_jobs collector.

Covers the three data-source cases:

  1. SQLite present + populated → source="sqlite", jobs are read from
     ``cron_jobs`` / ``cron_run_logs`` and run analysis succeeds.
  2. SQLite missing → source="legacy-json", the legacy
     ``cron/jobs.json`` + ``runs/<id>.jsonl`` fallback path runs.
  3. Both SQLite AND legacy ``cron/jobs.json`` populated → source="both"
     and a ``cron.source_inconsistency`` warn surfaces (OpenClaw issue
     #90072).

Each test stages its own temp $HOME / $OPENCLAW_HOME tree and seeds a
fresh sqlite file via stdlib ``sqlite3``. Real OpenClaw state at
``~/.openclaw/state/openclaw.sqlite`` is NEVER touched.

Same style as ``run_collector_tests.py``: pure stdlib, no pytest, prints
[OK]/[FAIL] per case and exits 0 on success.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OCDIAG = REPO_ROOT / "bin" / "ocdiag"

STUB_CONFIG: Dict[str, Any] = {
    "gateway": {"port": 18789},
    "agents": {"defaults": {}, "list": [{"id": "main", "workspace": ""}]},
    "plugins": {"installs": {}},
    "models": {"providers": {}},
}


# ── env staging ──

def _stage_home() -> Tuple[Path, Path, Dict[str, str]]:
    """Build an isolated $HOME/.openclaw + return env for subprocess."""
    tmpdir = Path(tempfile.mkdtemp(prefix="ocdiag-cron-sqlite-"))
    home = tmpdir / "home"
    oc_home = home / ".openclaw"
    (oc_home / "agents").mkdir(parents=True, exist_ok=True)
    (oc_home / "openclaw.json").write_text(
        json.dumps(STUB_CONFIG, ensure_ascii=False),
        encoding="utf-8",
    )
    log_dir = tmpdir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["OPENCLAW_HOME"] = str(oc_home)
    env["OPENCLAW_CONFIG"] = str(oc_home / "openclaw.json")
    env["OPENCLAW_SESSIONS"] = str(oc_home / "agents")
    env["OPENCLAW_LOG_DIR"] = str(log_dir)
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    )
    env.pop("FORCE_COLOR", None)
    return tmpdir, oc_home, env


def _seed_sqlite(
    db_path: Path, store_key: str, jobs: List[Dict[str, Any]],
) -> None:
    """Create a minimal cron_jobs + cron_run_logs schema and seed it.

    The real OpenClaw schema has ~30 columns, but the collector only
    SELECTs store_key/job_id/job_json/state_json from cron_jobs and
    entry_json/ts/seq from cron_run_logs. Keeping the test schema small
    keeps the test focused on the columns we actually consume — if
    upstream changes that contract, this will fail loudly.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE cron_jobs ("
            "store_key TEXT, job_id TEXT, "
            "job_json TEXT, state_json TEXT, "
            "PRIMARY KEY(store_key, job_id))",
        )
        conn.execute(
            "CREATE TABLE cron_run_logs ("
            "store_key TEXT, job_id TEXT, seq INTEGER, ts INTEGER, "
            "entry_json TEXT, "
            "PRIMARY KEY(store_key, job_id, seq))",
        )
        for j in jobs:
            jid = j["id"]
            # job_json mirrors the real shape: full job dict but
            # state field stripped to {} (runtime state lives in
            # the sibling state_json column).
            embedded = {**j, "state": {}}
            state = j.get("state") or {}
            conn.execute(
                "INSERT INTO cron_jobs (store_key, job_id, job_json, "
                "state_json) VALUES (?, ?, ?, ?)",
                (store_key, jid, json.dumps(embedded), json.dumps(state)),
            )
            for i, run in enumerate(j.get("_runs", [])):
                conn.execute(
                    "INSERT INTO cron_run_logs "
                    "(store_key, job_id, seq, ts, entry_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (store_key, jid, i, run.get("ts", 0), json.dumps(run)),
                )
        conn.commit()
    finally:
        conn.close()


def _run_cron(env: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """Spawn the collector and return (rc, unwrapped report data dict)."""
    cmd = [
        sys.executable, str(OCDIAG), "cron_jobs", "--json", "--no-color",
    ]
    r = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=20.0,
    )
    out = r.stdout.strip()
    if not out:
        raise RuntimeError(
            f"empty stdout (rc={r.returncode}, "
            f"stderr={r.stderr[:300]!r})"
        )
    last = out.splitlines()[-1]
    envelope = json.loads(last)
    if (isinstance(envelope, dict) and envelope.get("ok") and
            isinstance(envelope.get("data"), dict)):
        return r.returncode, envelope["data"]
    raise RuntimeError(f"unexpected envelope: {envelope}")


def _checks(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for sec in payload.get("sections", []):
        out.extend(sec.get("checks", []))
    return out


# ── test cases ──

_failures: List[str] = []


def _expect(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  [OK]   {label}")
    else:
        _failures.append(f"{label}: {detail}")
        print(f"  [FAIL] {label}: {detail}")


def test_sqlite_only() -> None:
    print("[1/3] SQLite-only (no legacy jobs.json) ...")
    tmpdir, oc_home, env = _stage_home()
    try:
        store_key = str(oc_home / "cron" / "jobs.json")
        now_ms = int(time.time() * 1000)
        jobs = [{
            "id": "j-aaa",
            "name": "daily-report",
            "enabled": True,
            "createdAtMs": now_ms - 86400000 * 3,
            "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
            "state": {
                "lastRunAtMs": now_ms - 1800000,
                "nextRunAtMs": now_ms + 1800000,
                "lastStatus": "ok",
                "consecutiveErrors": 0,
            },
            "_runs": [
                {
                    "ts": now_ms - 1800000 * (i + 1),
                    "jobId": "j-aaa",
                    "status": "ok",
                    "durationMs": 1500,
                    "runAtMs": now_ms - 1800000 * (i + 1),
                    "deliveryStatus": "delivered",
                }
                for i in range(3)
            ],
        }]
        _seed_sqlite(oc_home / "state" / "openclaw.sqlite", store_key, jobs)

        rc, payload = _run_cron(env)
        data = payload.get("data", {})
        _expect(
            "rc in {0,1}",
            rc in (0, 1),
            f"rc={rc}",
        )
        _expect(
            "source==sqlite",
            data.get("source") == "sqlite",
            f"got {data.get('source')!r}",
        )
        _expect(
            "sqlite_job_count==1",
            data.get("sqlite_job_count") == 1,
            f"got {data.get('sqlite_job_count')!r}",
        )
        _expect(
            "legacy_job_count==0",
            data.get("legacy_job_count") == 0,
            f"got {data.get('legacy_job_count')!r}",
        )
        _expect(
            "total_jobs==1",
            data.get("total_jobs") == 1,
            f"got {data.get('total_jobs')!r}",
        )
        jobs_payload = data.get("jobs") or []
        _expect(
            "job name preserved",
            jobs_payload and jobs_payload[0].get("name") == "daily-report",
            f"got {jobs_payload}",
        )
        _expect(
            "success_rate from run logs",
            jobs_payload and jobs_payload[0].get("success_rate") == 100.0,
            f"got {jobs_payload[0].get('success_rate') if jobs_payload else 'N/A'}",
        )
        check_names = [c["name"] for c in _checks(payload)]
        _expect(
            "no source_inconsistency check",
            "cron.source_inconsistency" not in check_names,
            f"checks={check_names}",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_legacy_fallback() -> None:
    print("[2/3] Legacy JSON fallback (no SQLite) ...")
    tmpdir, oc_home, env = _stage_home()
    try:
        # No state/openclaw.sqlite — collector must fall back.
        cron_dir = oc_home / "cron"
        runs_dir = cron_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (cron_dir / "jobs.json").write_text(json.dumps({
            "jobs": [{
                "id": "leg-1",
                "name": "old-job",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "0 * * * *"},
            }],
        }))
        now_ms = int(time.time() * 1000)
        (cron_dir / "jobs-state.json").write_text(json.dumps({
            "jobs": {
                "leg-1": {
                    "state": {
                        "lastRunAtMs": now_ms - 1800000,
                        "consecutiveErrors": 0,
                        "lastStatus": "ok",
                    },
                },
            },
        }))
        with open(runs_dir / "leg-1.jsonl", "w") as f:
            for i in range(2):
                f.write(json.dumps({
                    "ts": now_ms - 1800000 * (i + 1),
                    "jobId": "leg-1",
                    "status": "ok",
                    "durationMs": 500,
                    "runAtMs": now_ms - 1800000 * (i + 1),
                }) + "\n")

        rc, payload = _run_cron(env)
        data = payload.get("data", {})
        _expect(
            "source==legacy-json",
            data.get("source") == "legacy-json",
            f"got {data.get('source')!r}",
        )
        _expect(
            "total_jobs==1 (from jobs.json)",
            data.get("total_jobs") == 1,
            f"got {data.get('total_jobs')!r}",
        )
        jobs_payload = data.get("jobs") or []
        _expect(
            "legacy job name read",
            jobs_payload and jobs_payload[0].get("name") == "old-job",
            f"got {jobs_payload}",
        )
        check_names = [c["name"] for c in _checks(payload)]
        _expect(
            "no source_inconsistency check (no SQLite)",
            "cron.source_inconsistency" not in check_names,
            f"checks={check_names}",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_both_present_warn() -> None:
    print("[3/3] SQLite+legacy coexistence → warn ...")
    tmpdir, oc_home, env = _stage_home()
    try:
        store_key = str(oc_home / "cron" / "jobs.json")
        now_ms = int(time.time() * 1000)
        # Seed SQLite with one job.
        sqlite_jobs = [{
            "id": "sj-1",
            "name": "in-sqlite",
            "enabled": True,
            "createdAtMs": now_ms - 86400000,
            "schedule": {"kind": "cron", "expr": "*/30 * * * *"},
            "state": {"consecutiveErrors": 0},
            "_runs": [],
        }]
        _seed_sqlite(
            oc_home / "state" / "openclaw.sqlite", store_key, sqlite_jobs,
        )
        # Also leave behind a stranded legacy jobs.json (the #90072
        # symptom: migration left the old file with an entry that the
        # SQLite store never picked up).
        (oc_home / "cron").mkdir(parents=True, exist_ok=True)
        (oc_home / "cron" / "jobs.json").write_text(json.dumps({
            "jobs": [{
                "id": "stranded-1",
                "name": "only-in-json",
                "enabled": True,
                "schedule": {"kind": "every", "everyMs": 3600000},
            }],
        }))

        rc, payload = _run_cron(env)
        data = payload.get("data", {})
        _expect(
            "source==both",
            data.get("source") == "both",
            f"got {data.get('source')!r}",
        )
        _expect(
            "sqlite_job_count==1",
            data.get("sqlite_job_count") == 1,
            f"got {data.get('sqlite_job_count')!r}",
        )
        _expect(
            "legacy_job_count==1 (stranded)",
            data.get("legacy_job_count") == 1,
            f"got {data.get('legacy_job_count')!r}",
        )
        check_names = [c["name"] for c in _checks(payload)]
        _expect(
            "cron.source_inconsistency check fired",
            "cron.source_inconsistency" in check_names,
            f"checks={check_names}",
        )
        # Verdict must be at least warn (the inconsistency is a warn).
        _expect(
            "verdict>=warn",
            payload.get("verdict") in ("warn", "fail"),
            f"got {payload.get('verdict')!r}",
        )
        # SQLite still wins for the job list — the stranded "only-in-json"
        # job MUST NOT appear under data.jobs (that's the bug we're
        # surfacing, not perpetuating).
        names = [j.get("name") for j in (data.get("jobs") or [])]
        _expect(
            "SQLite job list wins (stranded job hidden from data.jobs)",
            names == ["in-sqlite"],
            f"got {names}",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── runner ──

def main() -> int:
    if not OCDIAG.is_file():
        print(f"FATAL: dispatcher not found at {OCDIAG}", file=sys.stderr)
        return 2

    t0 = time.time()
    test_sqlite_only()
    print()
    test_legacy_fallback()
    print()
    test_both_present_warn()
    print()

    elapsed = time.time() - t0
    if _failures:
        print(
            f"\n{len(_failures)} failure(s) in {elapsed:.1f}s:",
            file=sys.stderr,
        )
        for f in _failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"All cron-sqlite tests passed in {elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
