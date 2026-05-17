#!/usr/bin/env python3
"""模块 5：近期错误日志（应用日志 + journalctl + 工具调用错误）。"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output, recent_logs


_ERR_RE = re.compile(r'"logLevelName"\s*:\s*"(ERROR|FATAL)"')
_LEVEL_KEY = re.compile(r'"(logLevelName|level)"\s*:\s*"(ERROR|WARN|error|warn)"')
_HTTP_ERR_RE = re.compile(
    r"HTTP [45][0-9][0-9]|\"status\":\s*(?:4[0-9][0-9]|5[0-9][0-9])|"
    r"rate.limit|quota.exceeded",
    re.IGNORECASE,
)
_API_EXCLUDE_SUB_RE = re.compile(
    r'"subsystem":\s*"(tools|agent/embedded)"|allowlist contains',
    re.IGNORECASE,
)
_API_EXCLUDE_TXT_RE = re.compile(r"embedded run agent|agent end|agent start", re.IGNORECASE)
_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\]]*)\]\s*(.*)")
_SUBSYSTEM_STRIP_RE = re.compile(r'\s*\[\{[^}]*"subsystem"[^}]*\}\]\s*')


def extract_msg(obj):
    parts = []
    for k in ("0", "1", "2", "msg", "message"):
        v = obj.get(k, "")
        if not v or not isinstance(v, str):
            continue
        if v.startswith("{"):
            try:
                inner = json.loads(v)
                if isinstance(inner, dict):
                    meaningful = {ik: iv for ik, iv in inner.items() if ik != "subsystem"}
                    if meaningful:
                        parts.append(" ".join(f"{ik}={iv}" for ik, iv in meaningful.items()))
                    continue
            except Exception:
                pass
        parts.append(v)
    return " ".join(parts) if parts else None


def render_log_line(line: str, max_len: int = 300) -> str:
    line = line.strip()
    if not line:
        return ""
    try:
        obj = json.loads(line)
        ts = obj.get("time", "")[:19]
        msg = extract_msg(obj)
        if not msg:
            msg = str({k: v for k, v in obj.items() if k not in ("_meta", "time")})
        if isinstance(msg, str) and len(msg) > max_len:
            msg = msg[:max_len] + "..."
        level = obj.get("_meta", {}).get("logLevelName", "ERROR")
        return f"[{ts}] {level}: {msg}"
    except Exception:
        line = _SUBSYSTEM_STRIP_RE.sub(" ", line).strip()
        m = _TS_RE.match(line)
        if m:
            line = f"[{m.group(1)[:19]}] {m.group(2)}"
        if len(line) > max_len:
            line = line[:max_len] + "..."
        return line


def collect_error_lines(log_files: List[str]) -> List[str]:
    out: List[str] = []
    for lf in log_files:
        try:
            with open(lf, errors="replace") as f:
                for ln in f:
                    if _ERR_RE.search(ln):
                        out.append(ln.rstrip("\n"))
        except OSError:
            continue
    return out


def collect_api_errors(log_files: List[str]) -> List[str]:
    out: List[str] = []
    for lf in log_files:
        try:
            with open(lf, errors="replace") as f:
                for ln in f:
                    if not _LEVEL_KEY.search(ln):
                        continue
                    if not _HTTP_ERR_RE.search(ln):
                        continue
                    if _API_EXCLUDE_SUB_RE.search(ln):
                        continue
                    if _API_EXCLUDE_TXT_RE.search(ln):
                        continue
                    out.append(ln.rstrip("\n"))
        except OSError:
            continue
    return out


def journalctl_errors() -> str:
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "openclaw-gateway",
             "--since", "today", "--priority", "err", "--no-pager"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def find_recent_session(sessions_base: str):
    if not os.path.isdir(sessions_base):
        return None
    best = None
    best_mtime = -1.0
    for f in glob.glob(os.path.join(sessions_base, "*", "**", "*.jsonl"), recursive=True):
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if m > best_mtime:
            best_mtime = m
            best = f
    return best


def tool_errors_from_session(session_path: str):
    counts = Counter()
    try:
        # tail-equivalent: load all but only keep last 500
        with open(session_path, errors="replace") as f:
            lines = f.readlines()
        for line in lines[-500:]:
            try:
                obj = json.loads(line)
                msg = obj.get("message", {}) or {}
                if msg.get("isError"):
                    counts[msg.get("toolName", "unknown")] += 1
            except Exception:
                pass
    except OSError:
        pass
    return counts


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 5：采集近期错误日志",
        prog="05_recent_errors",
    )
    args = parser.parse_args()

    out = output.init("recent_errors", json_mode=args.json, no_color=args.no_color)
    out.section("模块 5：近期日志")

    logs = recent_logs.discover_recent_logs(args.log_dir)
    out.set_data("scanned_logs", [os.path.basename(p) for p in logs])

    if logs:
        out.item(f"今日有更新的日志文件 ({len(logs)} 个):")
        for lf in logs:
            try:
                ts = os.path.getmtime(lf)
                ts_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            except OSError:
                ts_str = "?"
            out.item(f"  {os.path.basename(lf)} (mtime: {ts_str})")
    else:
        out.item("今日无更新的日志文件")

    out.line("")

    if logs:
        err_lines = collect_error_lines(logs)
        out.set_data("app_error_count", len(err_lines))
        if err_lines:
            out.item(f"应用日志 ERROR 级别: {len(err_lines)} 条 — Gateway 运行时报错，包括工具失败、模型异常等")
            rendered = []
            for ln in err_lines[:100]:
                r = render_log_line(ln, 300)
                if r:
                    rendered.append(r)
            if len(err_lines) > 100:
                rendered.append(f"... 共 {len(err_lines)} 条")
            out.evidence("近期日志", "\n".join(rendered))
        else:
            out.item("应用日志 ERROR 级别: 0 条 — Gateway 运行时报错")

        api_lines = collect_api_errors(logs)
        out.set_data("api_error_count", len(api_lines))
        if api_lines:
            out.item(f"模型 API HTTP 错误: {len(api_lines)} 条 ")
            rendered = []
            for ln in api_lines[:100]:
                r = render_log_line(ln, 500)
                if r:
                    rendered.append(r)
            out.evidence("近期日志", "\n".join(rendered))
    else:
        out.item("应用日志未找到（今日无更新的日志文件）")

    journal_out = journalctl_errors()
    if journal_out and "No entries" not in journal_out and "no entries" not in journal_out:
        lines = journal_out.splitlines()[:50]
        if lines:
            out.item("Journalctl ERROR 级别:")
            out.evidence("journalctl --priority err", "\n".join(lines))
        out.set_data("journalctl_errors", len(lines))
    else:
        out.item("Journalctl ERROR: 0 条 — 系统级进程错误")
        out.set_data("journalctl_errors", 0)

    recent_session = find_recent_session(args.sessions_base)
    if recent_session:
        counts = tool_errors_from_session(recent_session)
        total = sum(counts.values())
        out.item(f"最近 Session 的工具调用错误: {total} — 工具返回 error 的次数，过多说明某个工具持续异常")
        out.set_data("session_tool_error_count", total)
        if total > 0:
            detail = "; ".join(f"{n}:{c}" for n, c in counts.most_common(10))
            out.evidence(os.path.basename(recent_session), detail)
            out.set_data("session_tool_errors", dict(counts))
    else:
        out.item("未找到 Session 文件，跳过工具调用检查")

    return out.done()


if __name__ == "__main__":
    sys.exit(main())
