"""feishu-bundled (``@openclaw/feishu``) variant diagnostic.

This is the only variant fully implemented in P0 (lark / dingtalk /
wecom are stubbed — see siblings). Three layers run on every call:

  L1 — config diagnosis (always runs; pure config, no I/O):
       credential completeness, connection-mode self-consistency,
       dmPolicy/allowFrom sanity, optional sender allowlist check.

  L2/L3 — log signature scan (always runs; reads ``log_dir`` files):
       matches the per-account ``feishu[acct]:`` log lines documented
       in the design doc §4 (drop reasons, WS/webhook anomalies, bot
       probe outcomes).

  L5 — active credential probe (only when ``ctx.probe`` is True):
       resolves each account's appSecret via ``probe.resolve_secret_ref``
       and POSTs to the canonical Feishu token endpoint. Three result
       states (valid / invalid / unreachable) — explicit unreachability
       so we don't false-positive a network error as "credential bad".

Source-of-truth references for the diagnostic rules:
  - config schema: ``@openclaw/feishu/dist/channel-DTfK2nVn.js``
    (FeishuConfigSchema, FeishuAccountConfigSchema, superRefine).
  - log signatures: ``monitor.account-BvKcwxaW.js`` (drop /
    block / unauthorized / pairing log lines) and
    ``monitor.state-r4OLFBfg.js`` (probe timeout, webhook anomaly).
  - probe endpoint: ``probe-BjKRV7em.js`` (auth/v3/tenant_access_token
    canonical path) and the streaming-card token loader in
    ``monitor.account-BvKcwxaW.js``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .. import probe as probe_mod
from ..log_utils import extract_ts, iter_plugin_log_lines
from .base import AccountReport, Finding, VariantReport, apply_account_filter


# Path-prefix whitelist passed to ``iter_plugin_log_lines``. The two
# bundled-plugin npm package paths are listed first so a host that
# emits with a real plugin dist path still matches; the openclaw
# runtime sink (``subsystem-*.js``) is included because the openclaw
# logger forwards plugin-imported logger calls through that file on
# every current build (verified on this host: real ``feishu[main]:``
# lines carry ``_meta.path.fullFilePath`` ending in
# ``openclaw/dist/subsystem-DM7CD-js.js``). The gateway console
# relay (``/dist/console-*``) is rejected unconditionally inside
# ``iter_plugin_log_lines`` regardless of this whitelist.
_FEISHU_BUNDLED_PATH_PREFIXES = (
    "@openclaw/feishu/",
    "openclaw/dist/subsystem",
)


# ─── Config helpers ──────────────────────────────────────────────────


# Per the upstream zod schema, dmPolicy defaults to "pairing" and
# groupPolicy defaults to "allowlist" at the top level. Account-level
# values inherit the merged top-level. We mirror those defaults here so
# our diagnosis of an under-specified config matches what the runtime
# would actually see.
_DEFAULT_DM_POLICY = "pairing"
_DEFAULT_GROUP_POLICY = "allowlist"
_DEFAULT_CONNECTION_MODE = "websocket"


def _is_present(value: Any) -> bool:
    """Truthy presence check that recognizes both literal strings and
    SecretRef objects. A SecretRef is "configured" (its ref is set)
    even though we haven't resolved it yet — matches upstream's
    ``hasConfiguredSecretInput``.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        # SecretRef-shaped: source+id present → configured.
        return bool(value.get("source") and value.get("id"))
    return bool(value)


def _credential_label(value: Any) -> str:
    """Return a non-revealing label for a credential field.

    Never expose the raw value, never expose its length. We only say
    "absent" / "literal" / "ref:<source>:<provider>" so a reader can
    tell what kind of secret config they have without seeing it.
    """
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
    """Return ``feishu_cfg`` merged with ``feishu_cfg.accounts[account_id]``.

    Mirrors the runtime's account-resolution: account-level fields
    override top-level. Unknown account_id → return top-level only
    (default account semantics).
    """
    merged = dict(feishu_cfg)
    accounts = feishu_cfg.get("accounts")
    if isinstance(accounts, dict):
        # Drop the accounts map itself — we don't want to leak it into
        # the merged view.
        merged.pop("accounts", None)
        sub = accounts.get(account_id)
        if isinstance(sub, dict):
            for k, v in sub.items():
                if v is not None:
                    merged[k] = v
    return merged


