"""Self-pollution-safe iteration over openclaw JSON log lines.

Why this exists
---------------
The openclaw logger writes structured JSON; plugin diagnostic lines
emerge from the per-plugin logger and carry their own path metadata in
``_meta.path.fullFilePath``. The openclaw gateway also has a **console
relay** (``console-*.js``) that captures the assistant's own chat
output verbatim and writes it back into the same log file — message
body containing whatever the assistant just said.

That created a self-pollution problem: when a previous diagnostic run
mentioned literal signature strings (e.g. ``feishu[main]: blocked
unauthorized sender ...``) in its own report, the **next** time the
diagnostic ran it would re-match those same words from its own prior
output and falsely report drops. Worst case is "I caught a drop" being
captured by the relay and matched again next run = self-pollution
loop.

This helper enforces two layered defences variants share:

  (1) PATH WHITELIST — only accept JSON lines whose
      ``_meta.path.fullFilePath`` looks like a known plugin-dist tree
      OR an openclaw-runtime logger sink that plugins legitimately
      emit through. Lines whose path matches a gateway console-relay
      sink (``/dist/console-*``) are dropped before any regex runs.
      Lines without a path field at all (older log formats, raw
      non-JSON) fall through to (2).

  (2) ANCHORED REGEXES — variants build their patterns with ``^`` so
      a signature must begin at the START of the message field, never
      embedded mid-sentence. Combined with (1) this all but eliminates
      the case where natural-language assistant chat text containing a
      signature substring registers as a hit.

The helper itself is the path-filter half (1); each variant supplies
the anchored regexes (2) plus its own path-prefix whitelist.
"""

from __future__ import annotations

import json
import os
from typing import Iterable, Iterator, Optional, Sequence, Tuple


# Channel-line markers
# --------------------
# The openclaw logger writes the structured-args field ``_meta.name`` as a
# JSON-ENCODED STRING that holds ``{"subsystem":"<name>"}``. Field ``"0"``
# of the line carries the same string. To classify a line as a CHANNEL
# emission we extract the inner ``subsystem`` value and check whether it
# (lowercased) contains any of these tokens. Note that the lark plugin
# also logs through subsystem ``feishu/<...>`` (see
# ``channel-src/openclaw-lark/src/core/lark-logger.ts`` —
# ``resolveRuntimeLogger`` builds the name as ``feishu/${subsystem}``).
# So matching ``"feishu"`` covers both bundled feishu AND lark; we never
# require the literal token ``"lark"``.
CHANNEL_SUBSYSTEM_TOKENS = ("feishu", "lark", "dingtalk", "wecom")

# Some channel emissions don't carry a subsystem at all (older lines,
# secondary loggers). As a fallback, accept message bodies whose first
# few characters match a known channel prefix.
CHANNEL_MESSAGE_PREFIXES = (
    "feishu[",
    "[DingTalk]",
    "[DingTalk:",
    "DingTalk:",
    "dingtalk-connector[",
    "[wecom",
    "[WeCom",
    "[webhook]",
)


# Paths that always indicate the openclaw gateway's console relay,
# never a plugin's own diagnostic emission. Reject everywhere — assistant
# chat captured by the relay must not feed the scanner. The substring
# ``/dist/console-`` is specific enough that genuine plugin code can't
# accidentally match (no plugin ships a ``console-*.js`` under its own
# ``dist/`` tree — that is the openclaw runtime's bundling convention).
_GATEWAY_RELAY_REJECT = ("/dist/console-",)


def _full_file_path(obj: dict) -> Optional[str]:
    """Return ``_meta.path.fullFilePath`` if present and a string."""
    meta = obj.get("_meta")
    if not isinstance(meta, dict):
        return None
    path_obj = meta.get("path")
    if not isinstance(path_obj, dict):
        return None
    full = path_obj.get("fullFilePath") or path_obj.get("filePath")
    return full if isinstance(full, str) else None


def _extract_message(obj: dict) -> Optional[str]:
    """Pick the human-readable message body from a parsed log line.

    The openclaw logger writes the formatted message into the
    top-level ``message`` field; the structured args go under "0",
    "1", ... When ``message`` is missing (some compact records) we
    fall back to the "1" arg (where plugin code typically passes the
    rendered string), then "0" as a final defence.
    """
    msg = obj.get("message")
    if isinstance(msg, str):
        return msg
    arg1 = obj.get("1")
    if isinstance(arg1, str):
        return arg1
    arg0 = obj.get("0")
    if isinstance(arg0, str):
        return arg0
    return None


# Public alias — the leading underscore is historical (this function
# used to be an internal helper of ``iter_plugin_log_lines``). New
# callers that need the message body without going through the
# iteration helper should use :func:`extract_message` directly.
def extract_message(obj: dict) -> Optional[str]:
    """Public alias for :func:`_extract_message` (see docstring)."""
    return _extract_message(obj)


