"""Shared helpers for synthesizing trajectory test fixtures.

Each fixture file is a JSONL of trajectory events grouped by runId. Lines
must use the canonical traceSchema/schemaVersion envelope so the production
parser exercises the same code paths it does on real data.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional


SESSION_ID = "11111111-1111-1111-1111-111111111111"
SESSION_KEY = "agent:test:cli:direct:test-user"


def _envelope(
    *,
    seq: int,
    type_: str,
    run_id: str,
    ts: str,
    data: Dict[str, Any],
    session_id: str = SESSION_ID,
    session_key: str = SESSION_KEY,
    schema_version: int = 1,
    provider: str = "test-provider",
    model_id: str = "test-model",
    model_api: str = "test-api",
) -> Dict[str, Any]:
    return {
        "traceSchema": "openclaw-trajectory",
        "schemaVersion": schema_version,
        "traceId": session_id,
        "source": "runtime",
        "type": type_,
        "ts": ts,
        "seq": seq,
        "sourceSeq": seq,
        "sessionId": session_id,
        "sessionKey": session_key,
        "runId": run_id,
        "workspaceDir": "/tmp/test-workspace",
        "provider": provider,
        "modelId": model_id,
        "modelApi": model_api,
        "data": data,
    }


def make_events(
    run_id: str,
    *,
    started_ts: str,
    ended_ts: Optional[str] = None,
    trigger: str = "user",
    final_status: str = "success",
    abort_flags: Optional[Dict[str, bool]] = None,
    prompt_error_source: Optional[str] = None,
    item_lifecycle: Optional[Dict[str, int]] = None,
    tool_metas: Optional[List[Dict[str, Any]]] = None,
    did_send: bool = False,
    messaging_texts: Optional[List[str]] = None,
    messaging_targets: Optional[List[Any]] = None,
    successful_cron_adds: int = 0,
    cache_broke: Optional[bool] = None,
    compaction_count: int = 0,
    assistant_texts: Optional[List[str]] = None,
    usage: Optional[Dict[str, int]] = None,
    system_prompt_chars: int = 1000,
    skills_block_chars: int = 100,
    tools_schema_chars: int = 5000,
    bootstrap_truncated: int = 0,
    plugin_entries: Optional[List[Dict[str, Any]]] = None,
    harness_version: str = "2026.5.0",
    schema_version: int = 1,
    incomplete: str = "",
) -> List[Dict[str, Any]]:
    """Build a 7-event run (or fewer if `incomplete` requests it).

    `incomplete` controls which events to omit:
      - "no_artifacts": drop trace.artifacts + session.ended
      - "only_started": only session.started + trace.metadata
    """
    abort_flags = abort_flags or {}
    item_lifecycle = item_lifecycle or {
        "startedCount": 1, "completedCount": 1, "activeCount": 0
    }
    usage = usage or {
        "input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0,
        "total": 150,
    }
    plugin_entries = plugin_entries or []
    tool_metas = tool_metas or []

    evs: List[Dict[str, Any]] = []
    seq = 0

    def add(t: str, data: Dict[str, Any], ts: str) -> None:
        nonlocal seq
        seq += 1
        evs.append(_envelope(
            seq=seq, type_=t, run_id=run_id, ts=ts, data=data,
            schema_version=schema_version,
        ))

    # 1. session.started
    add("session.started", {
        "trigger": trigger,
        "sessionFile": f"/tmp/{SESSION_ID}.jsonl",
        "workspaceDir": "/tmp/test-workspace",
        "agentId": "test-agent",
        "messageProvider": "test-channel",
        "toolCount": 56,
        "clientToolCount": 0,
    }, started_ts)

    if incomplete == "only_started":
        return evs

    # 2. trace.metadata
    add("trace.metadata", {
        "harness": {
            "version": harness_version,
            "runtime": {"node": "v22.22.2"},
            "invocation": ["/usr/bin/node", "openclaw", "gateway",
                            "--port", "18789"],
        },
        "model": {"provider": "test-provider", "name": "test-model",
                  "api": "test-api"},
        "config": {"runtime": {"trigger": trigger}},
        "plugins": {"entries": plugin_entries,
                    "importedRuntimePluginIds": []},
        "skills": {"snapshotVersion": "v1", "entries": [
            {"id": "skill-a", "name": "skill-a"},
        ]},
        "prompting": {
            "systemPromptReport": {
                "systemPrompt": {
                    "chars": system_prompt_chars,
                    "projectContextChars": system_prompt_chars // 2,
                    "nonProjectContextChars": system_prompt_chars // 2,
                },
                "skills": {
                    "promptChars": skills_block_chars * 3,
                    "entries": [
                        {"name": "skill-a", "blockChars": skills_block_chars},
                    ],
                },
                "tools": {
                    "schemaChars": tools_schema_chars,
                    "entries": [
                        {"name": "tool-a",
                         "schemaChars": tools_schema_chars,
                         "summaryChars": 100,
                         "propertiesCount": 5},
                    ],
                },
                "bootstrapTruncation": {
                    "truncatedFiles": bootstrap_truncated,
                    "nearLimitFiles": 0,
                },
                "injectedWorkspaceFiles": [],
            },
        },
        "redaction": {"config": "default", "payloads": "default", "harness": "default"},
    }, started_ts)

    # 3. context.compiled
    add("context.compiled", {
        "systemPrompt": "x" * min(system_prompt_chars, 1000),
        "prompt": "test prompt",
        "messages": [],
        "tools": [],
        "imagesCount": 0,
    }, started_ts)

    # 4. prompt.submitted
    add("prompt.submitted", {
        "prompt": "test prompt", "systemPrompt": "...",
    }, started_ts)

    # 5. model.completed
    add("model.completed", {
        "aborted": abort_flags.get("aborted", False),
        "externalAbort": abort_flags.get("externalAbort", False),
        "timedOut": abort_flags.get("timedOut", False),
        "idleTimedOut": abort_flags.get("idleTimedOut", False),
        "timedOutDuringCompaction": abort_flags.get("timedOutDuringCompaction", False),
        "timedOutDuringToolExecution": abort_flags.get("timedOutDuringToolExecution", False),
        "promptErrorSource": prompt_error_source,
        "usage": usage,
        "promptCache": {
            "observation": {"broke": cache_broke if cache_broke is not None else False,
                            "cacheRead": usage.get("cacheRead", 0)},
        },
        "compactionCount": compaction_count,
        "assistantTexts": assistant_texts or [],
        "messagesSnapshot": [],
    }, ended_ts or started_ts)

    if incomplete == "no_artifacts":
        return evs

    # 6. trace.artifacts
    add("trace.artifacts", {
        "finalStatus": final_status,
        "aborted": abort_flags.get("aborted", False),
        "externalAbort": abort_flags.get("externalAbort", False),
        "timedOut": abort_flags.get("timedOut", False),
        "idleTimedOut": abort_flags.get("idleTimedOut", False),
        "timedOutDuringCompaction": abort_flags.get("timedOutDuringCompaction", False),
        "timedOutDuringToolExecution": abort_flags.get("timedOutDuringToolExecution", False),
        "promptErrorSource": prompt_error_source,
        "usage": usage,
        "promptCache": {
            "observation": {"broke": cache_broke if cache_broke is not None else False,
                            "cacheRead": usage.get("cacheRead", 0)},
        },
        "compactionCount": compaction_count,
        "assistantTexts": assistant_texts or [],
        "itemLifecycle": item_lifecycle,
        "toolMetas": tool_metas,
        "didSendViaMessagingTool": did_send,
        "successfulCronAdds": successful_cron_adds,
        "messagingToolSentTexts": messaging_texts or [],
        "messagingToolSentTargets": messaging_targets or [],
    }, ended_ts or started_ts)

    # 7. session.ended
    add("session.ended", {
        "status": final_status,
        "aborted": abort_flags.get("aborted", False),
        "externalAbort": abort_flags.get("externalAbort", False),
        "timedOut": abort_flags.get("timedOut", False),
        "idleTimedOut": abort_flags.get("idleTimedOut", False),
        "timedOutDuringCompaction": abort_flags.get("timedOutDuringCompaction", False),
        "timedOutDuringToolExecution": abort_flags.get("timedOutDuringToolExecution", False),
    }, ended_ts or started_ts)

    return evs


def write_fixture(path: str, events: Iterable[Dict[str, Any]]) -> None:
    """Write events to disk as JSONL, ensuring parent dirs exist."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
