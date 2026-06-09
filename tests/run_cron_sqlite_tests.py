#!/usr/bin/env python3
"""Stdlib-only tests for the SQLite-aware cron_jobs collector.

Covers the four data-source cases:

  1. SQLite present + populated → source="sqlite", jobs are read from
     ``cron_jobs`` / ``cron_run_logs`` and run analysis succeeds.
  2. SQLite missing → source="legacy-json", the legacy
     ``cron/jobs.json`` + ``runs/<id>.jsonl`` fallback path runs.
  3. Both SQLite AND legacy ``cron/jobs.json`` populated → source="both"
     and a ``cron.source_inconsistency`` warn surfaces (OpenClaw issue
     #90072).
  4. SQLite with > _RUN_LIMIT_PER_JOB cron_run_logs rows → the loader
     keeps the *most recent* 200 (not the oldest 200) in ts-ascending
     order, so ``_analyze()``'s ``finished[-20:]`` sees real recent runs
     and matches the legacy ``deque(maxlen=200)`` semantics.

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

# A fake API token shape that sanitize_text() should mask in payload.message.
# `sk-ant-` is in the Anthropic token pattern; the trailing ID must be ≥16
# chars to match. The full literal must NOT survive into the rendered output.
FAKE_TOKEN = "sk-ant-deadbeefcafef00d12345678abcdef"

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
    print("[1/6] SQLite-only (no legacy jobs.json) ...")
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
    print("[2/6] Legacy JSON fallback (no SQLite) ...")
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
    print("[3/6] SQLite+legacy coexistence → warn ...")
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


def test_run_log_recency_cap() -> None:
    """Regression: when a job has > _RUN_LIMIT_PER_JOB runs, the loader
    must keep the *most recent* 200 (matching the legacy
    deque(maxlen=200) semantics) and return them ts-ascending so the
    downstream ``_analyze()`` sees real recent runs in
    ``finished[-20:]``. Earlier code used ORDER BY ts ASC LIMIT 200,
    which kept the *oldest* 200 — silently wrong on hot jobs."""
    print("[4/6] >200 runs → keep most recent 200, ascending ...")
    tmpdir, oc_home, env = _stage_home()
    try:
        # Import the loader directly so we can assert returned shape /
        # ordering without being limited to the JSON envelope (which
        # only exposes aggregate analysis, not the raw run list).
        from ocdiag.collectors.cron_jobs import (
            _RUN_LIMIT_PER_JOB,
            _fetch_sqlite_runs,
            _open_state_db,
            _resolved,
        )

        store_key = str(oc_home / "cron" / "jobs.json")
        now_ms = int(time.time() * 1000)
        # 250 runs with strictly increasing ts. First 200 are "ok",
        # last 50 are "error" — distinguishes "kept oldest 200"
        # (success_rate via _analyze ≈ 100%) from
        # "kept most recent 200" (success_rate via _analyze == 0%).
        TOTAL_RUNS = 250
        TAIL_FAILURES = 50
        runs = []
        for i in range(TOTAL_RUNS):
            ts = now_ms - (TOTAL_RUNS - i) * 1000  # monotonically ↑
            is_tail = i >= (TOTAL_RUNS - TAIL_FAILURES)
            runs.append({
                "ts": ts,
                "jobId": "j-hot",
                "status": "error" if is_tail else "ok",
                "summary": f"run#{i}",
                "durationMs": 5000 if is_tail else 100,
                "runAtMs": ts,
                "deliveryStatus": "delivered",
            })
        max_ts = runs[-1]["ts"]
        oldest_kept_ts = runs[TOTAL_RUNS - _RUN_LIMIT_PER_JOB]["ts"]

        jobs = [{
            "id": "j-hot",
            "name": "hot-job",
            "enabled": True,
            "createdAtMs": now_ms - 86400000 * 7,
            "schedule": {"kind": "cron", "expr": "* * * * *", "tz": "UTC"},
            "state": {
                "lastRunAtMs": max_ts,
                "consecutiveErrors": TAIL_FAILURES,
                "lastStatus": "error",
            },
            "_runs": runs,
        }]
        db_path = oc_home / "state" / "openclaw.sqlite"
        _seed_sqlite(db_path, store_key, jobs)

        # ─ direct loader assertions: shape, recency, order ─
        conn = _open_state_db(str(db_path))
        try:
            got = _fetch_sqlite_runs(conn, _resolved(store_key), "j-hot")
        finally:
            conn.close()

        _expect(
            "loader truncates to _RUN_LIMIT_PER_JOB (200, not 250)",
            len(got) == _RUN_LIMIT_PER_JOB,
            f"got {len(got)} (expected {_RUN_LIMIT_PER_JOB})",
        )
        _expect(
            "kept set excludes oldest 50 (first kept ts == 51st run's ts)",
            got and got[0].get("ts") == oldest_kept_ts,
            f"first.ts={got[0].get('ts') if got else None!r} "
            f"expected={oldest_kept_ts!r}",
        )
        _expect(
            "last kept ts == global max ts (most recent included)",
            got and got[-1].get("ts") == max_ts,
            f"last.ts={got[-1].get('ts') if got else None!r} "
            f"expected={max_ts!r}",
        )
        _expect(
            "returned in ts-ascending order (first.ts < last.ts)",
            got and got[0].get("ts") < got[-1].get("ts"),
            f"first={got[0].get('ts') if got else None!r} "
            f"last={got[-1].get('ts') if got else None!r}",
        )
        # Stricter: every adjacent pair non-decreasing.
        all_asc = all(
            got[i].get("ts", 0) <= got[i + 1].get("ts", 0)
            for i in range(len(got) - 1)
        ) if got else False
        _expect(
            "all adjacent pairs ts-nondecreasing",
            all_asc,
            "encountered an out-of-order pair",
        )
        # The original 50 oldest "ok" runs (summary run#0..run#49) must
        # NOT appear; the 50 newest "error" runs (run#200..run#249) MUST.
        summaries = [r.get("summary") for r in got]
        _expect(
            "oldest run (run#0) dropped from kept set",
            "run#0" not in summaries,
            f"summaries[:3]={summaries[:3]}",
        )
        _expect(
            "newest run (run#249) present in kept set",
            f"run#{TOTAL_RUNS - 1}" in summaries,
            f"summaries[-3:]={summaries[-3:]}",
        )

        # ─ end-to-end: _analyze() sees recent (failing) tail ─
        rc, payload = _run_cron(env)
        data = payload.get("data", {})
        jobs_payload = data.get("jobs") or []
        _expect(
            "single job surfaced from SQLite",
            len(jobs_payload) == 1,
            f"got {len(jobs_payload)} jobs",
        )
        # finished[-20:] of the most-recent-200 lands entirely inside
        # the 50-failure tail → success_rate == 0%. With the old
        # ORDER BY ASC bug we'd be analyzing the oldest 200, whose
        # tail is all "ok" → success_rate would be 100% (regression).
        sr = jobs_payload[0].get("success_rate") if jobs_payload else None
        _expect(
            "_analyze sees recent tail: success_rate == 0% (not 100%)",
            sr == 0.0,
            f"success_rate={sr!r} — if 100%, the old"
            " ORDER BY ASC bug is back",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── config-surfacing helpers (v1.7.0) ──

def _detail_for_jobs_overview(payload: Dict[str, Any]) -> str:
    """Pull the 6.1 任务列表 section's consecutive_failures detail blob —
    that's where _analyze_jobs_section attaches the per-job pretty
    rendering (调度 / 上次执行 / … / 配置)."""
    for sec in payload.get("sections", []):
        for c in sec.get("checks", []):
            if c.get("name") == "cron.consecutive_failures":
                return c.get("detail") or ""
    return ""


def _build_full_config_job(now_ms: int, with_secret: bool) -> Dict[str, Any]:
    """A job dict carrying every config field the collector should
    surface. Used by both the sqlite and legacy config-surfacing tests."""
    # Fabricate a >14KB message body so the pretty-print preview path is
    # exercised on a realistic OpenClaw message size; if we leak the full
    # text into detail we'll see it in the regression assertions below.
    long_filler = "需要分析最近 24 小时的告警分布。" * 600  # ~14k chars
    secret_part = (
        f"\n[ctx] credentials: api_key={FAKE_TOKEN}\n" if with_secret else ""
    )
    full_msg = long_filler + secret_part
    return {
        "id": "cfg-1",
        "name": "config-rich",
        "enabled": True,
        "createdAtMs": now_ms - 86400000 * 2,
        "agentId": "main",
        "sessionKey": "ou_demo_session_key_xyz",
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "description": "每天 9 点跑全量告警归因，结果回到飞书运维群。",
        "deleteAfterRun": False,
        "schedule": {
            "kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai",
            "anchorMs": 0,
        },
        "payload": {
            "kind": "agentTurn",
            "model": "claude-opus-4-6",
            "fallbacks": ["claude-sonnet-4-6"],
            "thinking": "auto",
            "timeoutSeconds": 900,
            "toolsAllow": ["Read", "Bash"],
            "allowUnsafeExternalContent": False,
            "lightContext": True,
            "message": full_msg,
        },
        "delivery": {
            "mode": "announce",
            "channel": "feishu",
            "to": "ou_target_chat_001",
            "threadId": "thread_abc123",
            "accountId": "acct-prod",
            "bestEffort": True,
        },
        "state": {
            "lastRunAtMs": now_ms - 1800000,
            "consecutiveErrors": 0,
            "lastStatus": "ok",
        },
        "_runs": [
            {
                "ts": now_ms - 1800000,
                "jobId": "cfg-1",
                "status": "ok",
                "durationMs": 8000,
                "runAtMs": now_ms - 1800000,
                "deliveryStatus": "delivered",
            },
        ],
    }


def _assert_config_surfacing(
    payload: Dict[str, Any], full_msg_len: int, label_prefix: str,
) -> None:
    """Shared assertions reused by both data-source variants."""
    data = payload.get("data", {})
    jobs_payload = data.get("jobs") or []
    _expect(
        f"{label_prefix} jobs[0].config exists",
        bool(jobs_payload) and isinstance(jobs_payload[0].get("config"), dict),
        f"got {jobs_payload[:1]}",
    )
    cfg = (jobs_payload[0] or {}).get("config", {}) if jobs_payload else {}
    for k in (
        "enabled", "agent_id", "session_key", "session_target", "wake_mode",
        "description", "created_at_ms", "schedule", "payload", "delivery",
    ):
        _expect(
            f"{label_prefix} config.{k} present",
            k in cfg,
            f"keys={sorted(cfg)}",
        )
    p = cfg.get("payload", {}) or {}
    for k in (
        "kind", "model", "fallbacks", "thinking", "timeout_seconds",
        "tools_allow", "light_context", "message", "message_len",
    ):
        _expect(
            f"{label_prefix} config.payload.{k} present",
            k in p,
            f"payload keys={sorted(p)}",
        )
    _expect(
        f"{label_prefix} message_len matches original char count",
        p.get("message_len") == full_msg_len,
        f"got {p.get('message_len')!r} expected {full_msg_len}",
    )
    msg = p.get("message") or ""
    _expect(
        f"{label_prefix} payload.message kept full-length (sanitized only)",
        isinstance(msg, str) and len(msg) >= full_msg_len - 200,
        f"got len={len(msg) if isinstance(msg, str) else None}",
    )
    _expect(
        f"{label_prefix} payload.message scrubs FAKE_TOKEN",
        FAKE_TOKEN not in msg,
        "raw token survived sanitize_text",
    )
    d = cfg.get("delivery", {}) or {}
    for k in ("mode", "channel", "to", "thread_id", "account_id"):
        _expect(
            f"{label_prefix} config.delivery.{k} present",
            k in d,
            f"delivery keys={sorted(d)}",
        )
    sched = cfg.get("schedule", {}) or {}
    _expect(
        f"{label_prefix} config.schedule.kind preserved",
        sched.get("kind") == "cron",
        f"got {sched!r}",
    )

    # Pretty/detail block: present, contains 配置 marker + char-count
    # annotation, but does NOT splat the full 14KB message into output.
    detail = _detail_for_jobs_overview(payload)
    _expect(
        f"{label_prefix} detail contains 配置 block",
        "配置:" in detail,
        f"detail head={detail[:200]!r}",
    )
    _expect(
        f"{label_prefix} detail contains message char-count annotation",
        f"message({full_msg_len} 字)" in detail,
        f"missing 'message({full_msg_len} 字)' marker",
    )
    _expect(
        f"{label_prefix} detail does NOT include full message text",
        # Splatting the full message would push detail past full_msg_len;
        # we expect the preview to cap it well below that.
        len(detail) < full_msg_len,
        f"detail len={len(detail)} ≥ msg len={full_msg_len} (full text leaked)",
    )
    _expect(
        f"{label_prefix} detail does NOT leak FAKE_TOKEN",
        FAKE_TOKEN not in detail,
        "token survived into detail",
    )
    # Sanity: payload + delivery summary lines render.
    _expect(
        f"{label_prefix} detail has payload summary line",
        "payload:" in detail and "model=claude-opus-4-6" in detail,
        f"detail head={detail[:400]!r}",
    )
    _expect(
        f"{label_prefix} detail has delivery summary line",
        "delivery:" in detail and "feishu" in detail,
        f"detail head={detail[:400]!r}",
    )


def test_config_surfacing_sqlite() -> None:
    """jobs_payload + detail must surface every config field for the
    SQLite data source, with payload.message sanitized."""
    print("[5/6] Config surfacing via SQLite source ...")
    tmpdir, oc_home, env = _stage_home()
    try:
        store_key = str(oc_home / "cron" / "jobs.json")
        now_ms = int(time.time() * 1000)
        job = _build_full_config_job(now_ms, with_secret=True)
        full_msg_len = len(job["payload"]["message"])
        _seed_sqlite(
            oc_home / "state" / "openclaw.sqlite", store_key, [job],
        )
        rc, payload = _run_cron(env)
        _expect(
            "source==sqlite",
            payload.get("data", {}).get("source") == "sqlite",
            f"got {payload.get('data', {}).get('source')!r}",
        )
        _assert_config_surfacing(payload, full_msg_len, "sqlite")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_config_surfacing_legacy() -> None:
    """Same surfacing must hold for the legacy jobs.json source."""
    print("[6/6] Config surfacing via legacy JSON source ...")
    tmpdir, oc_home, env = _stage_home()
    try:
        cron_dir = oc_home / "cron"
        runs_dir = cron_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        now_ms = int(time.time() * 1000)
        job = _build_full_config_job(now_ms, with_secret=True)
        full_msg_len = len(job["payload"]["message"])
        # legacy jobs.json carries the job without the synthetic _runs key.
        legacy_job = {k: v for k, v in job.items() if k != "_runs"}
        (cron_dir / "jobs.json").write_text(json.dumps({"jobs": [legacy_job]}))
        # legacy runtime state lives in jobs-state.json; carry the same
        # state we put on the SQLite variant for parity.
        (cron_dir / "jobs-state.json").write_text(json.dumps({
            "jobs": {legacy_job["id"]: {"state": legacy_job["state"]}},
        }))
        with open(runs_dir / f"{legacy_job['id']}.jsonl", "w") as f:
            for r in job["_runs"]:
                f.write(json.dumps(r) + "\n")
        rc, payload = _run_cron(env)
        _expect(
            "source==legacy-json",
            payload.get("data", {}).get("source") == "legacy-json",
            f"got {payload.get('data', {}).get('source')!r}",
        )
        _assert_config_surfacing(payload, full_msg_len, "legacy")
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
    test_run_log_recency_cap()
    print()
    test_config_surfacing_sqlite()
    print()
    test_config_surfacing_legacy()
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
