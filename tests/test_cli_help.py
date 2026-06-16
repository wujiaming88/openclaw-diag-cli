"""Regression tests for `openclaw-diag <collector> --help`.

Before v1.8.2 the generic state-collector dispatch in ``ocdiag.main.main``
shared an ``add_help=False`` parser via ``parse_known_args`` — so ``-h`` /
``--help`` fell into the discarded "unknown args" bucket and the collector
just ran. These tests pin the fixed behavior: collector --help prints
argparse usage and exits 0, instead of executing the diagnostic.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import main as ocdiag_main  # noqa: E402


def _capture_help(argv):
    """Run main(argv) expecting an argparse SystemExit(0); return stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        with pytest.raises(SystemExit) as exc:
            ocdiag_main.main(argv)
    assert exc.value.code == 0, (
        f"expected argparse to exit 0 for {argv!r}, got {exc.value.code!r}"
    )
    return buf.getvalue()


def test_channel_help_shows_usage_not_diagnostic():
    out = _capture_help(["channel", "--help"])
    # argparse usage line carries the prog we set; a real diagnostic run
    # would print the human renderer banner instead.
    assert "usage: openclaw-diag channel" in out
    # Channel keeps its single channel-only flag.
    assert "--account" in out
    # ``--probe`` and ``--sender`` were removed in v1.9.0 when the
    # channel collector dropped config interpretation and the active
    # probe path. Pin the absence so the flags don't sneak back in.
    assert "--sender" not in out
    assert "--probe" not in out
    # Negative control: the human renderer's banner must not appear.
    assert "OPENCLAW-DIAG" not in out


def test_gateway_help_shows_usage_not_diagnostic():
    out = _capture_help(["gateway", "--help"])
    assert "usage: openclaw-diag gateway" in out
    # Common flags still present.
    assert "--format" in out
    # Channel-only flag must not leak into a non-channel collector's help.
    assert "--account" not in out
    # ``--probe`` / ``--sender`` are gone everywhere.
    assert "--sender" not in out
    assert "--probe" not in out
    assert "OPENCLAW-DIAG" not in out


def test_cron_jobs_help_shows_usage_not_diagnostic():
    out = _capture_help(["cron_jobs", "--help"])
    assert "usage: openclaw-diag cron_jobs" in out
    assert "--format" in out
    # Channel-only flag must not appear here either.
    assert "--account" not in out


def test_collector_short_h_flag_also_shows_help():
    # `-h` is registered alongside `--help` by argparse's auto help action;
    # cover it explicitly so the regression can't sneak back through `-h`.
    out = _capture_help(["channel", "-h"])
    assert "usage: openclaw-diag channel" in out


def test_channel_without_help_runs_collector():
    """Negative control: omitting --help must still execute the diagnostic.

    We only assert the call returns an int exit code (no SystemExit raised
    by argparse). This proves the help branch is gated on -h/--help and
    does not steal normal invocations.
    """
    rc = ocdiag_main.main(["channel"])
    assert isinstance(rc, int)


def test_trace_help_documents_all_messages():
    """v1.11.0 introduced --all-messages/-A; pin the flag in trace --help."""
    out = _capture_help(["trace", "--help"])
    assert "usage: openclaw-diag trace" in out
    assert "--all-messages" in out
    # Negative control: the help branch must not run the inspector.
    assert "OPENCLAW-DIAG" not in out


def test_trace_argparse_rejects_all_messages_with_msg_index(capsys):
    """v1.11.0: the argparse layer of cmd_inspector rejects
    --all-messages combined with --msg-index/--msg-id/--msg-match BEFORE
    the inspector ever runs. parser.error() emits to stderr and SystemExit(2).

    The inspector layer also enforces the same mutex (see test_trace.py)
    but a CLI user hits the argparse path first. Pin both layers.
    """
    with pytest.raises(SystemExit) as exc:
        ocdiag_main.main([
            "trace", "1234567890abcdef",
            "--all-messages", "--msg-index", "0",
        ])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--all-messages" in err
    assert "--msg-index" in err


def test_trace_argparse_rejects_all_messages_with_msg_id(capsys):
    with pytest.raises(SystemExit) as exc:
        ocdiag_main.main([
            "trace", "1234567890abcdef",
            "--all-messages", "--msg-id", "user-1",
        ])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--all-messages" in err


def test_trace_argparse_rejects_all_messages_with_msg_match(capsys):
    with pytest.raises(SystemExit) as exc:
        ocdiag_main.main([
            "trace", "1234567890abcdef",
            "--all-messages", "--msg-match", "hi",
        ])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--all-messages" in err


def test_gateway_without_help_runs_without_attribute_error():
    """Regression guard for the v1.8.3 split.

    `gateway`'s parser no longer registers --probe/--sender/--account.
    `_build_context` must read them via getattr-with-default so the
    args namespace lacking those attributes does not raise AttributeError.
    """
    try:
        rc = ocdiag_main.main(["gateway"])
    except AttributeError as e:
        pytest.fail(f"gateway run raised AttributeError: {e}")
    assert isinstance(rc, int)
