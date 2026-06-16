"""cron_jobs collector — SQLite-aware (2026.6.x+) with legacy-JSON fallback.

Data source priority:
  1. ~/.openclaw/state/openclaw.sqlite — read-only — tables ``cron_jobs``
     (job definition + run-time state) and ``cron_run_logs`` (per-run
     entries). When present and non-empty, this is the authoritative source.
  2. Legacy ``cron/jobs.json`` + ``cron/jobs-state.json`` + ``cron/runs/``
     — used when the SQLite store is missing or has no cron_jobs rows.

When BOTH the SQLite store has rows AND ``cron/jobs.json`` still parses
into jobs, we read from SQLite but emit ``cron.source_inconsistency`` to
warn about a potentially incomplete migration (OpenClaw issue #90072 —
job silently lost when migrator ran twice or partially).

All SQLite operations are read-only (``mode=ro`` URI), wrapped in
try/except, and degrade to the legacy path on any failure. The collector
must NEVER crash on an unreachable / corrupt DB.
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter, deque
from typing import Any, Dict, List, Optional, Tuple

from .. import paths, trajectory
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict
from ..sensitive import sanitize_text
from ..timeutil import fmt_age, fmt_ts, window_token
from ..tokens import fmt_tokens, percentile

try:
    from croniter import croniter  # type: ignore
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False


def _fmt_duration(ms):
    if ms is None:
        return "?"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s/60:.1f}min"
    return f"{s/3600:.1f}h"


def _format_schedule(sched) -> str:
    k = sched.get("kind", "?")
    if k == "cron":
        return f"cron {sched.get('expr','?')} (tz={sched.get('tz','local')})"
    if k == "every":
        return f"every {sched.get('everyMs',0)/1000:.0f}s"
    if k == "at":
        return f"at {sched.get('at','?')}"
    return str(sched)[:100]


def _expected_interval_ms(sched, runs):
    k = sched.get("kind")
    if k == "every":
        return sched.get("everyMs")
    if k == "cron" and HAS_CRONITER:
        try:
            base = _dt.datetime.now()
            it = croniter(sched["expr"], base)
            t1 = it.get_next(_dt.datetime)
            t2 = it.get_next(_dt.datetime)
            return int((t2 - t1).total_seconds() * 1000)
        except Exception:
            pass
    if runs and len(runs) >= 3:
        ts_list = sorted([
            r.get("runAtMs") or r.get("ts") for r in runs
            if (r.get("runAtMs") or r.get("ts"))
        ])
        if len(ts_list) >= 3:
            gaps = sorted(
                ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)
            )
            return gaps[len(gaps) // 2]
    return None


def _load_runs(runs_dir, jid) -> List[dict]:
    if not jid or not runs_dir:
        return []
    p = os.path.join(runs_dir, f"{jid}.jsonl")
    if not os.path.isfile(p):
        return []
    buf: deque = deque(maxlen=200)
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    buf.append(line)
    except OSError:
        return []
    out = []
    for line in buf:
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            pass
    return out


# ─── SQLite-backed cron store (2026.6.x+) ─────────────────────────────
#
# Schema notes (from real openclaw.sqlite):
#  - cron_jobs.store_key is the resolved abs-path of the legacy jobs.json
#    file the runtime owns (e.g. /root/.openclaw/cron/jobs.json), NOT a
#    table or alias.
#  - cron_jobs.job_json is the FULL job definition; but its embedded
#    ``state`` field is always emptied to ``{}`` — runtime state is split
#    out into the sibling ``state_json`` column.
#  - cron_run_logs.entry_json round-trips to the legacy
#    ``runs/<jobId>.jsonl`` per-line shape (ts/status/summary/delivered/
#    deliveryStatus/sessionId/durationMs/runAtMs/nextRunAtMs/usage/...),
#    so the existing _analyze() consumer takes them verbatim.

# Cap how many run entries we pull per job, mirroring the legacy
# _load_runs() maxlen=200 — keeps memory + analysis bounded for hot jobs.
_RUN_LIMIT_PER_JOB = 200


def _open_state_db(path: str) -> Optional[sqlite3.Connection]:
    """Open the shared OpenClaw SQLite store read-only.

    Mirrors task_health._open_db: file:URI ``mode=ro`` so we cannot lock
    or mutate the live DB; short timeout; row_factory for dict-style
    access. Returns None on any error (missing file, sqlite_open failure,
    permission denied) — callers fall back to legacy JSON.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _resolved(p: str) -> str:
    """Normalize a path for store_key comparison (realpath ⇢ abspath)."""
    if not p:
        return ""
    try:
        return os.path.realpath(os.path.abspath(p))
    except OSError:
        return os.path.abspath(p)


def _fetch_sqlite_jobs(
    conn: sqlite3.Connection, expected_store_key: str,
) -> Tuple[List[dict], List[str]]:
    """Read job rows, returning ``(jobs, store_keys_seen)``.

    Strategy: prefer rows whose ``store_key`` resolves to the same path as
    ``paths.CRON_JOBS``; if none match (common in test setups or after a
    user moved $OPENCLAW_HOME), fall back to all rows and let the caller
    surface the store_key list as evidence.

    Each returned job dict is the deserialized ``job_json`` with
    ``state`` re-populated from ``state_json`` (since job_json's embedded
    state is always empty — see module docstring).
    """
    try:
        cur = conn.execute(
            "SELECT store_key, job_id, job_json, state_json FROM cron_jobs",
        )
        rows = cur.fetchall()
    except sqlite3.Error:
        return [], []

    keys_seen: List[str] = []
    matched: List[sqlite3.Row] = []
    others: List[sqlite3.Row] = []
    for r in rows:
        sk = r["store_key"] or ""
        if sk and sk not in keys_seen:
            keys_seen.append(sk)
        if expected_store_key and _resolved(sk) == expected_store_key:
            matched.append(r)
        else:
            others.append(r)

    chosen = matched if matched else others

    jobs: List[dict] = []
    for r in chosen:
        # Defensive parse — a corrupted blob must not crash the collector.
        job: Optional[dict] = None
        try:
            raw = r["job_json"]
            if raw:
                job = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            job = None
        if not isinstance(job, dict):
            # Synthesize a stub so the row is still surfaced; the
            # downstream _analyze() tolerates missing fields.
            job = {"id": r["job_id"]}

        # job_id column is the source of truth for the row identity; the
        # job_json `id` field should match but we trust the column.
        if r["job_id"] and not job.get("id"):
            job["id"] = r["job_id"]

        # Rehydrate runtime state from state_json (job_json's embedded
        # state is always {} per OpenClaw migration design).
        state_obj: Dict[str, Any] = {}
        try:
            sj_raw = r["state_json"]
            if sj_raw:
                parsed = json.loads(sj_raw)
                if isinstance(parsed, dict):
                    state_obj = parsed
        except (json.JSONDecodeError, ValueError, TypeError):
            state_obj = {}
        if state_obj:
            job["state"] = state_obj

        jobs.append(job)

    return jobs, keys_seen


