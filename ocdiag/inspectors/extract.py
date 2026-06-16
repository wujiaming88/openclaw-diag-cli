"""extract inspector — export session JSONL to a structured Report.

The Report sections summarize the session(s) that match the requested uuid
(active, reset, deleted, backup, checkpoint…). Verdict semantics:

  OK    extracted at least one file
  FAIL  session not found / invalid query
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Set

from .. import sessions
from ..core.context import DiagContext
from ..core.errors import DiagError
from ..core.registry import register
from ..core.types import Report, Section, Verdict
from ..timeutil import fmt_iso_local
from ..extracting import (
    collect_records,
    collect_summary,
    human_size,
    system_prompt_for,
)


def _section_summary(s: Section, summary: Dict[str, Any]) -> None:
    s.ok(
        "extract.records",
        f"Total records: {summary['total_records']}",
        data={"total": summary["total_records"]},
    )
    if summary["parse_errors"]:
        s.warn(
            "extract.parse_errors",
            f"Parse errors: {summary['parse_errors']}",
            data={"count": summary["parse_errors"]},
        )
    by_type = summary["by_type"]
    if by_type:
        lines = [
            f"  {k}: {v}"
            for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])
        ]
        s.ok(
            "extract.by_type",
            f"By type: {len(by_type)} kinds",
            detail="\n".join(lines),
            data={"by_type": by_type},
        )
    tr = summary["time_range"]
    if tr["start"] or tr["end"]:
        s.ok(
            "extract.time_range",
            f"Time range: {fmt_iso_local(tr['start'])}  →  {fmt_iso_local(tr['end'])}",
            data=tr,
        )


def _section_system_prompt(s: Section, sp: Dict[str, Any]) -> None:
    src = sp.get("source") or "?"
    chars = sp.get("chars") or 0
    s.ok(
        "sp.size",
        f"System prompt: {chars:,} chars [{src}]",
        data={"chars": chars, "source": src},
    )
    if isinstance(sp.get("project_context_chars"), int):
        s.ok(
            "sp.project",
            f"project-context: {sp['project_context_chars']:,} chars",
        )
    if isinstance(sp.get("non_project_context_chars"), int):
        s.ok(
            "sp.non_project",
            f"non-project: {sp['non_project_context_chars']:,} chars",
        )
    if isinstance(sp.get("tools_count"), int):
        msg = f"tools: {sp['tools_count']}"
        if isinstance(sp.get("tools_schema_chars"), int):
            msg += f" ({sp['tools_schema_chars']:,} chars schema)"
        s.ok("sp.tools", msg)
    if isinstance(sp.get("skills_count"), int):
        msg = f"skills: {sp['skills_count']}"
        if isinstance(sp.get("skills_prompt_chars"), int):
            msg += f" ({sp['skills_prompt_chars']:,} chars prompt)"
        s.ok("sp.skills", msg)


@register
class ExtractInspector:
    id = "extract"
    title = "Session Extract"
    kind = "inspector"

    def collect(self, ctx: DiagContext, **kwargs) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)

        session_id: Optional[str] = kwargs.get("session_id")
        if not session_id:
            report.error = "missing session_id"
            report.diag_error = DiagError(
                code="MISSING_ARGUMENT",
                message="missing session_id",
                hint="usage: openclaw-diag extract <session-uuid>",
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        ok, msg = sessions.is_valid_query(session_id)
        if not ok:
            report.error = msg
            report.diag_error = DiagError(
                code="INVALID_QUERY",
                message=msg,
                details={"query": session_id},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        list_only = bool(kwargs.get("list_only"))
        all_versions = bool(kwargs.get("all_versions"))
        summary_only = bool(kwargs.get("summary"))
        unmask = bool(kwargs.get("unmask")) or ctx.unmask
        types_filter = kwargs.get("types_filter")
        type_filter: Optional[Set[str]] = None
        if isinstance(types_filter, str):
            type_filter = {t.strip() for t in types_filter.split(",") if t.strip()}
        elif isinstance(types_filter, (list, tuple, set)):
            type_filter = {str(t).strip() for t in types_filter if str(t).strip()}

        # --list and --all see lock files; default mode hides them so non-interactive
        # callers (cron, jq pipes) don't trip on a transient .jsonl.lock sibling.
        include_transient = list_only or all_versions

        files, candidates = sessions.resolve(
            session_id,
            base_dir=str(ctx.sessions_base),
            agent=kwargs.get("agent"),
            include_transient=include_transient,
        )
        if candidates:
            report.error = (
                f"前缀 '{session_id}' 匹配多个 session: "
                + ", ".join(candidates)
            )
            report.diag_error = DiagError(
                code="AMBIGUOUS_SESSION",
                message=report.error,
                hint="provide a longer prefix or the full uuid",
                details={"query": session_id, "matches": candidates},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report
        if not files:
            recent = sessions.recent_session_ids(str(ctx.sessions_base), limit=5)
            hint_msg = f"recent sessions: {', '.join(recent)}" if recent else None
            hint = f"; recent: {', '.join(recent)}" if recent else ""
            report.error = f"找不到 session '{session_id}'{hint}"
            report.diag_error = DiagError(
                code="SESSION_NOT_FOUND",
                message=f"找不到 session '{session_id}'",
                hint=hint_msg,
                details={"query": session_id},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        full_session_id = (
            os.path.basename(files[0][0]).split(".jsonl", 1)[0] or session_id
        )
        report.data["session_id"] = full_session_id
        report.add_scope(
            "session", f"session:{full_session_id[:8]}",
            f"{len(files)} files",
        )

        # Pick which files to walk.
        if list_only:
            selected = files
        elif all_versions:
            selected = [(p, st) for p, st in files if st != "lock"]
        else:
            # Default: just the first non-lock file, mirroring legacy behavior
            # for non-interactive callers (no stdin prompt in v2).
            picks = [(p, st) for p, st in files if st != "lock"]
            selected = picks[:1] if picks else []

        s_files = report.section("Extract · 文件清单")
        for path, state in files:
            try:
                size_s = human_size(os.path.getsize(path))
            except OSError:
                size_s = "?"
            s_files.ok(
                f"file.{os.path.basename(path)}",
                f"[{state}] {size_s} {path}",
                data={"path": path, "state": state},
            )
        report.data["files"] = [
            {"path": p, "state": st} for p, st in files
        ]

        if list_only:
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        if not selected:
            report.error = "no extractable files (only lock entries present)"
            report.diag_error = DiagError(
                code="FILE_READ_ERROR",
                message=report.error,
                details={"query": session_id},
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        sanitize = not unmask
        report.data["sanitized"] = sanitize

        files_payload: List[Dict[str, Any]] = []
        aggregate_total = 0
        aggregate_by_type: Dict[str, int] = {}
        aggregate_start: Optional[str] = None
        aggregate_end: Optional[str] = None

        for path, state in selected:
            try:
                size_bytes = os.path.getsize(path)
            except OSError:
                size_bytes = 0
            entry: Dict[str, Any] = {
                "path": path,
                "state": state,
                "size_bytes": size_bytes,
            }

            sp_info: Optional[Dict[str, Any]] = None
            if state == "active":
                sp_info = system_prompt_for(path, full_session_id)
                if sp_info:
                    entry["system_prompt"] = sp_info

            file_summary = collect_summary(path, sanitize=sanitize)
            entry["summary"] = file_summary
            aggregate_total += file_summary["total_records"]
            for k, v in file_summary["by_type"].items():
                aggregate_by_type[k] = aggregate_by_type.get(k, 0) + v
            tr = file_summary["time_range"]
            if tr["start"] and (
                aggregate_start is None or tr["start"] < aggregate_start
            ):
                aggregate_start = tr["start"]
            if tr["end"] and (
                aggregate_end is None or tr["end"] > aggregate_end
            ):
                aggregate_end = tr["end"]

            if not summary_only:
                entry["records"] = collect_records(
                    path, type_filter, sanitize=sanitize,
                )
            files_payload.append(entry)

            section_title = f"Extract · {state} {os.path.basename(path)}"
            s_file = report.section(section_title)
            s_file.ok(
                "file.size",
                f"size: {human_size(size_bytes)}",
                data={"bytes": size_bytes},
            )
            _section_summary(s_file, file_summary)
            if sp_info is not None:
                _section_system_prompt(s_file, sp_info)

        report.data["files_payload"] = files_payload
        report.data["aggregate"] = {
            "total_records": aggregate_total,
            "by_type": aggregate_by_type,
            "time_range": {"start": aggregate_start, "end": aggregate_end},
        }

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
