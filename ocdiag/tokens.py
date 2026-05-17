"""Token / size formatters and percentile helper."""

from __future__ import annotations

from typing import List, Optional


def fmt_tokens(n) -> str:
    if n is None:
        return "?"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def percentile(sorted_list: List[float], p: float) -> Optional[float]:
    if not sorted_list:
        return None
    k = max(0, min(len(sorted_list) - 1, int(len(sorted_list) * p)))
    return sorted_list[k]


def pct(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = min(n - 1, int(n * p))
    return sorted_vals[idx]


def human_size(b) -> str:
    b = int(b)
    if b < 1024:
        return f"{b}B"
    if b < 1048576:
        return f"{b/1024:.1f}KB"
    if b < 1073741824:
        return f"{b/1048576:.1f}MB"
    return f"{b/1073741824:.1f}GB"
