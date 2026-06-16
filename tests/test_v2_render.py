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
    _check("envelope ok=True (no error)", env["ok"] is True)
    _check("envelope error=None (no error)", env["error"] is None)
    data = env["data"]
    _check("envelope data.module", data["module"] == "demo")
    _check("envelope data.verdict=fail (because of fail check)",
           data["verdict"] == "fail")
    _check("envelope data.status=error (legacy)", data["status"] == "error")
    _check("envelope data.summary",
           data["summary"] == {"pass": 1, "warn": 1, "fail": 1, "total": 3},
           repr(data["summary"]))
    _check("envelope data.elapsed_ms is int",
           isinstance(data["elapsed_ms"], int))
    _check("envelope data.sections list length", len(data["sections"]) == 2)
    _check("envelope first section verdict",
           data["sections"][0]["verdict"] == "warn")
    _check("envelope second section verdict",
           data["sections"][1]["verdict"] == "fail")
    _check("envelope check has data field",
           data["sections"][0]["checks"][1]["data"] == {"k": 1})
    _check("envelope data.data passthrough", data["data"] == {"meta": "x"})


def test_json_renderer_emits_one_line_per_report():
    r = _make_report()
    buf = io.StringIO()
    JsonRenderer(stream=buf).write(r)
    out = buf.getvalue()
    _check("json output ends with newline", out.endswith("\n"))
    parsed = json.loads(out.strip())
    _check("json roundtrip ok flag", parsed["ok"] is True)
    _check("json roundtrip data.module", parsed["data"]["module"] == "demo")


def test_json_warn_keeps_legacy_status_ok():
    r = Report(module_id="m", title="t")
    r.section("x").warn("a", "warn")
    env = to_envelope(r)
    _check("WARN sets verdict=warn", env["data"]["verdict"] == "warn")
    _check("WARN keeps legacy status=ok", env["data"]["status"] == "ok")


def test_json_error_envelope():
    from ocdiag.core.errors import DiagError
    r = Report(module_id="m", title="t")
    r.error = "found nothing"
    r.diag_error = DiagError(
        code="SESSION_NOT_FOUND",
        message="found nothing",
        hint="try a longer prefix",
        details={"query": "abc"},
    )
    env = to_envelope(r)
    _check("error envelope ok=False", env["ok"] is False)
    _check("error envelope data is None", env["data"] is None)
    _check("error envelope error.code", env["error"]["code"] == "SESSION_NOT_FOUND")
    _check("error envelope error.hint", env["error"]["hint"] == "try a longer prefix")
    _check("error envelope error.details",
           env["error"]["details"] == {"query": "abc"})


def test_json_error_envelope_unstructured_fallback():
    r = Report(module_id="m", title="t")
    r.error = "boom"
    env = to_envelope(r)
    _check("fallback ok=False", env["ok"] is False)
    _check("fallback code=RUNTIME_ERROR",
           env["error"]["code"] == "RUNTIME_ERROR")
    _check("fallback message", env["error"]["message"] == "boom")


def test_ndjson_renderer_one_line_per_section():
    from ocdiag.render.ndjson import NdjsonRenderer
    r = _make_report()
    buf = io.StringIO()
    NdjsonRenderer(stream=buf).write(r)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    _check("ndjson emits 2 lines", len(lines) == 2)
    objs = [json.loads(ln) for ln in lines]
    _check("ndjson first line is section", objs[0]["section"] == "first")
    _check("ndjson second line verdict=fail", objs[1]["verdict"] == "fail")


def test_json_envelope_data_scope_present():
    r = Report(module_id="demo", title="Demo")
    r.add_scope("trajectory", "7d", "240 runs")
    r.add_scope("config", "current")
    r.section("s").ok("a", "ok")
    env = to_envelope(r)
    scope = env["data"]["data_scope"]
    _check("envelope data_scope is list", isinstance(scope, list))
    _check("envelope data_scope has 2 items", len(scope) == 2)
    _check(
        "first scope shape",
        scope[0] == {
            "source": "trajectory", "window": "7d", "detail": "240 runs",
        },
        repr(scope[0]),
    )
    _check(
        "second scope no detail key",
        scope[1] == {"source": "config", "window": "current"},
        repr(scope[1]),
    )