def _fetch_sqlite_runs(
    conn: sqlite3.Connection, store_key_filter: Optional[str], jid: str,
) -> List[dict]:
    """Read per-job run entries from cron_run_logs.

    Returns the deserialized ``entry_json`` list, ordered by ts (then
    seq) ascending — same shape as the legacy ``runs/<jobId>.jsonl``
    consumer in _analyze() expects.

    ``store_key_filter`` is the resolved expected path. We pass it as a
    SQL filter when known, but if it doesn't match anything we retry
    without the filter (a stray store_key shouldn't hide runs).
    """
    if not jid:
        return []

    # ORDER BY DESC + LIMIT N picks the *most recent* N rows; we then
    # reverse to ts-ascending in Python so _analyze() (which slices
    # `finished[-20:]`) sees real recent runs. ASC + LIMIT would keep the
    # oldest N — divergent from the legacy deque(maxlen=200) semantics
    # and silently wrong once a job's log exceeds _RUN_LIMIT_PER_JOB.
    def _query(with_filter: bool) -> List[sqlite3.Row]:
        try:
            if with_filter and store_key_filter:
                cur = conn.execute(
                    "SELECT entry_json, ts, seq FROM cron_run_logs "
                    "WHERE job_id=? AND store_key=? "
                    "ORDER BY COALESCE(ts, 0) DESC, seq DESC "
                    "LIMIT ?",
                    (jid, store_key_filter, _RUN_LIMIT_PER_JOB),
                )
            else:
                cur = conn.execute(
                    "SELECT entry_json, ts, seq FROM cron_run_logs "
                    "WHERE job_id=? "
                    "ORDER BY COALESCE(ts, 0) DESC, seq DESC "
                    "LIMIT ?",
                    (jid, _RUN_LIMIT_PER_JOB),
                )
            return cur.fetchall()
        except sqlite3.Error:
            return []

    rows = _query(with_filter=True)
    if not rows:
        rows = _query(with_filter=False)

    out: List[dict] = []
    for r in rows:
        try:
            entry = json.loads(r["entry_json"]) if r["entry_json"] else None
        except (json.JSONDecodeError, ValueError, TypeError):
            entry = None
        if isinstance(entry, dict):
            out.append(entry)
    out.reverse()
    return out


def _read_legacy_jobs(jobs_file: str) -> Optional[List[dict]]:
    """Parse legacy jobs.json. Returns None on missing / parse failure,
    [] on parsable-but-empty, otherwise the job list. Used both as the
    primary fallback and as the inconsistency probe alongside SQLite.
    """
    if not jobs_file or not os.path.isfile(jobs_file):
        return None
    try:
        with open(jobs_file) as f:
            jdata = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(jdata, dict):
        jobs = jdata.get("jobs", [])
        if isinstance(jobs, dict):
            return list(jobs.values())
        if isinstance(jobs, list):
            return jobs
        return []
    if isinstance(jdata, list):
        return jdata
    return []


def _extract_usage(r):
    u = r.get("usage")
    if not u and isinstance(r.get("result"), dict):
        u = r["result"].get("usage")
    if not isinstance(u, dict):
        return None, None, None
    inp = u.get("input") or u.get("input_tokens")
    out = u.get("output") or u.get("output_tokens")
    cost = None
    c = u.get("cost")
    if isinstance(c, dict):
        cost = c.get("total")
    elif isinstance(c, (int, float)):
        cost = c
    return inp, out, cost


def _extract_error_text(r) -> str:
    err = r.get("error") or r.get("errorMessage")
    if not err and isinstance(r.get("result"), dict):
        err = r["result"].get("error")
    if isinstance(err, dict):
        err = err.get("message") or json.dumps(err, ensure_ascii=False)
    if not err:
        return ""
    return re.sub(r"\s+", " ", str(err))[:100]


def _extract_delivery_reason(r) -> str:
    reason = r.get("deliveryError")
    if not reason:
        dlv = r.get("delivery")
        if isinstance(dlv, dict):
            res = dlv.get("resolved")
            if isinstance(res, dict):
                reason = res.get("error")
    if isinstance(reason, dict):
        reason = (
            reason.get("message") or json.dumps(reason, ensure_ascii=False)
        )
    if not reason:
        return ""
    return re.sub(r"\s+", " ", str(reason))[:100]


