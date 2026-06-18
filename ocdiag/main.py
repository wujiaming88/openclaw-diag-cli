"""v2 CLI entry point — the default path since v1.0.0.

Routes to registered collectors / inspectors via @register decorators.
The ``--legacy`` flag (or ``OCDIAG_LEGACY=1``) on the launcher falls back
to the pre-v2 dispatcher in ``_legacy/``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional

from . import __version__, paths
from .core import registry
from .core.context import DiagContext
from .core.errors import (
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    EXIT_WARN_OR_FAIL,
    DiagError,
    exit_code_for,
)
from .core.types import Report, Verdict
from .render.human import HumanRenderer
from .render.json_renderer import JsonRenderer
from .render.ndjson import NdjsonRenderer


_FORMAT_CHOICES = ("pretty", "json", "ndjson")


# Centralized command description table (id -> one-line English summary).
# Reused by `list`, `<id> --help`, and the top-level help. When adding a new
# collector/inspector, add a row here; missing ids fall back to "" via _desc().
_COMMAND_DESC = {
    # Scan-type collectors (state)
    "channel":        "Scan channel (Feishu/Telegram/etc.) connect + message logs; flag disconnects, expired-discard, auth failures",
    "configuration":  "Parse key openclaw.json settings (agents/models/plugins/channels) and flag missing or risky values",
    "cron_jobs":      "List every cron job's full config (schedule/payload/sessionTarget/delivery/enabled); detect no-fire / delivery failures",
    "doctor":         "Check Node / Python / openclaw-diag / OpenClaw install and versions are ready",
    "environment":    "Collect host environment, Gateway process, and OpenClaw version basics",
    "gateway":        "Analyze Gateway process lifecycle logs (start/restart/WS connect/crash)",
    "performance":    "Measure model-call latency (E2E/TTFT), tool durations, and availability",
    "plugin_diag":    "Check plugin load status, hook subscriptions, trust gate, and plugin errors",
    "recent_errors":  "Extract and categorize recent errors/exceptions from logs",
    "run_health":     "Assess agent run completion rate, stalls, and interruptions",
    "sessions_diag":  "Scan session.jsonl; count toolCall/toolResult pairing, orphans, anomalies",
    "shell_history":  "Summarize shell commands the agent has executed",
    "sys_health":     "Check system-level health: CPU/memory/disk/OOM/processes",
    "task_health":    "Assess background task (openclaw tasks) status and health",
    # Object-type inspectors
    "extract":        "Export a session file to readable form (incl. active/reset/deleted/backup versions)",
    "panorama":       "360° diagnosis: correlate trajectory + app logs + subtasks + cron",
    "trace":          "Trace one user message's full lifecycle (prompt->toolCall->toolResult->reply)",
}


def _desc(mid: str) -> str:
    """Return the one-line English description for a command id, or ""."""
    return _COMMAND_DESC.get(mid, "")


def _paged_print(text: str) -> None:
    """Print text through a pager when stdout is a TTY and output is long.

    Uses $PAGER or falls back to 'less -R' (preserves ANSI colors).
    If pager is unavailable or stdout is not a TTY, prints directly.
    """
    import shutil as _shutil
    import subprocess as _sp

    if not sys.stdout.isatty():
        sys.stdout.write(text)
        sys.stdout.flush()
        return

    term_lines = _shutil.get_terminal_size().lines
    text_lines = text.count("\n")
    if text_lines <= term_lines - 2:
        sys.stdout.write(text)
        sys.stdout.flush()
        return

    pager_cmd = os.environ.get("PAGER", "less -R")
    try:
        proc = _sp.Popen(
            pager_cmd, shell=True, stdin=_sp.PIPE,
            encoding="utf-8", errors="replace",
        )
        proc.communicate(input=text)
    except (OSError, BrokenPipeError):
        sys.stdout.write(text)
        sys.stdout.flush()


def _resolve_format(args) -> str:
    """Resolve effective output format from --format / --json flags.

    --format X      → X
    --json          → json (backward compat)
    neither         → pretty
    """
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "pretty"


def _build_context(args) -> DiagContext:
    fmt = _resolve_format(args)
    return DiagContext(
        openclaw_home=Path(args.openclaw_home),
        config_path=Path(args.config),
        log_dir=Path(args.log_dir),
        sessions_base=Path(args.sessions_base),
        unmask=getattr(args, "unmask", False),
        no_color=getattr(args, "no_color", False),
        json_mode=fmt != "pretty",
        account_id=getattr(args, "account", None) or None,
    )


def _common_arguments(p: argparse.ArgumentParser) -> None:
    """Register the global options shared by every subcommand, in a dedicated
    argument group so --help output stays aligned.
    """
    g = p.add_argument_group("global options")
    g.add_argument(
        "--config", metavar="PATH", default=paths.CONFIG,
        help="Path to openclaw.json (default: ~/.openclaw/openclaw.json)",
    )
    g.add_argument(
        "--log-dir", metavar="PATH", default=paths.LOG_DIR,
        help="OpenClaw log directory (default: /tmp/openclaw)",
    )
    g.add_argument(
        "--sessions-base", metavar="PATH", default=paths.SESSIONS_BASE,
        help="Sessions root directory (default: ~/.openclaw/agents)",
    )
    g.add_argument(
        "--openclaw-home", metavar="PATH", default=paths.OPENCLAW_HOME,
        help="OpenClaw home directory (default: ~/.openclaw)",
    )
    g.add_argument(
        "--format",
        choices=list(_FORMAT_CHOICES),
        default=None,
        help="Output format pretty|json|ndjson (default: pretty; json/ndjson suit agents/scripts)",
    )
    g.add_argument(
        "--json", action="store_true",
        help="Alias for --format json",
    )
    g.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color (use when writing to a file or pipe)",
    )
    g.add_argument(
        "--unmask", action="store_true",
        help="Do not mask; show raw sensitive content (tokens/message bodies). Masked by default",
    )


def _channel_arguments(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("channel options")
    g.add_argument(
        "--account", default=None,
        help="Filter channel signals by account substring "
             "(matched against the channel-prefix portion of the message body, "
             "e.g. --account default keeps only feishu[default]: lines). Default: no filter",
    )


def _render(report: Report, args) -> None:
    fmt = _resolve_format(args)
    if fmt == "json":
        JsonRenderer().write(report)
    elif fmt == "ndjson":
        NdjsonRenderer().write(report)
    else:
        text = HumanRenderer(no_color=getattr(args, "no_color", False)).render(report)
        _paged_print(text + "\n")


def _exit_code(report: Report) -> int:
    """Map a Report to a process exit code.

    0 — OK (verdict ok)
    1 — verdict warn or fail (no structured error)
    2 — input error (DiagError with input-class code)
    3 — runtime error (DiagError with runtime-class code, or unstructured error)
    """
    if report.diag_error is not None:
        return exit_code_for(report.diag_error)
    if report.error:
        return EXIT_RUNTIME_ERROR
    if report.verdict == Verdict.OK:
        return EXIT_OK
    return EXIT_WARN_OR_FAIL


def cmd_list(args) -> int:
    state = registry.all_state()
    inspectors = registry.all_inspectors()
    fmt = _resolve_format(args)
    if fmt != "pretty":
        payload = {
            "state_collectors": [
                {"id": c.id, "label": _desc(c.id), "description": _desc(c.id)}
                for c in state
            ],
            "object_inspectors": [
                {"id": c.id, "label": _desc(c.id), "description": _desc(c.id)}
                for c in inspectors
            ],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    print("openclaw-diag — available diagnostics (v2)")
    print()
    print("  Scan type (no args required):")
    for c in state:
        d = _desc(c.id)
        print(f"    {c.id:<16s} {d}")
    print()
    if inspectors:
        print("  Object type (require session uuid):")
        for c in inspectors:
            d = _desc(c.id)
            print(f"    {c.id:<16s} {d}")
        print()
    print("  Other commands:")
    print("    all              Run all scan-type diagnostics at once")
    print("    doctor           Check Node / Python / openclaw-diag / OpenClaw environment")
    print("    examples         Print common usage examples")
    return 0


def cmd_examples() -> int:
    print("""openclaw-diag — common scenarios

  # Full health check
  openclaw-diag all

  # JSON output (for agents / scripts)
  openclaw-diag all --format json

  # Check Gateway status
  openclaw-diag gateway

  # Trace one message's full lifecycle
  openclaw-diag trace <uuid>
  openclaw-diag trace abc12345 --msg-index 0

  # Export session conversation content
  openclaw-diag extract <uuid>
  openclaw-diag extract abc12345 --summary

  # Session panorama diagnosis (correlates trajectory + logs + subtasks + cron)
  openclaw-diag panorama <uuid>
  openclaw-diag panorama abc12345 --strict-correlation --format json

  # Model performance
  openclaw-diag performance

  # Cron job status
  openclaw-diag cron_jobs

  # Quick verdict via jq
  openclaw-diag all --format json | jq '.data.verdict'
