"""Structured JSON renderer for v2 Reports.

Envelope (v1.1+):
    {
      "ok": bool,
      "data": {
        "module": "<id>",
        "verdict": "ok" | "warn" | "fail",
        "summary": {"pass": N, "warn": N, "fail": N, "total": N},
        "elapsed_ms": int,
        "sections": [...],
        "data": {...},
        "status": "ok" | "error"           # legacy 2-state, kept for compat
      } | null,
      "error": {
        "code": "...",
        "message": "...",
        "retryable": bool,
        "hint": "...",
        "details": {...}
      } | null
    }

When ``report.error`` is set, ``data`` is null and ``error`` is populated.
``error`` falls back to a generic ``RUNTIME_ERROR`` envelope when only the
legacy string error is available (no structured ``DiagError``).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, TextIO

from ..core.errors import DiagError
from ..core.types import Check, Report, Section, Verdict


def _check_to_dict(c: Check) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "name": c.name,
        "verdict": c.verdict.value,
        "message": c.message,
    }
    if c.detail is not None:
        out["detail"] = c.detail
    if c.evidence is not None:
        out["evidence"] = c.evidence
    if c.data is not None:
        out["data"] = c.data
    return out


def _section_to_dict(s: Section) -> Dict[str, Any]:
    return {
        "title": s.title,
        "verdict": s.verdict.value,
        "checks": [_check_to_dict(c) for c in s.checks],
    }


def _format_error(report: Report) -> Dict[str, Any]:
    if report.diag_error is not None:
        return report.diag_error.to_dict()
    # Fall back to a generic RUNTIME_ERROR envelope.
    return DiagError(
        code="RUNTIME_ERROR",
        message=report.error or "unknown error",
        retryable=False,
    ).to_dict()


def to_envelope(report: Report) -> Dict[str, Any]:
    if report.error:
        return {
            "ok": False,
            "data": None,
            "error": _format_error(report),
        }

    verdict = report.verdict
    legacy_status = "error" if verdict == Verdict.FAIL else "ok"
    data: Dict[str, Any] = {
        "module": report.module_id,
        "verdict": verdict.value,
        "summary": report.summary,
        "elapsed_ms": int(report.elapsed_ms),
        "sections": [_section_to_dict(s) for s in report.sections],
        "data": report.data,
        "status": legacy_status,
    }
    return {"ok": True, "data": data, "error": None}


class JsonRenderer:
    def __init__(self, stream: Optional[TextIO] = None):
        self.stream = stream or sys.stdout

    def render(self, report: Report) -> str:
        return json.dumps(to_envelope(report), ensure_ascii=False)

    def write(self, report: Report) -> None:
        try:
            self.stream.write(self.render(report))
            self.stream.write("\n")
            self.stream.flush()
        except BrokenPipeError:
            pass


def render(report: Report) -> str:
    return JsonRenderer().render(report)
