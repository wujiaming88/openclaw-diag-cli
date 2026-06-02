"""Structured diagnostic errors.

Used by inspectors / collectors to attach machine-readable failure
information that the JSON renderer surfaces in the envelope:

    {"ok": false, "error": {"code", "message", "retryable", "hint", "details"}}

Common codes:
    SESSION_NOT_FOUND   session UUID not found
    AMBIGUOUS_SESSION   prefix matches multiple sessions
    INVALID_QUERY       bad session ID format
    FILE_READ_ERROR     can't read config/log/session file
    COMMAND_NOT_FOUND   unknown subcommand
    MISSING_ARGUMENT    required arg missing
    RUNTIME_ERROR       unexpected exception in collector
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class DiagError:
    code: str
    message: str
    retryable: bool = False
    hint: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = self.details
        return out


# Exit code conventions
EXIT_OK = 0
EXIT_WARN_OR_FAIL = 1
EXIT_INPUT_ERROR = 2
EXIT_RUNTIME_ERROR = 3


# Map error codes → exit codes
_INPUT_CODES = {
    "SESSION_NOT_FOUND",
    "AMBIGUOUS_SESSION",
    "INVALID_QUERY",
    "COMMAND_NOT_FOUND",
    "MISSING_ARGUMENT",
}


def exit_code_for(error: Optional[DiagError]) -> int:
    if error is None:
        return EXIT_OK
    if error.code in _INPUT_CODES:
        return EXIT_INPUT_ERROR
    return EXIT_RUNTIME_ERROR
