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
    """Pull a ``time`` field for sample display; tolerate missing obj."""
    if not isinstance(obj, dict):
        return ""
    ts = obj.get("time")
    return str(ts)[:19] if isinstance(ts, str) else ""
