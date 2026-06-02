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
