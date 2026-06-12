"""Tests for the channel log-scanning collector.

This collector is a pure log scanner — no config interpretation, no
network probe, no per-variant detection. The tests below use synthetic
JSON log fixtures shaped exactly like the openclaw structured logger
produces (verified against /tmp/openclaw/openclaw-2026-06-12.log).

Coverage:
  * Level-based catch (WARN / ERROR on a channel subsystem)
  * Phrase-based catch (INFO + attention phrase, e.g. "blocked
    unauthorized sender")
  * Lark-shaped detection: the lark plugin emits subsystem
    ``feishu/<sub>`` and message prefix ``feishu[<acct>]:``; the word
    "lark" never appears, so detection MUST key on ``feishu``.
  * Non-channel INFO line ignored
  * Console-relay path (``/dist/console-*``) rejected
  * >20 matched → cap + 倒序 note
  * Newest-first ordering
  * Verdict: FAIL on error, WARN on warn-only, OK on none
  * Dingtalk Chinese phrase ``群聊被拦截`` collected
  * ``--account`` substring filter
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag.channels import log_utils, signals  # noqa: E402
from ocdiag.collectors import channel as channel_module  # noqa: E402
from ocdiag.core import registry  # noqa: E402
from ocdiag.core.context import DiagContext  # noqa: E402
from ocdiag.core.types import Verdict  # noqa: E402


# Real-shape paths used in fixtures.
_PLUGIN_PATH_RUNTIME = (
    "file:///root/.local/share/pnpm/global/5/.pnpm/openclaw@2026.6.1/"
    "node_modules/openclaw/dist/subsystem-DM7CD-js.js:179:14"
)
_GATEWAY_CONSOLE_RELAY_PATH = (
    "file:///root/.local/share/pnpm/global/5/.pnpm/openclaw@2026.6.1/"
    "node_modules/openclaw/dist/console-DcpfMatG.js:153:46"
)


# ── log line helper ─────────────────────────────────────────────────


def _make_line(
    *,
    message: str,
    subsystem: Optional[str] = None,
    level_id: int = 3,
    level_name: str = "INFO",
    ts: str = "2026-06-10T12:00:00",
    full_path: str = _PLUGIN_PATH_RUNTIME,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a single openclaw-logger JSON line.

    ``subsystem`` is wrapped into the JSON-encoded ``_meta.name`` /
    field ``"0"`` shape that the real logger emits — the channel
    collector extracts the inner ``"subsystem"`` value from those.
    Pass ``None`` to omit the subsystem (matches the older log shape
    or non-structured emissions).
    """
    name_str = (
        json.dumps({"subsystem": subsystem}, ensure_ascii=False)
        if subsystem is not None
        else "openclaw"
    )
    record: Dict[str, Any] = {
        "0": name_str,
        "1": message,
        "_meta": {
            "name": name_str,
            "logLevelId": level_id,
            "logLevelName": level_name,
            "path": {"fullFilePath": full_path},
            "date": ts,
        },
        "time": ts,
        "message": message,
    }
    if extra:
        record.update(extra)
    return json.dumps(record, ensure_ascii=False)