def _analyze(job, runs, now_ms) -> dict:
    state = job.get("state", {}) or {}
    enabled = job.get("enabled", True)

    finished = [
        r for r in runs if r.get("status") or r.get("action") == "finished"
    ]
    recent = finished[-20:]

    ok = sum(1 for r in recent if r.get("status") == "ok")
    fail = len(recent) - ok
    success_rate = (ok / len(recent) * 100) if recent else None

    durs = sorted(
        [r["durationMs"] for r in recent if r.get("durationMs") is not None],
    )
    p50 = percentile(durs, 0.5)
    p95 = percentile(durs, 0.95)
    dur_max = durs[-1] if durs else None
    last_dur = recent[-1].get("durationMs") if recent else None

    deliv = Counter(
        r.get("deliveryStatus") for r in recent if r.get("deliveryStatus")
    )
    deliv_total = sum(deliv.values())
    deliv_ok = deliv.get("delivered", 0)
    deliv_unrequested = deliv.get("not-requested", 0)
    deliv_effective = deliv_total - deliv_unrequested
    deliv_fail_rate = None
    if deliv_effective > 0:
        deliv_fail_rate = (deliv_effective - deliv_ok) / deliv_effective * 100

    exp_interval = _expected_interval_ms(job.get("schedule", {}), runs)

    flags: list = []
    if not enabled:
        return dict(
            status="disabled", flags=flags, recent=recent,
            success_rate=success_rate, ok=ok, fail=fail,
            p50=p50, p95=p95, dur_max=dur_max, last_dur=last_dur,
            deliv_ok=deliv_ok, deliv_total=deliv_total,
            deliv_effective=deliv_effective, deliv_fail_rate=deliv_fail_rate,
            exp_interval=exp_interval, consecutive_errors=0,
        )

    cerr = state.get("consecutiveErrors", 0) or 0
    last_err = job.get("lastError") or state.get("lastError") or ""
    if cerr >= 3:
        le = re.sub(r"\s+", " ", str(last_err))[:100] if last_err else ""
        flags.append((
            "error",
            f"连续失败 {cerr} 次" + (f"（最近错误: {le}）" if le else ""),
        ))
    elif cerr >= 1:
        flags.append(("note", f"最近失败 {cerr} 次"))

    if (success_rate is not None and len(recent) >= 5
            and success_rate < 80):
        flags.append((
            "error",
            f"成功率 {success_rate:.0f}%（最近 {len(recent)} 次）",
        ))

    next_run = state.get("nextRunAtMs")
    if next_run and exp_interval:
        drift = now_ms - next_run
        if drift > 2 * exp_interval:
            flags.append((
                "error",
                f"调度卡住：nextRun 已过期 {fmt_age(drift)}"
                f"（预期间隔 {fmt_age(exp_interval)}）",
            ))

    if last_dur and p95 and len(durs) >= 5 and last_dur > p95 * 2:
        flags.append((
            "note",
            f"最近耗时 {_fmt_duration(last_dur)} 超历史 P95 "
            f"({_fmt_duration(p95)}) 两倍",
        ))

    if (deliv_effective >= 5 and deliv_fail_rate is not None
            and deliv_fail_rate > 20):
        flags.append((
            "error",
            f"投递失败率 {deliv_fail_rate:.0f}%"
            f"（{deliv_effective - deliv_ok}/{deliv_effective}）",
        ))

    created = job.get("createdAtMs", 0) or 0
    age_since_create = now_ms - created if created else 0
    last_run = state.get("lastRunAtMs")
    is_silent = False
    if age_since_create > 3600 * 1000:
        if not last_run and not runs:
            flags.append(("silent", "任务已创建但从未执行"))
            is_silent = True
        elif exp_interval and last_run:
            idle = now_ms - last_run
            if idle > 2 * exp_interval:
                flags.append((
                    "silent",
                    f"已 {fmt_age(idle)} 未执行"
                    f"（预期间隔 {fmt_age(exp_interval)}）",
                ))
                is_silent = True

    if is_silent:
        status = "silent"
    elif any(f[0] == "error" for f in flags):
        status = "warn"
    else:
        status = "ok"

    return dict(
        status=status, flags=flags, recent=recent,
        success_rate=success_rate, ok=ok, fail=fail,
        p50=p50, p95=p95, dur_max=dur_max, last_dur=last_dur,
        deliv_ok=deliv_ok, deliv_total=deliv_total,
        deliv_effective=deliv_effective, deliv_fail_rate=deliv_fail_rate,
        exp_interval=exp_interval, consecutive_errors=cerr,
    )


