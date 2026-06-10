"""feishu-lark (``@larksuite/openclaw-lark``) variant diagnostic.

Sibling of ``feishu_bundled.py``. The two plugins share the
``channels.feishu`` config block (``domain: "lark"`` is the
discriminator) but diverge in three ways the diagnostic must respect:

  1. **Webhook mode is unimplemented**. ``monitor.ts:58-61`` logs
     ``"webhook mode not implemented in monitor"`` and returns —
     i.e. configuring ``connectionMode: "webhook"`` produces a silent
     no-op. We surface this as a distinct fail finding so operators
     don't waste hours wondering why no events arrive.

  2. **Different log strings** for nearly every gate decision (the
     bundled plugin and the lark fork were written separately; the
     literals diverged). Every signature in ``_LOG_SIGNATURES`` below
     is grep-verified against
     ``repos/channel-src/openclaw-lark/src/`` — see line citations.

  3. **Lark-specific config layers**: per-group ``allowFrom``,
     per-group ``allowBots``, ``groupAllowFrom`` legacy chat-id
     entries that the runtime warns about. We diagnose what we can
     determine from config alone; runtime-specific concerns (token
     store, pairing requests, scope-check fallbacks) require an
     active environment we don't have on this host.

L5 (probe) reuses ``probe.feishu_token_probe(domain="lark")`` — the
lark endpoint at ``https://open.larksuite.com`` is the same
canonical ``tenant_access_token/internal`` shape as feishu, just a
different base host (verified in ``probe.py``).

The lark plugin is **not installed on this host**. All rules and
signatures are anchored on TS source; the test suite exercises every
rule via fixtures since we cannot reach a live lark account from CI.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .. import probe as probe_mod
from ..log_utils import extract_ts, iter_plugin_log_lines
from .base import AccountReport, Finding, VariantReport


# Lark variant has two distinct npm publishing paths (the larksuite
# fork and the m1heng-clawd vendor). Either is acceptable. We also
# accept the openclaw runtime sink (``subsystem-*.js``) because the
# logger forwards plugin emissions through it on current builds.
_FEISHU_LARK_PATH_PREFIXES = (
    "@larksuite/openclaw-lark/",
    "@m1heng-clawd/feishu/",
    "openclaw/dist/subsystem",
)


# ─── Config helpers (mirror bundled, kept in-module so the two
# variants stay independently testable) ──────────────────────────────


_DEFAULT_DM_POLICY = "pairing"
_DEFAULT_GROUP_POLICY = "allowlist"
_DEFAULT_CONNECTION_MODE = "websocket"


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value.get("source") and value.get("id"))
    return bool(value)


def _credential_label(value: Any) -> str:
    if value is None:
        return "absent"
    if isinstance(value, str):
        return "absent" if not value.strip() else "literal"
    if isinstance(value, dict):
        src = value.get("source") or "?"
        prov = value.get("provider") or "?"
        return f"ref:{src}:{prov}"
    return "unknown"


def _merge_account_config(
    feishu_cfg: Dict[str, Any], account_id: str,
) -> Dict[str, Any]:
    merged = dict(feishu_cfg)
    accounts = feishu_cfg.get("accounts")
    if isinstance(accounts, dict):
        merged.pop("accounts", None)
        sub = accounts.get(account_id)
        if isinstance(sub, dict):
            for k, v in sub.items():
                if v is not None:
                    merged[k] = v
    return merged


def _list_account_ids(feishu_cfg: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    accounts = feishu_cfg.get("accounts")
    if isinstance(accounts, dict) and accounts:
        ids.extend(sorted(accounts.keys()))
    has_top_creds = (
        _is_present(feishu_cfg.get("appId"))
        and _is_present(feishu_cfg.get("appSecret"))
    )
    if not ids:
        ids = ["default"]
    elif has_top_creds and "default" not in ids and "main" not in ids:
        ids.insert(0, "default")
    return ids


# ─── L1: config rules ────────────────────────────────────────────────


def _diagnose_account_config(
    account_id: str,
    merged: Dict[str, Any],
    sender_open_id: Optional[str] = None,
) -> AccountReport:
    """Apply L1 rules to a single account's merged config.

    Diverges from bundled in two places:
      - ``WEBHOOK_NOT_IMPLEMENTED`` (lark-specific; bundled supports
        webhook mode, lark logs "webhook mode not implemented in
        monitor" and silently returns — no events ever arrive).
      - ``DOMAIN_MISMATCH`` (lark warn): the lark plugin reads the
        same ``channels.feishu`` key as bundled, but the canonical
        domain for an installed lark plugin is "lark". When the
        account explicitly sets ``domain: "feishu"`` the operator may
        have misconfigured (probe will hit feishu.cn, not larksuite.com).
    """
    rep = AccountReport(account_id=account_id)

    app_id_val = merged.get("appId")
    app_secret_val = merged.get("appSecret")
    encrypt_key_val = merged.get("encryptKey")
    verify_token_val = merged.get("verificationToken")
    connection_mode = merged.get("connectionMode") or _DEFAULT_CONNECTION_MODE
    dm_policy = merged.get("dmPolicy") or _DEFAULT_DM_POLICY
    group_policy = merged.get("groupPolicy") or _DEFAULT_GROUP_POLICY
    require_mention = bool(merged.get("requireMention"))
    allow_from = merged.get("allowFrom") or []
    if not isinstance(allow_from, list):
        allow_from = []
    domain = merged.get("domain") or "lark"

    rep.config_summary = {
        "connection_mode": connection_mode,
        "dm_policy": dm_policy,
        "group_policy": group_policy,
        "require_mention": require_mention,
        "domain": domain,
        "credentials": {
            "appId": _credential_label(app_id_val),
            "appSecret": _credential_label(app_secret_val),
            "encryptKey": _credential_label(encrypt_key_val),
            "verificationToken": _credential_label(verify_token_val),
        },
        "allow_from_count": len(allow_from),
    }

    if not _is_present(app_id_val) or not _is_present(app_secret_val):
        rep.findings.append(Finding(
            verdict="fail", code="CRED_MISSING",
            msg=(
                f"账号 {account_id} 缺少 appId 或 appSecret — "
                "WebSocket 连接 / API 调用都会失败"
            ),
            evidence=(
                f"appId={_credential_label(app_id_val)} "
                f"appSecret={_credential_label(app_secret_val)}"
            ),
            data={
                "appId_present": _is_present(app_id_val),
                "appSecret_present": _is_present(app_secret_val),
            },
        ))

    # WEBHOOK_NOT_IMPLEMENTED — lark-specific. The schema accepts
    # connectionMode="webhook" but ``channel/monitor.ts`` line 58-61
    # bails out with a single log line and never starts an event
    # listener. Diagnose this even when webhook fields are filled in,
    # because the config is structurally valid yet semantically dead.
    if connection_mode == "webhook":
        rep.findings.append(Finding(
            verdict="fail", code="WEBHOOK_NOT_IMPLEMENTED",
            msg=(
                f"账号 {account_id} 配置 connectionMode=webhook，"
                "但 lark 插件 monitor 未实现 webhook 模式 — "
                "事件不会到达"
            ),
            evidence=(
                "channel/monitor.ts:58-61 logs "
                '"webhook mode not implemented in monitor" and returns'
            ),
            data={"connection_mode": connection_mode},
        ))

    if dm_policy == "open":
        wildcard = any(
            isinstance(x, str) and x.strip() == "*" for x in allow_from
        )
        if not wildcard:
            rep.findings.append(Finding(
                verdict="warn", code="DM_POLICY_OPEN_NO_WILDCARD",
                msg=(
                    "dmPolicy=open 但 allowFrom 未包含 \"*\" — "
                    "DM 实际仍受限，行为不自洽"
                ),
                evidence=f"allowFrom={allow_from}",
                data={"dm_policy": dm_policy,
                      "allow_from": allow_from},
            ))

    # GROUP_ALLOWFROM_LEGACY_CHATID — the runtime warns when
    # ``groupAllowFrom`` contains chat_id entries (oc_xxx) instead of
    # sender open_ids. Surface as warn so the operator can migrate.
    # Source: gate.ts:195-205 ("groupAllowFrom contains chat_id entries").
    group_allow_from = merged.get("groupAllowFrom") or []
    if isinstance(group_allow_from, list):
        legacy_chat_ids = [
            x for x in group_allow_from
            if isinstance(x, str) and x.strip().startswith("oc_")
        ]
        if legacy_chat_ids:
            rep.findings.append(Finding(
                verdict="warn", code="GROUP_ALLOWFROM_LEGACY_CHATID",
                msg=(
                    "groupAllowFrom 包含 chat_id（应仅放发送者 open_id），"
                    f"将触发运行时迁移警告 — 影响项: {legacy_chat_ids}"
                ),
                evidence=(
                    "gate.ts:195-205 splitLegacyGroupAllowFrom 触发警告"
                ),
                data={
                    "legacy_chat_ids": legacy_chat_ids,
                    "group_allow_from_size": len(group_allow_from),
                },
            ))

    # DOMAIN_MISMATCH — the lark plugin's canonical domain is "lark"
    # (open.larksuite.com). An explicit ``domain: "feishu"`` on a
    # lark-detected variant means token probes hit feishu.cn, not
    # larksuite.com — usually a config typo. Don't fire for the
    # default-or-unset case.
    if isinstance(merged.get("domain"), str) and merged.get("domain") == "feishu":
        rep.findings.append(Finding(
            verdict="warn", code="DOMAIN_MISMATCH",
            msg=(
                "lark 插件账号显式声明 domain=\"feishu\" — 凭证探测"
                "将命中 open.feishu.cn 而非 open.larksuite.com，"
                "通常是配置笔误"
            ),
            evidence=f"domain={merged.get('domain')}",
            data={"domain": merged.get("domain")},
        ))

    if sender_open_id and dm_policy == "allowlist":
        normalized_sender = sender_open_id.strip().lower()
        allow_norm = {
            x.strip().lower() for x in allow_from
            if isinstance(x, str)
        }
        if normalized_sender not in allow_norm:
            rep.findings.append(Finding(
                verdict="warn", code="GATE_SENDER_NOT_IN_ALLOWLIST",
                msg=(
                    f"查询发送者 {sender_open_id} 不在 allowFrom — "
                    "该用户的 DM 会被静默丢弃 (dmPolicy=allowlist)"
                ),
                evidence=f"allowFrom_size={len(allow_from)}",
                data={
                    "sender": sender_open_id,
                    "dm_policy": dm_policy,
                    "allow_from_size": len(allow_from),
                },
            ))

    return rep


# ─── L2/L3: log signatures ───────────────────────────────────────────


# Each pattern is anchored on a phrase the lark plugin emits LITERALLY.
# Source citations point at openclaw-lark/src/* — verified by grep on
# this host (see channel-src TS sources). Lark log strings differ from
# the bundled plugin's even where the gate concept is the same; see the
# unit-test fixtures for an exact-line check per signature.
_LOG_SIGNATURES: Dict[str, re.Pattern] = {
    # ── Connection / lifecycle ──────────────────────────────────────
    # monitor.ts:59 — webhook mode is a silent NOOP in lark
    "webhook_mode_not_implemented": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*webhook mode not implemented "
        r"in monitor",
    ),
    # monitor.ts:73 / monitor.ts:122 — startup heartbeats (informational)
    "ws_starting": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*starting WebSocket connection\.\.\.",
    ),
    "ws_started": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*WebSocket client started$",
    ),
    # ── Group gates (gate.ts) ───────────────────────────────────────
    # gate.ts:225
    "group_blocked_by_policy": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*group (?P<chat>\S+) blocked "
        r"by group-level policy",
    ),
    # gate.ts:239
    "group_disabled": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*group (?P<chat>\S+) disabled "
        r"by per-group config",
    ),
    # gate.ts:369
    "sender_not_allowed_in_group": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*sender (?P<sender>\S+) not "
        r"allowed in group (?P<chat>\S+)",
    ),
    # gate.ts:401
    "group_did_not_mention_bot": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*message in group "
        r"(?P<chat>\S+) did not mention bot",
    ),
    # gate.ts:284 / 293 / 314 — bot-sender drops
    "bot_sender_disabled": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*drop bot sender "
        r"(?P<sender>\S+)[^\n]*allowBots=false",
    ),
    "bot_sender_not_mentioned": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*drop bot sender "
        r"(?P<sender>\S+)[^\n]*allowBots=mentions, not mentioned",
    ),
    "bot_sender_no_mention": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*drop bot sender "
        r"(?P<sender>\S+)\s*\(no_mention\)",
    ),
    # ── DM gates (gate.ts) ──────────────────────────────────────────
    # gate.ts:435
    "dm_disabled_by_policy": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*DM disabled by policy, "
        r"rejecting sender (?P<sender>\S+)",
    ),
    # gate.ts:453
    "dm_sender_not_in_allowlist": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*sender (?P<sender>\S+) not "
        r"in DM allowlist",
    ),
    # gate.ts:474
    "dm_pairing_request_created": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*sender (?P<sender>\S+) not "
        r"paired, creating pairing request",
    ),
    # gate.ts:483 — pairing request creation failure
    "dm_pairing_request_failed": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*failed to create pairing "
        r"request for (?P<sender>\S+):",
    ),
    # ── Message lifecycle (event-handlers.ts / handler.ts) ──────────
    # handler.ts:106
    "skipping_empty_message": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*empty message (?P<msg>\S+) "
        r"\(no text, no media\), skipping",
    ),
    # event-handlers.ts:89
    "skipping_self_echo": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*drop self-echo message "
        r"(?P<msg>\S+)",
    ),
    # event-handlers.ts:102
    "skipping_duplicate_message": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*duplicate message "
        r"(?P<msg>\S+), skipping",
    ),
    # event-handlers.ts:108
    "message_expired": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*message (?P<msg>\S+) expired, "
        r"discarding",
    ),
    # event-handlers.ts:159 / handler.ts:237 — dispatch error
    "dispatch_failed": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*failed to dispatch message:",
    ),
    # ── VC gates (vc-meeting-invited-handler.ts) ────────────────────
    # vc-meeting-invited-handler.ts:156
    "vc_dm_disabled": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*vc invited event rejected "
        r"\(dmPolicy=disabled\)",
    ),
    # vc-meeting-invited-handler.ts:192
    "vc_inviter_not_in_allowlist": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*vc invited event rejected "
        r"\(dmPolicy=(?P<dm_policy>[^,]+), inviter not in allowlist\)",
    ),
}


_SIGNATURE_LABELS: Dict[str, str] = {
    "webhook_mode_not_implemented": (
        "webhook 模式未在 monitor 中实现 — 事件不会到达"
    ),
    "ws_starting": "WebSocket 启动中",
    "ws_started": "WebSocket 已就绪",
    "group_blocked_by_policy": "群被 group-level 策略拦截",
    "group_disabled": "群被 per-group 配置禁用",
    "sender_not_allowed_in_group": "发送者不在群 sender allowlist",
    "group_did_not_mention_bot": "群消息未 @ 机器人 (requireMention)",
    "bot_sender_disabled": "Bot 发送者被禁止 (allowBots=false)",
    "bot_sender_not_mentioned": "Bot 发送者未 @ (allowBots=mentions)",
    "bot_sender_no_mention": "Bot 发送者命中 requireMention",
    "dm_disabled_by_policy": "DM 被 dmPolicy=disabled 拒绝",
    "dm_sender_not_in_allowlist": "DM 发送者不在 allowlist",
    "dm_pairing_request_created": "DM pairing 请求已发起",
    "dm_pairing_request_failed": "DM pairing 请求创建失败",
    "skipping_empty_message": "空消息被丢弃",
    "skipping_self_echo": "丢弃自循环消息",
    "skipping_duplicate_message": "重复消息被丢弃",
    "message_expired": "消息超时被丢弃",
    "dispatch_failed": "dispatch 失败",
    "vc_dm_disabled": "VC 邀请被 dmPolicy=disabled 拒绝",
    "vc_inviter_not_in_allowlist": "VC 邀请者不在 DM allowlist",
}


def _scan_log_line(line: str) -> Optional[Tuple[str, Dict[str, str]]]:
    for key, pat in _LOG_SIGNATURES.items():
        m = pat.search(line)
        if m:
            return key, {k: v for k, v in m.groupdict().items() if v}
    return None


def _scan_log_files(log_files: List[str]) -> Dict[str, Any]:
    counts: Dict[str, int] = {k: 0 for k in _LOG_SIGNATURES}
    samples: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _LOG_SIGNATURES}
    SAMPLES_PER_SIG = 3

    for obj, message, basename in iter_plugin_log_lines(
        log_files, _FEISHU_LARK_PATH_PREFIXES,
    ):
        if "feishu" not in message:
            continue
        hit = _scan_log_line(message)
        if hit is None:
            continue
        key, fields = hit
        counts[key] += 1
        if len(samples[key]) < SAMPLES_PER_SIG:
            samples[key].append({
                "ts": extract_ts(obj),
                "fields": fields,
                "log_path": basename,
            })
    return {"counts": counts, "samples": samples}


def _build_log_findings(scan: Dict[str, Any]) -> List[Finding]:
    counts = scan["counts"]
    samples = scan["samples"]
    out: List[Finding] = []

    fail_keys = {
        # silent-NOOP: webhook mode reaches the log line and nothing
        # else ever happens. fail.
        "webhook_mode_not_implemented",
        "dispatch_failed",
    }
    warn_keys = {
        "group_blocked_by_policy",
        "group_disabled",
        "sender_not_allowed_in_group",
        "group_did_not_mention_bot",
        "bot_sender_disabled",
        "bot_sender_not_mentioned",
        "bot_sender_no_mention",
        "dm_disabled_by_policy",
        "dm_sender_not_in_allowlist",
        "dm_pairing_request_failed",
        "vc_dm_disabled",
        "vc_inviter_not_in_allowlist",
    }

    for key, count in counts.items():
        if count == 0:
            continue
        verdict = (
            "fail" if key in fail_keys
            else "warn" if key in warn_keys
            else "ok"
        )
        label = _SIGNATURE_LABELS.get(key, key)
        sample_lines = []
        for s in samples.get(key, []):
            field_str = " ".join(
                f"{k}={v}" for k, v in (s.get("fields") or {}).items()
            )
            ts = s.get("ts") or "?"
            sample_lines.append(f"  [{ts}] {field_str}".rstrip())
        out.append(Finding(
            verdict=verdict, code=f"LOG_{key.upper()}",
            msg=f"日志命中: {label} ({count} 次)",
            evidence="\n".join(sample_lines) if sample_lines else None,
            data={
                "signature": key,
                "count": count,
                "samples": samples.get(key, []),
            },
        ))
    return out


# ─── L5: probe ───────────────────────────────────────────────────────


def _run_probe(
    account_id: str, merged: Dict[str, Any], cfg: Dict[str, Any],
) -> Finding:
    """Resolve credentials and probe the lark token endpoint.

    Reuses ``probe.feishu_token_probe`` with ``domain="lark"``: the
    request shape is identical (same ``auth/v3/tenant_access_token/
    internal`` path) but the base host resolves to
    ``https://open.larksuite.com``. When ``merged.domain`` is an
    explicit ``https://`` URL we honor it (self-deployed lark).
    """
    app_id_val = merged.get("appId")
    app_secret_val = merged.get("appSecret")
    raw_domain = merged.get("domain")
    # Default lark variant to the lark endpoint regardless of whether
    # the operator set the field — the variant ID told us already.
    if isinstance(raw_domain, str) and raw_domain.startswith("https://"):
        domain = raw_domain
    else:
        domain = "lark"

    if not _is_present(app_id_val):
        return Finding(
            verdict="warn", code="PROBE_SKIPPED_NO_APP_ID",
            msg=f"账号 {account_id} 未配置 appId — 跳过主动探测",
            data={"account_id": account_id},
        )

    app_id_resolution = probe_mod.resolve_secret_ref(app_id_val, cfg)
    if not app_id_resolution.ok:
        return Finding(
            verdict="warn", code="PROBE_SKIPPED_APPID_UNRESOLVED",
            msg=(
                f"账号 {account_id} appId 解析失败 — 跳过主动探测: "
                f"{app_id_resolution.msg}"
            ),
            data={"account_id": account_id,
                  "app_id_ref": app_id_resolution.ref_label},
        )

    app_secret_resolution = probe_mod.resolve_secret_ref(app_secret_val, cfg)
    if not app_secret_resolution.ok:
        return Finding(
            verdict="warn", code="SECRET_UNRESOLVED",
            msg=(
                f"账号 {account_id} appSecret 解析失败 — 跳过主动探测: "
                f"{app_secret_resolution.msg}"
            ),
            evidence=f"ref={app_secret_resolution.ref_label}",
            data={
                "account_id": account_id,
                "app_secret_ref": app_secret_resolution.ref_label,
                "skipped": True,
            },
        )

    result = probe_mod.feishu_token_probe(
        app_id=app_id_resolution.value,
        app_secret=app_secret_resolution.value,
        domain=domain,
    )

    base_data = {
        "account_id": account_id,
        "domain": result.domain,
        "state": result.state,
        "api_code": result.api_code,
        "app_secret_ref": app_secret_resolution.ref_label,
    }
    base_data.update(result.extra)

    if result.state == "valid":
        return Finding(
            verdict="ok", code="PROBE_VALID",
            msg=(
                f"账号 {account_id} 凭证有效 (tenant_access_token "
                f"获取成功, expire={result.extra.get('expire_s', '?')}s)"
            ),
            data=base_data,
        )
    if result.state == "invalid":
        return Finding(
            verdict="fail", code="PROBE_INVALID",
            msg=(
                f"账号 {account_id} 凭证被 Lark 拒绝: "
                f"code={result.api_code} msg={result.msg}"
            ),
            evidence=f"endpoint={result.domain}",
            data=base_data,
        )
    return Finding(
        verdict="warn", code="PROBE_UNREACHABLE",
        msg=(
            f"账号 {account_id} 主动探测无法到达 Lark API "
            f"(网络/超时/DNS): {result.msg}"
        ),
        evidence=f"endpoint={result.domain}",
        data=base_data,
    )


# ─── Variant entry point ─────────────────────────────────────────────


def diagnose(
    cfg: Dict[str, Any],
    log_files: List[str],
    detect_basis: str,
    do_probe: bool = False,
    sender_open_id: Optional[str] = None,
) -> VariantReport:
    """Run all enabled layers for feishu-lark.

    Note: lark and bundled both consume ``channels.feishu``. When the
    detected variant is lark but the config block is missing we surface
    the same "config absent" note bundled does — the variant detection
    layer already disambiguated bundled vs lark before we got here.
    """
    report = VariantReport(variant="feishu-lark", detect_basis=detect_basis)

    feishu_cfg = (
        (cfg.get("channels") or {}).get("feishu")
        if isinstance(cfg, dict) else None
    )
    if not isinstance(feishu_cfg, dict):
        report.notes.append(
            "检测到 feishu-lark 包但 channels.feishu 配置缺失 — "
            "插件不会启动",
        )
        return report

    for account_id in _list_account_ids(feishu_cfg):
        merged = _merge_account_config(feishu_cfg, account_id)
        acct_rep = _diagnose_account_config(
            account_id, merged, sender_open_id=sender_open_id,
        )
        report.accounts.append(acct_rep)

        if do_probe:
            report.probe_findings.append(_run_probe(account_id, merged, cfg))

    log_scan = _scan_log_files(log_files)
    report.log_findings = _build_log_findings(log_scan)

    return report
