#!/usr/bin/env python3
"""Tests for v2 renderers (HumanRenderer, JsonRenderer)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag.core.types import Report, Verdict  # noqa: E402
from ocdiag.render.ansi import strip_ansi  # noqa: E402
from ocdiag.render.human import HumanRenderer  # noqa: E402
from ocdiag.render.json_renderer import JsonRenderer, to_envelope  # noqa: E402


_failures = []


def _check(name, ok, detail=""):
    if ok:
        print(f"  [OK]   {name}")
    else:
        _failures.append(name)
        print(f"  [FAIL] {name}: {detail}")


def _make_report() -> Report:
    r = Report(module_id="demo", title="Demo")
    s1 = r.section("first")
    s1.ok("a", "all good")
    s1.warn("b", "watch out", data={"k": 1})
    s2 = r.section("second")
    s2.fail("c", "broken", evidence="line1\nline2")
    r.elapsed_ms = 1234
    r.data["meta"] = "x"
    return r


def test_human_renderer_structure():
    r = _make_report()
    out = HumanRenderer(no_color=True).render(r)
    plain = strip_ansi(out)
    _check("banner contains module id", "demo" in plain)
    _check("banner contains title", "Demo" in plain)
    _check("verdict line shows FAIL", "FAIL" in plain)
    _check("section title 'first' rendered", "first" in plain)
    _check("section title 'second' rendered", "second" in plain)
    _check("check message rendered", "all good" in plain)
    _check("evidence rendered", "line1" in plain and "line2" in plain)
    _check("footer shows error count", "1 error" in plain)
    _check("elapsed shows in seconds when >=1s", "Run 1.2s" in plain)


def test_human_renderer_clean_run():
    r = Report(module_id="m", title="t")
    s = r.section("s")
    s.ok("a", "fine")
    out = strip_ansi(HumanRenderer(no_color=True).render(r))
    _check("clean run shows PASS", "PASS" in out)
    _check("clean run shows all checks passed", "all checks passed" in out)


def test_json_envelope_fields():
    r = _make_report()
    env = to_envelope(r)
    _check("envelope module", env["module"] == "demo")
    _check("envelope verdict=fail (because of fail check)",
           env["verdict"] == "fail")
    _check("envelope legacy status=error", env["status"] == "error")
    _check("envelope summary",
           env["summary"] == {"pass": 1, "warn": 1, "fail": 1, "total": 3},
           repr(env["summary"]))
    _check("envelope elapsed_ms is int", isinstance(env["elapsed_ms"], int))
    _check("envelope sections list length", len(env["sections"]) == 2)
    _check("envelope first section verdict",
           env["sections"][0]["verdict"] == "warn")
    _check("envelope second section verdict",
           env["sections"][1]["verdict"] == "fail")
    _check("envelope check has data field",
           env["sections"][0]["checks"][1]["data"] == {"k": 1})
    _check("envelope data passthrough", env["data"] == {"meta": "x"})


def test_json_renderer_emits_one_line_per_report():
    r = _make_report()
    buf = io.StringIO()
    JsonRenderer(stream=buf).write(r)
    out = buf.getvalue()
    _check("json output ends with newline", out.endswith("\n"))
    parsed = json.loads(out.strip())
    _check("json roundtrip valid", parsed["module"] == "demo")


def test_json_warn_keeps_legacy_status_ok():
    r = Report(module_id="m", title="t")
    r.section("x").warn("a", "warn")
    env = to_envelope(r)
    _check("WARN sets verdict=warn", env["verdict"] == "warn")
    _check("WARN keeps legacy status=ok", env["status"] == "ok")


def main():
    print("[1/1] v2 render tests...")
    test_human_renderer_structure()
    test_human_renderer_clean_run()
    test_json_envelope_fields()
    test_json_renderer_emits_one_line_per_report()
    test_json_warn_keeps_legacy_status_ok()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} test(s)")
        for n in _failures:
            print(f"  - {n}")
        return 1
    print("All v2 render tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
