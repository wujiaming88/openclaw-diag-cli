"""Shared types for channel-variant diagnostics.

Variant modules return a ``VariantReport`` describing every account
they could resolve from config + the per-account findings. The
``channel`` collector folds these into the ``Report`` envelope —
variants don't talk to ``Section`` directly so they stay easy to
fixture and unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Finding:
    """One observation about an account.

    ``verdict`` is one of ``"ok" | "warn" | "fail"`` (string, not the
    Verdict enum, so variant modules don't depend on core types — keeps
    them trivially testable). The collector lifts these into Section.add
    calls when assembling the Report.
    """
    verdict: str
    code: str
    msg: str
    evidence: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountReport:
    account_id: str
    findings: List[Finding] = field(default_factory=list)
    config_summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VariantReport:
    variant: str
    detect_basis: str
    accounts: List[AccountReport] = field(default_factory=list)
    log_findings: List[Finding] = field(default_factory=list)
    probe_findings: List[Finding] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