""")
    return 0


def cmd_doctor(args, node_version: Optional[str] = None) -> int:
    """Run the v2 doctor collector.

    The ``--node-version`` value forwarded by the Node launcher is stashed in
    an env var the collector reads, so we keep DiagContext free of CLI noise.
    """
    if node_version:
        os.environ["OCDIAG_NODE_VERSION"] = node_version
    return cmd_run_collector(args, "doctor")


def cmd_all(args, skip_ids: List[str]) -> int:
    ctx = _build_context(args)
    state = [c for c in registry.all_state() if c.id not in skip_ids]
    rc_overall = 0
    fmt = _resolve_format(args)
    if fmt != "pretty":
        for c in state:
            t0 = time.time()
            try:
                report = c.collect(ctx)
            except BaseException as e:  # noqa: BLE001
                report = Report(module_id=c.id, title=c.title)
                report.error = f"{type(e).__name__}: {e}"
                report.diag_error = DiagError(
                    code="RUNTIME_ERROR",
                    message=f"{type(e).__name__}: {e}",
                )
                report.elapsed_ms = (time.time() - t0) * 1000
                rc_overall = EXIT_RUNTIME_ERROR
                traceback.print_exc(file=sys.stderr)
            _render(report, args)
            rc = _exit_code(report)
            if rc != 0 and rc > rc_overall:
                rc_overall = rc
        return rc_overall

    total = len(state)
    n = 0
    renderer = HumanRenderer(no_color=getattr(args, "no_color", False))
    buffered_output: List[str] = []
    for c in state:
        n += 1
        print(
            f"\n[{n}/{total}] {c.title} ({c.id})...",
            flush=True, file=sys.stderr,
        )
        t0 = time.time()
        try:
            report = c.collect(ctx)
        except BaseException as e:  # noqa: BLE001
            report = Report(module_id=c.id, title=c.title)
            report.error = f"{type(e).__name__}: {e}"
            report.diag_error = DiagError(
                code="RUNTIME_ERROR",
                message=f"{type(e).__name__}: {e}",
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            traceback.print_exc(file=sys.stderr)
            rc_overall = EXIT_RUNTIME_ERROR
        buffered_output.append(renderer.render(report))
        elapsed = report.elapsed_ms / 1000.0
        print(
            f"[{n}/{total}] {c.title} ({c.id}) ... done ({elapsed:.1f}s)",
            flush=True, file=sys.stderr,
        )
        rc = _exit_code(report)
        if rc != 0 and rc > rc_overall:
            rc_overall = rc

    # Output all buffered content, using pager if needed
    full_output = "\n".join(buffered_output) + "\n"
    _paged_print(full_output)
    return rc_overall


def cmd_run_collector(args, mid: str) -> int:
    c = registry.get(mid)
    if c is None:
        print(f"Error: unknown collector '{mid}'", file=sys.stderr)
        return EXIT_INPUT_ERROR
    ctx = _build_context(args)
    t0 = time.time()
    try:
        report = c.collect(ctx)
    except BaseException as e:  # noqa: BLE001
        report = Report(module_id=c.id, title=c.title)
        report.error = f"{type(e).__name__}: {e}"
        report.diag_error = DiagError(
            code="RUNTIME_ERROR",
            message=f"{type(e).__name__}: {e}",
        )
        report.elapsed_ms = (time.time() - t0) * 1000
        traceback.print_exc(file=sys.stderr)
    _render(report, args)
    return _exit_code(report)


_TRACE_EPILOG = """Examples:
  openclaw-diag trace 7e9f3b31                    # last user message in the session
  openclaw-diag trace 7e9f3b31 --msg-index 0      # the first one
  openclaw-diag trace 7e9f3b31 --msg-match deploy # match by content
  openclaw-diag trace 7e9f3b31 --all-messages     # trace every user message in one run (one block per turn)
  openclaw-diag trace 7e9f3b31 -A --format json   # all user messages, JSON output
