#!/usr/bin/env python3
"""Extract OpenClaw session JSONL files into human-readable format."""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Iterator, List, Optional, TextIO, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import paths
from ocdiag.sensitive import sanitize_text


DEFAULT_BASE_DIR = paths.SESSIONS_BASE
SEPARATOR = "═" * 63


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def classify_state(filename: str) -> str:
    if filename.endswith(".jsonl"):
        return "active"
    if ".jsonl.deleted." in filename:
        return "deleted"
    if ".jsonl.reset." in filename:
        return "reset"
    if ".jsonl.bak-" in filename:
        return "backup"
    return "unknown"


def find_session_files(session_id, base_dir=DEFAULT_BASE_DIR, agent=None):
    if agent:
        agent_dirs = [os.path.join(base_dir, agent)]
    else:
        agent_dirs = sorted(glob.glob(os.path.join(base_dir, "*")))
    found = []
    for agent_dir in agent_dirs:
        sessions_dir = os.path.join(agent_dir, "sessions")
        if not os.path.isdir(sessions_dir):
            continue
        pattern = os.path.join(sessions_dir, f"{session_id}.jsonl*")
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if ".trajectory" in name:
                continue
            state = classify_state(name)
            found.append((path, state))
    return found


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
        # Also scrub any top-level text-ish fields the gateway may have set.
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
            # Non-pretty mode: emit the (possibly sanitized) JSON or fall back
            # to the original raw line if we didn't touch it.
            out.write(json.dumps(obj, ensure_ascii=False) if sanitize else raw)
        out.write("\n\n")
        written += 1
    return written


def summarize_file(path, state, out):
    write_header(out, path, state)
    counts: dict = {}
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
            counts["<non-object>"] = counts.get("<non-object>", 0) + 1
            continue
        rtype = obj.get("type", "<no-type>")
        counts[rtype] = counts.get(rtype, 0) + 1
        ts = obj.get("timestamp")
        if isinstance(ts, str):
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts
    out.write(f"Total records: {total}\n")
    if parse_errors:
        out.write(f"Parse errors: {parse_errors}\n")
    out.write("By type:\n")
    for k in sorted(counts, key=lambda k: -counts[k]):
        out.write(f"  {k}: {counts[k]}\n")
    if earliest or latest:
        out.write(f"Time range: {earliest or '?'}  →  {latest or '?'}\n")
    out.write("\n")


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


def main() -> int:
    p = argparse.ArgumentParser(
        description="Extract OpenClaw session JSONL files into human-readable format.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("session_id", help="Session UUID to extract")
    p.add_argument("-o", "--output", help="Write output to FILE instead of stdout")
    p.add_argument("-a", "--all", action="store_true",
                   help="Extract all versions found (active + deleted + reset + backup)")
    p.add_argument("--list", action="store_true", help="List found files; do not extract")
    p.add_argument("--agent", help="Limit search to specific agent directory")
    p.add_argument("--base-dir", default=DEFAULT_BASE_DIR, help="Override base directory")
    p.add_argument("--no-pretty", action="store_true", help="Output raw JSON lines")
    p.add_argument("--types", help="Filter by record type (comma-separated, e.g. 'message,toolCall')")
    p.add_argument("--summary", action="store_true",
                   help="Show record-count summary instead of full extraction")
    p.add_argument("--unmask", action="store_true",
                   help="Disable default sanitization of secret-shaped substrings "
                        "in message content (off = scrubbed)")
    args = p.parse_args()

    files = find_session_files(args.session_id, args.base_dir, args.agent)
    if not files:
        sys.stderr.write(
            f"Error: no files found for session ID '{args.session_id}' under {args.base_dir}"
            + (f" (agent={args.agent})" if args.agent else "")
            + "\n"
        )
        return 1

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
