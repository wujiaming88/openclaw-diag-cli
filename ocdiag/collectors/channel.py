"""channel collector — pure IM-channel log scanner.

Scope and contract
------------------
This collector is intentionally NOT a config interpreter. It does not
look at ``channels.*`` config keys, does not detect installed packages,
and never makes outbound network calls. Its single job is to surface
the IM-channel-related ERROR/WARNING lines (plus a small set of
INFO-level "silent drop / gating" signals) from the openclaw structured
logs over the last 7 days.

This positioning is deliberate: prior versions tried to interpret config
+ run live probes, which produced noisy bundled-vs-lark double reports
and required maintaining a per-variant rule table that lagged upstream
plugin changes. Operators consistently asked one question: *"what does
the log actually say went wrong?"* This collector answers exactly that.

Design choices
~~~~~~~~~~~~~~

* **Detection key on subsystem, not literal "lark".** The lark plugin
  logs through subsystem ``feishu/<sub>`` (see
  ``channel-src/openclaw-lark/src/core/lark-logger.ts``); the bundled
  feishu plugin logs through ``channels/feishu``. Both share the
  ``feishu[<acct>]:`` message prefix. We classify a line as channel
  iff its subsystem (lowercased) contains one of the four channel
  tokens — or its message body starts with a known channel prefix
  (fallback for plugins that bypass the structured subsystem field).

* **Self-pollution guard reused.** ``ocdiag/channels/log_utils.py``
  already rejects gateway console-relay lines (path contains
  ``/dist/console-``) and we keep that gate. Diagnostic output that
  echoes back through the relay is on a non-channel subsystem AND a
  relay path, so it can't pollinate the catalog.

* **Severity merge.** A line's severity is the worst of (level-based,
  phrase-based). Level-based: ``logLevelId>=5`` → error, ``==4`` →
  warn. Phrase-based: see :mod:`ocdiag.channels.signals`. The mapping
  to verdict is error→FAIL, warn→WARN, info/benign→OK (still shown).

* **Display cap.** We cap displayed signals at 20 newest-first; if more
  matched, we add a note. Cap exists because operators typically
  scroll-and-skim; a 7-day window can produce dozens of warns and the
  dump becomes unreadable. JSON output is unaffected (full count in
  ``report.data``).
"""

from __future__ import annotations

import glob
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ..channels import log_utils, signals
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict


# Display cap for matched signals. JSON consumers see the full total
# under ``report.data['matched_count']``; the human renderer never
# needs to scroll past 20 lines per command. 20 was chosen empirically
# as the comfort cliff before a terminal-page flood.
_DISPLAY_CAP = 20

# Log level ids the openclaw logger emits. Mapping confirmed from
# ``logLevelId``/``logLevelName`` pairs in ``/tmp/openclaw/openclaw-*.log``:
# 2=DEBUG 3=INFO 4=WARN 5=ERROR 6=FATAL.
_LEVEL_WARN = 4
_LEVEL_ERROR = 5


# Convenience: severity ordering used to merge level-based + phrase-based
# severity into a single bucket per line. Higher rank → worse.
_SEVERITY_RANK = {"info": 0, "warn": 1, "error": 2}


