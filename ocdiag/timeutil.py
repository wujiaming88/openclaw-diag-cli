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


def fmt_duration_ms(ms) -> str:
    if ms is None:
        return "?"
    s = float(ms) / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s/60:.1f}min"
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
    if not ms:
        return "?"
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms)


def fmt_hms(ts: Optional[str]) -> str:
    if not ts:
        return "?"
    try:
        return ts.split("T", 1)[1][:8]
    except Exception:
        return ts[:19]
