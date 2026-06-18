"""Regression tests for `openclaw-diag <collector> --help`.

Before v1.8.2 the generic state-collector dispatch in ``ocdiag.main.main``
shared an ``add_help=False`` parser via ``parse_known_args`` — so ``-h`` /
``--help`` fell into the discarded "unknown args" bucket and the collector
just ran. These tests pin the fixed behavior: collector --help prints
argparse usage and exits 0, instead of executing the diagnostic.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import main as ocdiag_main  # noqa: E402
from ocdiag.core import registry as ocdiag_registry  # noqa: E402


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


def _capture_stdout(argv):
    """Run main(argv), expect a normal int return; capture stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ocdiag_main.main(argv)
    assert isinstance(rc, int)
    return rc, buf.getvalue()


# -----------------------------------------------------------------------------
# v1.12.1 — top-level / per-command --help is fully English
# -----------------------------------------------------------------------------

def test_top_level_help_has_key_sections():
    """The top-level help must include the intro, health checks, scan
    diagnostics, object diagnostics, helper commands, global options, and
    exit codes section headers — these are the agent's / user's anchor points."""
    _, out = _capture_stdout(["--help"])
    assert "OpenClaw operations diagnostics CLI" in out
    assert "Usage:" in out
    assert "Health checks:" in out
    assert "Scan diagnostics" in out
    assert "Object diagnostics" in out
    assert "Helper commands:" in out
    assert "Global options" in out
    assert "Exit codes:" in out
    # All four exit codes must be listed.
    for code in ("0", "1", "2", "3"):
        assert code in out


def test_top_level_help_lists_every_state_collector_id():
    """The top-level help renders the scan list dynamically from the registry
    and must cover every registered state collector id — guards against a
    newly added collector being silently dropped from --help."""
    ocdiag_registry.discover()
    _, out = _capture_stdout(["--help"])
    state_ids = [c.id for c in ocdiag_registry.all_state()]
    # At least 10 state collectors expected, so an empty registry can't pass.
    assert len(state_ids) >= 10, f"unexpectedly few state collectors: {state_ids}"
    for sid in state_ids:
        assert sid in out, f"collector id {sid!r} missing from top-level --help"


def test_top_level_help_lists_inspectors():
    """Object diagnostics section must list the trace/extract/panorama trio."""
    _, out = _capture_stdout(["--help"])
    for iid in ("trace", "extract", "panorama"):
        assert iid in out, f"inspector {iid!r} missing from top-level --help"


def test_trace_help_documents_previously_bare_flags():
    """trace's previously-bare flags must each carry an English help string;
    the per-command group header and the description one-liner must show too."""
    out = _capture_help(["trace", "--help"])
    # Per-command group header
    assert "trace options" in out
    # The 5 flags that previously had no help text — assert each English snippet
    assert "Do not read trajectory.jsonl" in out          # --no-trajectory
    assert "Do not correlate openclaw application logs" in out  # --no-log
    assert "Show full meta for each toolCall" in out      # --show-tool-metas
    assert "Show plugin snapshot" in out                  # --show-plugin-snapshot
    assert "Force masking" in out                         # --mask
    # description one-liner must appear
    assert "Trace one user message's full lifecycle" in out


def test_collector_help_shows_global_options_with_descriptions():
    """A collector's --help must show the "global options" group, and every
    flag (--config / --unmask / ...) must carry an English help line."""
    out = _capture_help(["gateway", "--help"])
    assert "global options" in out
    assert "Path to openclaw.json" in out                 # --config
    assert "Do not mask" in out                           # --unmask
    assert "Disable ANSI color" in out                    # --no-color
    assert "OpenClaw log directory" in out                # --log-dir
    assert "Sessions root directory" in out               # --sessions-base
    assert "OpenClaw home directory" in out               # --openclaw-home


def test_collector_help_description_includes_one_liner():
    """gateway --help description must surface the one-liner from
    _COMMAND_DESC, no Chinese title leaked through."""
    out = _capture_help(["gateway", "--help"])
    # description line looks like: "Analyze Gateway process lifecycle ... (gateway)"
    assert "(gateway)" in out
    assert "Gateway process lifecycle" in out


def test_list_pretty_includes_command_descriptions():
    """`openclaw-diag list` pretty output must include the description text
    after each id (one scan + one inspector sample)."""
    rc, out = _capture_stdout(["list"])
    assert rc == 0
    assert "Gateway process lifecycle" in out                            # gateway
    assert "Trace one user message's full lifecycle" in out              # trace


def test_extract_help_documents_options_and_groups():
    """extract --help must carry description / per-command group / English
    help line for every flag."""
    out = _capture_help(["extract", "--help"])
    assert "usage: openclaw-diag extract" in out
    assert "extract options" in out
    assert "readable form" in out                                    # description fragment
    # Each flag's help text
    assert "Per-file record-count summary" in out                    # --summary
    assert "Export all versions" in out                              # --all
    assert "List matching files only" in out                         # --list
    assert "Filter by record type" in out                            # --types
    # global options group + --config help must be present
    assert "global options" in out
    assert "Path to openclaw.json" in out


def test_panorama_help_documents_options_and_groups():
    """panorama --help must carry the same level of detail as extract."""
    out = _capture_help(["panorama", "--help"])
    assert "usage: openclaw-diag panorama" in out
    assert "panorama options" in out
    assert "360° diagnosis" in out                                       # description fragment
    assert "Sanitize tool args" in out                                   # --mask
    assert "Pick the Nth run" in out                                     # --run-index
    assert "Include every run in the session" in out                     # --all-runs
    assert "Match only on sessionId / runIds" in out                     # --strict-correlation
    assert "global options" in out


def test_examples_output_is_english():
    """`openclaw-diag examples` is fully English in v1.12.1; the header and
    every comment line is translated. Example commands themselves stay
    unchanged because we only flipped human-facing text."""
    rc, out = _capture_stdout(["examples"])
    assert rc == 0
    assert "openclaw-diag — common scenarios" in out
    # A few representative comment translations
    assert "# Full health check" in out
    assert "# Trace one message's full lifecycle" in out
    assert "# Quick verdict via jq" in out


def test_list_json_each_entry_has_description_field():
    """`openclaw-diag list --format json` must include a description on every
    entry so agents/scripts can render the catalog without hardcoding names."""
    rc, out = _capture_stdout(["list", "--format", "json"])
    assert rc == 0
    payload = json.loads(out)
    assert "state_collectors" in payload
    assert "object_inspectors" in payload
    for entry in payload["state_collectors"]:
        assert "description" in entry
        # Well-known ids must carry a non-empty description.
        if entry["id"] in ("doctor", "gateway", "channel", "trace"):
            assert entry["description"], (
                f"description must not be empty for known id {entry['id']!r}"
            )
    for entry in payload["object_inspectors"]:
        assert "description" in entry
        assert entry["description"], (
            f"inspector {entry['id']!r} description should not be empty"
        )


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
