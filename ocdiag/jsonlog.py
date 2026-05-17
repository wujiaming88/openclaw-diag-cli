"""Helpers for OpenClaw JSON-formatted log entries."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple


def parse_log_msg(obj: Dict[str, Any]) -> str:
    """Extract human-readable text from an OpenClaw JSON log entry."""
    texts = []
    for k in ("0", "1", "2", "3", "msg", "message"):
        v = obj.get(k, "")
        if not v or not isinstance(v, str):
            continue
        if v.startswith("{"):
            try:
                inner = json.loads(v)
                if isinstance(inner, dict) and inner.get("subsystem"):
                    continue
                if isinstance(inner, dict):
                    texts.append(" ".join(f"{ik}={iv}" for ik, iv in inner.items()))
                else:
                    texts.append(v)
            except (json.JSONDecodeError, AttributeError):
                texts.append(v)
        else:
            texts.append(v)
    return " | ".join(texts) if texts else ""


def get_log_subsystem(obj: Dict[str, Any]) -> str:
    """Extract subsystem name from an OpenClaw JSON log entry."""
    for k in ("0", "1", "2", "3"):
        v = obj.get(k, "")
        if v and isinstance(v, str) and v.startswith("{"):
            try:
                inner = json.loads(v)
                if isinstance(inner, dict):
                    return inner.get("subsystem", "") or ""
            except Exception:
                pass
    return ""


def parse_name(obj: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return (plugin, subsystem) from _meta.name."""
    meta = obj.get("_meta") or {}
    name = meta.get("name", "") if isinstance(meta, dict) else ""
    if not isinstance(name, str) or not name:
        return None, None
    try:
        p = json.loads(name)
    except Exception:
        return None, None
    if not isinstance(p, dict):
        return None, None
    return p.get("plugin"), p.get("subsystem")


