"""Channel-log key-signal phrase catalog.

The :mod:`channel` collector is a PURE log collector — it surfaces
ERROR/WARNING lines from IM channel subsystems plus the small set of
INFO-level "silent drop / gating" lines that operators usually care
about (a config rejected a message, a channel intentionally swallowed
an event, etc.). The level filter alone misses those because the
plugins log them at INFO.

This module owns the phrase catalog. ``classify(message)`` returns:

  - ``"error"``: a phrase that we want surfaced as a hard finding even
    if the underlying log level is below ERROR (rarely used today —
    the level filter catches almost every "real" error already, but we
    leave the bucket open so a phrase can promote a benign-level line
    if a future plugin starts emitting ERROR-grade content at INFO).
  - ``"warn"``: gating / blocking decisions that warn operators when
    something silently rejected a sender or a group.
  - ``"info"``: benign drop reasons (duplicates, empty messages,
    self-echoes) — collected for visibility but never bump the verdict.
  - ``None``: no catalog phrase matched.

Sources verified for each phrase:

  * feishu (bundled + lark share the same emission shape):
      - ``@openclaw/feishu/dist/monitor.account-*.js`` — bundled
      - ``channel-src/openclaw-lark/src/messaging/inbound/gate.ts``
      - ``channel-src/openclaw-lark/src/messaging/inbound/event-handlers.ts``
      - ``channel-src/openclaw-lark/src/messaging/inbound/handler.ts``
      - ``channel-src/openclaw-lark/src/channel/monitor.ts``
      - ``channel-src/openclaw-lark/src/channel/comment-handler.ts``
      - ``channel-src/openclaw-lark/src/channel/vc-meeting-invited-handler.ts``
  * dingtalk:
      - ``channel-src/dingtalk-openclaw-connector/src/core/message-handler.ts``
      - ``channel-src/dingtalk-openclaw-connector/src/core/connection.ts``
  * wecom:
      - ``channel-src/wecom-openclaw-plugin/src/webhook/monitor.ts``
      - ``channel-src/wecom-openclaw-plugin/src/dm-policy.ts``
      - ``channel-src/wecom-openclaw-plugin/src/group-policy.ts``

The phrases are SUBSTRING matches on the raw message body (case-
sensitive — every plugin emits the literal English/Chinese form).
Self-pollution risk is mitigated upstream: ``classify`` is only
invoked on messages that already passed the channel-subsystem /
prefix gate, which excludes assistant chat (relay echo never carries
a channel subsystem).
"""

from __future__ import annotations

from typing import Optional


# Phrases that signal an ATTENTION-grade silent drop or gating
# decision. Each is a literal string emitted by one of the plugins
# (feishu / lark / dingtalk / wecom). Substring matched.
_ATTENTION_PHRASES = (
    # ── feishu / lark gating + bot-identity recovery ──────────────────
    # gate.ts:139 (lark) / monitor.account-*.js (bundled) — sender allowlist
    "blocked unauthorized sender",
    # comment-handler.ts:148 (lark) — comment sender allowlist
    "blocked unauthorized comment sender",
    # gate.ts:195 (lark) — legacy chat-id in groupAllowFrom warning
    "not in groupAllowFrom",
    # gate.ts:453 (lark) — DM allowlist rejection
    "not in DM allowlist",
    # gate.ts:369 (lark) — sender not allowed in group
    "not allowed in group",
    # gate.ts:225 (lark) — group blocked by group-level policy
    "blocked by group-level policy",
    # gate.ts:239 (lark) — per-group disabled
    "disabled by per-group config",
    # gate.ts:401 (lark) / monitor.account-*.js (bundled)
    "did not mention bot",
    # event-handlers.ts:108 (lark) — message expired
    "expired, discarding",
    # gate.ts:474 (lark) — DM pairing flow (info-ish but operator-visible)
    "not paired, creating pairing request",
    # comment-handler.ts:165 (lark) — pairing request creation failure
    "failed to create pairing request",
    # dm-policy.ts:109 (wecom) — pairing reply send failure
    "pairing reply failed",
    # message-handler.ts:1126 (dingtalk) — pairing request failure (zh)
    "pairing request failed",
    # monitor.account-*.js:3500 (bundled) — bot identity recovery exhausted
    "bot identity background retry exhausted",
    # monitor.account-*.js:3506 (bundled)
    "requireMention group messages stay gated",
    # monitor.account-*.js companion line
    "stay gated until bot identity",
    # monitor.account-*.js — bot probe timeout
    "bot info probe timed out",
    # channel/monitor.ts:58-61 (lark) — silent NOOP webhook mode
    "webhook mode not implemented",
    # message-handler.ts (dingtalk) — dispatch failure
    "failed to dispatch",
    # card-handler.ts (lark) — card action rejected
    "rejected card action",

    # ── dingtalk Chinese phrases (literal in source) ──────────────────
    # message-handler.ts:1108 — group blocked
    "群聊被拦截",
    # message-handler.ts:1063 — explicit group disabled state
    "groupPolicy=disabled",
    # connection.ts — token fetch error
    "failed to get access token",
    # connection.ts — bad creds
    "clientId or clientSecret is invalid",
)