def _section_jobs(s: Section, db_path: str, jobs_file: str, state_file: str,
                  runs_dir: str) -> dict:
    """Top-level dispatcher: pick SQLite vs legacy JSON, run analysis.

    Source priority (see module docstring for full reasoning):
      1. SQLite (~/.openclaw/state/openclaw.sqlite, table cron_jobs)
      2. Legacy jobs.json + jobs-state.json + runs/

    When SQLite has rows AND legacy jobs.json still parses with jobs,
    SQLite wins but we ALSO emit a ``cron.source_inconsistency`` warn
    (OpenClaw issue #90072: incomplete migration may silently drop jobs).
    """
    data: dict = {}

    expected_key = _resolved(jobs_file)
    sqlite_jobs: List[dict] = []
    sqlite_keys: List[str] = []

    conn = _open_state_db(db_path)
    if conn is not None:
        try:
            if _table_exists(conn, "cron_jobs"):
                try:
                    sqlite_jobs, sqlite_keys = _fetch_sqlite_jobs(
                        conn, expected_key,
                    )
                except sqlite3.Error:
                    sqlite_jobs, sqlite_keys = [], []
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    legacy_jobs = _read_legacy_jobs(jobs_file)
    legacy_count = len(legacy_jobs) if isinstance(legacy_jobs, list) else 0

    if sqlite_jobs:
        # SQLite wins. If legacy JSON still has jobs, surface
        # inconsistency for #90072 debugging.
        source = "both" if legacy_count > 0 else "sqlite"
        data["source"] = source
        data["sqlite_job_count"] = len(sqlite_jobs)
        data["legacy_job_count"] = legacy_count
        if sqlite_keys:
            data["store_keys"] = sqlite_keys

        if source == "both":
            s.warn(
                "cron.source_inconsistency",
                "检测到 legacy JSON 与 SQLite cron 存储并存，"
                "可能迁移不完整或丢失（参考 OpenClaw issue #90072），"
                "建议核对两边 job 数量",
                evidence=(
                    f"sqlite_job_count={len(sqlite_jobs)} "
                    f"legacy_job_count={legacy_count} "
                    f"db={db_path} jobs_json={jobs_file}"
                ),
                data={
                    "sqlite_job_count": len(sqlite_jobs),
                    "legacy_job_count": legacy_count,
                    "db_path": db_path,
                    "jobs_file": jobs_file,
                },
            )

        if expected_key and sqlite_keys and not any(
            _resolved(k) == expected_key for k in sqlite_keys
        ):
            s.warn(
                "cron.store_key_mismatch",
                "SQLite cron_jobs.store_key 与期望路径不匹配 — "
                "已读取所有行；如有歧义请用环境变量校准 OPENCLAW_CRON_JOBS",
                evidence=(
                    f"expected={expected_key} "
                    f"seen={sqlite_keys}"
                ),
                data={
                    "expected_store_key": expected_key,
                    "store_keys_seen": sqlite_keys,
                },
            )

        return _analyze_jobs_section(
            s, sqlite_jobs, data,
            run_loader=lambda jid: _load_sqlite_runs_for(
                db_path, expected_key, jid,
            ),
            source_label=source,
        )

    # ── fallback: legacy JSON path ──
    if legacy_jobs is None and not os.path.isfile(jobs_file):
        # Neither SQLite nor JSON — no cron data on this host.
        s.ok(
            "cron.jobs",
            "未发现 cron 数据 — SQLite/JSON 两侧均无任务",
            data={"found": False, "source": "none"},
        )
        data["source"] = "none"
        return data

    if legacy_jobs is None:
        # jobs.json exists but failed to parse — preserve old warn shape.
        s.warn(
            "cron.jobs",
            "jobs.json 解析失败",
            data={"found": True, "parse_error": True, "source": "legacy-json"},
        )
        data["source"] = "legacy-json"
        return data

    if not legacy_jobs:
        s.ok(
            "cron.jobs",
            "jobs.json 存在但无任务",
            data={"total_jobs": 0, "source": "legacy-json"},
        )
        data["source"] = "legacy-json"
        return data

    # Hydrate runtime state from jobs-state.json.
    ext_state: dict = {}
    if state_file and os.path.isfile(state_file):
        try:
            with open(state_file) as f:
                sd = json.load(f)
            ext_jobs = sd.get("jobs", {}) if isinstance(sd, dict) else {}
            for jid, entry in ext_jobs.items():
                if isinstance(entry, dict):
                    ext_state[jid] = entry.get("state", {}) or {}
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    for j in legacy_jobs:
        jid = j.get("id")
        if jid and not j.get("state") and jid in ext_state:
            j["state"] = ext_state[jid]

    data["source"] = "legacy-json"
    data["legacy_job_count"] = len(legacy_jobs)
    return _analyze_jobs_section(
        s, legacy_jobs, data,
        run_loader=lambda jid: _load_runs(runs_dir, jid),
        source_label="legacy-json",
    )


def _load_sqlite_runs_for(
    db_path: str, expected_key: str, jid: str,
) -> List[dict]:
    """Per-job SQLite run lookup. Re-opens the DB per job because
    _section_jobs' dispatcher closes the connection before delegating —
    cheap (read-only file URI) and keeps the lifetime short. If the DB
    becomes unavailable mid-collect the loader simply returns []."""
    conn = _open_state_db(db_path)
    if conn is None:
        return []
    try:
        if not _table_exists(conn, "cron_run_logs"):
            return []
        return _fetch_sqlite_runs(conn, expected_key, jid)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ─── Job configuration surfacing ──────────────────────────────────────
#
# OpenClaw 2026.6.x moved cron config into SQLite, so users can no longer
# `cat jobs.json` to see the full configuration. The collector now
# surfaces every config field (payload model/message/timeout/tools,
# delivery channel/to/thread, sessionTarget, wakeMode, …) in the JSON
# envelope and a compact pretty block, so a `cron_jobs --json | jq` or
# human inspection is enough to recover the full config view.

# Cap the message preview shown in pretty detail. The full (sanitized)
# text still lives in the JSON envelope under config.payload.message.
_MSG_PREVIEW_CHARS = 200


def _drop_empty(d: dict) -> dict:
    """Drop keys whose value is None or an empty container.

    Keeps booleans (incl. False) and numeric zeros — those are real
    config values. Used so the JSON envelope stays clean instead of
    flooding consumers with a sea of nulls.
    """
    return {
        k: v for k, v in d.items()
        if v is not None and v != "" and v != [] and v != {}
    }


def _preview_one_line(text: str, limit: int = _MSG_PREVIEW_CHARS) -> str:
    """Collapse whitespace, sanitize, then truncate for one-line display."""
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", str(text)).strip()
    flat = sanitize_text(flat, context="generic")
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def _build_job_config(j: dict) -> dict:
    """Build the JSON-envelope `config` sub-object for one job.

    Mirrors the legacy jobs.json shape (which is identical to the
    SQLite job_json blob), with two differences: keys are snake_case
    (collector convention) and free-form text fields go through
    sanitize_text so embedded tokens / API keys are masked.
    """
    sched = j.get("schedule") or {}
    payload = j.get("payload") or {}
    delivery = j.get("delivery") or {}

    msg_raw = payload.get("message")
    msg_clean: Any = None
    msg_len: Any = None
    if isinstance(msg_raw, str) and msg_raw:
        msg_len = len(msg_raw)
        msg_clean = sanitize_text(msg_raw, context="generic")

    text_raw = payload.get("text")
    text_clean: Any = None
    if isinstance(text_raw, str) and text_raw:
        text_clean = sanitize_text(text_raw, context="generic")

    payload_out = _drop_empty({
        "kind": payload.get("kind"),
        "model": payload.get("model"),
        "fallbacks": payload.get("fallbacks"),
        "thinking": payload.get("thinking"),
        "timeout_seconds": payload.get("timeoutSeconds"),
        "tools_allow": payload.get("toolsAllow"),
        "allow_unsafe_external_content":
            payload.get("allowUnsafeExternalContent"),
        "light_context": payload.get("lightContext"),
        "message": msg_clean,
        "message_len": msg_len,
        "text": text_clean,
    })

    delivery_out = _drop_empty({
        "mode": delivery.get("mode"),
        "channel": delivery.get("channel"),
        "to": delivery.get("to"),
        "thread_id": delivery.get("threadId"),
        "account_id": delivery.get("accountId"),
        "best_effort": delivery.get("bestEffort"),
        "completion_mode": delivery.get("completionMode"),
        "completion_to": delivery.get("completionTo"),
    })

    return _drop_empty({
        "enabled": j.get("enabled", True),
        "agent_id": j.get("agentId"),
        "session_key": j.get("sessionKey"),
        "session_target": j.get("sessionTarget"),
        "wake_mode": j.get("wakeMode"),
        "description": j.get("description"),
        "delete_after_run": j.get("deleteAfterRun"),
        "created_at_ms": j.get("createdAtMs"),
        "schedule": dict(sched) if sched else {},
        "payload": payload_out,
        "delivery": delivery_out,
    })