def test_json_envelope_data_scope_empty_when_no_scope():
    r = Report(module_id="m", title="t")
    r.section("s").ok("a", "ok")
    env = to_envelope(r)
    _check(
        "envelope data_scope present and empty",
        env["data"]["data_scope"] == [],
    )


def test_human_renderer_data_scope_line():
    r = Report(module_id="m", title="t")
    r.add_scope("trajectory", "7d", "240 runs")
    r.add_scope("app_logs", "today", "3")
    r.add_scope("config", "current")
    r.section("s").ok("a", "ok")
    out = strip_ansi(HumanRenderer(no_color=True).render(r))
    _check("human shows 数据口径 label", "数据口径" in out)
    _check("human shows trajectory:7d(240 runs)", "trajectory:7d(240 runs)" in out)
    _check("human shows 应用日志:今日(3)", "应用日志:今日(3)" in out)
    _check("human shows 配置:当前", "配置:当前" in out)


def test_human_renderer_data_scope_omitted_when_empty():
    r = Report(module_id="m", title="t")
    r.section("s").ok("a", "ok")
    out = strip_ansi(HumanRenderer(no_color=True).render(r))
    _check("human omits 数据口径 when empty", "数据口径" not in out)


def test_ndjson_renderer_emits_leading_scope_line():
    from ocdiag.render.ndjson import NdjsonRenderer
    r = Report(module_id="demo", title="Demo")
    r.add_scope("trajectory", "7d", "5 runs")
    r.section("first").ok("a", "all good")
    buf = io.StringIO()
    NdjsonRenderer(stream=buf).write(r)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    _check("ndjson emits 2 lines (scope + section)", len(lines) == 2)
    first = json.loads(lines[0])
    _check("ndjson first kind=scope", first.get("kind") == "scope")
    _check("ndjson first module=demo", first["module"] == "demo")
    _check(
        "ndjson scope payload",
        first["data_scope"]
        == [{"source": "trajectory", "window": "7d", "detail": "5 runs"}],
        repr(first["data_scope"]),
    )
    second = json.loads(lines[1])
    _check("ndjson second is section", second.get("section") == "first")


def test_ndjson_renderer_omits_scope_when_empty():
    from ocdiag.render.ndjson import NdjsonRenderer
    r = Report(module_id="m", title="t")
    r.section("first").ok("a", "ok")
    buf = io.StringIO()
    NdjsonRenderer(stream=buf).write(r)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    _check("ndjson emits 1 line when no scope", len(lines) == 1)
    obj = json.loads(lines[0])
    _check("ndjson line is section, not scope", obj.get("section") == "first")


def test_ndjson_error_emits_single_line():
    from ocdiag.core.errors import DiagError
    from ocdiag.render.ndjson import NdjsonRenderer
    r = Report(module_id="m", title="t")
    r.error = "x"
    r.diag_error = DiagError(code="INVALID_QUERY", message="x")
    buf = io.StringIO()
    NdjsonRenderer(stream=buf).write(r)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    _check("ndjson error emits one line", len(lines) == 1)
    obj = json.loads(lines[0])
    _check("ndjson error has ok=False", obj["ok"] is False)
    _check("ndjson error has code", obj["error"]["code"] == "INVALID_QUERY")


def main():
    print("[1/1] v2 render tests...")
    test_human_renderer_structure()
    test_human_renderer_clean_run()
    test_json_envelope_fields()
    test_json_renderer_emits_one_line_per_report()
    test_json_warn_keeps_legacy_status_ok()
    test_json_error_envelope()
    test_json_error_envelope_unstructured_fallback()
    test_ndjson_renderer_one_line_per_section()
    test_json_envelope_data_scope_present()
    test_json_envelope_data_scope_empty_when_no_scope()
    test_human_renderer_data_scope_line()
    test_human_renderer_data_scope_omitted_when_empty()
    test_ndjson_renderer_emits_leading_scope_line()
    test_ndjson_renderer_omits_scope_when_empty()
    test_ndjson_error_emits_single_line()
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
