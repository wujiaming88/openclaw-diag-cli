"""wecom (``@wecom/wecom-openclaw-plugin``) variant diagnostic.

The WeCom plugin is **dual-mode**: a single account can carry a
``Bot`` configuration (websocket via ``openws.work.weixin.qq.com`` OR
webhook callback) AND/OR an ``agent`` block (corp self-built app
talking to ``qyapi.weixin.qq.com``). Either path can deliver
messages; some operators run them side-by-side with a bot-first /
agent-fallback discipline.

Three implications for the diagnostic:

  1. **Two credential surfaces**. Bot mode requires ``botId/secret``;
     Agent mode requires ``agent.{corpId, corpSecret, token,
     encodingAESKey}``. They MUST NOT be sanity-checked against each
     other — a bot-only account does not need agent fields.
  2. **Bot mode has no simple HTTP token-introspection endpoint** we
     can call from the diagnostic. The Agent mode does
     (``GET /cgi-bin/gettoken``). The L5 probe consequently runs only
     for Agent mode; Bot-only accounts surface
     ``WECOM_BOT_NO_PROBE_ENDPOINT`` (info) so the operator knows the
     check was honestly skipped, not silently dropped.
  3. **encodingAESKey is exactly 43 chars Base64**. WeCom's server
     refuses anything else. We validate this length at L1 because the
     runtime error you see (a generic decryption failure) is much
     harder to chase than a config-time fail.

Source-of-truth references:
  - schema: ``src/utils.ts:WeComConfig`` (top-level / single-account)
    + ``src/types/config.ts:WecomBotConfig / WecomAgentConfig``
    (sub-blocks). Multi-account: ``src/accounts.ts``.
  - Bot WS lifecycle log lines: ``src/monitor.ts`` (lines 967-1063 —
    ``[<accountId>] WebSocket connected / Authentication successful /
    WebSocket disconnected: <reason> / Reconnecting attempt N... /
    WebSocket error: <msg>``).
  - Bot-vs-agent error classes: ``WSAuthFailureError`` /
    ``WSReconnectExhaustedError`` from monitor.ts:24-25 (surfaced as
    ``Auth failure attempts exhausted (...)`` / generic ``WebSocket
    error: ...`` literals — see emit sites at monitor.ts:1017+1033).
  - Gate decisions: ``src/dm-policy.ts`` + ``src/group-policy.ts``.
  - Agent message lifecycle: ``src/agent/handler.ts``.
  - Webhook routing: ``src/webhook/handler.ts``.
  - Media: ``src/media-uploader.ts`` + ``src/monitor.ts:395``.
  - MCP doc-auth interceptor: ``src/mcp/interceptors/doc-auth-error.ts``.
  - Agent token endpoint: ``src/const.ts:165`` (GET_TOKEN) +
    ``src/agent/api-client.ts:103`` (issuance).

The wecom plugin is **not installed on this host**. Every signature
in ``_LOG_SIGNATURES`` is grep-verified against
``repos/channel-src/wecom-openclaw-plugin/src/`` — see citations
beside each.

Config key: ``channels.wecom``. There is no shorter alias; the
plugin's CHANNEL_ID is the literal string ``"wecom"``
(``src/const.ts:8``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .. import probe as probe_mod
from ..log_utils import extract_ts, iter_plugin_log_lines
from .base import AccountReport, Finding, VariantReport, apply_account_filter


# WeCom plugin npm scope is ``@wecom``. Include the openclaw runtime
# sink for the same forwarding reason as the other variants.
_WECOM_PATH_PREFIXES = (
    "@wecom/",
    "openclaw/dist/subsystem",
)


_DEFAULT_BOT_CONNECTION_MODE = "websocket"
# Per dm-policy and the schema enum, default dmPolicy is "open".
_DEFAULT_DM_POLICY = "open"
_DEFAULT_GROUP_POLICY = "open"
# encodingAESKey is exactly 43 chars (Base64-encoded 32-byte AES key).
_AES_KEY_LEN = 43


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
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


def _aes_key_length(value: Any) -> int:
    """Return the on-the-wire length of an encodingAESKey.

    For a SecretRef we don't have the resolved value at L1 time, so we
    return -1 (= unknown) and skip the length check rather than guess.
    """
    if isinstance(value, str):
        return len(value.strip())
    return -1


def _merge_account_config(
    wecom_cfg: Dict[str, Any], account_id: str,
) -> Dict[str, Any]:
    merged = dict(wecom_cfg)
    accounts = wecom_cfg.get("accounts")
    if isinstance(accounts, dict):
        merged.pop("accounts", None)
        sub = accounts.get(account_id)
        if isinstance(sub, dict):
            for k, v in sub.items():
                if v is not None:
                    merged[k] = v
    return merged


def _list_account_ids(wecom_cfg: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    accounts = wecom_cfg.get("accounts")
    if isinstance(accounts, dict) and accounts:
        ids.extend(sorted(accounts.keys()))
    has_top_bot_creds = (
        _is_present(wecom_cfg.get("botId"))
        and _is_present(wecom_cfg.get("secret"))
    )
    has_top_agent = isinstance(wecom_cfg.get("agent"), dict)
    if not ids:
        ids = ["default"]
    elif (has_top_bot_creds or has_top_agent) and "default" not in ids and "main" not in ids:
        ids.insert(0, "default")
    return ids


def _bot_block_present(merged: Dict[str, Any]) -> bool:
    """Bot mode is "configured" if any bot-mode field is set.

    We treat a partially-populated bot block (e.g. botId without
    secret) as "configured but broken" so the L1 ``BOT_CRED_MISSING``
    finding can fire on it. An entirely empty bot section means the
    operator simply isn't using bot mode, so we don't fire anything.
    """
    return any(
        _is_present(merged.get(k))
        for k in ("botId", "secret", "websocketUrl")
    )


def _agent_block_present(merged: Dict[str, Any]) -> bool:
    return isinstance(merged.get("agent"), dict)


# ─── L1: config rules ────────────────────────────────────────────────


def _diagnose_account_config(
    account_id: str,
    merged: Dict[str, Any],
    sender_open_id: Optional[str] = None,
) -> AccountReport:
    rep = AccountReport(account_id=account_id)

    bot_id = merged.get("botId")
    bot_secret = merged.get("secret")
    connection_mode = merged.get("connectionMode") or _DEFAULT_BOT_CONNECTION_MODE
    bot_token = merged.get("token")
    bot_aes_key = merged.get("encodingAESKey")

    agent_block = merged.get("agent") if isinstance(merged.get("agent"), dict) else None
    agent_corp_id = agent_block.get("corpId") if agent_block else None
    agent_corp_secret = agent_block.get("corpSecret") if agent_block else None
    agent_token = agent_block.get("token") if agent_block else None
    agent_aes_key = agent_block.get("encodingAESKey") if agent_block else None

    dm_policy = merged.get("dmPolicy") or _DEFAULT_DM_POLICY
    group_policy = merged.get("groupPolicy") or _DEFAULT_GROUP_POLICY
    allow_from = merged.get("allowFrom") or []
    if not isinstance(allow_from, list):
        allow_from = []
    group_allow_from = merged.get("groupAllowFrom") or []
    if not isinstance(group_allow_from, list):
        group_allow_from = []

    bot_present = _bot_block_present(merged)
    agent_present = _agent_block_present(merged)

    rep.config_summary = {
        "bot_mode": (
            connection_mode if bot_present else "(unconfigured)"
        ),
        "agent_mode": "configured" if agent_present else "(unconfigured)",
        "dm_policy": dm_policy,
        "group_policy": group_policy,
        "credentials": {
            "bot.botId": _credential_label(bot_id),
            "bot.secret": _credential_label(bot_secret),
            "bot.token": _credential_label(bot_token),
            "bot.encodingAESKey": _credential_label(bot_aes_key),
            "agent.corpId": _credential_label(agent_corp_id),
            "agent.corpSecret": _credential_label(agent_corp_secret),
            "agent.token": _credential_label(agent_token),
            "agent.encodingAESKey": _credential_label(agent_aes_key),
        },
        "allow_from_count": len(allow_from),
        "group_allow_from_count": len(group_allow_from),
    }

    # NO_MODE_CONFIGURED — both surfaces empty; the plugin will start
    # but no message path is wired. Fail (silent dead-end).
    if not bot_present and not agent_present:
        rep.findings.append(Finding(
            verdict="fail", code="NO_MODE_CONFIGURED",
            msg=(
                f"账号 {account_id}: 既未配置 Bot 凭证 (botId/secret)，"
                "也未配置 agent 块 — 插件启动后没有任何消息通道"
            ),
            evidence="utils.ts:WeComConfig + types/config.ts:WecomAgentConfig",
            data={"bot_present": False, "agent_present": False},
        ))

    # ── Bot mode rules ──────────────────────────────────────────────
    if bot_present:
        if not _is_present(bot_id) or not _is_present(bot_secret):
            rep.findings.append(Finding(
                verdict="fail", code="BOT_CRED_MISSING",
                msg=(
                    f"账号 {account_id}: Bot 模式已配置但缺少 "
                    "botId 或 secret — WebSocket 鉴权会立即失败"
                ),
                evidence=(
                    f"botId={_credential_label(bot_id)} "
                    f"secret={_credential_label(bot_secret)}"
                ),
                data={
                    "botId_present": _is_present(bot_id),
                    "secret_present": _is_present(bot_secret),
                },
            ))
        # BOT_WEBHOOK_INCONSISTENT — webhook mode requires token AND
        # encodingAESKey to verify + decrypt the callback payload.
        # types/config.ts:36-38 documents both as required when
        # connectionMode == "webhook".
        if connection_mode == "webhook":
            missing = []
            if not _is_present(bot_token):
                missing.append("token")
            if not _is_present(bot_aes_key):
                missing.append("encodingAESKey")
            if missing:
                rep.findings.append(Finding(
                    verdict="fail", code="BOT_WEBHOOK_INCONSISTENT",
                    msg=(
                        f"账号 {account_id}: connectionMode=webhook "
                        f"但缺少 {', '.join(missing)} — 回调签名校验"
                        "/解密会失败"
                    ),
                    evidence=(
                        "types/config.ts:36-38 — token + encodingAESKey "
                        "are mandatory for webhook mode"
                    ),
                    data={"missing": missing,
                          "connection_mode": connection_mode},
                ))

    # ── Agent mode rules ────────────────────────────────────────────
    if agent_present:
        agent_missing = []
        if not _is_present(agent_corp_id):
            agent_missing.append("corpId")
        if not _is_present(agent_corp_secret):
            agent_missing.append("corpSecret")
        if not _is_present(agent_token):
            agent_missing.append("token")
        if not _is_present(agent_aes_key):
            agent_missing.append("encodingAESKey")
        if agent_missing:
            rep.findings.append(Finding(
                verdict="fail", code="AGENT_CRED_MISSING",
                msg=(
                    f"账号 {account_id}: agent 块已声明但缺少 "
                    f"{', '.join(agent_missing)} — gettoken / 回调"
                    "鉴权将失败"
                ),
                evidence="types/config.ts:WecomAgentConfig (required fields)",
                data={"missing": agent_missing},
            ))
        # AGENT_AES_KEY_INVALID — WeCom hard-requires len == 43.
        # Only enforce when the value is a literal string (a SecretRef
        # has unknown length at config time).
        aes_len = _aes_key_length(agent_aes_key)
        if aes_len > 0 and aes_len != _AES_KEY_LEN:
            rep.findings.append(Finding(
                verdict="fail", code="AGENT_AES_KEY_INVALID",
                msg=(
                    f"账号 {account_id}: agent.encodingAESKey 长度 "
                    f"{aes_len} 非 {_AES_KEY_LEN} — 企微服务端将拒绝"
                ),
                evidence=(
                    "WeCom 硬性要求：encodingAESKey 必须为 43 字符 "
                    "Base64（即 32-byte AES key）"
                ),
                data={"length": aes_len, "expected": _AES_KEY_LEN},
            ))

    # DM_POLICY_OPEN_NO_WILDCARD — same shape as feishu's variants:
    # dmPolicy=open + allowFrom missing "*" is self-inconsistent.
    if dm_policy == "open":
        wildcard = any(
            isinstance(x, str) and x.strip() == "*" for x in allow_from
        )
        if allow_from and not wildcard:
            rep.findings.append(Finding(
                verdict="warn", code="DM_POLICY_OPEN_NO_WILDCARD",
                msg=(
                    "dmPolicy=open 但 allowFrom 未包含 \"*\" — "
                    "DM 实际仍受限，行为不自洽"
                ),
                evidence=f"allowFrom={allow_from}",
                data={"dm_policy": dm_policy, "allow_from": allow_from},
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


# All literals grep-verified against
# repos/channel-src/wecom-openclaw-plugin/src/. Each comment cites the
# emit site so a future upstream rename triggers a fixture mismatch.
_LOG_SIGNATURES: Dict[str, re.Pattern] = {
    # ── Gate decisions (warn) ───────────────────────────────────────
    # dm-policy.ts:58
    "blocked_dm_disabled": re.compile(
        r"^\[WeCom\] Blocked DM from (?P<sender>\S+) "
        r"\(dmPolicy=disabled\)",
    ),
    # dm-policy.ts:118
    "blocked_unauthorized_sender": re.compile(
        r"^\[WeCom\] Blocked unauthorized sender (?P<sender>\S+) "
        r"\(dmPolicy=(?P<policy>[^)]+)\)",
    ),
    # group-policy.ts:145
    "group_not_allowed": re.compile(
        r"^\[WeCom\] Group (?P<chat>\S+) not allowed "
        r"\(groupPolicy=(?P<policy>[^)]+)\)",
    ),
    # group-policy.ts:158
    "sender_not_in_group_allowlist": re.compile(
        r"^\[WeCom\] Sender (?P<sender>\S+) not in group "
        r"(?P<chat>\S+) sender allowlist",
    ),
    # agent/handler.ts:247
    "agent_duplicate_msgid": re.compile(
        r"^\[wecom-agent\] duplicate msgId=(?P<msg>\S+) "
        r"from=(?P<sender>\S+) chatId=(?P<chat>\S+) "
        r"type=(?P<type>\S+); skipped",
    ),
    # agent/handler.ts:271 — generic skip path; reason= varies
    "agent_skip_processing": re.compile(
        r"^\[wecom-agent\] skip processing: type=(?P<type>\S+) "
        r"event=(?P<event>\S+) from=(?P<sender>\S+) "
        r"reason=(?P<reason>.+?)(?=\"|$)",
    ),
    # agent/handler.ts:495
    "agent_unauthorized_command": re.compile(
        r"^\[wecom-agent\] unauthorized command: replied via DM to "
        r"(?P<sender>\S+)",
    ),
    # webhook/handler.ts:335
    "webhook_skipped_no_targets": re.compile(
        r"^\[wecom\] inbound\(http\): reqId=(?P<req>\S+) "
        r"skipped — no active targets",
    ),
    # media-uploader.ts:372 / :457
    "media_rejected": re.compile(
        r"^\[wecom\] Media rejected: (?P<reason>.+?)(?=\"|$)",
    ),
    # monitor.ts:395
    "media_send_failed": re.compile(
        r"^\[wecom\] Media send failed: url=(?P<url>\S+), "
        r"reason=(?P<reason>.+?)(?=\"|$)",
    ),
    # ── Connection / lifecycle ──────────────────────────────────────
    # monitor.ts:967
    "ws_connected": re.compile(
        r"^\[(?P<acct>[^\]\n]+)\] WebSocket connected",
    ),
    # monitor.ts:972
    "ws_authenticated": re.compile(
        r"^\[(?P<acct>[^\]\n]+)\] Authentication successful",
    ),
    # monitor.ts:978
    "ws_disconnected": re.compile(
        r"^\[(?P<acct>[^\]\n]+)\] WebSocket disconnected: "
        r"(?P<reason>.+?)(?=\"|$)",
    ),
    # monitor.ts:1012
    "ws_reconnecting": re.compile(
        r"^\[(?P<acct>[^\]\n]+)\] Reconnecting attempt (?P<n>\d+)\.\.\.",
    ),
    # ── Specific WS terminal failures (must precede the generic
    # ``ws_error`` pattern so the more-specific match wins) ────────
    # monitor.ts:1033 — auth-failure terminal. Anchored on the
    # wrapper-prefixed form so a chat message containing the literal
    # phrase doesn't false-positive.
    "ws_auth_failure_exhausted": re.compile(
        r"^\[(?P<acct>[^\]\n]+)\] Auth failure attempts exhausted "
        r"\((?P<attempts>\d+) attempts\)\. Please check botId/secret "
        r"configuration\.",
    ),
    # WSReconnectExhaustedError / WSAuthFailureError class names
    # (monitor.ts:24-25). These appear inside the wrapper-prefixed
    # ``[acct] WebSocket error: <msg>`` line; we anchor on that form
    # and require the class name in the trailing message body. This
    # avoids matching plain-text discussion of the class name.
    "ws_reconnect_exhausted_class": re.compile(
        r"^\[(?P<acct>[^\]\n]+)\] WebSocket error: "
        r"[^\n]*WSReconnectExhaustedError",
    ),
    "ws_auth_failure_class": re.compile(
        r"^\[(?P<acct>[^\]\n]+)\] WebSocket error: "
        r"[^\n]*WSAuthFailureError",
    ),
    # monitor.ts:1017 — generic catch-all; matches LAST so the
    # specific class patterns above can promote first.
    "ws_error": re.compile(
        r"^\[(?P<acct>[^\]\n]+)\] WebSocket error: "
        r"(?P<msg>.+?)(?=\"|$)",
    ),
    # ── Pairing (info / warn) ───────────────────────────────────────
    # dm-policy.ts:95
    "pairing_request_created": re.compile(
        r"^\[WeCom\] Pairing request created for sender=(?P<sender>\S+)",
    ),
    # dm-policy.ts:112
    "pairing_request_already_exists": re.compile(
        r"^\[WeCom\] Pairing request already exists for "
        r"sender=(?P<sender>\S+)",
    ),
    # dm-policy.ts:109
    "pairing_reply_failed": re.compile(
        r"^\[WeCom\] Failed to send pairing reply to (?P<sender>\S+): "
        r"(?P<reason>.+?)(?=\"|$)",
    ),
    # ── MCP doc-auth interceptor ───────────────────────────────────
    # mcp/interceptors/doc-auth-error.ts:105 — quotes around
    # accountId become ``\"`` once JSON-logged. Accept either form.
    "mcp_doc_auth_no_ws": re.compile(
        r"^\[mcp\] doc-auth-error: WSClient 未连接 "
        r"\(accountId=\\?\"(?P<acct>[^\"\\]+)\\?\"\)，无法发送授权卡片",
    ),
}


_SIGNATURE_LABELS: Dict[str, str] = {
    "blocked_dm_disabled": "DM 被 dmPolicy=disabled 拦截",
    "blocked_unauthorized_sender": "未授权发送者 DM 被拦截",
    "group_not_allowed": "群被 groupPolicy 拦截",
    "sender_not_in_group_allowlist": "群成员不在 sender allowlist",
    "agent_duplicate_msgid": "Agent 模式 msgId 重复，跳过",
    "agent_skip_processing": "Agent 模式 skip processing",
    "agent_unauthorized_command": "未授权命令（已经 DM 回复）",
    "webhook_skipped_no_targets": "Webhook 入站但无活跃目标，跳过",
    "media_rejected": "媒体被拒（大小/类型校验失败）",
    "media_send_failed": "媒体发送失败",
    "ws_connected": "WebSocket 已连接",
    "ws_authenticated": "WebSocket 鉴权成功",
    "ws_disconnected": "WebSocket 断开",
    "ws_reconnecting": "WebSocket 重连尝试",
    "ws_error": "WebSocket 错误",
    "ws_auth_failure_exhausted": (
        "认证失败重试用尽（请检查 botId/secret）— 框架不会再重启"
    ),
    "ws_reconnect_exhausted_class": "WSReconnectExhaustedError 命中",
    "ws_auth_failure_class": "WSAuthFailureError 命中",
    "pairing_request_created": "Pairing 请求已创建",
    "pairing_request_already_exists": "Pairing 请求已存在",
    "pairing_reply_failed": "Pairing 回复发送失败",
    "mcp_doc_auth_no_ws": "MCP doc-auth：WSClient 未连接，授权卡片无法发送",
}


def _scan_log_line(line: str) -> Optional[Tuple[str, Dict[str, str]]]:
    for key, pat in _LOG_SIGNATURES.items():
        m = pat.search(line)
        if m:
            return key, {k: v for k, v in m.groupdict().items() if v}
    return None


_LOG_NEEDLES = (
    "[WeCom]", "[wecom]", "[wecom-agent]", "[mcp] doc-auth-error",
    "WebSocket connected", "WebSocket disconnected", "WebSocket error",
    "Authentication successful", "Reconnecting attempt",
    "WSAuthFailureError", "WSReconnectExhaustedError",
    "Auth failure attempts exhausted",
)


def _scan_log_files(log_files: List[str]) -> Dict[str, Any]:
    counts: Dict[str, int] = {k: 0 for k in _LOG_SIGNATURES}
    samples: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _LOG_SIGNATURES}
    SAMPLES_PER_SIG = 3

    for obj, message, basename in iter_plugin_log_lines(
        log_files, _WECOM_PATH_PREFIXES,
    ):
        if not any(needle in message for needle in _LOG_NEEDLES):
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
        "ws_auth_failure_exhausted",
        "ws_auth_failure_class",
        "ws_reconnect_exhausted_class",
    }
    warn_keys = {
        "blocked_dm_disabled",
        "blocked_unauthorized_sender",
        "group_not_allowed",
        "sender_not_in_group_allowlist",
        "agent_unauthorized_command",
        "media_rejected",
        "media_send_failed",
        "ws_disconnected",
        "ws_error",
        "pairing_reply_failed",
        "mcp_doc_auth_no_ws",
        "webhook_skipped_no_targets",
    }
    # Everything else is informational (info / ok).

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


def _run_agent_probe(
    account_id: str, agent_block: Dict[str, Any], cfg: Dict[str, Any],
) -> Finding:
    corp_id_val = agent_block.get("corpId")
    corp_secret_val = agent_block.get("corpSecret")

    if not _is_present(corp_id_val):
        return Finding(
            verdict="warn", code="PROBE_SKIPPED_NO_CORP_ID",
            msg=(
                f"账号 {account_id} agent.corpId 未配置 — 跳过主动探测"
            ),
            data={"account_id": account_id},
        )

    corp_id_resolution = probe_mod.resolve_secret_ref(corp_id_val, cfg)
    if not corp_id_resolution.ok:
        return Finding(
            verdict="warn", code="PROBE_SKIPPED_CORP_ID_UNRESOLVED",
            msg=(
                f"账号 {account_id} agent.corpId 解析失败 — 跳过主动探测: "
                f"{corp_id_resolution.msg}"
            ),
            data={"account_id": account_id,
                  "corp_id_ref": corp_id_resolution.ref_label},
        )

    corp_secret_resolution = probe_mod.resolve_secret_ref(corp_secret_val, cfg)
    if not corp_secret_resolution.ok:
        return Finding(
            verdict="warn", code="SECRET_UNRESOLVED",
            msg=(
                f"账号 {account_id} agent.corpSecret 解析失败 — 跳过主动探测: "
                f"{corp_secret_resolution.msg}"
            ),
            evidence=f"ref={corp_secret_resolution.ref_label}",
            data={
                "account_id": account_id,
                "corp_secret_ref": corp_secret_resolution.ref_label,
                "skipped": True,
            },
        )

    result = probe_mod.wecom_agent_token_probe(
        corp_id=corp_id_resolution.value,
        corp_secret=corp_secret_resolution.value,
    )

    base_data = {
        "account_id": account_id,
        "mode": "agent",
        "domain": result.domain,
        "state": result.state,
        "api_code": result.api_code,
        "corp_id_ref": corp_id_resolution.ref_label,
        "corp_secret_ref": corp_secret_resolution.ref_label,
    }
    base_data.update(result.extra)

    if result.state == "valid":
        return Finding(
            verdict="ok", code="PROBE_VALID",
            msg=(
                f"账号 {account_id} agent 凭证有效 (access_token "
                f"获取成功"
                f"{', expire=' + str(result.extra.get('expire_s')) + 's' if result.extra.get('expire_s') else ''})"
            ),
            data=base_data,
        )
    if result.state == "invalid":
        return Finding(
            verdict="fail", code="PROBE_INVALID",
            msg=(
                f"账号 {account_id} agent 凭证被 WeCom 拒绝: "
                f"errcode={result.api_code} errmsg={result.msg}"
            ),
            evidence=f"endpoint={result.domain}",
            data=base_data,
        )
    return Finding(
        verdict="warn", code="PROBE_UNREACHABLE",
        msg=(
            f"账号 {account_id} agent 主动探测无法到达 WeCom API "
            f"(网络/超时/DNS): {result.msg}"
        ),
        evidence=f"endpoint={result.domain}",
        data=base_data,
    )


def _bot_no_probe_finding(account_id: str) -> Finding:
    return Finding(
        verdict="ok", code="WECOM_BOT_NO_PROBE_ENDPOINT",
        msg=(
            f"账号 {account_id}: Bot 模式无简单 HTTP token 探测端点 — "
            "诚实跳过 (仅校验 botId/secret 是否存在已在 L1 完成)"
        ),
        evidence=(
            "WeCom Bot WS 走 wss://openws.work.weixin.qq.com，"
            "鉴权信息在 WS 握手帧内，无独立 GET/POST 探测端点"
        ),
        data={"account_id": account_id, "mode": "bot"},
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
    report = VariantReport(variant="wecom", detect_basis=detect_basis)

    wecom_cfg = (
        (cfg.get("channels") or {}).get("wecom")
        if isinstance(cfg, dict) else None
    )
    if not isinstance(wecom_cfg, dict):
        report.notes.append(
            "检测到 wecom 包但 channels.wecom 配置缺失 — "
            "插件不会启动",
        )
        return report

    all_ids = _list_account_ids(wecom_cfg)
    iter_ids = apply_account_filter(all_ids, account_filter, report)
    for account_id in iter_ids:
        merged = _merge_account_config(wecom_cfg, account_id)
        acct_rep = _diagnose_account_config(
            account_id, merged, sender_open_id=sender_open_id,
        )
        report.accounts.append(acct_rep)

        if do_probe:
            agent_block = (
                merged.get("agent") if isinstance(merged.get("agent"), dict)
                else None
            )
            if agent_block is not None:
                # Agent mode → real network probe.
                report.probe_findings.append(
                    _run_agent_probe(account_id, agent_block, cfg),
                )
            elif _bot_block_present(merged):
                # Bot-only account → honest skip with explicit code.
                report.probe_findings.append(_bot_no_probe_finding(account_id))
            # else: NO_MODE_CONFIGURED already fired at L1; nothing
            # extra to surface at L5.

    log_scan = _scan_log_files(log_files)
    report.log_findings = _build_log_findings(log_scan)

    return report