def _detail_config_lines(j: dict) -> List[str]:
    """Return compact 配置 block lines (4-space indent), skipping empties."""
    payload = j.get("payload") or {}
    delivery = j.get("delivery") or {}

    out: List[str] = ["    配置:"]

    head_bits = []
    head_bits.append(
        f"启用: {'是' if j.get('enabled', True) else '否'}",
    )
    if j.get("sessionTarget"):
        head_bits.append(f"sessionTarget: {j['sessionTarget']}")
    if j.get("wakeMode"):
        head_bits.append(f"wakeMode: {j['wakeMode']}")
    if j.get("agentId"):
        head_bits.append(f"agentId: {j['agentId']}")
    if j.get("sessionKey"):
        head_bits.append(f"sessionKey: {j['sessionKey']}")
    if j.get("deleteAfterRun"):
        head_bits.append("deleteAfterRun: 是")
    out.append("      " + " | ".join(head_bits))

    if j.get("description"):
        desc = re.sub(r"\s+", " ", str(j["description"])).strip()
        if len(desc) > 120:
            desc = desc[:120] + "…"
        out.append(f"      description: {desc}")

    if payload:
        bits = []
        kind = payload.get("kind")
        if kind:
            bits.append(str(kind))
        if payload.get("model"):
            bits.append(f"model={payload['model']}")
        fb = payload.get("fallbacks")
        if isinstance(fb, list) and fb:
            bits.append(f"fallbacks={','.join(str(x) for x in fb)}")
        if payload.get("timeoutSeconds") is not None:
            bits.append(f"timeout={payload['timeoutSeconds']}s")
        if payload.get("thinking") is not None:
            bits.append(f"thinking={payload['thinking']}")
        tools = payload.get("toolsAllow")
        if isinstance(tools, list) and tools:
            bits.append(f"tools=[{','.join(str(t) for t in tools)}]")
        if payload.get("allowUnsafeExternalContent"):
            bits.append("allowUnsafeExternalContent=true")
        if payload.get("lightContext"):
            bits.append("lightContext=true")
        if bits:
            out.append("      payload: " + " | ".join(bits))

    if delivery:
        bits = []
        if delivery.get("mode"):
            bits.append(str(delivery["mode"]))
        chan = delivery.get("channel")
        to = delivery.get("to")
        thread = delivery.get("threadId")
        target = " → ".join(
            x for x in [chan, to] if x
        ) if (chan or to) else ""
        if target:
            bits.append(target)
        if thread:
            bits.append(f"thread={thread}")
        if delivery.get("accountId"):
            bits.append(f"account={delivery['accountId']}")
        if delivery.get("bestEffort"):
            bits.append("bestEffort=true")
        if delivery.get("completionMode"):
            bits.append(f"completionMode={delivery['completionMode']}")
        if delivery.get("completionTo"):
            bits.append(f"completionTo={delivery['completionTo']}")
        if bits:
            out.append("      delivery: " + " | ".join(bits))

    msg = payload.get("message")
    if isinstance(msg, str) and msg:
        out.append(
            f"      message({len(msg)} 字): {_preview_one_line(msg)}",
        )
    txt = payload.get("text")
    if isinstance(txt, str) and txt:
        out.append(
            f"      text({len(txt)} 字): {_preview_one_line(txt)}",
        )

    return out