# Phrases that signal a BENIGN drop. We surface them because operators
# sometimes ask "why didn't my message get through" and want to see
# "your duplicate/empty/echo was deduped", but they never raise the
# overall report verdict.
_BENIGN_PHRASES = (
    # handler.ts:106 (lark) — empty message, paired with "skipping"
    "skipping empty message",
    # event-handlers.ts:102 (lark) — duplicate dedup
    "skipping duplicate",
    # event-handlers.ts:89 (lark) — self-echo guard
    "drop self-echo",
    # comment-handler.ts (lark) — duplicate reaction de-dup
    "duplicate reaction",
    # agent/handler.ts:247 (wecom) — agent-mode duplicate
    "dropping duplicate",
    # webhook/handler.ts (wecom) — in-flight retry
    "dropping in-flight",
    # gate.ts:474 (lark) — pairing request creation log line (info)
    "pairing request sender=",
    # comment-handler.ts (lark) — pairing for comment sender
    "comment pairing request sender=",
)


# Substring fragments that, taken together, mean "duplicate" + "skip".
# Catches the two-token form "duplicate message ... skipping" /
# "duplicate ... skipping" without committing to a fixed phrase.
_BENIGN_DUPLICATE_FRAGMENTS = ("duplicate", "skipping")
_BENIGN_EMPTY_FRAGMENTS = ("empty message", "skipping")


# Substring fragments for "group ... is disabled" / "group ... disabled
# by per-group" — the runtime-reported group-disabled signal that
# wraps an operator-visible chatId. We treat as ATTENTION.
_GROUP_DISABLED_FRAGMENTS = ("group ", " is disabled")
_GROUP_DISABLED_FRAGMENTS_PERGROUP = ("group ", "disabled by per-group")


def classify(message: str) -> Optional[str]:
    """Classify a CHANNEL log message body.

    Returns:
      ``"warn"``  – attention-grade phrase (silent drop / gating /
                    bot-identity / connection).
      ``"info"``  – benign phrase (dedup, empty, self-echo, pairing
                    request creation).
      ``None``    – no catalog phrase matched. The caller still gets
                    the line collected if it was a level-based catch
                    (WARN / ERROR); ``classify`` only handles
                    phrase-based catches.

    Note: this catalog never returns ``"error"`` directly today —
    every error-grade signal is already caught by the
    ``logLevelId>=5`` filter upstream. The bucket is reserved for
    future cases where a plugin emits an error-grade phrase at a
    benign level.

    Substring matching is intentional: plugin emissions vary across
    versions in surrounding context (account ids, chat ids, error
    text) but the core phrase is stable. Anchoring with ``^`` would
    have rejected real lines like ``feishu[default]: blocked
    unauthorized sender …`` because of the ``feishu[<acct>]: ``
    prefix.
    """
    if not message:
        return None

    # ATTENTION first — a single line could match both buckets (rare,
    # but e.g. a message containing both "duplicate" and "blocked" is
    # safer flagged as warn than info).
    for needle in _ATTENTION_PHRASES:
        if needle in message:
            return "warn"

    # Group-disabled multi-fragment pattern (operator pasted a chat id
    # in the middle of the phrase).
    if all(frag in message for frag in _GROUP_DISABLED_FRAGMENTS):
        return "warn"
    if all(frag in message for frag in _GROUP_DISABLED_FRAGMENTS_PERGROUP):
        return "warn"

    # BENIGN fixed phrases.
    for needle in _BENIGN_PHRASES:
        if needle in message:
            return "info"

    # Multi-fragment benign forms.
    if all(frag in message for frag in _BENIGN_DUPLICATE_FRAGMENTS):
        return "info"
    if all(frag in message for frag in _BENIGN_EMPTY_FRAGMENTS):
        return "info"

    return None


__all__ = ["classify"]