def _list_account_ids(feishu_cfg: Dict[str, Any]) -> List[str]:
    """Enumerate account ids to diagnose.

    The plugin treats top-level fields as the implicit "default"
    account when no ``accounts`` map exists. When a map is provided,
    each key is a real account; we still include "default" if there
    are top-level credentials so we don't miss the implicit account.
    """
    ids: List[str] = []
    accounts = feishu_cfg.get("accounts")
    if isinstance(accounts, dict) and accounts:
        ids.extend(sorted(accounts.keys()))
    has_top_creds = (
        _is_present(feishu_cfg.get("appId"))
        and _is_present(feishu_cfg.get("appSecret"))
    )
    # Include "default" only if accounts map is empty, OR top-level
    # credentials exist and aren't already reflected in some account
    # entry. The plugin's listFeishuAccountIds handles this with a
    # ``allowUnlistedDefaultAccount`` flag — keep behavior identical.
    if not ids:
        # No accounts map → top-level alone is the implicit "default"
        # account, regardless of whether creds are there (we still want
        # to diagnose the missing-cred case).
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
    """Apply L1 (config-only) rules to one account's merged config."""
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

    rep.config_summary = {
        "connection_mode": connection_mode,
        "dm_policy": dm_policy,
        "group_policy": group_policy,
        "require_mention": require_mention,
        "domain": merged.get("domain") or "feishu",
        "credentials": {
            "appId": _credential_label(app_id_val),
            "appSecret": _credential_label(app_secret_val),
            "encryptKey": _credential_label(encrypt_key_val),
            "verificationToken": _credential_label(verify_token_val),
        },
        "allow_from_count": len(allow_from),
    }

    # CRED_MISSING — most common failure mode: account row exists but
    # credentials weren't provisioned. Top-level inheritance is already
    # baked into the merged view, so we can check directly.
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

    # CONN_MODE_INCONSISTENT — webhook mode requires verificationToken
    # AND encryptKey AND a webhookPort (the upstream zod schema fails
    # validation if the first two are missing; a missing port still
    # boots but binds to the default 3000, which is rarely what the
    # operator intended).
    if connection_mode == "webhook":
        missing = []
        if not _is_present(verify_token_val):
            missing.append("verificationToken")
        if not _is_present(encrypt_key_val):
            missing.append("encryptKey")
        # webhookPort is OPTIONAL in the schema (defaults to 3000), so
        # we only warn rather than fail when absent.
        if missing:
            rep.findings.append(Finding(
                verdict="fail", code="CONN_MODE_INCONSISTENT",
                msg=(
                    f"connectionMode=webhook 但缺少 {', '.join(missing)} — "
                    "Feishu schema 校验会拒绝该 account"
                ),
                evidence=f"missing={','.join(missing)}",
                data={"missing": missing,
                      "connection_mode": connection_mode},
            ))
        if merged.get("webhookPort") is None:
            rep.findings.append(Finding(
                verdict="warn", code="WEBHOOK_PORT_DEFAULT",
                msg=(
                    "connectionMode=webhook 但未配置 webhookPort — "
                    "插件将监听默认 3000 端口"
                ),
                data={"webhookPort": None},
            ))

    # DM_POLICY_OPEN_NO_WILDCARD — dmPolicy=open requires allowFrom to
    # include "*" (also enforced by the upstream zod superRefine, but
    # we surface it as a friendlier warning).
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

    # GATE_SENDER_NOT_IN_ALLOWLIST — only when caller passed
    # --sender. Helps debug "I sent a DM and got nothing" when the
    # sender forgot they're not on the list.
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


