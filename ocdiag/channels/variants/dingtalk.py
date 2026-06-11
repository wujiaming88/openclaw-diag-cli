"""dingtalk (``@dingtalk-real-ai/dingtalk-connector``) variant diagnostic.

Sibling of ``feishu_*.py``. The DingTalk plugin is structurally similar
(multi-account, gate-based DM/group filtering, optional pairing) but
diverges in three ways the diagnostic must respect:

  1. **No connectionMode field**. The plugin ships a single transport
     (DingTalk DWClient stream — long-poll over WebSocket); the
     ``endpoint`` / ``debug`` fields tune that transport but there is
     no webhook/websocket switch like Feishu has.
  2. **dmPolicy=pairing is unimplemented at runtime** —
     ``core/message-handler.ts:1003`` warns ``dmPolicy="pairing" 暂不
     支持，将按 "open" 策略处理`` and falls back to open. We surface
     this as a warn-level L1 finding because it silently relaxes a
     security-relevant policy.
  3. **Different log prefixes**. The plugin's logger writes
     ``[DingTalk:<accountId>] <msg>`` for the gate path
     (``core/message-handler.ts``), bare ``dingtalk-connector[<acct>]
     ...`` from the gateway startAccount hook (``channel.ts:444``),
     and the connection retry loop (``core/connection.ts``) emits
     plain message-only lines (``连接建立失败: <err>``,
     ``重连失败：<err>``, ``收到服务端 disconnect topic，即将重连``,
     ``SDK reconnecting...``, ``✅ SDK reconnected successfully``,
     ``✅ 重连成功 (socket 状态=<X>)``). Every signature here is
     grep-verified against the upstream source — see
     ``repos/channel-src/dingtalk-openclaw-connector/src``.

L5 (probe) hits ``probe.dingtalk_token_probe`` which posts to
``api.dingtalk.com/v1.0/oauth2/accessToken`` with
``{"appKey": clientId, "appSecret": clientSecret}`` (verified in
``probe.ts:101-104``).

The dingtalk plugin is **not installed on this host**. All rules and
signatures are anchored on TS source; the test suite exercises every
rule via fixtures since we cannot reach a live DingTalk corp from CI.

Config block: ``channels.dingtalk-connector`` (the upstream's
canonical key — see ``CHANNEL_ID = "dingtalk-connector"`` at
``channel.ts:35`` and the schema/superRefine messages quoting
``channels.dingtalk-connector.*``). For backward compatibility with
older / abbreviated configs we ALSO accept ``channels.dingtalk`` —
the detect layer fans out via ``_CONFIG_KEY_TO_VARIANTS["dingtalk"]``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .. import probe as probe_mod
from ..log_utils import extract_ts, iter_plugin_log_lines
from .base import AccountReport, Finding, VariantReport, apply_account_filter


# DingTalk plugin npm scope is ``@dingtalk-real-ai``. Include the
# openclaw runtime sink for the same forwarding reason as the feishu
# variants.
_DINGTALK_PATH_PREFIXES = (
    "@dingtalk-real-ai/",
    "openclaw/dist/subsystem",
)


_DEFAULT_DM_POLICY = "open"
_DEFAULT_GROUP_POLICY = "open"


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # clientId may be numeric per the zod schema:
        # ``clientId: z.union([z.string(), z.number()])`` — treat any
        # non-zero number as present (zero is unusual but configurable).
        return True
    if isinstance(value, dict):
        return bool(value.get("source") and value.get("id"))
    return bool(value)


def _credential_label(value: Any) -> str:
    if value is None:
        return "absent"
    if isinstance(value, str):
        return "absent" if not value.strip() else "literal"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "literal"
    if isinstance(value, dict):
        src = value.get("source") or "?"
        prov = value.get("provider") or "?"
        return f"ref:{src}:{prov}"
    return "unknown"


def _extract_dingtalk_cfg(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read the per-variant config block.

    Upstream's canonical key is ``channels.dingtalk-connector`` (see
    ``CHANNEL_ID`` in channel.ts:35 + schema superRefine error strings).
    We also accept the shorter ``channels.dingtalk`` form because it's
    the form mentioned in the design doc + matches our detect.py
    config-fallback table.
    """
    if not isinstance(cfg, dict):
        return None
    channels = cfg.get("channels") or {}
    if not isinstance(channels, dict):
        return None
    for key in ("dingtalk-connector", "dingtalk"):
        block = channels.get(key)
        if isinstance(block, dict):
            return block
    return None


