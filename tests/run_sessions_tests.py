#!/usr/bin/env python3
"""Unit tests for ``ocdiag.sessions``.

Pure stdlib, same style as the other tests/run_*.py scripts — no pytest.

Covers the checkpoint-file regression: ``<uuid>.checkpoint.<cp-uuid>.jsonl``
must be attributed to ``<uuid>`` (not parsed as a separate session) so that
``extract <full-uuid>`` doesn't fail with "prefix matches multiple sessions".

Usage:
    python3 tests/run_sessions_tests.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ocdiag import sessions  # noqa: E402


SESSION_UUID = "0123456789abcdef0123456789abcdef"
CP_UUID = "fedcba9876543210fedcba9876543210"


def _check(label: str, cond: bool, detail: str = "") -> Tuple[str, bool, str]:
    return label, cond, detail


def test_session_uuid_of() -> List[Tuple[str, bool, str]]:
    results: List[Tuple[str, bool, str]] = []

    cases = [
        # (filename, expected uuid (or None))
        (f"{SESSION_UUID}.jsonl", SESSION_UUID),
        (f"{SESSION_UUID}.jsonl.lock", SESSION_UUID),
        (f"{SESSION_UUID}.jsonl.deleted.1700000000", SESSION_UUID),
        (f"{SESSION_UUID}.jsonl.reset.1700000000", SESSION_UUID),
        (f"{SESSION_UUID}.jsonl.bak-12345", SESSION_UUID),
        (f"{SESSION_UUID}.checkpoint.{CP_UUID}.jsonl", SESSION_UUID),
        (f"{SESSION_UUID}.trajectory.jsonl", None),
        (f"{SESSION_UUID}.acp-stream.jsonl", None),
        (f"{SESSION_UUID}.json", None),
    ]
    for filename, expected in cases:
        actual = sessions._session_uuid_of(filename)
        ok = actual == expected
        results.append(_check(
            f"_session_uuid_of({filename!r}) == {expected!r}",
            ok, f"got {actual!r}",
        ))
    return results


def test_classify_state() -> List[Tuple[str, bool, str]]:
    results: List[Tuple[str, bool, str]] = []
    cases = [
        (f"{SESSION_UUID}.jsonl", "active"),
        (f"{SESSION_UUID}.jsonl.lock", "lock"),
        (f"{SESSION_UUID}.jsonl.deleted.1700000000", "deleted"),
        (f"{SESSION_UUID}.jsonl.reset.1700000000", "reset"),
        (f"{SESSION_UUID}.jsonl.bak-12345", "backup"),
        (f"{SESSION_UUID}.checkpoint.{CP_UUID}.jsonl", "checkpoint"),
    ]
    for filename, expected in cases:
        actual = sessions.classify_state(filename)
        ok = actual == expected
        results.append(_check(
            f"classify_state({filename!r}) == {expected!r}",
            ok, f"got {actual!r}",
        ))
    return results


def test_resolve_with_checkpoint() -> List[Tuple[str, bool, str]]:
    results: List[Tuple[str, bool, str]] = []
    tmp = Path(tempfile.mkdtemp(prefix="ocdiag-sessions-test-"))
    try:
        agent_dir = tmp / "main"
        sessions_dir = agent_dir / "sessions"
        sessions_dir.mkdir(parents=True)

        active = sessions_dir / f"{SESSION_UUID}.jsonl"
        checkpoint = sessions_dir / f"{SESSION_UUID}.checkpoint.{CP_UUID}.jsonl"
        active.write_text("{}\n", encoding="utf-8")
        checkpoint.write_text("{}\n", encoding="utf-8")

        files, candidates = sessions.resolve(SESSION_UUID, base_dir=str(tmp))
        results.append(_check(
            "resolve full UUID returns no ambiguity candidates",
            candidates == [],
            f"got candidates={candidates!r}",
        ))
        states = sorted(state for _, state in files)
        results.append(_check(
            "resolve returns both active and checkpoint files",
            states == ["active", "checkpoint"],
            f"got states={states!r}",
        ))
        # active should sort first by priority
        results.append(_check(
            "active file is sorted first by priority",
            len(files) >= 1 and files[0][1] == "active",
            f"got files={files!r}",
        ))
        paths_only = {os.path.basename(p) for p, _ in files}
        expected_paths = {active.name, checkpoint.name}
        results.append(_check(
            "resolve includes both on-disk filenames",
            paths_only == expected_paths,
            f"got {paths_only!r}",
        ))

        # Independent UUID alongside should NOT be merged.
        other_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        (sessions_dir / f"{other_uuid}.jsonl").write_text("{}\n", encoding="utf-8")
        # Prefix shared by checkpoint sibling alone shouldn't make it ambiguous,
        # but a real two-session prefix collision should still trigger candidates.
        files2, cands2 = sessions.resolve("aaaaaaaa", base_dir=str(tmp))
        results.append(_check(
            "unrelated 8-char prefix resolves to its own session",
            cands2 == [] and len(files2) == 1,
            f"got files={files2!r}, cands={cands2!r}",
        ))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def main() -> int:
    suites = [
        ("_session_uuid_of", test_session_uuid_of),
        ("classify_state", test_classify_state),
        ("resolve(checkpoint)", test_resolve_with_checkpoint),
    ]
    print(f"[1/1] running {len(suites)} sessions test suites...", flush=True)
    failures: List[str] = []
    total = 0
    for label, fn in suites:
        results = fn()
        for r_label, ok, detail in results:
            total += 1
            if ok:
                print(f"  [OK]   {label}: {r_label}", flush=True)
            else:
                msg = f"{label}: {r_label} ({detail})"
                failures.append(msg)
                print(f"  [FAIL] {msg}", flush=True)
    if failures:
        print(f"\n{len(failures)}/{total} failure(s):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"\nAll {total} sessions tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
