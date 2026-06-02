#!/usr/bin/env python3
"""Wall-clock comparison: v1 (runpy.run_path) vs v2 (direct collect()).

Runs sys_health and shell_history through both code paths in JSON mode
(disables TTY rendering, so we measure pure collection cost) and prints a
side-by-side timing.

Usage:
    python3 tests/bench_v2.py [--runs N]
"""

from __future__ import annotations

import argparse
import io
import os
import runpy
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# v1 entrypoints
LEGACY_SCRIPTS = {
    "sys_health": REPO_ROOT / "diag" / "01_sys_health.py",
    "shell_history": REPO_ROOT / "diag" / "10_shell_history.py",
}


def time_v1(mid: str) -> float:
    script = LEGACY_SCRIPTS[mid]
    saved_argv = sys.argv[:]
    saved_prog = os.environ.get("OPENCLAW_DIAG_PROG")
    sys.argv = [str(script), "--json", "--no-color"]
    os.environ["OPENCLAW_DIAG_PROG"] = f"openclaw-diag {mid}"
    t0 = time.time()
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                runpy.run_path(str(script), run_name="__main__")
            except SystemExit:
                pass
    finally:
        sys.argv = saved_argv
        if saved_prog is None:
            os.environ.pop("OPENCLAW_DIAG_PROG", None)
        else:
            os.environ["OPENCLAW_DIAG_PROG"] = saved_prog
    return time.time() - t0


def time_v2(mid: str) -> float:
    from ocdiag.core import registry
    from ocdiag.core.context import DiagContext
    registry.discover()
    c = registry.get(mid)
    ctx = DiagContext.default(json_mode=True, no_color=True)
    t0 = time.time()
    c.collect(ctx)
    return time.time() - t0


def bench(mid: str, runs: int) -> None:
    v1_times = []
    v2_times = []
    # Warmup once to pull modules into cache for both paths.
    time_v1(mid)
    time_v2(mid)
    for _ in range(runs):
        v1_times.append(time_v1(mid))
        v2_times.append(time_v2(mid))
    v1_avg = sum(v1_times) / len(v1_times)
    v2_avg = sum(v2_times) / len(v2_times)
    speedup = v1_avg / v2_avg if v2_avg > 0 else float("inf")
    print(f"  {mid}:")
    print(f"    v1 (runpy.run_path):  {v1_avg*1000:8.1f} ms (avg of {runs})")
    print(f"    v2 (direct collect):  {v2_avg*1000:8.1f} ms (avg of {runs})")
    print(f"    speedup:              {speedup:.2f}x")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    print(f"openclaw-diag v1-vs-v2 benchmark ({args.runs} runs each, after warmup)\n")
    for mid in ("shell_history", "sys_health"):
        bench(mid, args.runs)


if __name__ == "__main__":
    main()