# Each pattern is anchored on a phrase the plugin emits LITERALLY in its
# logs (verified against monitor.account-BvKcwxaW.js / monitor.state-).
# We capture the account id (when present) and the sender / chat id
# (when present) so the human can see WHO got dropped and WHERE.
#
# WebSocket-failure note: the SDK strings ``"WebSocket reconnect
# exhausted after N attempts"`` and ``"WebSocket connect failed and
# autoReconnect is disabled"`` are *error message constants* (used by
# ``isFeishuWsTerminalError``), NEVER printed as standalone log lines.
# They show up only as the ``<err>`` tail of the WS log lines below
# (``WebSocket start failed, retrying...`` / ``WebSocket connection
# ended, recreating...`` / ``error closing WebSocket client...``). We
# capture ``<err>`` and reclassify in ``_scan_log_line`` so the SDK-
# constant matches still surface as ``ws_reconnect_exhausted`` /
# ``ws_autoreconnect_disabled`` (fail) while plain retry/recreate lines
# stay at warn.
_LOG_SIGNATURES: Dict[str, re.Pattern] = {
    # Connection lifecycle log lines — base severity warn; promoted to
    # fail when ``err`` carries an SDK terminal-error constant.
    "ws_start_failed_retrying": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*WebSocket start failed, "
        r"retrying in (?P<delay_ms>\d+)ms: (?P<err>[^\"\\]+)",
    ),
    "ws_connection_ended_recreating": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*WebSocket connection ended, "
        r"recreating client in (?P<delay_ms>\d+)ms: (?P<err>[^\"\\]+)",
    ),
    "ws_close_error": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*error closing WebSocket "
        r"client: (?P<err>[^\"\\]+)",
    ),
    "ws_recoverable_error": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*WebSocket SDK reported "
        r"recoverable error",
    ),
    "bot_probe_timed_out": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*bot info probe timed out",
    ),
    "webhook_anomaly": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*webhook anomaly",
    ),
    # Bot-identity recovery — high-value silent-drop signals: when the
    # bot can't identify itself, requireMention groups skip messages.
    "bot_identity_retry_exhausted": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*bot identity background retry "
        r"exhausted",
    ),
    "require_mention_gated": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*requireMention group messages "
        r"stay gated",
    ),
    # Drops / gating — "message arrived but bot didn't reply"
    "blocked_unauthorized_dm": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*blocked unauthorized sender "
        r"(?P<sender>\S+)\s*\(dmPolicy=(?P<dm_policy>[^)]+)\)",
    ),
    "blocked_unauthorized_comment": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*blocked unauthorized comment "
        r"sender (?P<sender>\S+)",
    ),
    "group_disabled": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*group (?P<chat>\S+) is disabled",
    ),
    "group_not_in_allowlist": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*group (?P<chat>\S+) not in "
        r"groupAllowFrom",
    ),
    "group_did_not_mention_bot": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*message in group (?P<chat>\S+) "
        r"did not mention bot",
    ),
    "sender_not_in_group_allowlist": re.compile(
        r"^feishu:[^\n]*sender (?P<sender>\S+) not in group "
        r"(?P<chat>\S+) sender allowlist",
    ),
    "skipping_empty_message": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*skipping empty message",
    ),
    "skipping_duplicate_message": re.compile(
        r"^feishu:[^\n]*skipping duplicate message",
    ),
    # comment_pairing_request must be checked BEFORE pairing_request
    # since the latter's relaxed prefix would otherwise consume the
    # comment-flavoured line first.
    "comment_pairing_request": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*comment pairing request "
        r"sender=(?P<sender>\S+) code=(?P<code>\S+)",
    ),
    "pairing_request": re.compile(
        r"^feishu\[(?P<acct>[^\]]+)\][^\n]*(?<!comment )pairing request "
        r"sender=(?P<sender>\S+)",
    ),
}


# Promotion-only "virtual" keys that are reached via SDK-constant
# detection inside the WS log lines' ``err`` capture. They share the
# regex of the base WS lines but get their own counters / labels /
# severity. Listed here so ``_scan_log_files`` initializes them.
_WS_PROMOTED_KEYS = ("ws_reconnect_exhausted", "ws_autoreconnect_disabled")

# Substrings we look for inside the captured ``<err>`` of WS lines.
# Order matters: the ``reconnect exhausted`` test must come first since
# both substrings could in theory appear; in practice they're disjoint
# but we keep the order deterministic.
_WS_ERR_SDK_PROMOTIONS = (
    ("WebSocket reconnect exhausted", "ws_reconnect_exhausted"),
    (
        "WebSocket connect failed and autoReconnect is disabled",
        "ws_autoreconnect_disabled",
    ),
)


