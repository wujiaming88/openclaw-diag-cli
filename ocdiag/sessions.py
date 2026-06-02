"""Shared session-file lookup utilities for trace/extract.

A "session" is identified by a UUID. On disk it can have multiple files:
  <uuid>.jsonl                          — active
  <uuid>.jsonl.lock                     — write lock (transient, filtered by default)
  <uuid>.jsonl.deleted.<ts>             — soft-deleted
  <uuid>.jsonl.reset.<ts>               — pre-reset snapshot
  <uuid>.jsonl.bak-<pid>                — backup snapshot
  <uuid>.checkpoint.<cp-uuid>.jsonl     — checkpoint snapshot (belongs to <uuid>)

Sibling artifacts (NOT session content):
  <uuid>.trajectory.jsonl, <uuid>.acp-stream.jsonl, <uuid>.json

Callers may pass a full UUID or a prefix of at least MIN_PREFIX_LEN chars.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import paths


MIN_PREFIX_LEN = 8

_TRANSIENT_SUFFIXES = (".lock", ".tmp", ".swp")

_UUID_CHAR = re.compile(r"^[0-9a-fA-F-]+$")


def classify_state(filename: str) -> str:
    """Tag a session-file basename with its lifecycle state."""
    if ".jsonl.deleted." in filename:
        return "deleted"
    if ".jsonl.reset." in filename:
        return "reset"
    if ".jsonl.bak-" in filename:
        return "backup"
    if filename.endswith(".jsonl.lock"):
        return "lock"
    if ".checkpoint." in filename and filename.endswith(".jsonl"):
        return "checkpoint"
    if filename.endswith(".jsonl"):
        return "active"
    return "unknown"


def _session_uuid_of(filename: str) -> Optional[str]:
    """Return the session UUID the file belongs to, or None for siblings."""
    if ".trajectory" in filename or ".acp-stream" in filename:
        return None
    if filename.endswith(".json") and not filename.endswith(".jsonl"):
        return None
    idx = filename.find(".jsonl")
    if idx <= 0:
        return None
    stem = filename[:idx]
    cp_idx = stem.find(".checkpoint.")
    if cp_idx > 0:
        return stem[:cp_idx]
    return stem


def _is_transient(filename: str) -> bool:
    if ".jsonl.bak-" in filename:
        return False
    return any(filename.endswith(s) for s in _TRANSIENT_SUFFIXES) or filename.endswith(".bak")


def is_valid_query(session_id: str) -> Tuple[bool, str]:
    """Reject queries shorter than MIN_PREFIX_LEN or with non-UUID chars."""
    if not session_id:
        return False, "session id 不能为空"
    if len(session_id) < MIN_PREFIX_LEN:
        return False, (
            f"session id 太短（'{session_id}' 只有 {len(session_id)} 字符），"
            f"至少需要 {MIN_PREFIX_LEN} 位 UUID 前缀"
        )
    if not _UUID_CHAR.match(session_id):
        return False, f"session id 含非法字符（仅允许十六进制和连字符）: '{session_id}'"
    return True, ""


def resolve(
    session_id: str,
    base_dir: str = paths.SESSIONS_BASE,
    agent: Optional[str] = None,
    include_transient: bool = False,
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Resolve a UUID or prefix to its on-disk session files.

    Returns ``(files, candidates)``:
      - ``files``: ``[(abs_path, state), ...]`` for the resolved session,
        sorted by lifecycle priority (active first). Empty when ambiguous or
        when there are 0 matches.
      - ``candidates``: when multiple distinct session UUIDs share the
        prefix, this lists their full UUIDs sorted; otherwise empty.
    """
    if agent:
        agent_dirs = [os.path.join(base_dir, agent)]
    else:
        agent_dirs = sorted(glob.glob(os.path.join(base_dir, "*")))

    by_uuid: Dict[str, List[Tuple[str, str]]] = {}
    for ad in agent_dirs:
        sd = os.path.join(ad, "sessions")
        if not os.path.isdir(sd):
            continue
        try:
            entries = os.listdir(sd)
        except OSError:
            continue
        for entry in entries:
            if not entry.startswith(session_id):
                continue
            uuid = _session_uuid_of(entry)
            if uuid is None:
                continue
            if not include_transient and _is_transient(entry):
                continue
            full = os.path.join(sd, entry)
            if not os.path.isfile(full):
                continue
            state = classify_state(entry)
            by_uuid.setdefault(uuid, []).append((full, state))

    if not by_uuid:
        return [], []
    if len(by_uuid) > 1:
        return [], sorted(by_uuid.keys())

    files = next(iter(by_uuid.values()))
    prio = {"active": 0, "lock": 1, "checkpoint": 2, "deleted": 3, "reset": 4, "backup": 5, "unknown": 9}
    files.sort(key=lambda x: (prio.get(x[1], 9), x[0]))
    return files, []


def lookup_system_prompt_report(
    session_file: str,
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """从 session store (`sessions.json`) 里捞出该 sessionId 的 systemPromptReport。

    OpenClaw 在 ``<session-dir>/sessions.json`` 维护一个以 sessionKey
    （channel + 用户/subagent id）为主键、value 为 SessionEntry 的 store。
    其中 ``systemPromptReport`` 字段是最近一次 run 时记录的 system prompt
    画像（含 chars / projectContextChars / tools / skills /
    injectedWorkspaceFiles 等），是 trace/extract 展示「system prompt 大小」
    的首选数据源。

    线性扫一遍 store 找到 ``entry.sessionId == session_id`` 的项，命中则
    返回其 ``systemPromptReport``（可能为 None）；找不到、文件不存在、
    JSON 解析失败、IO 异常一律安静返回 None — 调用方不应因为这条诊断
    增强而 crash。
    """
    try:
        store_path = os.path.join(os.path.dirname(session_file), "sessions.json")
        if not os.path.isfile(store_path):
            return None
        with open(store_path, "r", encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(store, dict):
        return None
    for entry in store.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("sessionId") == session_id:
            report = entry.get("systemPromptReport")
            return report if isinstance(report, dict) else None
    return None


def recent_session_ids(
    base_dir: str = paths.SESSIONS_BASE,
    limit: int = 5,
) -> List[str]:
    """Return the most-recently-modified active session UUIDs."""
    found: List[Tuple[float, str]] = []
    for ad in glob.glob(os.path.join(base_dir, "*")):
        sd = os.path.join(ad, "sessions")
        if not os.path.isdir(sd):
            continue
        try:
            entries = os.listdir(sd)
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".jsonl"):
                continue
            uuid = _session_uuid_of(entry)
            if uuid is None or entry != f"{uuid}.jsonl":
                continue
            path = os.path.join(sd, entry)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            found.append((mtime, uuid))
    found.sort(reverse=True)
    return [sid for _, sid in found[:limit]]