def _analyze_jobs_section(
    s: Section,
    jobs: List[dict],
    data: dict,
    run_loader,
    source_label: str,
) -> dict:
    """Shared per-job analysis + check emission (was the body of
    _section_jobs; kept verbatim except parameterized run lookup +
    source-aware overview message)."""
    now_ms = int(time.time() * 1000)
    analyses = []
    for j in jobs:
        runs = run_loader(j.get("id"))
        analyses.append((j, runs, _analyze(j, runs, now_ms)))

    total = len(jobs)
    enabled_count = sum(1 for j in jobs if j.get("enabled", True))
    disabled_count = total - enabled_count

    ok_list = [a for a in analyses if a[2]["status"] == "ok"]
    warn_list = [a for a in analyses if a[2]["status"] == "warn"]
    silent_list = [a for a in analyses if a[2]["status"] == "silent"]
    disabled_list = [a for a in analyses if a[2]["status"] == "disabled"]

    def _job_name(j):
        return j.get("name") or j.get("id", "?")

    data["total_jobs"] = total
    data["enabled_count"] = enabled_count
    data["disabled_count"] = disabled_count
    data["status_overview"] = {
        "ok": [_job_name(j) for j, _, _ in ok_list],
        "warn": [_job_name(j) for j, _, _ in warn_list],
        "silent": [_job_name(j) for j, _, _ in silent_list],
        "disabled": [_job_name(j) for j, _, _ in disabled_list],
    }

    jobs_payload = []
    for j, runs, a in analyses:
        state = j.get("state", {}) or {}
        jobs_payload.append({
            "id": j.get("id"),
            "name": j.get("name") or j.get("id"),
            "status": a["status"],
            "schedule": j.get("schedule", {}),
            "success_rate": a["success_rate"],
            "p50_ms": a["p50"],
            "p95_ms": a["p95"],
            "last_run_ts": state.get("lastRunAtMs"),
            "next_run_ts": state.get("nextRunAtMs"),
            "consecutive_errors": state.get("consecutiveErrors", 0) or 0,
            "flags": [{"kind": k, "msg": m} for k, m in a["flags"]],
            "config": _build_job_config(j),
        })
    data["jobs"] = jobs_payload

    overview_lines = [
        f"共 {total} 个任务（{enabled_count} 启用, {disabled_count} 禁用）",
    ]
    if ok_list:
        overview_lines.append(f"正常: {len(ok_list)} 个")
    if warn_list:
        overview_lines.append(f"异常: {len(warn_list)} 个")
        for j, _, a in warn_list:
            nm = _job_name(j)
            msg = next((f[1] for f in a["flags"] if f[0] == "error"), "")
            overview_lines.append(f"  · {nm}: {msg}")
    if silent_list:
        overview_lines.append(f"静默: {len(silent_list)} 个")
        for j, _, a in silent_list:
            nm = _job_name(j)
            msg = next((f[1] for f in a["flags"] if f[0] == "silent"), "")
            overview_lines.append(f"  · {nm}: {msg}")
    if disabled_list:
        overview_lines.append(f"禁用: {len(disabled_list)} 个")
    source_hint = {
        "sqlite": "数据源: SQLite (state/openclaw.sqlite)",
        "both": "数据源: SQLite (检测到 legacy JSON 并存)",
        "legacy-json": "数据源: legacy cron/jobs.json",
    }.get(source_label, f"数据源: {source_label}")
    overview_lines.insert(0, source_hint)
    summary_msg = (
        f"任务总览: {total} 个（{enabled_count} 启用, "
        f"{disabled_count} 禁用 / {len(warn_list)} 异常, "
        f"{len(silent_list)} 静默） [{source_label}]"
    )
    s.ok(
        "cron.overview",
        summary_msg,
        detail="\n".join(overview_lines),
        data={
            "total": total, "enabled": enabled_count,
            "disabled": disabled_count,
            "warn": len(warn_list), "silent": len(silent_list),
            "source": source_label,
        },
    )

    detail_lines = []
    max_consec = 0
    for idx, (j, runs, a) in enumerate(analyses, 1):
        nm = _job_name(j)
        status = a["status"]
        icon_label = {
            "ok": "正常", "warn": "异常",
            "silent": "静默", "disabled": "禁用",
        }.get(status, "?")
        detail_lines.append(f"[{idx}] {nm} ({icon_label})")
        detail_lines.append(
            f"    调度: {_format_schedule(j.get('schedule', {}))}",
        )
        state = j.get("state", {}) or {}
        last_run = state.get("lastRunAtMs")
        if last_run:
            ls = state.get("lastStatus") or state.get("lastRunStatus") or "?"
            ld = state.get("lastDurationMs")
            line = f"    上次执行: {fmt_ts(last_run)} | {ls}"
            if ld is not None:
                line += f" | {_fmt_duration(ld)}"
            detail_lines.append(line)
        else:
            detail_lines.append("    上次执行: 从未执行")
        nr = state.get("nextRunAtMs")
        if nr:
            delta = nr - now_ms
            if delta >= 0:
                detail_lines.append(
                    f"    下次执行: {fmt_ts(nr)} (在 {fmt_age(delta)}后)",
                )
            else:
                detail_lines.append(
                    f"    下次执行: {fmt_ts(nr)} (已过期 {fmt_age(delta)})",
                )
        if a["success_rate"] is not None:
            n = a["ok"] + a["fail"]
            detail_lines.append(
                f"    成功率: {a['success_rate']:.0f}% "
                f"(最近 {n} 次: ok={a['ok']} fail={a['fail']})",
            )
        if a["p50"] is not None:
            parts = [f"P50={_fmt_duration(a['p50'])}"]
            if a["p95"] is not None and a["p95"] != a["p50"]:
                parts.append(f"P95={_fmt_duration(a['p95'])}")
            if a["dur_max"] is not None and a["dur_max"] != a["p50"]:
                parts.append(f"Max={_fmt_duration(a['dur_max'])}")
            detail_lines.append("    耗时: " + " ".join(parts))
        detail_lines.extend(_detail_config_lines(j))
        cerr = a.get("consecutive_errors", 0) or 0
        if cerr > max_consec:
            max_consec = cerr
        detail_lines.append("")
    detail = "\n".join(detail_lines)
    data["max_consecutive_errors"] = max_consec

    if max_consec >= 10:
        s.fail(
            "cron.consecutive_failures",
            f"任务连续失败次数最大为 {max_consec}（>=10）",
            detail=detail,
            data={"max_consecutive_errors": max_consec},
        )
    elif max_consec >= 3:
        s.warn(
            "cron.consecutive_failures",
            f"任务连续失败次数最大为 {max_consec}（>=3）",
            detail=detail,
            data={"max_consecutive_errors": max_consec},
        )
    else:
        s.ok(
            "cron.consecutive_failures",
            f"任务连续失败次数最大为 {max_consec}",
            detail=detail,
            data={"max_consecutive_errors": max_consec},
        )

    if silent_list:
        silent_names = ", ".join(_job_name(j) for j, _, _ in silent_list)
        s.warn(
            "cron.silent",
            f"静默任务: {len(silent_list)} 个未按预期执行",
            evidence=silent_names,
            data={"count": len(silent_list)},
        )
    else:
        s.ok(
            "cron.silent",
            "静默任务: 0",
            data={"count": 0},
        )
    return data


