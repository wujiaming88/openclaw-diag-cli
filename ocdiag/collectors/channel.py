"""channel collector — IM channel plugin diagnostics.

This is the single-command surface for diagnosing "no reply" / "no
response" failures across all IM channel variants (feishu-bundled in
P0; lark / dingtalk / wecom stubbed for later phases). The flow:

  1. Detect installed variants (npm package + config keys).
  2. For each detected variant, run the variant module's diagnose():
        L1 — config rules (always)
        L2/L3 — log signature scan (always)
        L5 — active credential probe (only when ctx.probe is True)
  3. Fold all findings into ``Report`` sections, one per variant.

Design decisions worth calling out:

  - We DO NOT split this into multiple collector ids. "channel" is the
    diagnostic dimension; config / log / probe are layers within it.
    Splitting would force users to learn three commands when one
    suffices.
  - ``--probe`` plumbing lives in main.py / DiagContext; reading
    ``ctx.probe`` here lets the variant modules stay context-free
    (they only see ``do_probe: bool``).
  - The collector treats secret strings as poison — they enter the
    probe call site and exit (only) into the urllib request. They are
    NEVER copied into Report.data, evidence, or detail strings; the
    variant modules carry that contract too.
  - **Self-pollution defence** (see ``channels/log_utils.py``): the
    L2/L3 log scan is double-protected against the openclaw gateway's
    console relay echoing assistant chat back into the same log file.
    Variants iterate via ``iter_plugin_log_lines`` which (1) parses
    each JSON line and rejects any whose ``_meta.path.fullFilePath``
    matches a relay sink (``/dist/console-*``) and only accepts paths
    inside the plugin's own dist tree (or the openclaw runtime's
    plugin-forwarding sink); (2) anchors every signature regex with
    ``^`` so a phrase embedded mid-sentence cannot register as a hit.
    Without this we previously self-pollinated every diagnostic run
    by capturing our own report back through the relay.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

from .. import recent_logs
from ..channels import detect as channel_detect
from ..channels.variants import (
    base as variant_base,
    dingtalk as variant_dingtalk,
    feishu_bundled as variant_feishu_bundled,
    feishu_lark as variant_feishu_lark,
    wecom as variant_wecom,
)
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict


# Map variant id → variant module exposing ``diagnose``. Stable order
# so the output is deterministic when multiple variants are detected.
_VARIANT_DISPATCH = [
    ("feishu-bundled", variant_feishu_bundled),
    ("feishu-lark", variant_feishu_lark),
    ("dingtalk", variant_dingtalk),
    ("wecom", variant_wecom),
]


# Convert the variant-layer string verdicts to the core Verdict enum so
# Section.add can accept them. Keeps variant modules dependency-free.
_VERDICT_FROM_STR = {
    "ok": Verdict.OK,
    "warn": Verdict.WARN,
    "fail": Verdict.FAIL,
}


def _coerce_verdict(s: str) -> Verdict:
    return _VERDICT_FROM_STR.get(s, Verdict.WARN)


def _add_finding(section: Section, finding: variant_base.Finding) -> None:
    """Lift a variant-layer Finding into a Section check."""
    section.add(
        name=finding.code,
        verdict=_coerce_verdict(finding.verdict),
        message=finding.msg,
        evidence=finding.evidence,
        data=finding.data or None,
    )


def _format_account_summary(account: variant_base.AccountReport) -> str:
    """Compact one-line summary for an account block (human render)."""
    cs = account.config_summary
    if not cs:
        return f"账号 {account.account_id}: (无配置摘要)"
    creds = cs.get("credentials", {})
    bits = [
        f"connectionMode={cs.get('connection_mode', '?')}",
        f"dmPolicy={cs.get('dm_policy', '?')}",
        f"groupPolicy={cs.get('group_policy', '?')}",
        f"requireMention={cs.get('require_mention', False)}",
        f"domain={cs.get('domain', '?')}",
        f"appId={creds.get('appId', '?')}",
        f"appSecret={creds.get('appSecret', '?')}",
    ]
    return f"账号 {account.account_id}: " + " | ".join(bits)


def _section_for_variant(
    report: Report,
    variant_id: str,
    variant_report: variant_base.VariantReport,
) -> None:
    """Emit Sections for a single variant's diagnosis."""
    config_section = report.section(
        f"{variant_id} · L1 配置 ({variant_report.detect_basis})",
    )
    if variant_report.notes:
        config_section.ok(
            "channel.detect_note",
            "; ".join(variant_report.notes),
            data={"notes": variant_report.notes},
        )

    if not variant_report.accounts:
        config_section.ok(
            "channel.no_accounts",
            f"{variant_id}: 未发现可诊断的 account",
            data={"variant": variant_id},
        )
    else:
        # Per-account: a header check (always ok — it's metadata, not
        # a verdict) then one check per finding so the renderer shows
        # them inline with the right glyph.
        for account in variant_report.accounts:
            # Summary is built ENTIRELY from non-secret metadata labels
            # (``literal`` / ``ref:<source>:<provider>`` / policy strings)
            # by ``_format_account_summary`` — no real credential value
            # ever lands here. Skipping ``sanitize_text`` keeps the
            # provider hint visible (e.g. "ref:file:lark-secrets") which
            # the kv-bare regex would otherwise mask.
            summary = _format_account_summary(account)
            config_section.ok(
                f"channel.account.{account.account_id}",
                summary,
                data={
                    "account_id": account.account_id,
                    "config_summary": account.config_summary,
                },
            )
            for f in account.findings:
                _add_finding(config_section, f)

    log_section = report.section(f"{variant_id} · L2/L3 日志签名")
    if not variant_report.log_findings:
        log_section.ok(
            "channel.log.clean",
            f"{variant_id}: 未观测到已知断点 / 丢弃日志",
            data={"variant": variant_id},
        )
    else:
        for f in variant_report.log_findings:
            _add_finding(log_section, f)

    # Probe section is omitted entirely when probe wasn't requested —
    # we don't want to fake an "ok" probe section for the passive run.
    if variant_report.probe_findings:
        probe_section = report.section(f"{variant_id} · L5 主动凭证探测")
        for f in variant_report.probe_findings:
            _add_finding(probe_section, f)


