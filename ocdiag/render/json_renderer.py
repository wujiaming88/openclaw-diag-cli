"""Structured JSON renderer for v2 Reports.

Envelope:
    {
      "module": "<id>",
      "status": "ok" | "error",          # legacy 2-state
      "verdict": "ok" | "warn" | "fail",  # new 3-state
      "summary": {"pass": N, "warn": N, "fail": N, "total": N},
      "elapsed_ms": int,
      "sections": [...],
      "data": {...},
      "error": "..."  # only when present
    }
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, TextIO

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


def to_envelope(report: Report) -> Dict[str, Any]:
    verdict = report.verdict
    legacy_status = "error" if verdict == Verdict.FAIL else "ok"
    payload: Dict[str, Any] = {
        "module": report.module_id,
        "status": legacy_status,
        "verdict": verdict.value,
        "summary": report.summary,
        "elapsed_ms": int(report.elapsed_ms),
        "sections": [_section_to_dict(s) for s in report.sections],
        "data": report.data,
    }
    if report.error:
        payload["error"] = report.error
    return payload


class JsonRenderer:
    def __init__(self, stream: Optional[TextIO] = None, ndjson: bool = False):
        self.stream = stream or sys.stdout
        self.ndjson = ndjson

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
