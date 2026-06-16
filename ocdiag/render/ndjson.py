"""NDJSON renderer for v2 Reports.

Emits one JSON line per section of a Report (or a single error line when the
collector failed). Useful for streaming pipelines where consumers want to see
each section as it comes in, instead of buffering a whole envelope.

Each section line shape:

    {
      "module": "gateway",
      "section": "Gateway · listening",
      "verdict": "ok",
      "checks": [{"name": "...", "verdict": "ok", "message": "..."}, ...]
    }

Error line shape (mirrors the JSON envelope ``error`` field):

    {
      "module": "extract",
      "ok": false,
      "error": {"code": "SESSION_NOT_FOUND", "message": "...", ...}
    }
"""

from __future__ import annotations

import json
import sys
from typing import Optional, TextIO

from ..core.errors import DiagError
from ..core.types import Report


class NdjsonRenderer:
    def __init__(self, stream: Optional[TextIO] = None):
        self.stream = stream or sys.stdout

    def render_lines(self, report: Report):
        if report.error:
            err = report.diag_error or DiagError(
                code="RUNTIME_ERROR",
                message=report.error or "unknown error",
                retryable=False,
            )
            yield {
                "module": report.module_id,
                "ok": False,
                "error": err.to_dict(),
            }
            return

        if report.data_scope:
            yield {
                "module": report.module_id,
                "kind": "scope",
                "data_scope": [
                    {
                        "source": si.source,
                        "window": si.window,
                        **({"detail": si.detail} if si.detail else {}),
                    }
                    for si in report.data_scope
                ],
            }

        for section in report.sections:
            yield {
                "module": report.module_id,
                "section": section.title,
                "verdict": section.verdict.value,
                "checks": [
                    {
                        "name": c.name,
                        "verdict": c.verdict.value,
                        "message": c.message,
                    }
                    for c in section.checks
                ],
            }

    def write(self, report: Report) -> None:
        try:
            for obj in self.render_lines(report):
                self.stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.stream.flush()
        except BrokenPipeError:
            pass
