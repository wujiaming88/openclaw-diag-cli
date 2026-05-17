"""Discover today's openclaw log files."""

from __future__ import annotations

import glob
import os
import time
from datetime import datetime
from typing import List, Optional


def _today_start_epoch() -> float:
    today = datetime.now().date()
    return time.mktime(today.timetuple())


def discover_recent_logs(log_dir: str) -> List[str]:
    """Return log files whose mtime >= today 00:00, sorted newest first."""
    pattern = os.path.join(log_dir, "openclaw-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].log")
    matched: List[tuple] = []
    cutoff = _today_start_epoch()
    for f in glob.glob(pattern):
        if not os.path.isfile(f):
            continue
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if m >= cutoff:
            matched.append((m, f))
    matched.sort(reverse=True)
    return [p for _, p in matched]


def latest_app_log(log_dir: str) -> Optional[str]:
    pattern = os.path.join(log_dir, "openclaw-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].log")
    matched: List[tuple] = []
    for f in glob.glob(pattern):
        try:
            matched.append((os.path.getmtime(f), f))
        except OSError:
            continue
    if not matched:
        today = datetime.now().strftime("%Y-%m-%d")
        candidate = os.path.join(log_dir, f"openclaw-{today}.log")
        return candidate if os.path.isfile(candidate) else None
    matched.sort(reverse=True)
    return matched[0][1]