def _resolve_log_files(ctx: DiagContext) -> List[str]:
    """Pick the candidate log files for a 7d-window scan.

    ``recent_logs.discover_recent_logs`` only looks at today's log.
    For channel diagnostics we want a wider window — drop signals
    from earlier in the week are still useful (e.g. "your DM has been
    blocked every day this week"). Walk the openclaw-YYYY-MM-DD.log
    files directly with a 7-day floor.
    """
    log_dir = str(ctx.log_dir)
    if not os.path.isdir(log_dir):
        return []
    import glob
    pattern = os.path.join(log_dir, "openclaw-*.log")
    cutoff = time.time() - 7 * 86400
    matched: List = []
    for path in glob.glob(pattern):
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        if mt >= cutoff:
            matched.append((mt, path))
    matched.sort(reverse=True)
    return [p for _, p in matched]


@register
class ChannelCollector:
    id = "channel"
    title = "渠道诊断"
    kind = "state"

    def collect(
        self, ctx: DiagContext, **kwargs,
    ) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)
        report.data["probe_enabled"] = bool(getattr(ctx, "probe", False))

        cfg = ctx.config or {}
        npm_root = channel_detect.npm_projects_root(str(ctx.openclaw_home))
        detected = channel_detect.detect_variants(npm_root, cfg)

        report.data["detected_variants"] = [
            {
                "variant": d.variant,
                "detect_basis": d.detect_basis,
                "package_path": d.package_path,
                "config_key": d.config_key,
                "ambiguous": d.ambiguous,
            }
            for d in detected
        ]

        # Top-level summary section first — gives a "what was detected"
        # answer regardless of how the variants come out.
        head = report.section("0. 检测概览")
        head.ok(
            "channel.summary",
            (
                f"检测到 {len(detected)} 个变体" if detected
                else "未检测到任何已知 IM 渠道变体"
            ),
            evidence=(
                "; ".join(
                    f"{d.variant}({d.detect_basis})" for d in detected
                ) or None
            ),
            data={
                "variant_count": len(detected),
                "probe": bool(getattr(ctx, "probe", False)),
            },
        )

        if not detected:
            head.add(
                name="NO_CHANNEL_DETECTED",
                verdict=Verdict.OK,
                message=(
                    "未在 ~/.openclaw/npm/projects 下找到 feishu/lark/"
                    "dingtalk/wecom 包，channels.* 配置也无对应键 — 跳过诊断"
                ),
                data={"npm_root": npm_root},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        log_files = _resolve_log_files(ctx)
        report.data["log_files_scanned"] = [
            os.path.basename(p) for p in log_files
        ]

        # ``--sender`` parameter — kwargs path (used by tests calling
        # ``coll.collect(ctx, sender_open_id=...)`` directly) takes
        # precedence; CLI runs reach us via ``ctx.sender_open_id`` which
        # ``main._build_context`` populates from ``args.sender``.
        sender_open_id: Optional[str] = (
            kwargs.get("sender_open_id")
            or getattr(ctx, "sender_open_id", None)
        )

        # ``--account`` parameter — same precedence shape as --sender.
        # When set, variant diagnoses iterate only the matching account
        # id, killing cross-account noise from rules like
        # GATE_SENDER_NOT_IN_ALLOWLIST that fire per-account.
        account_filter: Optional[str] = (
            kwargs.get("account_filter")
            or getattr(ctx, "account_id", None)
        )
        report.data["account_filter"] = account_filter

        # Group detected variants by id so multiple-evidence (npm + config)
        # collapses into one diagnosis run.
        seen: set = set()
        for d in detected:
            if d.variant in seen:
                continue
            seen.add(d.variant)
            module = next(
                (m for vid, m in _VARIANT_DISPATCH if vid == d.variant),
                None,
            )
            if module is None:
                head.add(
                    name="VARIANT_UNKNOWN",
                    verdict=Verdict.WARN,
                    message=(
                        f"变体 {d.variant} 未注册诊断模块 — 跳过"
                    ),
                    data={"variant": d.variant},
                )
                continue
            variant_report = module.diagnose(
                cfg=cfg,
                log_files=log_files,
                detect_basis=d.detect_basis,
                do_probe=bool(getattr(ctx, "probe", False)),
                sender_open_id=sender_open_id,
                account_filter=account_filter,
            )
            _section_for_variant(report, d.variant, variant_report)

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
