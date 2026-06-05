"""Discover openclaw log files.

Two discovery strategies are exposed:

  ``discover_recent_logs(log_dir)``
      Returns log files with mtime >= today 00:00. Cheap and correct for
      "what happened today" callers; it is also the safe fallback when the
      caller has no session-window information.

  ``discover_logs_for_window(log_dir, window_start_ms, window_end_ms)``
      Returns log files whose filename date intersects the given window
      (with a one-day margin on each side to absorb midnight / timezone
      boundaries). Required for callers that diagnose sessions older than
      "today" — e.g. yesterday's session whose log file's mtime is < today
      and would be excluded by ``discover_recent_logs``. The downstream
      window-bound filter still does the precise ±5s slice; this function
      only widens the candidate file set so the right file is even
      considered.
"""

from __future__ import annotations

import glob
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import List, Optional


_LOG_FILE_GLOB = (
    "openclaw-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].log"
)
_LOG_FILE_DATE_RE = re.compile(
    r"openclaw-(\d{4})-(\d{2})-(\d{2})\.log$"
)


def _today_start_epoch() -> float:
    today = datetime.now().date()
    return time.mktime(today.timetuple())


def _filename_date(path: str) -> Optional[date]:
    """Parse the YYYY-MM-DD encoded in an openclaw-*.log filename."""
    m = _LOG_FILE_DATE_RE.search(os.path.basename(path))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def discover_recent_logs(log_dir: str) -> List[str]:
    """Return log files whose mtime >= today 00:00, sorted newest first."""
    pattern = os.path.join(log_dir, _LOG_FILE_GLOB)
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


def discover_logs_for_window(
    log_dir: str,
    window_start_ms: int,
    window_end_ms: int,
) -> List[str]:
    """Return log files that could plausibly contain entries from the
    session window ``[window_start_ms, window_end_ms]`` (epoch ms in
    local time), sorted newest-first.

    The candidate set is the **union** of two strategies:

      ① Filename-date match. Any log whose name encodes a date in
         ``[local-date(start) − 1 day, local-date(end) + 1 day]`` is
         included. This is what makes diagnosing yesterday's (or older)
         sessions work — the file's mtime is stale, but its name still
         tells us when it was written.
      ② Recent-mtime match (``discover_recent_logs`` semantics). Any log
         whose mtime is >= today 00:00 is also included. This preserves
         the live-session behavior: today's log is still being appended
         to, so it must always be considered, regardless of how the
         window was computed.

    The ±1 day margin on (1) absorbs midnight crossings (a run starting
    at 23:59:59 lands in the next day's file) and timezone offsets
    between the host that wrote the log and the host running ``ocdiag``
    (worst-case 14h fits in 24h on each side).

    The downstream window-bound filter (``_bound_logs_to_window``) does
    the precise ±5s slice, so a generous file-set here does not leak
    unrelated entries into ``correlated_logs`` — it just keeps the
    *right file* in the candidate set in the first place.

    When ``window_start_ms`` and ``window_end_ms`` are both 0/falsey the
    window is unknown — fall back to ``discover_recent_logs`` exactly,
    so the behavior degrades to the pre-v1.4.10 default.
    """
    if not window_start_ms and not window_end_ms:
        return discover_recent_logs(log_dir)
    start_dt = datetime.fromtimestamp(
        (window_start_ms or window_end_ms) / 1000
    ).date()
    end_dt = datetime.fromtimestamp(
        (window_end_ms or window_start_ms) / 1000
    ).date()
    lo_date = start_dt - timedelta(days=1)
    hi_date = end_dt + timedelta(days=1)
    mtime_cutoff = _today_start_epoch()

    pattern = os.path.join(log_dir, _LOG_FILE_GLOB)
    # Sort key per file: prefer mtime when available (newer first); fall
    # back to filename date for files we can't stat. The result is sorted
    # newest-first like discover_recent_logs.
    matched: List[tuple] = []  # (sort_key, path)
    for f in glob.glob(pattern):
        if not os.path.isfile(f):
            continue
        d = _filename_date(f)
        try:
            mt = os.path.getmtime(f)
        except OSError:
            mt = 0.0
        in_filename_window = d is not None and lo_date <= d <= hi_date
        in_recent_mtime = mt >= mtime_cutoff
        if not (in_filename_window or in_recent_mtime):
            continue
        matched.append((mt, f))
    matched.sort(reverse=True)
    return [p for _, p in matched]


def window_log_dates(
    log_dir: str,
    window_start_ms: int,
    window_end_ms: int,
) -> tuple:
    """Classify the dates a session window spans by whether the log_dir
    holds the matching ``openclaw-YYYY-MM-DD.log`` file.

    Returns ``(present, missing, available)`` where each element is a list
    of ``YYYY-MM-DD`` strings, sorted ascending. ``present`` lists the
    window dates whose log file exists; ``missing`` lists the window dates
    whose file is absent; ``available`` lists every openclaw-*.log date
    in the directory (regardless of window).

    When ``window_start_ms`` and ``window_end_ms`` are both 0/falsey the
    window is unknown — returns ``([], [], available)`` so callers can
    detect the degenerate case.
    """
    available: List[str] = []
    for f in glob.glob(os.path.join(log_dir, _LOG_FILE_GLOB)):
        d = _filename_date(f)
        if d is not None:
            available.append(d.isoformat())
    available.sort()

    if not window_start_ms and not window_end_ms:
        return ([], [], available)

    start_ms = window_start_ms or window_end_ms
    end_ms = window_end_ms or window_start_ms
    if start_ms > end_ms:
        start_ms, end_ms = end_ms, start_ms
    try:
        start_d = datetime.fromtimestamp(start_ms / 1000).date()
        end_d = datetime.fromtimestamp(end_ms / 1000).date()
    except (OverflowError, OSError, ValueError):
        return ([], [], available)

    present: List[str] = []
    missing: List[str] = []
    d = start_d
    while d <= end_d:
        iso = d.isoformat()
        path = os.path.join(log_dir, f"openclaw-{iso}.log")
        if os.path.isfile(path):
            present.append(iso)
        else:
            missing.append(iso)
        d += timedelta(days=1)
    return (present, missing, available)


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