def _merge_account_config(
    dingtalk_cfg: Dict[str, Any], account_id: str,
) -> Dict[str, Any]:
    merged = dict(dingtalk_cfg)
    accounts = dingtalk_cfg.get("accounts")
    if isinstance(accounts, dict):
        merged.pop("accounts", None)
        sub = accounts.get(account_id)
        if isinstance(sub, dict):
            for k, v in sub.items():
                if v is not None:
                    merged[k] = v
    return merged


def _list_account_ids(dingtalk_cfg: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    accounts = dingtalk_cfg.get("accounts")
    if isinstance(accounts, dict) and accounts:
        ids.extend(sorted(accounts.keys()))
    has_top_creds = (
        _is_present(dingtalk_cfg.get("clientId"))
        and _is_present(dingtalk_cfg.get("clientSecret"))
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
    rep = AccountReport(account_id=account_id)

    client_id_val = merged.get("clientId")
    client_secret_val = merged.get("clientSecret")
    dm_policy = merged.get("dmPolicy") or _DEFAULT_DM_POLICY
    group_policy = merged.get("groupPolicy") or _DEFAULT_GROUP_POLICY
    require_mention = bool(merged.get("requireMention"))
    allow_from = merged.get("allowFrom") or []
    if not isinstance(allow_from, list):
        allow_from = []
    group_allow_from = merged.get("groupAllowFrom") or []
    if not isinstance(group_allow_from, list):
        group_allow_from = []
    endpoint = merged.get("endpoint")
    debug = bool(merged.get("debug"))

    rep.config_summary = {
        # No connection_mode for dingtalk — single transport.
        "connection_mode": "stream",
        "dm_policy": dm_policy,
        "group_policy": group_policy,
        "require_mention": require_mention,
        "endpoint": endpoint or "(default)",
        "debug": debug,
        "credentials": {
            "clientId": _credential_label(client_id_val),
            "clientSecret": _credential_label(client_secret_val),
        },
        "allow_from_count": len(allow_from),
        "group_allow_from_count": len(group_allow_from),
    }

    if not _is_present(client_id_val) or not _is_present(client_secret_val):
        rep.findings.append(Finding(
            verdict="fail", code="CRED_MISSING",
            msg=(
                f"账号 {account_id} 缺少 clientId 或 clientSecret — "
                "DWS Stream 连接 / API 调用都会失败"
            ),
            evidence=(
                f"clientId={_credential_label(client_id_val)} "
                f"clientSecret={_credential_label(client_secret_val)}"
            ),
            data={
                "clientId_present": _is_present(client_id_val),
                "clientSecret_present": _is_present(client_secret_val),
            },
        ))

    # DM_POLICY_PAIRING_UNSUPPORTED — runtime degrades pairing → open.
    # Source: core/message-handler.ts:1004 emits literal
    # ``dmPolicy="pairing" 暂不支持，将按 "open" 策略处理`` (warn).
    # Surface at config-time so operators don't ship a pairing config
    # under the false belief that DMs are gated.
    if dm_policy == "pairing":
        rep.findings.append(Finding(
            verdict="warn", code="DM_POLICY_PAIRING_UNSUPPORTED",
            msg=(
                "dmPolicy=pairing 在 dingtalk-connector 暂不支持 — "
                "运行时将退化为 open，DM 不会受白名单保护"
            ),
            evidence=(
                "core/message-handler.ts:1004 "
                'logs `dmPolicy="pairing" 暂不支持，将按 "open" 策略处理`'
            ),
            data={"dm_policy": dm_policy},
        ))

    # DM_ALLOWLIST_EMPTY / GROUP_ALLOWLIST_EMPTY — schema's superRefine
    # would normally reject these, but multi-account inheritance can
    # leave a sub-account with an empty list at runtime; the gate path
    # then warns and rejects everything (message-handler.ts:1021 / 1091).
    if dm_policy == "allowlist" and len(allow_from) == 0:
        rep.findings.append(Finding(
            verdict="warn", code="DM_ALLOWLIST_EMPTY",
            msg=(
                f"账号 {account_id}: dmPolicy=allowlist 但 allowFrom 为空 — "
                "运行时将拒绝所有 DM"
            ),
            evidence=(
                "message-handler.ts:1021 → "
                "`[DingTalk] DM 被拦截: allowFrom 白名单为空，拒绝所有请求`"
            ),
            data={"dm_policy": dm_policy, "allow_from_size": 0},
        ))

    if group_policy == "allowlist" and len(group_allow_from) == 0:
        rep.findings.append(Finding(
            verdict="warn", code="GROUP_ALLOWLIST_EMPTY",
            msg=(
                f"账号 {account_id}: groupPolicy=allowlist 但 "
                "groupAllowFrom 为空 — 运行时将拒绝所有群消息"
            ),
            evidence=(
                "message-handler.ts:1091 → "
                "`群聊被拦截: groupAllowFrom 白名单为空，拒绝所有请求`"
            ),
            data={"group_policy": group_policy, "group_allow_from_size": 0},
        ))

    if sender_open_id and dm_policy == "allowlist":
        normalized_sender = str(sender_open_id).strip().lower()
        allow_norm = {
            str(x).strip().lower() for x in allow_from
            if isinstance(x, (str, int, float))
        }
        if normalized_sender and normalized_sender not in allow_norm:
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


# Each pattern anchors on a phrase the plugin emits LITERALLY. The
# plugin uses several distinct prefixes, so patterns are written to
# match the BARE phrase rather than guess the prefix:
#   - ``[DingTalk:<accountId>] DM 被拦截: ...``  (gate path)
#   - ``[DingTalk] DM 被拦截: allowFrom 白名单为空 ...`` (one literal
#     line at message-handler.ts:1021 — note the explicit [DingTalk]
#     prefix INSIDE the message, on top of the wrapper prefix)
#   - bare ``连接建立失败: ...`` / ``重连失败：...`` (connection.ts)
#   - bare ``[<accountId>] 重连失败：...``  (connection.ts:376)
#   - bare ``dingtalk-connector[<acct>] is disabled, skipping startup``
#     (channel.ts:444 — gateway-supplied logger, no wrapper prefix)
#
# The probe-against-fixtures unit tests pin each line to its TS source
# citation so a future upstream rename forces the test to fail loudly.
# Optional prefix anchor: every plugin-emitted line starts with one of
# these wrapper forms (createLogger("DingTalk:<acct>") yields the first;
# the gateway-supplied logger uses the second). Anchoring with this
# prefix prevents mid-sentence false positives — the regex must begin
# at the start of the message body, not match some embedded substring.
_DT_PREFIX = r"^(?:\[DingTalk[^\]\n]*\]\s*|dingtalk-connector\[[^\]\n]+\]\s*)"


_LOG_SIGNATURES: Dict[str, re.Pattern] = {
    # ── Gate decisions (warn) ───────────────────────────────────────
    # message-handler.ts:1021 — note literal [DingTalk] inside the msg
    "dm_allowlist_empty": re.compile(
        _DT_PREFIX + r"\[DingTalk\] DM 被拦截: allowFrom 白名单为空，拒绝所有请求",
    ),
    # message-handler.ts:1038 — bare phrase after the wrapper prefix
    "dm_sender_not_in_allowlist": re.compile(
        _DT_PREFIX
        + r"DM 被拦截: senderId=(?P<sender>\S+) \((?P<name>[^)]*)\) 不在白名单中",
    ),
    # message-handler.ts:1011
    "dm_sender_empty": re.compile(
        _DT_PREFIX + r"DM 被拦截: senderId 为空",
    ),
    # message-handler.ts:1108
    "group_not_in_allowlist": re.compile(
        _DT_PREFIX
        + r"群聊被拦截: conversationId=(?P<chat>\S+) 不在 groupAllowFrom 白名单中",
    ),
    # message-handler.ts:1091
    "group_allowlist_empty": re.compile(
        _DT_PREFIX + r"群聊被拦截: groupAllowFrom 白名单为空，拒绝所有请求",
    ),
    # message-handler.ts:1063
    "group_disabled": re.compile(
        _DT_PREFIX + r"群聊被拦截: groupPolicy=disabled",
    ),
    # message-handler.ts:1004 — silent policy degradation. Quotes
    # inside the message become ``\"`` once the openclaw logger
    # JSON-encodes the line, so accept either form.
    "dm_policy_pairing_unsupported": re.compile(
        _DT_PREFIX
        + r"dmPolicy=\\?\"pairing\\?\" 暂不支持，将按 \\?\"open\\?\" 策略处理",
    ),
    # channel.ts:444 — startup-disabled emits this from the gateway-
    # supplied logger, so the wrapper prefix may be absent. Anchored on
    # the literal phrase at message start instead.
    "account_disabled_skipping_startup": re.compile(
        r"^dingtalk-connector\[(?P<acct>[^\]\n]+)\] is disabled, skipping "
        r"startup",
    ),
    # ── Connection lifecycle ────────────────────────────────────────
    # connection.ts:324
    "connect_failed": re.compile(
        _DT_PREFIX + r"连接建立失败: (?P<err>.+?)(?=\"|$)",
    ),
    # connection.ts:730
    "connect_error_detail": re.compile(
        _DT_PREFIX + r"连接失败，错误详情：",
    ),
    # connection.ts:348 / 376 / 403 — covers all three reconnect
    # failure emissions; the optional [<acct>] is the bracketed-id form
    # the connection module sometimes writes inside the message.
    "reconnect_failed": re.compile(
        _DT_PREFIX
        + r"(?:\[(?P<acct>[^\]\n]+)\] )?重连失败：(?P<err>.+?)(?=\"|$)",
    ),
    # connection.ts:344
    "reconnect_succeeded": re.compile(
        _DT_PREFIX + r"✅ 重连成功 \(socket 状态=(?P<state>[^)]+)\)",
    ),
    # connection.ts:372
    "disconnect_topic": re.compile(
        _DT_PREFIX + r"收到服务端 disconnect topic，即将重连",
    ),
    # connection.ts:783
    "sdk_reconnecting": re.compile(
        _DT_PREFIX + r"SDK reconnecting\.\.\.",
    ),
    # connection.ts:787
    "sdk_reconnected": re.compile(
        _DT_PREFIX + r"✅ SDK reconnected successfully",
    ),
    # ── Pairing (info) ──────────────────────────────────────────────
    # channel.ts:98
    "pairing_approved": re.compile(
        _DT_PREFIX + r"Pairing approved for user: (?P<user>\S+)",
    ),
}


_SIGNATURE_LABELS: Dict[str, str] = {
    "dm_allowlist_empty": "DM 被拦截 — allowFrom 白名单为空（运行时拒绝所有 DM）",
    "dm_sender_not_in_allowlist": "DM 发送者不在 allowFrom",
    "dm_sender_empty": "DM senderId 为空被拦截",
    "group_not_in_allowlist": "群 conversationId 不在 groupAllowFrom",
    "group_allowlist_empty": "群被拦截 — groupAllowFrom 为空",
    "group_disabled": "群被 groupPolicy=disabled 禁用",
    "dm_policy_pairing_unsupported": (
        "dmPolicy=pairing 不支持，运行时退化为 open（DM 安全降级）"
    ),
    "account_disabled_skipping_startup": "账号被禁用，启动跳过",
    "connect_failed": "WebSocket 连接建立失败",
    "connect_error_detail": "连接失败错误详情",
    "reconnect_failed": "重连失败",
    "reconnect_succeeded": "重连成功",
    "disconnect_topic": "服务端下发 disconnect topic（即将重连）",
    "sdk_reconnecting": "SDK 重连中",
    "sdk_reconnected": "SDK 重连成功",
    "pairing_approved": "Pairing 已批准",
}


def _scan_log_line(line: str) -> Optional[Tuple[str, Dict[str, str]]]:
    for key, pat in _LOG_SIGNATURES.items():
        m = pat.search(line)
        if m:
            return key, {k: v for k, v in m.groupdict().items() if v}
    return None


_DT_NEEDLES = (
    "DingTalk", "dingtalk-connector", "DM 被拦截", "群聊被拦截",
    "重连", "disconnect topic", "SDK reconnect", "Pairing approved",
    'dmPolicy=\\"pairing\\"',
)


def _scan_log_files(log_files: List[str]) -> Dict[str, Any]:
    counts: Dict[str, int] = {k: 0 for k in _LOG_SIGNATURES}
    samples: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _LOG_SIGNATURES}
    SAMPLES_PER_SIG = 3

    for obj, message, basename in iter_plugin_log_lines(
        log_files, _DINGTALK_PATH_PREFIXES,
    ):
        if not any(needle in message for needle in _DT_NEEDLES):
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
        "connect_failed",
        "connect_error_detail",
    }
    warn_keys = {
        "dm_allowlist_empty",
        "dm_sender_not_in_allowlist",
        "dm_sender_empty",
        "group_not_in_allowlist",
        "group_allowlist_empty",
        "group_disabled",
        "dm_policy_pairing_unsupported",
        "reconnect_failed",
        "account_disabled_skipping_startup",
    }
    # ok_keys (informational): disconnect_topic, sdk_reconnecting,
    # sdk_reconnected, reconnect_succeeded, pairing_approved.

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
    """Resolve credentials and probe DingTalk's accessToken endpoint."""
    client_id_val = merged.get("clientId")
    client_secret_val = merged.get("clientSecret")

    if not _is_present(client_id_val):
        return Finding(
            verdict="warn", code="PROBE_SKIPPED_NO_CLIENT_ID",
            msg=f"账号 {account_id} 未配置 clientId — 跳过主动探测",
            data={"account_id": account_id},
        )

    # clientId is allowed to be a number per the zod schema
    # (`z.union([z.string(), z.number()])`); coerce to str for the
    # HTTP body. Numbers can't be SecretRefs.
    if isinstance(client_id_val, (int, float)) and not isinstance(client_id_val, bool):
        client_id_str = str(client_id_val)
        client_id_label = "literal"
    else:
        client_id_resolution = probe_mod.resolve_secret_ref(client_id_val, cfg)
        if not client_id_resolution.ok:
            return Finding(
                verdict="warn", code="PROBE_SKIPPED_CLIENT_ID_UNRESOLVED",
                msg=(
                    f"账号 {account_id} clientId 解析失败 — 跳过主动探测: "
                    f"{client_id_resolution.msg}"
                ),
                data={"account_id": account_id,
                      "client_id_ref": client_id_resolution.ref_label},
            )
        client_id_str = client_id_resolution.value
        client_id_label = client_id_resolution.ref_label

    client_secret_resolution = probe_mod.resolve_secret_ref(
        client_secret_val, cfg,
    )
    if not client_secret_resolution.ok:
        return Finding(
            verdict="warn", code="SECRET_UNRESOLVED",
            msg=(
                f"账号 {account_id} clientSecret 解析失败 — 跳过主动探测: "
                f"{client_secret_resolution.msg}"
            ),
            evidence=f"ref={client_secret_resolution.ref_label}",
            data={
                "account_id": account_id,
                "client_secret_ref": client_secret_resolution.ref_label,
                "skipped": True,
            },
        )

    result = probe_mod.dingtalk_token_probe(
        client_id=client_id_str,
        client_secret=client_secret_resolution.value,
    )

    base_data = {
        "account_id": account_id,
        "domain": result.domain,
        "state": result.state,
        "api_code": result.api_code,
        "client_id_ref": client_id_label,
        "client_secret_ref": client_secret_resolution.ref_label,
    }
    base_data.update(result.extra)

    if result.state == "valid":
        return Finding(
            verdict="ok", code="PROBE_VALID",
            msg=(
                f"账号 {account_id} 凭证有效 (accessToken 获取成功"
                f"{', expire=' + str(result.extra.get('expire_s')) + 's' if result.extra.get('expire_s') else ''})"
            ),
            data=base_data,
        )
    if result.state == "invalid":
        return Finding(
            verdict="fail", code="PROBE_INVALID",
            msg=(
                f"账号 {account_id} 凭证被 DingTalk 拒绝: "
                f"code={result.api_code} msg={result.msg}"
            ),
            evidence=f"endpoint={result.domain}",
            data=base_data,
        )
    return Finding(
        verdict="warn", code="PROBE_UNREACHABLE",
        msg=(
            f"账号 {account_id} 主动探测无法到达 DingTalk API "
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
    account_filter: Optional[str] = None,
) -> VariantReport:
    """``account_filter`` (``--account``) scopes the diagnosis to a
    single account id; None preserves all-accounts behavior."""
    report = VariantReport(variant="dingtalk", detect_basis=detect_basis)

    dingtalk_cfg = _extract_dingtalk_cfg(cfg)
    if dingtalk_cfg is None:
        report.notes.append(
            "检测到 dingtalk 包但 channels.dingtalk-connector / "
            "channels.dingtalk 配置缺失 — 插件不会启动",
        )
        return report

    all_ids = _list_account_ids(dingtalk_cfg)
    iter_ids = apply_account_filter(all_ids, account_filter, report)
    for account_id in iter_ids:
        merged = _merge_account_config(dingtalk_cfg, account_id)
        acct_rep = _diagnose_account_config(
            account_id, merged, sender_open_id=sender_open_id,
        )
        report.accounts.append(acct_rep)

        if do_probe:
            report.probe_findings.append(_run_probe(account_id, merged, cfg))

    log_scan = _scan_log_files(log_files)
    report.log_findings = _build_log_findings(log_scan)

    return report