"""

_EXTRACT_EPILOG = """Examples:
  openclaw-diag extract 7e9f3b31              # export the active file by default
  openclaw-diag extract 7e9f3b31 --summary    # stats only
  openclaw-diag extract 7e9f3b31 --all        # include reset / deleted / backup
  openclaw-diag extract 7e9f3b31 --format json
"""

_PANORAMA_EPILOG = """Examples:
  openclaw-diag panorama 7e9f3b31                       # latest run
  openclaw-diag panorama 7e9f3b31 --all-runs            # every run
  openclaw-diag panorama 7e9f3b31 --run-index 0         # first run
  openclaw-diag panorama 7e9f3b31 --strict-correlation  # only sessionId / runIds
  openclaw-diag panorama 7e9f3b31 --format json --mask
"""


def _build_trace_parser() -> argparse.ArgumentParser:
    desc_text = _desc("trace")
    description = f"{desc_text} (trace)" if desc_text else "(trace)"
    p = argparse.ArgumentParser(
        prog="openclaw-diag trace",
        description=description,
        add_help=True,
        epilog=_TRACE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_argument_group("trace options")
    g.add_argument(
        "session_id",
        help="Target session UUID (full or 8+ char prefix)",
    )
    g.add_argument(
        "--msg-index", type=int, default=None,
        help="Nth user message (0-based)",
    )
    g.add_argument(
        "--msg-id", default=None,
        help="User message by id field",
    )
    g.add_argument(
        "--msg-match", default=None,
        help="First user message containing TEXT",
    )
    g.add_argument(
        "-A", "--all-messages", action="store_true", dest="all_messages",
        help="Trace every user message in the session "
             "(mutually exclusive with --msg-index/--msg-id/--msg-match)",
    )
    g.add_argument(
        "--no-trajectory", action="store_true",
        help="Do not read trajectory.jsonl; analyze from session.jsonl only",
    )
    g.add_argument(
        "--no-log", action="store_true",
        help="Do not correlate openclaw application logs",
    )
    g.add_argument(
        "--show-tool-metas", action="store_true",
        help="Show full meta for each toolCall",
    )
    g.add_argument(
        "--show-plugin-snapshot", action="store_true",
        help="Show plugin snapshot (hooks/status)",
    )
    g.add_argument(
        "--mask", action="store_true",
        help="Force masking (trace does not mask by default)",
    )
    g.add_argument(
        "--agent", default=None,
        help="Limit to a specific agent",
    )
    _common_arguments(p)
    return p


def _build_extract_parser() -> argparse.ArgumentParser:
    desc_text = _desc("extract")
    description = f"{desc_text} (extract)" if desc_text else "(extract)"
    p = argparse.ArgumentParser(
        prog="openclaw-diag extract",
        description=description,
        add_help=True,
        epilog=_EXTRACT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_argument_group("extract options")
    g.add_argument(
        "session_id",
        help="Target session UUID (full or 8+ char prefix)",
    )
    g.add_argument(
        "--summary", action="store_true",
        help="Per-file record-count summary; do not dump record bodies",
    )
    g.add_argument(
        "-a", "--all", action="store_true", dest="all_versions",
        help="Export all versions (active + reset + deleted + backup)",
    )
    g.add_argument(
        "--list", action="store_true", dest="list_only",
        help="List matching files only; do not extract content",
    )
    g.add_argument(
        "--types", default=None,
        help="Filter by record type (comma-separated, e.g. user,assistant,toolCall)",
    )
    g.add_argument(
        "--agent", default=None,
        help="Limit to a specific agent",
    )
    _common_arguments(p)
    return p


def _build_panorama_parser() -> argparse.ArgumentParser:
    desc_text = _desc("panorama")
    description = f"{desc_text} (panorama)" if desc_text else "(panorama)"
    p = argparse.ArgumentParser(
        prog="openclaw-diag panorama",
        description=description,
        add_help=True,
        epilog=_PANORAMA_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_argument_group("panorama options")
    g.add_argument(
        "session_id",
        help="Target session UUID (full or 8+ char prefix)",
    )
    g.add_argument(
        "--mask", action="store_true",
        help="Sanitize tool args / message content / api keys",
    )
    g.add_argument(
        "--run-index", type=int, default=None,
        help="Pick the Nth run (default: -1 = latest)",
    )
    g.add_argument(
        "--all-runs", action="store_true",
        help="Include every run in the session",
    )
    g.add_argument(
        "--strict-correlation", action="store_true",
        help="Match only on sessionId / runIds (drop sessionKey and toolCallId hits)",
    )
    g.add_argument(
        "--agent", default=None,
        help="Limit to a specific agent",
    )
    _common_arguments(p)
    return p


def cmd_inspector(head: str, rest: List[str]) -> int:
    inspector = registry.get(head)
    if inspector is None or inspector.kind != "inspector":
        print(f"Error: unknown inspector '{head}'", file=sys.stderr)
        return EXIT_INPUT_ERROR
    if head == "trace":
        parser = _build_trace_parser()
        ns = parser.parse_args(rest)
        # --all-messages traces every user turn; it is mutually exclusive with
        # the single-turn selectors. Reject the combination at parse time so
        # the inspector never sees an ambiguous request.
        if ns.all_messages and (
            ns.msg_index is not None
            or ns.msg_id is not None
            or ns.msg_match is not None
        ):
            parser.error(
                "--all-messages/-A cannot be combined with "
                "--msg-index/--msg-id/--msg-match",
            )
        kwargs = {
            "session_id": ns.session_id,
            "msg_index": ns.msg_index,
            "msg_id": ns.msg_id,
            "msg_match": ns.msg_match,
            "all_messages": ns.all_messages,
            "no_trajectory": ns.no_trajectory,
            "no_log": ns.no_log,
            "show_tool_metas": ns.show_tool_metas,
            "show_plugin_snapshot": ns.show_plugin_snapshot,
            "mask": ns.mask,
            "agent": ns.agent,
        }
    elif head == "extract":
        parser = _build_extract_parser()
        ns = parser.parse_args(rest)
        kwargs = {
            "session_id": ns.session_id,
            "summary": ns.summary,
            "all_versions": ns.all_versions,
            "list_only": ns.list_only,
            "types_filter": ns.types,
            "agent": ns.agent,
            "unmask": ns.unmask,
        }
    elif head == "panorama":
        parser = _build_panorama_parser()
        ns = parser.parse_args(rest)
        kwargs = {
            "session_id": ns.session_id,
            "mask": ns.mask,
            "unmask": ns.unmask,
            "run_index": ns.run_index,
            "all_runs": ns.all_runs,
            "strict_correlation": ns.strict_correlation,
            "agent": ns.agent,
        }
    else:
        print(f"Error: inspector '{head}' has no argument schema", file=sys.stderr)
        return EXIT_INPUT_ERROR

    ctx = _build_context(ns)
    t0 = time.time()
    try:
        report = inspector.collect(ctx, **kwargs)
    except BaseException as e:  # noqa: BLE001
        report = Report(module_id=inspector.id, title=inspector.title)
        report.error = f"{type(e).__name__}: {e}"
        report.diag_error = DiagError(
            code="RUNTIME_ERROR",
            message=f"{type(e).__name__}: {e}",
        )
        report.elapsed_ms = (time.time() - t0) * 1000
        traceback.print_exc(file=sys.stderr)
    _render(report, ns)

    # Extract: dump records to stdout after summary in pretty mode
    # (json/ndjson modes already include the records under report.data).
    if (
        head == "extract"
        and not report.error
        and _resolve_format(ns) == "pretty"
    ):
        _dump_extract_records(report, ctx)

    return _exit_code(report)


def _dump_extract_records(report: Report, ctx: DiagContext) -> None:
    """Dump session records to stdout after the Report summary.

    This restores the legacy extract behavior: the primary output is the
    session content itself, not just the summary stats.
    """
    import json as _json
    files_payload = report.data.get("files_payload", [])
    buf: List[str] = []
    for entry in files_payload:
        records = entry.get("records")
        if not records:
            continue
        path = entry.get("path", "")
        state = entry.get("state", "")
        sep = "─" * 76
        buf.append(f"\n{sep}\n")
        buf.append(f"  Records: {os.path.basename(path)} [{state}]\n")
        buf.append(f"{sep}\n\n")
        for rec in records:
            buf.append(_json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
            buf.append("\n")
    if buf:
        _paged_print("".join(buf))


def _split_skip(rest: List[str]):
    skip_ids: List[str] = []
    passthrough: List[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--skip" and i + 1 < len(rest):
            skip_ids.extend(
                s.strip() for s in rest[i + 1].split(",") if s.strip()
            )
            i += 2
            continue
        passthrough.append(a)
        i += 1
    return skip_ids, passthrough


def _print_help() -> None:
    """Render the top-level --help.

    Structure: intro -> usage -> health checks -> scan diagnostics (rendered
    dynamically from the registry) -> object diagnostics -> helper commands
    -> global options -> exit codes -> more. Scan-list ids/descriptions are
    pulled from registry + _COMMAND_DESC so newly registered collectors are
    never missed.
    """
    state_ids = [c.id for c in registry.all_state()]
    width = 16
    lines: List[str] = []
    lines.append(f"openclaw-diag v{__version__} — OpenClaw operations diagnostics CLI")
    lines.append("")
    lines.append("Scans config / logs / sessions to pinpoint connection, performance, cron,")
    lines.append("plugin, and run issues in an OpenClaw deployment.")
    lines.append("")
    lines.append("Usage:")
    lines.append("  openclaw-diag <command> [args...]")
    lines.append("  openclaw-diag <command> --help     Show detailed args for a command")
    lines.append("")
    lines.append("Health checks:")
    lines.append(f"  {'all':<{width}s}Run every scan-type diagnostic at once (recommended first step)")
    lines.append(f"  {'doctor':<{width}s}Check the runtime (Node / Python / OpenClaw readiness)")
    lines.append("")
    lines.append("Scan diagnostics (no args required):")
    for sid in state_ids:
        if sid == "doctor":
            # doctor is already listed under Health checks; skip the duplicate.
            continue
        lines.append(f"  {sid:<{width}s}{_desc(sid)}")
    lines.append("")
    lines.append("Object diagnostics (require a session uuid):")
    lines.append(f"  {'trace <uuid>':<{width}s}{_desc('trace')}")
    lines.append(f"  {'extract <uuid>':<{width}s}{_desc('extract')}")
    lines.append(f"  {'panorama <uuid>':<{width}s}{_desc('panorama')}")
    lines.append("")
    lines.append("Helper commands:")
    lines.append(f"  {'list':<{width}s}List all available diagnostics (supports --format json)")
    lines.append(f"  {'examples':<{width}s}Print common usage examples")
    lines.append("")
    lines.append("Global options (all commands):")
    lines.append("  --format pretty|json|ndjson   Output format (default: pretty)")
    lines.append("  --json                        Alias for --format json")
    lines.append("  --no-color                    Disable colored output")
    lines.append("  --unmask                      Show raw (unmasked) sensitive content")
    lines.append("  --config / --log-dir / --sessions-base / --openclaw-home   Override default paths")
    lines.append("")
    lines.append("Exit codes:")
    lines.append("  0  OK (no warn/fail)")
    lines.append("  1  Warn or fail present")
    lines.append("  2  Input error (bad args/uuid)")
    lines.append("  3  Runtime error")
    lines.append("")
    lines.append("More: `openclaw-diag list` for all diagnostics, `openclaw-diag <command> --help` for details.")
    print("\n".join(lines))


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    registry.discover()

    if not argv or argv[0] in ("-h", "--help"):
        _print_help()
        return 0

    if argv[0] in ("--version", "-V", "-v", "version"):
        print(__version__)
        return 0

    head, rest = argv[0], argv[1:]

    parser = argparse.ArgumentParser(add_help=False)
    _common_arguments(parser)

    if head == "list":
        # --json / --format matter for `list`
        args, _ = parser.parse_known_args(rest)
        return cmd_list(args)

    if head == "examples":
        return cmd_examples()

    if head == "doctor":
        # Pull --node-version out before argparse so the v2 doctor collector
        # can read it from the env without a custom DiagContext field.
        node_version: Optional[str] = None
        passthrough: List[str] = []
        i = 0
        while i < len(rest):
            if rest[i] == "--node-version" and i + 1 < len(rest):
                node_version = rest[i + 1]
                i += 2
                continue
            passthrough.append(rest[i])
            i += 1
        args, _ = parser.parse_known_args(passthrough)
        return cmd_doctor(args, node_version=node_version)

    if head == "all":
        skip_ids, passthrough = _split_skip(rest)
        args, _ = parser.parse_known_args(passthrough)
        return cmd_all(args, skip_ids)

    if head in ("trace", "extract", "panorama"):
        return cmd_inspector(head, rest)

    coll = registry.get(head)
    if coll is not None:
        if coll.kind == "inspector":
            return cmd_inspector(head, rest)
        # Build a per-collector parser with add_help=True so `<id> --help`
        # prints usage + flag docs and exits, instead of being swallowed by
        # parse_known_args (which would otherwise execute the diagnostic).
        # parse_known_args is preserved to keep the existing lenient handling
        # of unrecognized flags from external callers.
        d = _desc(coll.id)
        desc = f"{d} ({coll.id})" if d else f"({coll.id})"
        cparser = argparse.ArgumentParser(
            prog=f"openclaw-diag {head}",
            description=desc,
            add_help=True,
        )
        _common_arguments(cparser)
        if head == "channel":
            _channel_arguments(cparser)
        args, _ = cparser.parse_known_args(rest)
        return cmd_run_collector(args, head)

    print(f"Error: unknown command '{head}'", file=sys.stderr)
    print("Run `openclaw-diag list` to see all diagnostics.", file=sys.stderr)
    return EXIT_INPUT_ERROR


if __name__ == "__main__":
    sys.exit(main())