# Pretty Chinese label for each signature so the human report reads
# clearly. Keys must cover _LOG_SIGNATURES + _WS_PROMOTED_KEYS.
_SIGNATURE_LABELS: Dict[str, str] = {
    "ws_start_failed_retrying": "WebSocket 启动失败 (将重试)",
    "ws_connection_ended_recreating": "WebSocket 连接断开 (将重建)",
    "ws_close_error": "WebSocket 关闭过程出错",
    "ws_autoreconnect_disabled": "WebSocket 连接失败且 autoReconnect 关闭",
    "ws_reconnect_exhausted": "WebSocket 重连耗尽",
    "ws_recoverable_error": "WebSocket SDK 可恢复错误",
    "bot_probe_timed_out": "启动期 bot info probe 超时",
    "webhook_anomaly": "webhook 异常 (path/status)",
    "bot_identity_retry_exhausted": (
        "bot identity 后台重试耗尽 — requireMention 群消息将被静默跳过"
    ),
    "require_mention_gated": "requireMention 群消息被门控 (等待 bot identity 恢复)",
    "blocked_unauthorized_dm": "DM 被 dmPolicy 拦截",
    "blocked_unauthorized_comment": "评论被 dmPolicy 拦截",
    "group_disabled": "群被 group 配置禁用",
    "group_not_in_allowlist": "群不在 groupAllowFrom",
    "group_did_not_mention_bot": "群消息未 @ 机器人 (requireMention)",
    "sender_not_in_group_allowlist": "发送者不在群 allowlist",
    "skipping_empty_message": "空消息被丢弃",
    "skipping_duplicate_message": "重复消息被丢弃",
    "pairing_request": "pairing 请求已发起",
    "comment_pairing_request": "评论 pairing 请求已发起",
}


# Base WS log-line keys whose ``err`` capture should be inspected for
# SDK-constant promotions (see ``_WS_ERR_SDK_PROMOTIONS``).
_WS_LIFECYCLE_KEYS = frozenset({
    "ws_start_failed_retrying",
    "ws_connection_ended_recreating",
    "ws_close_error",
})


def _scan_log_line(line: str) -> Optional[Tuple[str, Dict[str, str]]]:
    """Return ``(signature_key, named_groups)`` if any pattern matches.

    For WS lifecycle lines, inspect the captured ``err`` field for the
    two SDK terminal-error constants and reroute the count to the
    matching promoted key (``ws_reconnect_exhausted`` /
    ``ws_autoreconnect_disabled``) so severity reflects the real failure
    mode rather than the surrounding "retrying"/"recreating" wrapper.
    """
    for key, pat in _LOG_SIGNATURES.items():
        m = pat.search(line)
        if m:
            fields = {k: v for k, v in m.groupdict().items() if v}
            if key in _WS_LIFECYCLE_KEYS:
                err = fields.get("err", "")
                for needle, promoted in _WS_ERR_SDK_PROMOTIONS:
                    if needle in err:
                        return promoted, fields
            return key, fields
    return None


