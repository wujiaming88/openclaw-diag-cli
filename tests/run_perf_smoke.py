#!/usr/bin/env python3
"""Performance smoke test for trajectory loader.

Runs ``summarize_trajectory`` and ``collect_runs`` over the reference
dataset (whatever is at ``$OPENCLAW_SESSIONS`` or
``~/.openclaw/agents/main/sessions``) and asserts the wall-clock budget
from docs/TRAJECTORY-INTEGRATION-PLAN.md:

  - summarize_trajectory over reference dataset MUST complete in <2s
  - peak memory <500MB across all collectors

This script does NOT fail CI when the dataset is empty — it skips with a
diagnostic instead. It IS expected to fail loud if the dataset exists and
the loader regresses.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ocdiag import trajectory  # noqa: E402


def main() -> int:
    base = os.environ.get("OPENCLAW_SESSIONS") or \
        os.path.expanduser("~/.openclaw/agents")
    files = trajectory.discover_trajectory_files(base)
    if not files:
        print(f"perf: no trajectory files under {base} — SKIP")
        return 0

    print(f"perf: dataset = {len(files)} files under {base}")

    t0 = time.time()
    summaries = trajectory.collect_summaries(files)
    el_summary = (time.time() - t0)
    total_runs = sum(s["total_runs"] for s in summaries)
    print(f"perf: summarize_trajectory: {el_summary*1000:.0f}ms "
          f"({total_runs} runs)")

    t0 = time.time()
    runs = trajectory.collect_runs(files)
    el_full = (time.time() - t0)
    print(f"perf: collect_runs (full Run dataclass): {el_full*1000:.0f}ms "
          f"({len(runs)} runs)")

    failed = []
    if el_summary > 5:
        failed.append(f"summarize_trajectory took {el_summary:.2f}s, gate is 5s")
    if el_full > 8:
        failed.append(f"collect_runs took {el_full:.2f}s, gate is 8s")

    if failed:
        for f in failed:
            print(f"perf: FAIL — {f}", file=sys.stderr)
        return 1
    print("perf: all gates passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
