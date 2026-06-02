"""Pure helpers for session extract.

Extracted from ``tools/oc_session_extract.py`` so the v2 inspector
(``ocdiag.inspectors.extract``) can reuse them. The legacy CLI script keeps
its own argv parsing + chrome and re-imports these helpers.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import sessions
from .sensitive import sanitize_text


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


def _sanitize_record(obj):
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


def collect_summary(path: str, sanitize: bool = True) -> Dict[str, Any]:
    """Walk one file and produce summary stats."""
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


def collect_records(path: str, type_filter, sanitize: bool) -> List[Dict]:
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


def system_prompt_for(path: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Compact summary of the active session's systemPromptReport."""
    report = sessions.lookup_system_prompt_report(path, session_id)
    if not report:
        return None
    sp = report.get("systemPrompt") or {}
    chars = sp.get("chars")
    if not isinstance(chars, int) or chars <= 0:
        return None
    tools = report.get("tools") or {}
    skills = report.get("skills") or {}
    tools_entries = (
        tools.get("entries") if isinstance(tools.get("entries"), list) else []
    )
    skills_entries = (
        skills.get("entries") if isinstance(skills.get("entries"), list) else []
    )
    return {
        "source": report.get("source") or "run",
        "chars": chars,
        "project_context_chars": sp.get("projectContextChars"),
        "non_project_context_chars": sp.get("nonProjectContextChars"),
        "tools_count": len(tools_entries),
        "tools_schema_chars": tools.get("schemaChars"),
        "skills_count": len(skills_entries),
        "skills_prompt_chars": skills.get("promptChars"),
        "provider": report.get("provider"),
        "model": report.get("model"),
    }