def _ctx_for(tmp_path, log_lines: List[str]) -> DiagContext:
    """Wire a DiagContext pointing at a tempdir log file."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "openclaw-2026-06-10.log"
    log_file.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return DiagContext(
        openclaw_home=tmp_path,
        config_path=tmp_path / "openclaw.json",
        log_dir=log_dir,
        sessions_base=tmp_path / "agents",
    )


def _collect(ctx: DiagContext):
    """Run the channel collector against ``ctx`` and return its Report."""
    registry.discover()
    coll = registry.get("channel")
    assert coll is not None, "channel collector missing from registry"
    return coll.collect(ctx)


# ── log_utils helpers ────────────────────────────────────────────────


def test_extract_subsystem_handles_json_encoded_name():
    """Real openclaw logs put ``_meta.name`` as a JSON string holding
    ``{"subsystem":"<name>"}``. The helper must unwrap it."""
    line = _make_line(message="x", subsystem="channels/feishu")
    obj = json.loads(line)
    assert log_utils.extract_subsystem(obj) == "channels/feishu"


def test_extract_subsystem_returns_none_when_missing():
    """An ``openclaw``-named line (no JSON-encoded subsystem) yields None."""
    line = _make_line(message="generic", subsystem=None)
    obj = json.loads(line)
    assert log_utils.extract_subsystem(obj) is None


def test_is_console_relay_path_detects_console_substring():
    """Path contains ``/dist/console-`` → True (gateway relay sink)."""
    obj = json.loads(_make_line(
        message="x", full_path=_GATEWAY_CONSOLE_RELAY_PATH,
    ))
    assert log_utils.is_console_relay_path(obj) is True


def test_is_channel_subsystem_token_match():
    """Tokens are checked as lowercase substrings of the subsystem path."""
    assert log_utils.is_channel_subsystem("channels/feishu")
    assert log_utils.is_channel_subsystem("gateway/channels/feishu")
    # Lark logs as feishu/<sub> — the lark plugin's tag never literally
    # contains "lark"; we still classify it as channel via the feishu token.
    assert log_utils.is_channel_subsystem("feishu/channel/monitor")
    assert log_utils.is_channel_subsystem("plugins/dingtalk")
    assert not log_utils.is_channel_subsystem("agents/harness")
    assert not log_utils.is_channel_subsystem("memory")
    assert not log_utils.is_channel_subsystem(None)


def test_is_channel_message_prefix_matches_known_prefixes():
    assert log_utils.is_channel_message_prefix("feishu[default]: hello")
    assert log_utils.is_channel_message_prefix("[DingTalk:main] disconnected")
    assert log_utils.is_channel_message_prefix("[WeCom] blocked DM from x")
    # Embedded mid-sentence — must NOT match (anchor defence).
    assert not log_utils.is_channel_message_prefix(
        "  feishu[main]: hello"
    )
    assert not log_utils.is_channel_message_prefix(
        "earlier the agent said feishu[main]: blocked"
    )


# ── signals catalog ──────────────────────────────────────────────────


def test_signals_classify_attention():
    assert signals.classify(
        "feishu[default]: blocked unauthorized sender ou_xyz"
    ) == "warn"


def test_signals_classify_benign_duplicate():
    assert signals.classify(
        "feishu[default]: skipping duplicate message msg-abc"
    ) == "info"
    # Also matches the multi-fragment "duplicate ... skipping" form.
    assert signals.classify(
        "feishu[default]: duplicate message om_xxx, skipping"
    ) == "info"


def test_signals_classify_chinese_dingtalk_phrase():
    """``群聊被拦截`` is the literal dingtalk emission for blocked groups."""
    assert signals.classify(
        "[DingTalk:main] 群聊被拦截: groupPolicy=disabled"
    ) == "warn"


def test_signals_classify_unknown_returns_none():
    """A plain INFO line with no catalog phrase → None."""
    assert signals.classify(
        "feishu[default]: received message from ou_x in oc_y (p2p)"
    ) is None


# ── collector behaviour ─────────────────────────────────────────────


def test_warn_level_channel_line_collected(tmp_path):
    """A WARN-level line on a channel subsystem is collected as a signal,
    even if no catalog phrase matches."""
    line = _make_line(
        message=(
            "feishu[default]: embedded run agent end "
            "(Validation error in thinking block)"
        ),
        subsystem="gateway/channels/feishu",
        level_id=4, level_name="WARN",
        ts="2026-06-10T11:00:00",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.data["matched_count"] == 1
    # Find the signal check (skip head summary section).
    sig_section = next(s for s in report.sections if s.title.startswith("1."))
    assert len(sig_section.checks) == 1
    check = sig_section.checks[0]
    assert check.verdict == Verdict.WARN
    # Full message body preserved (no truncation).
    assert "Validation error in thinking block" in check.message


def test_info_level_channel_line_with_attention_phrase_collected(tmp_path):
    """An INFO-level line whose message matches a catalog ATTENTION
    phrase is still collected, with severity warn."""
    line = _make_line(
        message=(
            "feishu[default]: blocked unauthorized sender ou_xyz "
            "(dmPolicy=allowlist)"
        ),
        subsystem="channels/feishu",
        level_id=3, level_name="INFO",
        ts="2026-06-10T12:00:00",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.data["matched_count"] == 1
    sig_section = next(s for s in report.sections if s.title.startswith("1."))
    check = sig_section.checks[0]
    assert check.verdict == Verdict.WARN  # attention phrase → warn
    assert "blocked unauthorized sender" in check.message


def test_lark_shaped_line_detected_without_lark_word(tmp_path):
    """Lark logs through subsystem ``feishu/<sub>`` (verified at
    channel-src/openclaw-lark/src/core/lark-logger.ts:resolveRuntimeLogger).
    The literal word "lark" never appears — detection must key on
    ``feishu``."""
    line = _make_line(
        message=(
            "feishu[default]: webhook mode not implemented in monitor"
        ),
        subsystem="feishu/channel/monitor",  # lark-style
        level_id=3, level_name="INFO",
        ts="2026-06-10T12:30:00",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.data["matched_count"] == 1
    # The catalog phrase makes this warn.
    sig_section = next(s for s in report.sections if s.title.startswith("1."))
    assert sig_section.checks[0].verdict == Verdict.WARN
    assert "lark" not in line  # sanity: no literal "lark" in fixture


def test_non_channel_info_line_ignored(tmp_path):
    """INFO line on an unrelated subsystem — must NOT be collected."""
    line = _make_line(
        message="message queued: sessionId=abc",
        subsystem="diagnostic",
        level_id=3, level_name="INFO",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.data["matched_count"] == 0


def test_warn_non_channel_subsystem_also_ignored(tmp_path):
    """A WARN on a non-channel subsystem (e.g. agents/harness) is NOT
    a channel signal even though level alone would have caught it. The
    channel-subsystem gate is the boundary."""
    line = _make_line(
        message="long-running session: sessionId=abc state=processing",
        subsystem="diagnostic",
        level_id=4, level_name="WARN",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.data["matched_count"] == 0


def test_console_relay_line_rejected(tmp_path):
    """A line whose path matches ``/dist/console-`` is the gateway's
    self-relay of assistant output. Even if the message body looks
    like a channel emission, it must NOT be collected — otherwise we
    self-pollinate every run."""
    line = _make_line(
        message=(
            "feishu[default]: blocked unauthorized sender ou_xyz "
            "(dmPolicy=allowlist) — caught from a previous report"
        ),
        subsystem="channels/feishu",
        level_id=4, level_name="WARN",
        full_path=_GATEWAY_CONSOLE_RELAY_PATH,
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.data["matched_count"] == 0


def test_more_than_cap_truncates_with_note(tmp_path):
    """>20 matched → display capped at 20, total preserved in data,
    a note check appears in the signals section."""
    lines = [
        _make_line(
            message=f"feishu[default]: blocked unauthorized sender ou_{i:03d}",
            subsystem="channels/feishu",
            level_id=3, level_name="INFO",
            # Distinct timestamps so newest-first sort is deterministic.
            ts=f"2026-06-10T13:{i:02d}:00",
        )
        for i in range(25)
    ]
    ctx = _ctx_for(tmp_path, lines)
    report = _collect(ctx)
    assert report.data["matched_count"] == 25
    sig_section = next(s for s in report.sections if s.title.startswith("1."))
    # 20 signals + 1 cap-note check at the head.
    assert sum(
        1 for c in sig_section.checks if c.name.startswith("channel.signal.")
    ) == 20
    note_checks = [
        c for c in sig_section.checks if c.name == "channel.signals.cap_note"
    ]
    assert note_checks, "cap note missing"
    note = note_checks[0]
    assert "共 25 条匹配" in note.message
    assert "20" in note.message


def test_signals_sorted_newest_first(tmp_path):
    """Signals MUST be displayed newest-first (lexicographic sort on
    ISO-8601 timestamps within a single timezone)."""
    lines = [
        _make_line(
            message=f"feishu[default]: blocked unauthorized sender ou_{tag}",
            subsystem="channels/feishu",
            level_id=3, level_name="INFO",
            ts=ts,
        )
        for tag, ts in [
            ("oldest", "2026-06-08T01:00:00"),
            ("middle", "2026-06-09T01:00:00"),
            ("newest", "2026-06-10T01:00:00"),
        ]
    ]
    ctx = _ctx_for(tmp_path, lines)
    report = _collect(ctx)
    sig_section = next(s for s in report.sections if s.title.startswith("1."))
    signal_checks = [
        c for c in sig_section.checks if c.name.startswith("channel.signal.")
    ]
    assert "ou_newest" in signal_checks[0].message
    assert "ou_middle" in signal_checks[1].message
    assert "ou_oldest" in signal_checks[2].message


def test_verdict_fail_on_error_line(tmp_path):
    """An ERROR-level channel line drives the report verdict to FAIL."""
    line = _make_line(
        message=(
            "feishu[default]: WebSocket connection ended with fatal error"
        ),
        subsystem="channels/feishu",
        level_id=5, level_name="ERROR",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.verdict == Verdict.FAIL


def test_verdict_warn_when_only_warn_signals(tmp_path):
    line = _make_line(
        message="feishu[default]: blocked unauthorized sender ou_x",
        subsystem="channels/feishu",
        level_id=3, level_name="INFO",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.verdict == Verdict.WARN


def test_verdict_ok_when_no_signals(tmp_path):
    """Empty log → OK with the no-signal explanation check."""
    ctx = _ctx_for(tmp_path, [])
    report = _collect(ctx)
    assert report.verdict == Verdict.OK
    # The head summary carries an explicit "未发现 channel ..." check.
    head = report.sections[0]
    names = [c.name for c in head.checks]
    assert "channel.no_signals" in names


def test_verdict_ok_when_only_benign_info_signals(tmp_path):
    """A benign INFO drop (skipping duplicate) is collected, but the
    overall verdict stays OK because no warn/error fired."""
    line = _make_line(
        message=(
            "feishu[default]: skipping duplicate message msg-abc"
        ),
        subsystem="channels/feishu",
        level_id=3, level_name="INFO",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.verdict == Verdict.OK
    sig_section = next(s for s in report.sections if s.title.startswith("1."))
    signal_checks = [
        c for c in sig_section.checks if c.name.startswith("channel.signal.")
    ]
    assert len(signal_checks) == 1
    assert signal_checks[0].verdict == Verdict.OK


def test_dingtalk_chinese_phrase_collected(tmp_path):
    """The literal ``群聊被拦截`` from message-handler.ts:1108 is part of
    our catalog and must surface as warn."""
    line = _make_line(
        message=(
            "[DingTalk:main] 群聊被拦截: conversationId=convX "
            "不在 groupAllowFrom 白名单中"
        ),
        subsystem="plugins/dingtalk",
        level_id=3, level_name="INFO",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.data["matched_count"] == 1
    sig_section = next(s for s in report.sections if s.title.startswith("1."))
    check = sig_section.checks[0]
    assert check.verdict == Verdict.WARN
    assert "群聊被拦截" in check.message


def test_message_prefix_fallback_when_no_subsystem(tmp_path):
    """A line without a parseable subsystem but with the
    ``feishu[<acct>]:`` prefix still classifies as channel."""
    line = _make_line(
        message=(
            "feishu[default]: blocked unauthorized sender ou_xyz "
            "(dmPolicy=allowlist)"
        ),
        subsystem=None,  # no JSON-encoded subsystem
        level_id=3, level_name="INFO",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.data["matched_count"] == 1


def test_account_filter_substring_match(tmp_path):
    """``--account`` substring filter narrows to lines whose message
    contains that token (matched against the channel-prefix portion)."""
    lines = [
        _make_line(
            message="feishu[default]: blocked unauthorized sender ou_a",
            subsystem="channels/feishu",
            level_id=3, level_name="INFO",
            ts="2026-06-10T12:00:00",
        ),
        _make_line(
            message="feishu[other]: blocked unauthorized sender ou_b",
            subsystem="channels/feishu",
            level_id=3, level_name="INFO",
            ts="2026-06-10T12:01:00",
        ),
    ]
    ctx = _ctx_for(tmp_path, lines)
    ctx.account_id = "default"
    report = _collect(ctx)
    assert report.data["matched_count"] == 1
    sig_section = next(s for s in report.sections if s.title.startswith("1."))
    [check] = [
        c for c in sig_section.checks if c.name.startswith("channel.signal.")
    ]
    assert "feishu[default]" in check.message


def test_unreadable_log_file_does_not_crash(tmp_path, monkeypatch):
    """A file that disappears or refuses to open must NEVER crash the
    collector. We monkeypatch ``open`` for the missing path; the run
    should produce an OK no-signals report."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    fake_path = log_dir / "openclaw-9999-99-99.log"
    fake_path.write_text("")
    # Make the file appear to exist but refuse to open.
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == str(fake_path):
            raise OSError("permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    ctx = DiagContext(
        openclaw_home=tmp_path,
        config_path=tmp_path / "openclaw.json",
        log_dir=log_dir,
        sessions_base=tmp_path / "agents",
    )
    report = _collect(ctx)
    # Did not raise; produced a clean OK report.
    assert report.verdict == Verdict.OK
    assert report.data["matched_count"] == 0


