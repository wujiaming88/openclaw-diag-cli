#!/usr/bin/env python3
"""Extract OpenClaw session JSONL files into human-readable format."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import paths, sessions
from ocdiag.sensitive import sanitize_text


DEFAULT_BASE_DIR = paths.SESSIONS_BASE
SEPARATOR = "═" * 63


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def stream_records(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, start=1):
            stripped = line.rstrip("\n")
            if not stripped.strip():
                continue
            try:
                obj = json.loads(stripped)
                yield i, obj, stripped, None
            except json.JSONDecodeError as e:
                yield i, None, stripped, str(e)


def write_header(out, path, state):
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    out.write(SEPARATOR + "\n")
    out.write(f"File: {path}\n")
    out.write(f"Size: {human_size(size)}\n")
    out.write(f"State: {state}\n")
    out.write(SEPARATOR + "\n\n")


def _sanitize_record(obj):
    """Walk a session record and scrub free-form text content fields.

    Sessions store user/assistant messages under ``message.content``. We don't
    rewrite tool args or metadata: those keep structure that matters for
    diagnosis. We only scrub free-form prose where secrets typically live
    (user-pasted tokens, error tracebacks).
    """
    if not isinstance(obj, dict):
        return obj
    msg = obj.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = sanitize_text(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    for k in ("text", "content"):
                        v = part.get(k)
                        if isinstance(v, str):
                            part[k] = sanitize_text(v)
        for k in ("text", "summary"):
            v = msg.get(k)
            if isinstance(v, str):
                msg[k] = sanitize_text(v)
    return obj


def extract_file(path, state, out, pretty=True, type_filter=None, sanitize=True):
    write_header(out, path, state)
    written = 0
    for line_no, obj, raw, err in stream_records(path):
        if err is not None:
            out.write(f"--- Record {line_no} [PARSE ERROR: {err}] ---\n")
            out.write((sanitize_text(raw) if sanitize else raw) + "\n\n")
            written += 1
            continue
        rtype = obj.get("type", "?") if isinstance(obj, dict) else "?"
        if type_filter is not None and rtype not in type_filter:
            continue
        out.write(f"--- Record {line_no} [type: {rtype}] ---\n")
        if sanitize:
            obj = _sanitize_record(obj)
        if pretty:
            out.write(json.dumps(obj, indent=2, ensure_ascii=False))
        else:
            out.write(json.dumps(obj, ensure_ascii=False) if sanitize else raw)
        out.write("\n\n")
        written += 1
    return written


def summarize_file(path, state, out):
    write_header(out, path, state)
    info = _collect_summary(path, sanitize=False)
    out.write(f"Total records: {info['total_records']}\n")
    if info["parse_errors"]:
        out.write(f"Parse errors: {info['parse_errors']}\n")
    out.write("By type:\n")
    by_type = info["by_type"]
    for k in sorted(by_type, key=lambda k: -by_type[k]):
        out.write(f"  {k}: {by_type[k]}\n")
    tr = info["time_range"]
    if tr["start"] or tr["end"]:
        out.write(f"Time range: {tr['start'] or '?'}  →  {tr['end'] or '?'}\n")
    out.write("\n")


def _collect_summary(path: str, sanitize: bool = True) -> Dict[str, Any]:
    """Walk one file and produce a summary block (used by text + JSON mode)."""
    by_type: Dict[str, int] = {}
    total = 0
    earliest: Optional[str] = None
    latest: Optional[str] = None
    parse_errors = 0
    for _, obj, _, err in stream_records(path):
        total += 1
        if err is not None:
            parse_errors += 1
            continue
        if not isinstance(obj, dict):
            by_type["<non-object>"] = by_type.get("<non-object>", 0) + 1
            continue
        rtype = obj.get("type", "<no-type>")
        by_type[rtype] = by_type.get(rtype, 0) + 1
        ts = obj.get("timestamp")
        if isinstance(ts, str):
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts
    return {
        "total_records": total,
        "parse_errors": parse_errors,
        "by_type": by_type,
        "time_range": {"start": earliest, "end": latest},
    }


def _collect_records(path: str, type_filter, sanitize: bool) -> List[Dict]:
    out: List[Dict] = []
    for line_no, obj, raw, err in stream_records(path):
        if err is not None:
            out.append({"line": line_no, "parse_error": err, "raw": raw})
            continue
        if not isinstance(obj, dict):
            out.append({"line": line_no, "value": obj})
            continue
        rtype = obj.get("type", "?")
        if type_filter is not None and rtype not in type_filter:
            continue
        if sanitize:
            obj = _sanitize_record(obj)
        out.append(obj)
    return out


def list_files(files, out):
    out.write(f"Found {len(files)} file(s):\n\n")
    for i, (path, state) in enumerate(files, start=1):
        try:
            size_s = human_size(os.path.getsize(path))
        except OSError:
            size_s = "?"
        out.write(f"  [{i}] {state:8s} {size_s:>10s}  {path}\n")
    out.write("\n")


def select_files(files, extract_all, _out):
    if len(files) <= 1 or extract_all:
        return files
    list_files(files, sys.stderr)
    sys.stderr.write("Multiple files found. Enter index (1-based), 'a' for all, or 'q' to quit: ")
    sys.stderr.flush()
    try:
        choice = sys.stdin.readline().strip().lower()
    except (KeyboardInterrupt, EOFError):
        return []
    if choice in ("q", ""):
        return []
    if choice == "a":
        return files
    try:
        idx = int(choice)
        if 1 <= idx <= len(files):
            return [files[idx - 1]]
    except ValueError:
        pass
    sys.stderr.write(f"Invalid choice: {choice}\n")
    return []


def _resolve_or_die(session_id: str, base_dir: str, agent: Optional[str],
                    include_transient: bool) -> List[Tuple[str, str]]:
    ok, msg = sessions.is_valid_query(session_id)
    if not ok:
        sys.stderr.write(f"Error: {msg}\n")
        sys.exit(2)
    files, candidates = sessions.resolve(
        session_id, base_dir=base_dir, agent=agent,
        include_transient=include_transient,
    )
    if candidates:
        sys.stderr.write(
            f"Error: 前缀 '{session_id}' 匹配多个 session（请补长前缀）：\n"
        )
        for sid in candidates:
            sys.stderr.write(f"    {sid}\n")
        sys.exit(1)
    if not files:
        sys.stderr.write(
            f"Error: 找不到 session '{session_id}'（在 {base_dir} 下）"
            + (f" agent={agent}" if agent else "")
            + "\n"
        )
        suggestions = sessions.recent_session_ids(base_dir, limit=5)
        if suggestions:
            sys.stderr.write("  最近的 5 个 session：\n")
            for sid in suggestions:
                sys.stderr.write(f"    {sid}\n")
            sys.stderr.write("  提示：完整 UUID 或前缀（至少 8 位）都可。\n")
        sys.exit(1)
    return files


def _emit_json(session_id: str, selected: List[Tuple[str, str]],
               out_fp: TextIO, summary_only: bool, type_filter,
               sanitize: bool) -> None:
    files_payload: List[Dict[str, Any]] = []
    aggregate_total = 0
    aggregate_by_type: Dict[str, int] = {}
    aggregate_start: Optional[str] = None
    aggregate_end: Optional[str] = None
    for path, state in selected:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        entry: Dict[str, Any] = {
            "path": path,
            "state": state,
            "size_bytes": size,
        }
        if summary_only:
            s = _collect_summary(path, sanitize=sanitize)
            entry["summary"] = s
            aggregate_total += s["total_records"]
            for k, v in s["by_type"].items():
                aggregate_by_type[k] = aggregate_by_type.get(k, 0) + v
            tr = s["time_range"]
            if tr["start"] and (aggregate_start is None or tr["start"] < aggregate_start):
                aggregate_start = tr["start"]
            if tr["end"] and (aggregate_end is None or tr["end"] > aggregate_end):
                aggregate_end = tr["end"]
        else:
            entry["records"] = _collect_records(path, type_filter, sanitize=sanitize)
        files_payload.append(entry)

    payload: Dict[str, Any] = {
        "session_id": session_id,
        "files": files_payload,
        "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sanitized": sanitize,
    }
    if summary_only:
        payload["summary"] = {
            "total_records": aggregate_total,
            "by_type": aggregate_by_type,
            "time_range": {"start": aggregate_start, "end": aggregate_end},
        }
    out_fp.write(json.dumps(payload, ensure_ascii=False, indent=2))
    out_fp.write("\n")


def main() -> int:
    p = argparse.ArgumentParser(
        prog=os.environ.get("OPENCLAW_DIAG_PROG") or None,
        description="Extract OpenClaw session JSONL files into human-readable format.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("session_id", help="Session UUID (full or 8+ char prefix)")
    p.add_argument("-o", "--output", help="Write output to FILE instead of stdout")
    p.add_argument("-a", "--all", action="store_true",
                   help="Extract all versions (active + reset + deleted + backup + lock)")
    p.add_argument("--list", action="store_true",
                   help="List all matching files (incl. .lock); do not extract")
    p.add_argument("--agent", help="Limit search to specific agent directory")
    p.add_argument("--base-dir", default=DEFAULT_BASE_DIR, help="Override base directory")
    p.add_argument("--no-pretty", action="store_true", help="Output raw JSON lines")
    p.add_argument("--types", help="Filter by record type (comma-separated, e.g. 'message,toolCall')")
    p.add_argument("--summary", action="store_true",
                   help="Show record-count summary instead of full extraction")
    p.add_argument("--json", action="store_true",
                   help="Emit structured JSON (compatible with state collectors' --json)")
    p.add_argument("--unmask", action="store_true",
                   help="Disable default sanitization of secret-shaped substrings "
                        "in message content (off = scrubbed)")
    args = p.parse_args()

    # --list and --all see lock files; default mode hides them so non-interactive
    # callers (cron, jq pipes) don't trip on a transient .jsonl.lock sibling.
    include_transient = bool(args.all or args.list)
    files = _resolve_or_die(args.session_id, args.base_dir, args.agent,
                            include_transient=include_transient)

    if args.list:
        list_files(files, sys.stdout)
        return 0

    selected = select_files(files, args.all, sys.stdout)
    if not selected:
        sys.stderr.write("No files selected.\n")
        return 1

    type_filter: Optional[set] = None
    if args.types:
        type_filter = {t.strip() for t in args.types.split(",") if t.strip()}

    out_path = args.output
    out_fp: TextIO
    close_out = False
    if out_path:
        out_fp = open(out_path, "w", encoding="utf-8")
        close_out = True
    else:
        out_fp = sys.stdout

    try:
        if args.json:
            _emit_json(args.session_id, selected, out_fp,
                       summary_only=args.summary,
                       type_filter=type_filter,
                       sanitize=not args.unmask)
        else:
            for path, state in selected:
                if args.summary:
                    summarize_file(path, state, out_fp)
                else:
                    extract_file(path, state, out_fp, pretty=not args.no_pretty,
                                 type_filter=type_filter, sanitize=not args.unmask)
    except BrokenPipeError:
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            pass
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130
    finally:
        if close_out:
            out_fp.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