def _scan_log_files(log_files: List[str]) -> Dict[str, Any]:
    """Walk log files for feishu-bundled signatures.

    Path-filtering and the gateway-relay rejection happen inside
    ``iter_plugin_log_lines``; here we only run anchored regexes
    against the parsed message body. Returns per-signature counts +
    a capped sample of recent matches.
    """
    all_keys = list(_LOG_SIGNATURES) + list(_WS_PROMOTED_KEYS)
    counts: Dict[str, int] = {k: 0 for k in all_keys}
    samples: Dict[str, List[Dict[str, Any]]] = {k: [] for k in all_keys}
    SAMPLES_PER_SIG = 3

    for obj, message, basename in iter_plugin_log_lines(
        log_files, _FEISHU_BUNDLED_PATH_PREFIXES,
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
    """Convert raw scan counts into per-signature Findings.

    Severity rules:
      - ``ws_autoreconnect_disabled`` / ``ws_reconnect_exhausted`` /
        ``bot_probe_timed_out`` → fail (connection broken or near it)
      - ``webhook_anomaly`` / ``ws_recoverable_error`` → warn
      - drop signatures (blocked_*, group_*, sender_not_*, did_not_mention)
        → warn (these are USEFUL signals: "your DM is being silently
        dropped because allowlist") — surfaced because the user is
        almost always asking "why didn't I get a reply"
      - skipping_empty_message / skipping_duplicate_message /
        pairing_request → ok (informational; expected behavior)
    """
    counts = scan["counts"]
    samples = scan["samples"]
    out: List[Finding] = []

    fail_keys = {
        # SDK terminal errors promoted from WS lifecycle ``err`` text
        "ws_autoreconnect_disabled",
        "ws_reconnect_exhausted",
        # bot identity totally gave up — silent drops follow until restart
        "bot_identity_retry_exhausted",
        "bot_probe_timed_out",
    }
    warn_keys = {
        # WS lifecycle (no SDK terminal constant) — recoverable but
        # worth surfacing
        "ws_start_failed_retrying",
        "ws_connection_ended_recreating",
        "ws_close_error",
        "ws_recoverable_error",
        "webhook_anomaly",
        "require_mention_gated",
        "blocked_unauthorized_dm",
        "blocked_unauthorized_comment",
        "group_disabled",
        "group_not_in_allowlist",
        "group_did_not_mention_bot",
        "sender_not_in_group_allowlist",
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
    """Resolve credentials and invoke the canonical Feishu token probe.

    Returns ONE Finding per account (probe state). Plain string
    secrets are honored as-is (the plugin tolerates them too); ref
    objects go through ``probe.resolve_secret_ref``.
    """
    app_id_val = merged.get("appId")
    app_secret_val = merged.get("appSecret")
    domain = merged.get("domain") or "feishu"

    if not _is_present(app_id_val):
        return Finding(
            verdict="warn", code="PROBE_SKIPPED_NO_APP_ID",
            msg=f"账号 {account_id} 未配置 appId — 跳过主动探测",
            data={"account_id": account_id},
        )

    # appId is ALWAYS a literal string in real feishu configs (it's a
    # public identifier), but be defensive — resolve through the same
    # path so we don't crash on an exotic ref.
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

    # ── Hot path: actually call Feishu. ``app_secret_resolution.value``
    # is plaintext; do NOT pass it into Finding fields (data, evidence,
    # msg) below — only into the urllib request itself.
    result = probe_mod.feishu_token_probe(
        app_id=app_id_resolution.value,
        app_secret=app_secret_resolution.value,
        domain=str(domain),
    )

    # Build the finding from ``result`` (no plaintext secret involved).
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
                f"账号 {account_id} 凭证被 Feishu 拒绝: "
                f"code={result.api_code} msg={result.msg}"
            ),
            evidence=f"endpoint={result.domain}",
            data=base_data,
        )
    # unreachable — explicitly NOT a credential verdict
    return Finding(
        verdict="warn", code="PROBE_UNREACHABLE",
        msg=(
            f"账号 {account_id} 主动探测无法到达 Feishu API "
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
    """Run all enabled layers for feishu-bundled.

    ``log_files`` should already be filtered to the relevant time
    window by the caller (the channel collector reuses the recent_logs
    helpers — same window cron_jobs / recent_errors use).

    ``account_filter`` (the CLI's ``--account`` flag) scopes the
    diagnosis to a single account id when set. None → all accounts.
    Unknown id → zero accounts and an actionable note listing the
    available ids.
    """
    report = VariantReport(variant="feishu-bundled", detect_basis=detect_basis)

    feishu_cfg = (
        (cfg.get("channels") or {}).get("feishu")
        if isinstance(cfg, dict) else None
    )
    if not isinstance(feishu_cfg, dict):
        # Detection said feishu-bundled is installed but config block
        # is missing — surface honestly rather than fabricating an
        # account.
        report.notes.append(
            "检测到 feishu-bundled 包但 channels.feishu 配置缺失 — "
            "插件不会启动",
        )
        return report

    all_ids = _list_account_ids(feishu_cfg)
    iter_ids = apply_account_filter(all_ids, account_filter, report)
    for account_id in iter_ids:
        merged = _merge_account_config(feishu_cfg, account_id)
        acct_rep = _diagnose_account_config(
            account_id, merged, sender_open_id=sender_open_id,
        )
        report.accounts.append(acct_rep)

        if do_probe:
            report.probe_findings.append(_run_probe(account_id, merged, cfg))

    # L2/L3 log scan is shared across accounts — the regexes capture
    # the acct field so the human can still see per-account counts.
    log_scan = _scan_log_files(log_files)
    report.log_findings = _build_log_findings(log_scan)

    return report
