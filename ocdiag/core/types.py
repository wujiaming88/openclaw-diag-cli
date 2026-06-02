"""Core domain types for openclaw-diag v2."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Verdict(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"

    def __ge__(self, other):
        order = {Verdict.OK: 0, Verdict.WARN: 1, Verdict.FAIL: 2}
        return order[self] >= order[other]

    def __gt__(self, other):
        order = {Verdict.OK: 0, Verdict.WARN: 1, Verdict.FAIL: 2}
        return order[self] > order[other]

    @classmethod
    def worst(cls, *verdicts: "Verdict") -> "Verdict":
        if not verdicts:
            return cls.OK
        return max(verdicts, key=lambda v: {cls.OK: 0, cls.WARN: 1, cls.FAIL: 2}[v])


@dataclass
class Check:
    """Atomic check result — the minimal unit a collector produces."""
    name: str
    verdict: Verdict
    message: str
    detail: Optional[str] = None
    evidence: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


@dataclass
class Section:
    """A group of related checks."""
    title: str
    checks: List[Check] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        if not self.checks:
            return Verdict.OK
        return Verdict.worst(*(c.verdict for c in self.checks))

    def add(self, name: str, verdict: Verdict, message: str, **kwargs) -> Check:
        """Convenience: create and append a Check."""
        check = Check(name=name, verdict=verdict, message=message, **kwargs)
        self.checks.append(check)
        return check

    def ok(self, name: str, message: str, **kwargs) -> Check:
        return self.add(name, Verdict.OK, message, **kwargs)

    def warn(self, name: str, message: str, **kwargs) -> Check:
        return self.add(name, Verdict.WARN, message, **kwargs)

    def fail(self, name: str, message: str, **kwargs) -> Check:
        return self.add(name, Verdict.FAIL, message, **kwargs)


@dataclass
class Report:
    """Complete output of one collector."""
    module_id: str
    title: str
    sections: List[Section] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> Verdict:
        if self.error:
            return Verdict.FAIL
        if not self.sections:
            return Verdict.OK
        return Verdict.worst(*(s.verdict for s in self.sections))

    @property
    def summary(self) -> Dict[str, int]:
        checks = [c for s in self.sections for c in s.checks]
        return {
            "pass": sum(1 for c in checks if c.verdict == Verdict.OK),
            "warn": sum(1 for c in checks if c.verdict == Verdict.WARN),
            "fail": sum(1 for c in checks if c.verdict == Verdict.FAIL),
            "total": len(checks),
        }

    def section(self, title: str) -> Section:
        """Create and append a new section."""
        s = Section(title=title)
        self.sections.append(s)
        return s