def test_main_build_context_populates_account(tmp_path):
    """``main._build_context(args)`` lifts ``args.account`` into
    ``ctx.account_id``. Also confirms that the removed flags
    (``--probe`` / ``--sender``) are no longer expected on the
    Namespace — ``getattr`` defaults to None."""
    from argparse import Namespace
    from ocdiag.main import _build_context

    args = Namespace(
        config="/tmp/cfg.json",
        log_dir="/tmp/logs",
        sessions_base="/tmp/sessions",
        openclaw_home="/tmp/home",
        format=None,
        json=False,
        no_color=False,
        unmask=False,
        account="prod-account",
    )
    ctx = _build_context(args)
    assert ctx.account_id == "prod-account"

    args.account = None
    ctx = _build_context(args)
    assert ctx.account_id is None


def test_anchored_message_prefix_blocks_self_pollution(tmp_path):
    """Defense layer two: a line whose subsystem is non-channel AND
    whose body MENTIONS a channel prefix mid-sentence (rather than
    starting with one) must NOT be classified as channel.

    Without this we would pull in things like a relay line whose body
    quotes a prior diagnostic report."""
    line = _make_line(
        message=(
            "earlier the report said feishu[default]: blocked "
            "unauthorized sender ou_xyz — see attached"
        ),
        subsystem="diagnostic",
        level_id=3, level_name="INFO",
    )
    ctx = _ctx_for(tmp_path, [line])
    report = _collect(ctx)
    assert report.data["matched_count"] == 0


def test_channel_markers_lists_detected_tokens(tmp_path):
    """The head summary lists which channel tokens actually fired this
    window — useful operator sanity check."""
    lines = [
        _make_line(
            message="feishu[default]: blocked unauthorized sender ou_x",
            subsystem="channels/feishu",
            level_id=3, level_name="INFO",
        ),
        _make_line(
            message="[DingTalk:main] 群聊被拦截: groupPolicy=disabled",
            subsystem="plugins/dingtalk",
            level_id=3, level_name="INFO",
        ),
    ]
    ctx = _ctx_for(tmp_path, lines)
    report = _collect(ctx)
    markers = report.data["channel_markers"]
    assert "feishu" in markers
    assert "dingtalk" in markers
