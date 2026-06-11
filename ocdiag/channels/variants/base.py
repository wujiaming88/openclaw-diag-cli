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


def apply_account_filter(
    all_ids: List[str],
    account_filter: Optional[str],
    report: "VariantReport",
) -> List[str]:
    """Narrow the account-id iteration list by ``--account``.

    Three cases:
      - ``account_filter is None`` → return ``all_ids`` unchanged.
      - filter matches an id      → iterate only ``[account_filter]``.
      - filter set but no match   → iterate nothing AND append an
        actionable note to ``report.notes`` listing the available ids,
        so the user gets a clear reason instead of a silent-empty
        diagnosis.
    """
    if not account_filter:
        return all_ids
    if account_filter in all_ids:
        return [account_filter]
    available = ", ".join(all_ids) if all_ids else "(无)"
    report.notes.append(
        f"请求的 account '{account_filter}' 未在配置中找到 — "
        f"可用账号: {available}",
    )
    return []
