#!/usr/bin/env python3
"""Tests for the v2 core types (Verdict, Check, Section, Report).

Pure stdlib, no pytest. Run directly:
    python3 tests/test_v2_core.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag.core.types import Check, Report, Section, Verdict  # noqa: E402


_failures = []


def _check(name, ok, detail=""):
    if ok:
        print(f"  [OK]   {name}")
    else:
        _failures.append(name)
        print(f"  [FAIL] {name}: {detail}")


def test_verdict_ordering():
    _check("Verdict OK < WARN", Verdict.WARN > Verdict.OK)
    _check("Verdict WARN < FAIL", Verdict.FAIL > Verdict.WARN)
    _check("Verdict OK < FAIL", Verdict.FAIL > Verdict.OK)
    _check("Verdict OK >= OK", Verdict.OK >= Verdict.OK)
    _check("Verdict FAIL >= WARN", Verdict.FAIL >= Verdict.WARN)


def test_verdict_worst():
    _check(
        "worst() empty -> OK",
        Verdict.worst() == Verdict.OK,
    )
    _check(
        "worst(OK, OK, OK) == OK",
        Verdict.worst(Verdict.OK, Verdict.OK, Verdict.OK) == Verdict.OK,
    )
    _check(
        "worst(OK, WARN) == WARN",
        Verdict.worst(Verdict.OK, Verdict.WARN) == Verdict.WARN,
    )
    _check(
        "worst(OK, WARN, FAIL) == FAIL",
        Verdict.worst(Verdict.OK, Verdict.WARN, Verdict.FAIL) == Verdict.FAIL,
    )


def test_check_construction():
    c = Check(name="x", verdict=Verdict.OK, message="hello")
    _check("Check default detail is None", c.detail is None)
    _check("Check default evidence is None", c.evidence is None)
    _check("Check default data is None", c.data is None)

    c = Check(
        name="x", verdict=Verdict.WARN, message="m", detail="d",
        evidence="e", data={"k": 1},
    )
    _check("Check with all kwargs preserved", c.detail == "d" and c.evidence == "e")


def test_section_convenience_methods():
    s = Section(title="t")
    s.ok("a", "msg-a")
    s.warn("b", "msg-b")
    s.fail("c", "msg-c", data={"x": 1})
    _check("Section.ok appends", s.checks[0].verdict == Verdict.OK)
    _check("Section.warn appends", s.checks[1].verdict == Verdict.WARN)
    _check("Section.fail appends", s.checks[2].verdict == Verdict.FAIL)
    _check("Section data kwarg pass-through", s.checks[2].data == {"x": 1})


def test_section_verdict_is_worst():
    empty = Section(title="empty")
    _check("Empty section verdict is OK", empty.verdict == Verdict.OK)

    s = Section(title="t")
    s.ok("a", "m"); s.warn("b", "m")
    _check("Section with OK+WARN -> WARN", s.verdict == Verdict.WARN)
    s.fail("c", "m")
    _check("Section with OK+WARN+FAIL -> FAIL", s.verdict == Verdict.FAIL)


def test_report_summary():
    r = Report(module_id="m", title="T")
    s1 = r.section("s1")
    s1.ok("a", "m"); s1.ok("b", "m"); s1.warn("c", "m")
    s2 = r.section("s2")
    s2.ok("d", "m"); s2.fail("e", "m")
    summary = r.summary
    _check(
        "summary counts",
        summary == {"pass": 3, "warn": 1, "fail": 1, "total": 5},
        repr(summary),
    )


def test_report_verdict_derivation():
    r = Report(module_id="m", title="T")
    _check("Empty report verdict is OK", r.verdict == Verdict.OK)

    r2 = Report(module_id="m", title="T")
    r2.error = "boom"
    _check("Report with error -> FAIL", r2.verdict == Verdict.FAIL)

    r3 = Report(module_id="m", title="T")
    s = r3.section("x"); s.ok("a", "m")
    _check("Report all-OK -> OK", r3.verdict == Verdict.OK)
    s.warn("b", "m")
    _check("Report with WARN -> WARN", r3.verdict == Verdict.WARN)
    s.fail("c", "m")
    _check("Report with FAIL -> FAIL", r3.verdict == Verdict.FAIL)


def test_report_section_helper():
    r = Report(module_id="m", title="T")
    s = r.section("hello")
    _check("Report.section returns Section", isinstance(s, Section))
    _check("Report.section appends", r.sections == [s])


def main():
    print("[1/1] v2 core tests...")
    test_verdict_ordering()
    test_verdict_worst()
    test_check_construction()
    test_section_convenience_methods()
    test_section_verdict_is_worst()
    test_report_summary()
    test_report_verdict_derivation()
    test_report_section_helper()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} test(s)")
        for n in _failures:
            print(f"  - {n}")
        return 1
    print("All v2 core tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
