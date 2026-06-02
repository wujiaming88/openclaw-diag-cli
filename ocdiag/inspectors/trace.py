"""trace inspector — timeline of one user message.

Produces a v2 Report whose sections describe timeline phases (rather than
health checks). Verdict semantics for trace:

  OK    completed cleanly
  WARN  slow (E2E > 30s) or non-fatal anomalies (cache broken, plugin warnings)
  FAIL  prompt-error / timeout / aborted
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Optional

from .. import sessions, trajectory as traj_mod
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict

# Reuse the heavy lifting from the legacy script — these helpers are pure
# functions with no side effects, perfect for direct reuse.
from ..tracing import (
    analyze_phases,
    build_system_prompt_info,
    extract_trace_records,
    find_first_message,
    find_gateway_logs,
    find_trajectory_file,
    find_user_messages,
    fmt_duration,
    load_gateway_timing,
    load_records,
    load_trajectory_info,
    select_user_message,
)


SLOW_THRESHOLD_MS = 30_000


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{part / total * 100:.1f}%"


def _apply_traj_redaction(
    info: Dict[str, Any], *, mask: bool,
    show_tool_metas: bool, show_plugin_snapshot: bool,
) -> None:
    if not show_tool_metas:
        if isinstance(info.get("toolMetas"), list):
            info["toolMetas"] = [
                {"toolName": m.get("toolName")}
                for m in info["toolMetas"] if isinstance(m, dict)
            ]
    elif mask:
        info["toolMetas"] = traj_mod.sanitize_field(info.get("toolMetas"), mask=True)
    if not show_plugin_snapshot:
        info.pop("plugin_snapshot", None)
    if mask:
        for key in ("assistantTexts", "messagingTexts"):
            if key in info:
                info[key] = traj_mod.sanitize_field(info[key], mask=True)
        if isinstance(info.get("finalPromptText"), str):
            info["finalPromptText"] = traj_mod.sanitize_field(
                info["finalPromptText"], mask=True,
            )


def _format_event(ev: Dict[str, Any]) -> str:
    off = ev["offset_ms"]
    etype = ev["type"]
    detail = ev.get("detail", "")
    if etype == "user":
        return f"T+{off:<8} [user]      {detail}"
    if etype in ("model_start", "model_end"):
        return f"T+{off:<8} [model #{ev['num']}] {detail}"
    if etype == "tool_batch":
        return f"T+{off:<8} [tool]      {detail}"
    if etype == "error":
        return f"T+{off:<8} [ERROR]     {detail}"
    return f"T+{off:<8} [{etype}]    {detail}"


def _section_timeline(s: Section, analysis: Dict[str, Any]) -> None:
    for ev in analysis["events"]:
        msg = _format_event(ev)
        verdict = Verdict.FAIL if ev["type"] == "error" else Verdict.OK
        s.add(f"timeline.{ev['type']}", verdict, msg)


def _section_summary(s: Section, analysis: Dict[str, Any]) -> None:
    summary = analysis["summary"]
    total = summary["total_ms"]
    s.ok("trace.total", f"Total time: {fmt_duration(total)}",
         data={"total_ms": total})
    if summary["model_count"]:
        msg = (
            f"Model calls: {summary['model_count']}, "
            f"total {fmt_duration(summary['model_total_ms'])} "
            f"({_pct(summary['model_total_ms'], total)})"
        )
    else:
        msg = "Model calls: 0"
    s.ok("trace.model_count", msg, data={
        "count": summary["model_count"],
        "total_ms": summary["model_total_ms"],
    })
    if summary["tool_count"]:
        msg = (
            f"Tool executions: {summary['tool_count']}, "
            f"total {fmt_duration(summary['tool_total_ms'])} "
            f"({_pct(summary['tool_total_ms'], total)})"
        )
    else:
        msg = "Tool executions: 0"
    s.ok("trace.tool_count", msg, data={
        "count": summary["tool_count"],
        "total_ms": summary["tool_total_ms"],
    })
    tok_msg = (
        f"Tokens: in={summary['total_input_tokens']} "
        f"out={summary['total_output_tokens']}"
    )
    if summary["total_cache_read"]:
        tok_msg += f" cache_read={summary['total_cache_read']}"
    if summary["total_cache_write"]:
        tok_msg += f" cache_write={summary['total_cache_write']}"
    s.ok("trace.tokens", tok_msg, data={
        "input": summary["total_input_tokens"],
        "output": summary["total_output_tokens"],
        "cache_read": summary["total_cache_read"],
        "cache_write": summary["total_cache_write"],
    })
    if summary["model_total_ms"] > 0:
        rate = summary["total_output_tokens"] / (summary["model_total_ms"] / 1000)
        s.ok("trace.rate", f"Avg output rate: {rate:.1f} tok/s",
             data={"tok_per_s": round(rate, 1)})


def _section_model_breakdown(s: Section, analysis: Dict[str, Any]) -> None:
    for mc in analysis["model_calls"]:
        tag = ""
        if mc["stop_reason"] == "toolUse" and mc["tool_names"]:
            names = mc["tool_names"]
            if len(names) <= 3:
                tag = " (toolUse → " + ",".join(names) + ")"
            else:
                tag = f" (toolUse → {names[0]}+{len(names)-1}more)"
        elif mc["stop_reason"] == "stop":
            tag = " (stop) ← final"
        elif mc["stop_reason"]:
            tag = f" ({mc['stop_reason']})"
        msg = (
            f"#{mc['num']:<2} {fmt_duration(mc['duration_ms']):>8} "
            f"out={mc['tokens_out']}{tag}"
        )
        s.ok(f"model.call.{mc['num']}", msg, data=mc)


def _section_tool_breakdown(s: Section, analysis: Dict[str, Any]) -> None:
    by_name: Dict[str, Dict[str, Any]] = {}
    for te in analysis["tool_execs"]:
        name = te["name"]
        rec = by_name.setdefault(
            name, {"count": 0, "total_ms": 0, "errors": 0},
        )
        rec["count"] += 1
        rec["total_ms"] += te["duration_ms"]
        if te["is_error"]:
            rec["errors"] += 1
    for name, info in sorted(by_name.items(), key=lambda x: -x[1]["total_ms"]):
        avg = info["total_ms"] / info["count"] if info["count"] else 0
        err_str = f" ({info['errors']} errors)" if info["errors"] else ""
        msg = (
            f"{name}: {info['count']} call(s), "
            f"{fmt_duration(info['total_ms'])} total, "
            f"avg {fmt_duration(avg)}{err_str}"
        )
        verdict = Verdict.WARN if info["errors"] else Verdict.OK
        s.add(f"tool.{name}", verdict, msg, data={"name": name, **info})


def _section_trajectory(s: Section, info: Dict[str, Any]) -> Verdict:
    """Return aggregate verdict contribution from trajectory enrichment."""
    aggregate = Verdict.OK
    s.ok("traj.runId", f"runId: {info.get('runId', '?')}", data=info)
    if info.get("trigger"):
        s.ok("traj.trigger", f"trigger: {info['trigger']}")
    if info.get("status"):
        s.ok("traj.status", f"status: {info['status']}")

    final_status = info.get("finalStatus")
    if final_status is not None:
        abort_flags = [
            k for k in (
                "aborted", "externalAbort", "timedOut", "idleTimedOut",
                "timedOutDuringCompaction", "timedOutDuringToolExecution",
            ) if info.get(k)
        ]
        prompt_err = info.get("promptErrorSource")
        bits = [f"finalStatus={final_status}"]
        if prompt_err:
            bits.append(f"promptErrorSource={prompt_err}")
        if abort_flags:
            bits.append(f"flags=[{','.join(abort_flags)}]")
        if prompt_err or abort_flags:
            v = Verdict.FAIL
            aggregate = Verdict.worst(aggregate, v)
        else:
            v = Verdict.OK
        s.add("traj.outcome", v, "outcome: " + " | ".join(bits))

    lc = info.get("lifecycle") or {}
    if lc:
        active = lc.get("active", 0)
        msg = (
            f"lifecycle: started={lc.get('started',0)} "
            f"completed={lc.get('completed',0)} active={active}"
        )
        if active > 0:
            msg += " ← WARN: tool-call leak"
            v = Verdict.WARN
            aggregate = Verdict.worst(aggregate, v)
        else:
            v = Verdict.OK
        s.add("traj.lifecycle", v, msg, data=lc)

    cache = info.get("cache")
    if cache:
        usage = info.get("usage") or {}
        total = usage.get("total") or 0
        cache_read = cache.get("cacheRead") or 0
        ratio = (cache_read / total * 100) if total else 0.0
        broke = cache.get("broke")
        comp = info.get("compactionCount") or 0
        msg = (
            f"cache: broke={broke} | cacheRead={cache_read:,}/{total:,} "
            f"({ratio:.1f}%) | compactionCount={comp}"
        )
        if broke:
            v = Verdict.WARN
            aggregate = Verdict.worst(aggregate, v)
        else:
            v = Verdict.OK
        s.add("traj.cache", v, msg, data={
            "broke": broke, "cacheRead": cache_read,
            "totalTokens": total, "ratio": ratio,
            "compactionCount": comp,
        })

    delivery_active = (
        info.get("trigger") in ("cron", "user")
        or info.get("didSendViaMessagingTool")
        or info.get("messagingTextCount", 0) > 0
    )
    if delivery_active:
        sent = info.get("didSendViaMessagingTool")
        tc = info.get("messagingTextCount", 0)
        tg = info.get("messagingTargets") or []
        s.ok(
            "traj.delivery",
            f"delivery: didSendViaMessagingTool={sent} texts={tc} "
            f"targets={len(tg)} successfulCronAdds={info.get('successfulCronAdds', 0)}",
            data={
                "didSendViaMessagingTool": sent,
                "textCount": tc,
                "targets": tg,
                "successfulCronAdds": info.get("successfulCronAdds", 0),
            },
        )

    snapshot = info.get("plugin_snapshot")
    if snapshot:
        errored = [p for p in snapshot if p.get("error") and p.get("activated")]
        if errored:
            ev = "\n".join(
                f"  - {p.get('id')}: {p.get('error')}" for p in errored[:6]
            )
            s.warn(
                "traj.plugin_errors",
                f"plugin errors (this run): {len(errored)}",
                evidence=ev,
                data={"errors": errored},
            )
            aggregate = Verdict.worst(aggregate, Verdict.WARN)
        else:
            s.ok(
                "traj.plugin_snapshot",
                f"plugin snapshot: {len(snapshot)} entries, no activated-with-error",
                data={"count": len(snapshot)},
            )

    return aggregate


def _section_system_prompt(s: Section, sp: Dict[str, Any]) -> None:
    src = sp.get("source") or "?"
    chars = sp.get("chars") or 0
    s.ok("sp.size", f"System prompt: {chars:,} chars [{src}]", data={
        "chars": chars, "source": src,
    })
    pc = sp.get("projectContextChars")
    npc = sp.get("nonProjectContextChars")
    if isinstance(pc, int):
        s.ok("sp.project", f"project-context: {pc:,} chars", data={"chars": pc})
    if isinstance(npc, int):
        s.ok("sp.non_project", f"non-project: {npc:,} chars", data={"chars": npc})
    first_call_tok = sp.get("firstCallInputTokens")
    if isinstance(first_call_tok, int) and first_call_tok > 0:
        s.ok("sp.first_call_tok",
             f"First call context: {first_call_tok:,} tok (actual)",
             data={"tokens": first_call_tok})
    tools = sp.get("tools") or {}
    if isinstance(tools.get("schemaChars"), int):
        s.ok("sp.tools",
             f"Tool schemas (JSON): {tools['schemaChars']:,} chars "
             f"({tools.get('count', '?')} tools)",
             data=tools)
    elif isinstance(tools.get("count"), int):
        s.ok("sp.tools_count", f"Tools in request: {tools['count']}",
             data=tools)
    skills = sp.get("skills") or {}
    if isinstance(skills.get("promptChars"), int):
        s.ok("sp.skills",
             f"Skills (text): {skills['promptChars']:,} chars "
             f"({skills.get('count', '?')} skills)",
             data=skills)
    files = sp.get("injectedWorkspaceFiles") or []
    if files:
        lines = []
        for f in files[:8]:
            tag = " (TRUNCATED)" if f.get("truncated") else ""
            lines.append(
                f"  - {f.get('name', '?')}: raw {f.get('rawChars'):,} → "
                f"injected {f.get('injectedChars'):,}{tag}",
            )
        if len(files) > 8:
            lines.append(f"  ... +{len(files) - 8} more")
        s.ok("sp.injected", f"Injected workspace files ({len(files)}):",
             detail="\n".join(lines), data={"files": files})


def _section_gateway(s: Section, gw: Dict[str, Any]) -> None:
    if "run_to_prompt_ms" in gw:
        s.ok(
            "gw.run_to_prompt",
            f"run_start → prompt_start: {fmt_duration(gw['run_to_prompt_ms'])} "
            "(context compilation)",
            data={"ms": gw["run_to_prompt_ms"]},
        )
    if "prompt_duration_ms" in gw:
        s.ok(
            "gw.prompt_duration",
            f"prompt_start → prompt_end: {fmt_duration(gw['prompt_duration_ms'])} "
            "(total embedded run)",
            data={"ms": gw["prompt_duration_ms"]},
        )


@register
class TraceInspector:
    id = "trace"
    title = "Session Trace"
    kind = "inspector"

    def collect(self, ctx: DiagContext, **kwargs) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)

        session_id: Optional[str] = kwargs.get("session_id")
        if not session_id:
            report.error = "missing session_id"
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        ok, msg = sessions.is_valid_query(session_id)
        if not ok:
            report.error = msg
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        files, candidates = sessions.resolve(
            session_id,
            base_dir=str(ctx.sessions_base),
            agent=kwargs.get("agent"),
            include_transient=False,
        )
        if candidates:
            report.error = (
                f"前缀 '{session_id}' 匹配多个 session: "
                + ", ".join(candidates)
            )
            report.elapsed_ms = (time.time() - t0) * 1000
            return report
        if not files:
            recent = sessions.recent_session_ids(str(ctx.sessions_base), limit=5)
            hint = f"; recent: {', '.join(recent)}" if recent else ""
            report.error = f"找不到 session '{session_id}'{hint}"
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        session_file = files[0][0]
        full_session_id = os.path.basename(session_file).split(".jsonl", 1)[0]
        report.data["session_id"] = full_session_id
        report.data["session_file"] = session_file

        records = load_records(session_file)
        if not records:
            report.error = f"session file is empty: {session_file}"
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        user_msgs = find_user_messages(records) or find_first_message(records)
        if not user_msgs:
            report.error = "no message records in session"
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        rec_idx, user_rec = select_user_message(
            records,
            kwargs.get("msg_index"),
            kwargs.get("msg_id"),
            kwargs.get("msg_match"),
        )
        try:
            user_msg_ordinal = next(
                i for i, (ri, _) in enumerate(user_msgs) if ri == rec_idx
            )
        except StopIteration:
            user_msg_ordinal = 0
        user_msg_id = user_rec.get("id", "?")
        report.data["user_message_index"] = user_msg_ordinal
        report.data["user_message_id"] = user_msg_id

        trace_records = extract_trace_records(records, rec_idx)
        analysis = analyze_phases(trace_records)
        report.data["base_epoch_ms"] = analysis["base_epoch_ms"]
        report.data["timeline"] = analysis["events"]
        report.data["model_calls"] = analysis["model_calls"]
        report.data["tool_execs"] = analysis["tool_execs"]
        report.data["summary"] = analysis["summary"]

        traj_info: Optional[Dict[str, Any]] = None
        if not kwargs.get("no_trajectory"):
            traj_path = find_trajectory_file(session_file)
            if traj_path:
                traj_info = load_trajectory_info(traj_path, analysis["base_epoch_ms"])
                if traj_info is not None:
                    _apply_traj_redaction(
                        traj_info,
                        mask=bool(kwargs.get("mask")),
                        show_tool_metas=bool(kwargs.get("show_tool_metas")),
                        show_plugin_snapshot=bool(kwargs.get("show_plugin_snapshot")),
                    )

        gw_info: Optional[Dict[str, Any]] = None
        if not kwargs.get("no_log"):
            log_files = find_gateway_logs(str(ctx.log_dir))
            if log_files:
                gw_info = load_gateway_timing(
                    log_files, full_session_id, analysis["base_epoch_ms"],
                )

        store_report = sessions.lookup_system_prompt_report(
            session_file, full_session_id,
        )
        system_prompt = build_system_prompt_info(store_report, traj_info)
        if system_prompt is not None and analysis.get("model_calls"):
            first_call = analysis["model_calls"][0]
            actual_input = (
                (first_call.get("tokens_in") or 0)
                + (first_call.get("cache_read") or 0)
                + (first_call.get("cache_write") or 0)
            )
            if actual_input > 0:
                system_prompt["firstCallInputTokens"] = actual_input

        if traj_info is not None:
            report.data["trajectory"] = traj_info
        if gw_info is not None:
            report.data["gateway"] = gw_info
        if system_prompt is not None:
            report.data["systemPrompt"] = system_prompt

        # ── Sections ──
        s_meta = report.section("Trace · 元信息")
        s_meta.ok(
            "session", f"session: {full_session_id}",
            data={"session_id": full_session_id, "file": session_file},
        )
        s_meta.ok(
            "user_message",
            f"user message #{user_msg_ordinal} (id: {user_msg_id})",
            data={"index": user_msg_ordinal, "id": user_msg_id},
        )

        s_timeline = report.section("Trace · 时间轴")
        _section_timeline(s_timeline, analysis)

        s_summary = report.section("Trace · 汇总")
        _section_summary(s_summary, analysis)

        if analysis["model_calls"]:
            s_models = report.section("Trace · Model 拆解")
            _section_model_breakdown(s_models, analysis)

        if analysis["tool_execs"]:
            s_tools = report.section("Trace · 工具拆解")
            _section_tool_breakdown(s_tools, analysis)

        if traj_info:
            s_traj = report.section("Trace · Trajectory")
            _section_trajectory(s_traj, traj_info)

        if system_prompt:
            s_sp = report.section("Trace · System Prompt")
            _section_system_prompt(s_sp, system_prompt)

        if gw_info:
            s_gw = report.section("Trace · Gateway 计时")
            _section_gateway(s_gw, gw_info)

        # Slow E2E → WARN. Add as a synthetic check on the summary section so
        # the verdict surfaces without inventing a new section.
        total_ms = analysis["summary"]["total_ms"]
        if total_ms > SLOW_THRESHOLD_MS:
            s_summary.warn(
                "trace.slow",
                f"E2E elapsed {fmt_duration(total_ms)} > {SLOW_THRESHOLD_MS//1000}s",
                data={"total_ms": total_ms, "threshold_ms": SLOW_THRESHOLD_MS},
            )

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