def _section_heartbeat(s: Section, ctx: DiagContext) -> dict:
    data: dict = {"agents": {}}

    cfg = ctx.config or {}
    hb_every = "未配置"
    hb = cfg.get("agents", {}).get("defaults", {}).get("heartbeat", {})
    if isinstance(hb, dict):
        hb_every = hb.get("every", "未配置")
    data["config_every"] = hb_every

    sessions_base = str(ctx.sessions_base)
    agent_workspaces: dict = {}
    for a in cfg.get("agents", {}).get("list", []) or []:
        if isinstance(a, dict) and a.get("id"):
            agent_workspaces[a["id"]] = a.get("workspace", "")

    agent_lines = [f"配置: agents.defaults.heartbeat.every = {hb_every}"]
    if os.path.isdir(sessions_base):
        for agent_dir in sorted(glob.glob(os.path.join(sessions_base, "*"))):
            agent_id = os.path.basename(agent_dir)
            ws_dir = agent_workspaces.get(agent_id, "")
            hb_file = os.path.join(ws_dir, "HEARTBEAT.md") if ws_dir else ""
            if ws_dir and os.path.isfile(hb_file):
                try:
                    with open(hb_file) as f:
                        content = f.read()
                    nonempty = [
                        ln for ln in content.splitlines()
                        if ln.strip()
                        and not ln.startswith(("#", "```", "<!--"))
                    ]
                except OSError:
                    nonempty = []
                if not nonempty:
                    agent_lines.append(
                        f"{agent_id}: HEARTBEAT.md 存在但为空（不会触发）",
                    )
                    data["agents"][agent_id] = {"heartbeat_md": "empty"}
                else:
                    agent_lines.append(
                        f"{agent_id}: HEARTBEAT.md 有内容（会触发）",
                    )
                    data["agents"][agent_id] = {"heartbeat_md": "active"}
            else:
                agent_lines.append(f"{agent_id}: HEARTBEAT.md 不存在")
                data["agents"][agent_id] = {"heartbeat_md": "missing"}

    log_pattern = os.path.join(str(ctx.log_dir), "openclaw-*.log")
    interesting: List[str] = []
    started: List[tuple] = []
    for lf in sorted(glob.glob(log_pattern)):
        try:
            with open(lf, errors="replace") as f:
                for raw in f:
                    if "gateway/heartbeat" not in raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    ts = d.get("time", "?")[:19]
                    interval = ""
                    if isinstance(d.get("1"), dict):
                        ms = d["1"].get("intervalMs", 0)
                        interval = f"interval={ms/1000:.0f}s"
                        msg = str(d.get("2", ""))
                    else:
                        msg = str(d.get("1", ""))
                        if isinstance(d.get("2"), str):
                            msg += " | " + d["2"]
                    level = d.get("_meta", {}).get("logLevelName", "")
                    line = f"{ts} | {level} | {msg} {interval}".strip()
                    if "started" in msg:
                        started.append((ts, level, msg, interval))
                    else:
                        interesting.append(line)
        except OSError:
            continue

    data["events"] = len(interesting)
    data["started_count"] = len(started)
    if interesting:
        s.ok(
            "cron.heartbeat",
            f"Heartbeat 事件: {len(interesting)} 条 / 启动 {len(started)} 条",
            detail="\n".join(agent_lines + ["", "Recent events:"]
                             + interesting[:50]),
            data={"events": len(interesting), "started": len(started)},
        )
    elif started:
        intervals = sorted({s_[3] for s_ in started if s_[3]})
        data["intervals"] = list(intervals)
        s.ok(
            "cron.heartbeat",
            f"Heartbeat 调度器: {len(started)} 次启动，间隔 "
            f"{'、'.join(intervals)}",
            detail="\n".join(agent_lines),
            data={"started": len(started), "intervals": list(intervals)},
        )
    else:
        s.ok(
            "cron.heartbeat",
            "Heartbeat 日志: 0 条",
            detail="\n".join(agent_lines),
            data={"events": 0, "started": 0},
        )
    return data


def _section_system_crontab(s: Section) -> dict:
    data: dict = {}
    try:
        r = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True,
            timeout=5, check=False,
        )
        text = r.stdout if r.returncode == 0 else r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        text = ""
    if not text or "no crontab" in text.lower():
        s.ok(
            "cron.system_crontab",
            "系统 crontab: 无（未配置）",
            data={"entries": []},
        )
        data["system_crontab"] = []
        return data
    entries = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    data["system_crontab"] = entries
    if entries:
        s.ok(
            "cron.system_crontab",
            f"系统 crontab: {len(entries)} 条",
            evidence="\n".join(entries),
            data={"entries": entries, "count": len(entries)},
        )
    else:
        s.ok(
            "cron.system_crontab",
            "系统 crontab: 仅注释",
            data={"entries": []},
        )
    return data


