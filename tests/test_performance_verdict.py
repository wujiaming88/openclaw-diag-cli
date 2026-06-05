"""Unit tests for performance collector verdict logic and daily-trend
sampling window.

Covers two recent fixes (v1.4.20):

1. Model verdict decouples latency from availability — fail must come from
   real availability problems (min success rate <90%), not from slow-but-
   healthy heavy models. Latency >60s is at most a warn.

2. daily_trend uses an independent 7-day mtime window instead of the same
   latest-20-files perf sample, so days with real activity that fall
   outside the perf window aren't silently reported as 0 calls.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag.collectors.performance import (  # noqa: E402
    _collect_session_files_by_window,
    _parse_daily_stats,
    _section_daily_trend,
    _section_models,
)
from ocdiag.core.types import Section, Verdict  # noqa: E402


def _model_data(p95_s, success_rate_pct, calls=50):
    """Build a model_stats fixture for _section_models input.

    The function reads `model_stats[k].calls`, `.durations`,
    `.stop_reasons`, plus token/cost fields. We only need to set what
    drives the verdict path.
    """
    # Build durations such that P95 == p95_s. Simplest: enough samples
    # of p95_s itself; pct() rounds to ceil-index, so we just put p95
    # value at the top of a sorted list.
    durs = [0.5] * 19 + [p95_s]
    # success_rate is computed as (normal_stops / calls) * 100 inside
    # _section_models, where NORMAL_STOPS = {"stop", "end_turn",
    # "toolUse", "tool_calls", ""}. Encode the rate via stop_reasons.
    normal = round(calls * success_rate_pct / 100)
    abnormal = calls - normal
    from collections import defaultdict
    stops = defaultdict(int)
    if normal:
        stops["end_turn"] = normal
    if abnormal:
        stops["aborted"] = abnormal
    return {
        "calls": calls,
        "input": 1000, "output": 500,
        "cache_read": 0, "cache_write": 0, "cost": 0.0,
        "durations": durs,
        "stop_reasons": stops,
    }


def _run_section_models(model_stats):
    s = Section(title="test")
    out = _section_models(
        s,
        {"model_stats": model_stats},
        file_count=20,
    )
    assert len(s.checks) == 1, "_section_models must produce exactly one check"
    return s.checks[0], out


class ModelVerdictMatrix(unittest.TestCase):
    def test_high_p95_high_success_warn_latency(self):
        """高 P95 (70s) + 高成功率 (100%) → warn (latency 触发)."""
        check, out = _run_section_models({
            "amazon-bedrock/claude-opus-4-6": _model_data(70.0, 100.0),
        })
        self.assertEqual(check.verdict, Verdict.WARN)
        self.assertEqual(out["verdict_trigger"], "latency")
        self.assertIn("延迟", check.message)
        self.assertEqual(out["min_success_rate_pct"], 100.0)

    def test_high_p95_low_success_fail_critical(self):
        """高 P95 + 低成功率 (85%) → fail (availability_critical)."""
        check, out = _run_section_models({
            "x/foo": _model_data(70.0, 85.0),
        })
        self.assertEqual(check.verdict, Verdict.FAIL)
        self.assertEqual(out["verdict_trigger"], "availability_critical")

    def test_low_p95_low_success_fail_critical(self):
        """低 P95 (10s) + 低成功率 (85%) → fail."""
        check, out = _run_section_models({
            "x/foo": _model_data(10.0, 85.0),
        })
        self.assertEqual(check.verdict, Verdict.FAIL)
        self.assertEqual(out["verdict_trigger"], "availability_critical")

    def test_low_p95_high_success_ok(self):
        """低 P95 + 高成功率 (98%) → ok."""
        check, out = _run_section_models({
            "x/foo": _model_data(10.0, 98.0),
        })
        self.assertEqual(check.verdict, Verdict.OK)
        self.assertEqual(out["verdict_trigger"], "ok")

    def test_boundary_92pct_warn_availability(self):
        """成功率 92% + P95=20s → warn (availability，非 critical)."""
        check, out = _run_section_models({
            "x/foo": _model_data(20.0, 92.0),
        })
        self.assertEqual(check.verdict, Verdict.WARN)
        self.assertEqual(out["verdict_trigger"], "availability")

    def test_small_sample_excluded_from_verdict(self):
        """calls<10 的模型不应单独驱动 fail/warn — verdict 仅看延迟."""
        # Only one model, with just 5 calls and 0% success — should NOT
        # drive fail because sample is too small. P95 is low → ok.
        ms = _model_data(5.0, 0.0, calls=5)
        ms["stop_reasons"] = {"aborted": 5}  # ensure 0% success
        check, out = _run_section_models({"x/tiny": ms})
        self.assertEqual(check.verdict, Verdict.OK)
        self.assertIsNone(out["min_success_rate_pct"])
        self.assertIsNone(out["min_success_rate_model"])
        self.assertEqual(out["verdict_trigger"], "ok")

    def test_small_sample_high_p95_only_warn(self):
        """calls<10 且 P95>60 时仅延迟 warn，不 fail (保守)."""
        ms = _model_data(70.0, 0.0, calls=5)
        ms["stop_reasons"] = {"aborted": 5}
        check, out = _run_section_models({"x/tiny": ms})
        self.assertEqual(check.verdict, Verdict.WARN)
        self.assertEqual(out["verdict_trigger"], "latency")

    def test_min_success_picks_worst_model(self):
        """多模型时取最低成功率作为 verdict 触发指标."""
        check, out = _run_section_models({
            "good/m": _model_data(20.0, 100.0, calls=50),
            "bad/m": _model_data(20.0, 80.0, calls=50),
        })
        self.assertEqual(check.verdict, Verdict.FAIL)
        self.assertEqual(out["min_success_rate_model"], "bad/m")
        self.assertEqual(out["min_success_rate_pct"], 80.0)


# ─── daily_trend 时间窗采样 ─────────────────────────────────────────────────


def _write_session(base, agent_id, sess_id, lines, mtime_epoch):
    """Write a session jsonl at base/agent_id/{sess_id-prefix}/sess_id.jsonl
    with the given lines (each a dict), then set mtime."""
    sid = sess_id
    bucket = sid[:2] if len(sid) >= 2 else sid
    sess_dir = Path(base) / agent_id / bucket
    sess_dir.mkdir(parents=True, exist_ok=True)
    path = sess_dir / f"{sid}.jsonl"
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")
    os.utime(path, (mtime_epoch, mtime_epoch))
    return str(path)


def _assistant_record(iso_ts, msg_epoch_ms, dur_s, output_tokens=100):
    """Build one assistant jsonl line.

    obj.timestamp − msg.timestamp == dur_s → P50/P95 derives from this.
    """
    return {
        "timestamp": iso_ts,
        "message": {
            "role": "assistant",
            "provider": "anthropic",
            "model": "claude-test",
            "timestamp": msg_epoch_ms,
            "usage": {"input": 100, "output": output_tokens, "cacheRead": 0,
                      "cacheWrite": 0, "cost": {"total": 0}},
            "stopReason": "end_turn",
            "content": [{"type": "text", "text": "ok"}],
        },
    }


class DailyTrendWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ocdiag-trend-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_window_includes_recent_excludes_old(self):
        """8 天前 mtime 文件不进，1 天前的进."""
        now = time.time()
        recent_mtime = now - 1 * 86400
        old_mtime = now - 8 * 86400
        recent = _write_session(
            self.tmp, "main", "abcd1234",
            [_assistant_record(
                datetime.fromtimestamp(recent_mtime, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"),
                int(recent_mtime * 1000) - 5000,
                5.0,
            )],
            recent_mtime,
        )
        old = _write_session(
            self.tmp, "main", "deadbeef",
            [_assistant_record(
                datetime.fromtimestamp(old_mtime, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"),
                int(old_mtime * 1000) - 5000,
                5.0,
            )],
            old_mtime,
        )
        files = _collect_session_files_by_window(self.tmp, days=7)
        self.assertIn(recent, files)
        self.assertNotIn(old, files)

    def test_daily_trend_reports_calls_for_day_outside_perf_sample(self):
        """构造 06-04 / 前一天有调用，断言 daily_trend 7 天里那天 calls > 0
        且 P50 与构造的 duration 匹配."""
        now = time.time()
        # Pick a day that's 2 days ago — definitely outside any
        # latest-20 sample if we add many newer files, but inside the
        # 7-day window.
        target_dt = datetime.fromtimestamp(now - 2 * 86400, tz=timezone.utc)
        target_iso = target_dt.isoformat().replace("+00:00", "Z")
        target_msg_ms = int(target_dt.timestamp() * 1000) - 7000  # 7s prior
        target_day_key = target_dt.astimezone().strftime("%m-%d")

        _write_session(
            self.tmp, "main", "target01",
            [_assistant_record(target_iso, target_msg_ms, 7.0,
                               output_tokens=200),
             _assistant_record(target_iso, target_msg_ms, 7.0,
                               output_tokens=300)],
            target_dt.timestamp(),
        )
        # Add a few "newer" perf-sample-eligible files to simulate the
        # latest-20 window crowding out the older day.
        for i in range(3):
            day = datetime.fromtimestamp(now - i * 3600, tz=timezone.utc)
            _write_session(
                self.tmp, "main", f"newer{i:02d}",
                [_assistant_record(
                    day.isoformat().replace("+00:00", "Z"),
                    int(day.timestamp() * 1000) - 4000,
                    4.0,
                )],
                day.timestamp(),
            )

        trend_files = _collect_session_files_by_window(self.tmp, days=7)
        daily_stats = _parse_daily_stats(trend_files)
        s = Section(title="test")
        out = _section_daily_trend(s, daily_stats, len(trend_files))
        trend = out["daily_trend"]
        # find the row for our target day
        row = next((r for r in trend if r["date"] == target_day_key), None)
        self.assertIsNotNone(row, f"trend missing {target_day_key}: {trend}")
        self.assertEqual(row["calls"], 2)
        # P50 of [7.0, 7.0] is 7.0
        self.assertAlmostEqual(row["p50_s"], 7.0, delta=0.01)
        self.assertEqual(row["output_tokens"], 500)

    def test_files_outside_window_do_not_pollute_trend(self):
        """8 天前文件即使有 assistant 记录，daily_stats 中也不应出现."""
        now = time.time()
        old_mtime = now - 8 * 86400
        old_dt = datetime.fromtimestamp(old_mtime, tz=timezone.utc)
        _write_session(
            self.tmp, "main", "ancient1",
            [_assistant_record(
                old_dt.isoformat().replace("+00:00", "Z"),
                int(old_dt.timestamp() * 1000) - 3000,
                3.0,
            )],
            old_mtime,
        )
        trend_files = _collect_session_files_by_window(self.tmp, days=7)
        self.assertEqual(trend_files, [])
        daily_stats = _parse_daily_stats(trend_files)
        self.assertEqual(len(daily_stats), 0)


if __name__ == "__main__":
    unittest.main()
