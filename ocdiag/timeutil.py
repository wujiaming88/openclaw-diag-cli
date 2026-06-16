"""Timestamp parsing and duration formatting."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def parse_obj_ts(ts_str: Optional[str]) -> Optional[datetime]:
    """obj.timestamp is ISO 8601."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_msg_ts(ms) -> Optional[datetime]:
    """msg.timestamp is epoch milliseconds."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except Exception:
        return None


def fmt_duration(sec) -> str:
    if sec is None:
        return "?"
    s = float(sec)
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.1f}m"
    return f"{s/3600:.1f}h"


def fmt_age(ms_delta) -> str:
    s = abs(float(ms_delta)) / 1000
    if s < 60:
        return f"{s:.0f}秒"
    if s < 3600:
        return f"{s/60:.0f}分钟"
    if s < 86400:
        return f"{s/3600:.1f}小时"
    return f"{s/86400:.1f}天"


def fmt_ts(ms) -> str:
    """Format epoch-ms as local time string: YYYY-MM-DD HH:MM:SS."""
    if not ms:
        return "?"
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms)


def fmt_ts_short(ms) -> str:
    """Format epoch-ms as local time HH:MM:SS only."""
    if not ms:
        return "?"
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%H:%M:%S")
    except Exception:
        return str(ms)


def fmt_iso_local(iso_str: Optional[str]) -> str:
    """Convert ISO/UTC string to local time display: YYYY-MM-DD HH:MM:SS."""
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str[:19]


def fmt_epoch_local(ms) -> str:
    """Format epoch-ms as local ISO-like string for structured output."""
    if not ms:
        return "?"
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms)


def fmt_hms(ts: Optional[str]) -> str:
    if not ts:
        return "?"
    try:
        return ts.split("T", 1)[1][:8]
    except Exception:
        return ts[:19]


def window_token(ms: int) -> str:
    """Render a scan-window duration in ms as the canonical scope token.

    Single source of truth for `data_scope.window` strings derived from a
    millisecond cutoff. Callers that pass the same `ms` value into
    `collect_runs(since_ms=ms_ago(ms))` MUST also use this to produce the
    displayed window — never a parallel literal — so the displayed scope
    cannot drift from the actual scan.

    Standard windows map to friendly tokens (24h/7d/14d/30d). Anything else
    falls back to ``Nd`` if divisible by a day, otherwise ``Nh`` if
    divisible by an hour, else ``<ms>ms``.
    """
    try:
        ms_int = int(ms)
    except (TypeError, ValueError):
        return "?"
    if ms_int == 24 * 3600 * 1000:
        return "24h"
    if ms_int == 7 * 86400 * 1000:
        return "7d"
    if ms_int == 14 * 86400 * 1000:
        return "14d"
    if ms_int == 30 * 86400 * 1000:
        return "30d"
    if ms_int > 0 and ms_int % 86400000 == 0:
        return f"{ms_int // 86400000}d"
    if ms_int > 0 and ms_int % 3600000 == 0:
        return f"{ms_int // 3600000}h"
    return f"{ms_int}ms"