def _section_cron_trajectory(s: Section, ctx: DiagContext) -> dict:
    data: dict = {"runs_scanned_7d": 0}
    files = ctx.trajectory_files()
    if not files:
        s.ok(
            "trajectory.cron",
            "未发现 trajectory 文件 — 跳过 cron 投递审计",
            data={"found": False},
        )
        return data
    runs = ctx.collect_runs(
        since_ms=trajectory.ms_ago(7 * 86400 * 1000),
    )
    data["runs_scanned_7d"] = len(runs)
    cron_runs = [r for r in runs if r.trigger == "cron"]
    if not cron_runs:
        s.ok(
            "trajectory.cron",
            "最近 7d 无 cron-trigger run",
            data={"found": True, "cron_runs_7d": 0},
        )
    else:
        sent_ok = sum(1 for r in cron_runs if r.did_send_via_messaging_tool)
        final_success = sum(
            1 for r in cron_runs if r.final_status == "success"
        )
        final_error = sum(1 for r in cron_runs if r.final_status == "error")
        successful_adds = sum(r.successful_cron_adds for r in cron_runs)
        silent_runs = []
        for r in cron_runs:
            if r.final_status != "success":
                continue
            if r.did_send_via_messaging_tool:
                continue
            if r.successful_cron_adds > 0:
                continue
            text_len = sum(len(t) for t in r.assistant_texts)
            if text_len < 64:
                silent_runs.append((r, text_len))

        success_rate = (
            final_success / len(cron_runs) * 100 if cron_runs else 0.0
        )
        send_rate = sent_ok / len(cron_runs) * 100 if cron_runs else 0.0

        body = [
            f"7d cron run: {len(cron_runs)} 个 | success={final_success} "
            f"error={final_error} | did_send_via_messaging_tool={sent_ok} "
            f"({send_rate:.1f}%)",
            f"successful_cron_adds 总计: {successful_adds}",
        ]
        if silent_runs:
            body.append(
                f"静默 cron: {len(silent_runs)} 个（success+无投递+adds=0+短）",
            )
            for r, text_len in silent_runs[:5]:
                body.append(
                    f"  {r.session_id[:8]}#{r.run_id[:8]} "
                    f"text_len={text_len} "
                    f"targets={len(r.messaging_targets)}",
                )

        traj_data = {
            "found": True,
            "cron_runs_7d": len(cron_runs),
            "send_rate_pct": round(send_rate, 2),
            "success_rate_pct": round(success_rate, 2),
            "successful_cron_adds_7d": successful_adds,
            "silent_cron_runs": [
                {
                    "sessionId": r.session_id, "runId": r.run_id,
                    "started_ts_ms": r.started_ts_ms,
                    "text_len": text_len,
                    "messaging_targets": len(r.messaging_targets),
                }
                for r, text_len in silent_runs[:20]
            ],
        }
        data["trajectory_cron"] = traj_data

        if silent_runs:
            s.fail(
                "trajectory.silent_cron",
                f"静默 cron: {len(silent_runs)} 个 run",
                evidence="\n".join(body),
                data=traj_data,
            )
        elif final_success and success_rate < 95:
            s.warn(
                "trajectory.cron",
                f"7d cron 成功率 {success_rate:.1f}% < 95%",
                evidence="\n".join(body),
                data=traj_data,
            )
        else:
            s.ok(
                "trajectory.cron",
                f"7d cron run: {len(cron_runs)} 个 / "
                f"成功率 {success_rate:.1f}%",
                evidence="\n".join(body),
                data=traj_data,
            )

    by_trigger_send: dict = {}
    for r in runs:
        st = by_trigger_send.setdefault(r.trigger, {
            "total": 0, "did_send": 0, "non_empty_text": 0,
        })
        st["total"] += 1
        if r.did_send_via_messaging_tool:
            st["did_send"] += 1
        if any(t.strip() for t in r.assistant_texts):
            st["non_empty_text"] += 1

    da_lines = []
    for trig in sorted(
        by_trigger_send.keys(), key=lambda x: -by_trigger_send[x]["total"],
    ):
        st = by_trigger_send[trig]
        send_pct = (st["did_send"] / st["total"] * 100) if st["total"] else 0.0
        da_lines.append(
            f"{trig}: {st['total']} run | did_send="
            f"{st['did_send']} ({send_pct:.0f}%) | non_empty_text="
            f"{st['non_empty_text']}",
        )

    heartbeat_send = (
        by_trigger_send.get("heartbeat", {}).get("did_send", 0) or 0
    )
    data["trajectory_delivery_audit"] = by_trigger_send
    if heartbeat_send > 0:
        s.warn(
            "trajectory.delivery_audit",
            f"heartbeat 触发但 did_send=true {heartbeat_send} 次（异常）",
            evidence="\n".join(da_lines),
            data=by_trigger_send,
        )
    elif da_lines:
        s.ok(
            "trajectory.delivery_audit",
            f"Delivery audit (跨 trigger): {len(by_trigger_send)} 类 trigger",
            evidence="\n".join(da_lines),
            data=by_trigger_send,
        )
    return data


@register
class CronJobsCollector:
    id = "cron_jobs"
    title = "定时任务"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)
        report.add_scope("cron_store", "current")

        # We pull the legacy paths from $OPENCLAW_HOME (so test harnesses
        # that override only HOME/OPENCLAW_HOME continue to work) and the
        # SQLite path from paths.STATE_DB (which honors OPENCLAW_STATE_DB
        # and falls back to $OPENCLAW_HOME/state/openclaw.sqlite).
        home = str(ctx.openclaw_home)
        jobs_file = os.path.join(home, "cron", "jobs.json")
        state_file = os.path.join(home, "cron", "jobs-state.json")
        runs_dir = os.path.join(home, "cron", "runs")
        # Re-read STATE_DB so test runs that set OPENCLAW_STATE_DB or
        # OPENCLAW_HOME after import time pick up the override. paths.py
        # captures defaults at import; recomputing here keeps tests
        # hermetic (they spawn subprocesses anyway, but explicit is better).
        db_path = os.environ.get(
            "OPENCLAW_STATE_DB",
            os.path.join(home, "state", "openclaw.sqlite"),
        )

        s_jobs = report.section("6.1 任务列表")
        report.data.update(
            _section_jobs(s_jobs, db_path, jobs_file, state_file, runs_dir),
        )

        s_hb = report.section("6.2 Heartbeat")
        report.data.update(_section_heartbeat(s_hb, ctx))

        s_sys = report.section("6.3 系统 crontab")
        report.data.update(_section_system_crontab(s_sys))

        s_traj = report.section("6.4 Trajectory cron 审计 (7d)")
        traj_data = _section_cron_trajectory(s_traj, ctx)
        report.data.update(traj_data)
        runs_scanned_7d = traj_data.get("runs_scanned_7d", 0)
        report.add_scope(
            "trajectory", window_token(7 * 86400 * 1000),
            f"{runs_scanned_7d} runs",
        )

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