def iter_plugin_log_lines(
    log_files: Iterable[str],
    allowed_path_prefixes: Sequence[str],
) -> Iterator[Tuple[Optional[dict], str, str]]:
    """Yield ``(parsed_obj, message, log_basename)`` for plugin-emitted
    lines, filtering out gateway console relay self-pollution.

    Parameters
    ----------
    log_files
        Absolute paths to ``openclaw-YYYY-MM-DD.log`` files. Files we
        can't open are silently skipped (matches existing variant
        behaviour — log scan must never crash the channel collector).
    allowed_path_prefixes
        Substrings that mark a plugin-tree path. A line whose
        ``_meta.path.fullFilePath`` contains any of these passes. When
        this list is empty, only the rejection rules apply.

    Behaviour
    ---------
    For each line:

      * On JSON parse success, examine ``_meta.path.fullFilePath``:
        - reject if it matches a gateway relay sink
          (``/dist/console-*``);
        - if a path is present and ``allowed_path_prefixes`` is
          non-empty, reject when none of those substrings appears;
        - otherwise yield ``(obj, message, basename)`` where message
          is the parsed message body. Downstream regexes anchor with
          ``^`` against this body, NOT the raw line, so JSON noise
          (timestamps, ``_meta`` blob) can't act as a false anchor.

      * On JSON parse failure (non-JSON line, partial line), yield
        ``(None, raw, basename)`` so the variant can still apply its
        anchored regex against the raw text. Anchored regexes on raw
        JSON are still safe — assistant chat in console relay is
        always wrapped in JSON and reaches the success path above
        instead.
    """
    for path in log_files:
        try:
            f = open(path, errors="replace")
        except OSError:
            continue
        basename = os.path.basename(path)
        with f:
            for raw in f:
                obj = None
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        obj = parsed
                except (ValueError, TypeError):
                    obj = None

                if obj is None:
                    # Non-JSON line — yield raw text so anchored regexes
                    # still get a chance. Variants can decide whether to
                    # accept; in practice openclaw logs are JSON-only on
                    # current builds, so this is rare.
                    yield None, raw, basename
                    continue

                full = _full_file_path(obj)
                if full is not None:
                    if any(needle in full for needle in _GATEWAY_RELAY_REJECT):
                        continue
                    if allowed_path_prefixes and not any(
                        needle in full for needle in allowed_path_prefixes
                    ):
                        continue
                msg = _extract_message(obj)
                if msg is None:
                    continue
                yield obj, msg, basename


def extract_ts(obj: Optional[dict]) -> str:
    """Pull a ``time`` field for sample display; tolerate missing obj.

    Falls back to ``_meta.date`` (the openclaw logger's UTC timestamp)
    when the top-level ``time`` field is absent — older lines in the
    log archive sometimes have one but not the other.
    """
    if not isinstance(obj, dict):
        return ""
    ts = obj.get("time")
    if isinstance(ts, str):
        return ts[:19]
    meta = obj.get("_meta")
    if isinstance(meta, dict):
        date = meta.get("date")
        if isinstance(date, str):
            return date[:19]
    return ""


def extract_subsystem(obj: Optional[dict]) -> Optional[str]:
    """Pull the channel subsystem name from a parsed line.

    The openclaw logger encodes the structured ``name`` field as a JSON
    string holding ``{"subsystem":"<name>"}``. The same string is also
    written to field ``"0"`` of the top-level record. We try the
    ``_meta.name`` location first since it's the canonical source.

    Returns the inner ``subsystem`` value (e.g. ``"channels/feishu"``)
    or ``None`` when the line has no parseable subsystem.
    """
    if not isinstance(obj, dict):
        return None

    candidates = []
    meta = obj.get("_meta")
    if isinstance(meta, dict):
        name = meta.get("name")
        if isinstance(name, str):
            candidates.append(name)
    field0 = obj.get("0")
    if isinstance(field0, str):
        candidates.append(field0)

    for cand in candidates:
        # Cheap parse attempt — these strings are JSON-encoded by the
        # logger but malformed strings are common (older logs, tools
        # that hand-build a record). We tolerate failures silently.
        if not cand or "subsystem" not in cand:
            continue
        try:
            parsed = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            sub = parsed.get("subsystem")
            if isinstance(sub, str):
                return sub
    return None


def is_console_relay_path(obj: Optional[dict]) -> bool:
    """True when the line came from the gateway's console relay.

    Used by the channel collector's pre-classification gate: console-
    relay lines are assistant chat captured by the openclaw runtime
    and MUST never be matched against signature phrases (otherwise
    diagnostic output containing those phrases self-pollinates the
    next run). The full file path is recognisable by the
    ``/dist/console-`` substring (no plugin emits a file under that
    naming convention).
    """
    if not isinstance(obj, dict):
        return False
    full = _full_file_path(obj)
    if not full:
        return False
    return any(needle in full for needle in _GATEWAY_RELAY_REJECT)


def is_channel_subsystem(subsystem: Optional[str]) -> bool:
    """True when the subsystem (lowercased) carries a channel token.

    Subsystems are slash-paths like ``channels/feishu``,
    ``gateway/channels/feishu``, ``feishu/channel/monitor`` (lark's
    runtime-prefixed form). Substring matching against
    :data:`CHANNEL_SUBSYSTEM_TOKENS` covers all these shapes.
    """
    if not subsystem:
        return False
    lowered = subsystem.lower()
    return any(token in lowered for token in CHANNEL_SUBSYSTEM_TOKENS)


def is_channel_message_prefix(message: Optional[str]) -> bool:
    """True when the message body starts with a known channel prefix.

    Used as a fallback when a line has no parseable subsystem (older
    log lines, plugins that bypass the structured logger). Anchored
    at the start of the message body — substring elsewhere doesn't
    qualify, which is part of the self-pollution defence: relay echo
    of "we found feishu[main]: ..." in the middle of a sentence
    doesn't trip the prefix check.
    """
    if not message:
        return False
    for prefix in CHANNEL_MESSAGE_PREFIXES:
        if message.startswith(prefix):
            return True
    return False