def _severity_max(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Return the more-severe of two severity strings, ignoring ``None``."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _SEVERITY_RANK[a] >= _SEVERITY_RANK[b] else b


def _severity_to_verdict(sev: str) -> Verdict:
    """Map our 3-bucket severity to the core Verdict enum."""
    if sev == "error":
        return Verdict.FAIL
    if sev == "warn":
        return Verdict.WARN
    return Verdict.OK


def _resolve_log_files(ctx: DiagContext) -> List[str]:
    """Pick the candidate log files for a 7d-window scan.

    We walk ``openclaw-*.log`` under ``ctx.log_dir`` directly with a
    7-day mtime floor — wider than the today-only scan in
    :mod:`ocdiag.recent_logs`. Errors during stat are tolerated:
    unreadable files are silently skipped (the collector must never
    crash on a stat failure).

    Returned newest-first to make the line-count cap deterministic.
    """
    log_dir = str(ctx.log_dir)
    if not os.path.isdir(log_dir):
        return []
    pattern = os.path.join(log_dir, "openclaw-*.log")
    cutoff = time.time() - 7 * 86400
    matched: List[Tuple[float, str]] = []
    for path in glob.glob(pattern):
        try:
            mt = os.path.getmtime(path)
        except OSError:
            # File vanished mid-scan or permission error — skip silently.
            continue
        if mt >= cutoff:
            matched.append((mt, path))
    matched.sort(reverse=True)
    return [p for _, p in matched]


def _short_subsystem(subsystem: Optional[str]) -> str:
    """Compact subsystem label for display.

    The runtime emits paths like ``gateway/channels/feishu`` and
    ``feishu/channel/monitor`` — all useful but visually busy in a
    rendered Section. We keep the FULL subsystem here (never trimmed)
    so the operator can see whether a finding came from the gateway's
    forwarding layer or the plugin's own emission point. This was a
    common diagnosis question in v1.8.x and we want to preserve it.
    """
    return subsystem or "?"


def _line_severity(obj: Dict[str, Any], message: str) -> Optional[str]:
    """Compute the severity bucket for a single line.

    Combines level-based severity (``_meta.logLevelId``) and
    phrase-based severity (catalog match on ``message``). Returns
    ``None`` when neither side fires, meaning the caller should NOT
    collect this line as a signal.
    """
    level_sev: Optional[str] = None
    meta = obj.get("_meta") if isinstance(obj, dict) else None
    if isinstance(meta, dict):
        lvl = meta.get("logLevelId")
        if isinstance(lvl, (int, float)):
            if lvl >= _LEVEL_ERROR:
                level_sev = "error"
            elif lvl == _LEVEL_WARN:
                level_sev = "warn"

    phrase_sev = signals.classify(message)
    return _severity_max(level_sev, phrase_sev)


def _level_name(obj: Dict[str, Any]) -> str:
    """Best-effort log level name for display ('WARN' / 'ERROR' / ...)."""
    meta = obj.get("_meta") if isinstance(obj, dict) else None
    if isinstance(meta, dict):
        name = meta.get("logLevelName")
        if isinstance(name, str):
            return name
    return "?"


def _matches_account_filter(message: str, filt: Optional[str]) -> bool:
    """Substring-match the account filter against the channel prefix.

    The filter is matched against the WHOLE message body; the cheap
    rule is that the plugin emits ``feishu[<acct>]:`` /
    ``[DingTalk:<acct>]`` near the start, so a substring containing
    the account id will hit. We don't try to parse the prefix
    structurally — message bodies vary too much. ``filt is None``
    (no filter) → always passes.
    """
    if not filt:
        return True
    return filt in message


def _scan_log_file(
    path: str,
    account_filter: Optional[str],
) -> List[Dict[str, Any]]:
    """Yield collected signal records from one log file.

    Each record is a dict with the keys ``time``, ``level_name``,
    ``severity``, ``subsystem``, ``message``, ``log_file``. We never
    truncate ``message`` — full content is a hard requirement so the
    operator can see the entire line (account ids, chat ids, error
    text) without consulting the raw log.

    Errors are absorbed silently: an unreadable line yields nothing,
    an unreadable file yields ``[]``. Channel diagnostics must never
    crash on log noise.
    """
    out: List[Dict[str, Any]] = []
    try:
        f = open(path, errors="replace")
    except OSError:
        return out

    basename = os.path.basename(path)
    with f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line:
                continue

            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue

            # Self-pollution gate: console-relay lines are excluded
            # before subsystem / phrase checks ever run.
            if log_utils.is_console_relay_path(obj):
                continue

            message = log_utils.extract_message(obj)
            if not message:
                continue

            # Channel classification — by subsystem first, message
            # prefix as fallback.
            subsystem = log_utils.extract_subsystem(obj)
            is_channel = (
                log_utils.is_channel_subsystem(subsystem)
                or log_utils.is_channel_message_prefix(message)
            )
            if not is_channel:
                continue

            # Optional --account filter (substring match on body).
            if not _matches_account_filter(message, account_filter):
                continue

            severity = _line_severity(obj, message)
            if severity is None:
                continue

            ts = log_utils.extract_ts(obj)
            out.append({
                "time": ts,
                "level_name": _level_name(obj),
                "severity": severity,
                "subsystem": subsystem or "",
                "message": message,
                "log_file": basename,
            })
    return out


def _detected_markers(records: List[Dict[str, Any]]) -> List[str]:
    """Collect the distinct channel-token markers seen in records.

    Used in the head summary so the operator can confirm which
    channels actually emitted lines this window — a quick sanity
    check ("we expected dingtalk traffic and saw none").
    """
    markers: List[str] = []
    seen = set()
    for rec in records:
        sub = (rec.get("subsystem") or "").lower()
        msg = rec.get("message") or ""
        for token in log_utils.CHANNEL_SUBSYSTEM_TOKENS:
            if token in seen:
                continue
            if token in sub or token in msg.lower():
                seen.add(token)
                markers.append(token)
    return markers


def _format_signal_message(rec: Dict[str, Any]) -> str:
    """Build the full single-line display for a collected signal.

    Format: ``[<ts>] <LEVEL> <subsystem> | <full message>``. The full
    message is included verbatim (no truncation) per the log-fidelity
    requirement.
    """
    ts = rec.get("time") or "?"
    lvl = rec.get("level_name") or "?"
    sub = _short_subsystem(rec.get("subsystem"))
    msg = rec.get("message") or ""
    return f"[{ts}] {lvl} {sub} | {msg}"


@register
class ChannelCollector:
    """Pure channel-log signal collector.

    See module docstring for the full contract. The implementation
    intentionally stays small — every error path returns an empty list
    rather than raising, because the collector runs from the ``all``
    aggregator where one crash would abort downstream collectors.
    """

    id = "channel"
    title = "渠道日志信号"
    kind = "state"

    def collect(self, ctx: DiagContext, **kwargs) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)
        report.add_scope("app_logs", "7d")

        # ``--account`` (substring filter on the message body). Both
        # the kwargs path (test direct calls) and ``ctx.account_id``
        # (CLI-via-main path) are accepted; kwargs win for explicit
        # test injection.
        account_filter: Optional[str] = (
            kwargs.get("account_filter")
            or getattr(ctx, "account_id", None)
        )

        log_files = _resolve_log_files(ctx)
        report.data["log_files_scanned"] = [
            os.path.basename(p) for p in log_files
        ]
        report.data["account_filter"] = account_filter

        # Walk every candidate file. Errors per-file are already absorbed
        # inside ``_scan_log_file``; we only care about the merged stream.
        records: List[Dict[str, Any]] = []
        for path in log_files:
            records.extend(_scan_log_file(path, account_filter))

        # Newest-first by timestamp string. ``time`` is ISO-8601-like
        # (``2026-06-12T10:29:07``) so lexicographic sort is correct
        # within a single timezone. Empty timestamps sort last.
        records.sort(key=lambda r: (r.get("time") or ""), reverse=True)

        total = len(records)
        report.data["matched_count"] = total

        # Build display: cap at _DISPLAY_CAP. Note that we KEEP all
        # records in report.data['signals'] for JSON consumers; only
        # the rendered Section is capped.
        display = records[:_DISPLAY_CAP]
        report.data["signals"] = records  # full list for JSON
        report.data["display_cap"] = _DISPLAY_CAP

        markers = _detected_markers(records)
        report.data["channel_markers"] = markers

        # ── Section: head summary ────────────────────────────────────
        head = report.section("0. 概览")
        head_msg_bits = [
            f"扫描 {len(log_files)} 个日志文件 (7 天窗口)",
            f"匹配 {total} 条 channel 信号",
        ]
        if markers:
            head_msg_bits.append("检测到渠道: " + ", ".join(markers))
        if account_filter:
            head_msg_bits.append(f"账号过滤: {account_filter!r}")
        head.ok(
            "channel.summary",
            "; ".join(head_msg_bits),
            data={
                "log_files_scanned": report.data["log_files_scanned"],
                "matched_count": total,
                "channel_markers": markers,
                "account_filter": account_filter,
            },
        )

        # ── Section: signals ────────────────────────────────────────
        if total == 0:
            # Single OK check — explicitly tells the operator we
            # looked, and where, rather than rendering an empty
            # section.
            head.ok(
                "channel.no_signals",
                (
                    f"未发现 channel 错误/告警/关键信号日志（扫描 "
                    f"{len(log_files)} 个日志文件，7 天窗口）"
                ),
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        signals_section = report.section(
            f"1. 信号 (最近 {min(total, _DISPLAY_CAP)}/{total})"
        )

        if total > _DISPLAY_CAP:
            signals_section.ok(
                "channel.signals.cap_note",
                f"共 {total} 条匹配，按时间倒序显示最近 {_DISPLAY_CAP} 条",
                data={"total": total, "shown": _DISPLAY_CAP},
            )

        # Each collected signal is one Check whose verdict reflects
        # severity. Names are stable but indexed so renders deduplicate
        # cleanly; the human renderer surfaces verdict via glyph.
        for i, rec in enumerate(display):
            verdict = _severity_to_verdict(rec["severity"])
            signals_section.add(
                name=f"channel.signal.{i+1:02d}",
                verdict=verdict,
                message=_format_signal_message(rec),
                data={
                    "time": rec.get("time"),
                    "level_name": rec.get("level_name"),
                    "severity": rec.get("severity"),
                    "subsystem": rec.get("subsystem"),
                    "log_file": rec.get("log_file"),
                    "message": rec.get("message"),
                },
            )

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
