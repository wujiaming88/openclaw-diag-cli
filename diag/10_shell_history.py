#!/usr/bin/env python3
"""模块 10：采集 shell 历史（高危命令、openclaw 命令、最近 20 条）。"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output
from ocdiag.sensitive import sanitize_text


DANGEROUS_RE = re.compile(
    r"rm\s+.*-rf|kill\s+.*-9|shutdown|reboot|mkfs|dd\s+if=|>\s*/dev/sd|"
    r"pkill|killall|iptables\s+-F|ufw\s+disable|chmod\s+777",
    re.IGNORECASE,
)
SCTL_DANGEROUS_RE = re.compile(r"systemctl\s+(stop|disable)", re.IGNORECASE)
OC_RE = re.compile(r"openclaw|oc ", re.IGNORECASE)


def list_history_files() -> List[str]:
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".bash_history"),
        os.path.join(home, ".zsh_history"),
    ]
    if home != "/root":
        candidates.append("/root/.bash_history")
    return [c for c in candidates if os.path.isfile(c)]


def read_lines(path: str) -> Tuple[List[Tuple[int, str]], str]:
    """Read history file. Returns (lines, error_str). error_str=='' on success.

    Permission denied / missing files become an explicit error instead of an
    empty list, so the caller can distinguish "no commands" from "couldn't read".
    """
    out: List[Tuple[int, str]] = []
    try:
        with open(path, "r", errors="replace") as f:
            for i, line in enumerate(f, 1):
                out.append((i, line.rstrip("\n")))
        return out, ""
    except OSError as e:
        return out, f"{type(e).__name__}: {e}"


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 10：采集 shell 历史",
        prog="10_shell_history",
    )
    args = parser.parse_args()

    out = output.init("shell_history", json_mode=args.json, no_color=args.no_color)
    out.section("模块 10：命令执行历史")

    def maybe_sanitize(s: str) -> str:
        return s if args.unmask else sanitize_text(s)

    out.line("  系统 shell 历史记录，用于判断是否有人或脚本执行过高危命令"
             "（rm -rf、kill、systemctl stop 等）。")
    out.line("")

    history_files = list_history_files()
    if not history_files:
        out.item("未找到 shell 历史文件 (.bash_history / .zsh_history)")
        out.set_data("history_files", [])
        return out.done()

    files_data = []
    for hfile in history_files:
        lines, read_err = read_lines(hfile)
        if read_err:
            out.item(f"{os.path.basename(hfile)} — 读取失败 ({read_err})")
            files_data.append({
                "path": hfile,
                "found": False,
                "reason": "unreadable",
                "error": read_err,
            })
            continue
        total = len(lines)
        out.item(f"{os.path.basename(hfile)} — 共 {total} 条记录")

        dangerous: List[Tuple[int, str]] = []
        for n, ln in lines:
            if DANGEROUS_RE.search(ln) and "openclaw" not in ln.lower():
                dangerous.append((n, ln))
            elif SCTL_DANGEROUS_RE.search(ln) and "openclaw" not in ln.lower():
                dangerous.append((n, ln))

        if dangerous:
            out.item(f"  高危命令: {len(dangerous)} 条 ")
            ev = "\n".join(f"{n}: {maybe_sanitize(ln)}" for n, ln in dangerous)
            out.evidence(f"{hfile} (高危)", ev)
        else:
            out.item("  高危命令: 0 条")

        oc_all = [(n, ln) for n, ln in lines if OC_RE.search(ln)]
        oc_total = len(oc_all)
        oc_cmds = oc_all[-30:]
        if oc_total:
            out.item(
                f"  OpenClaw 相关命令: 全文 {oc_total} 条，最近 30 条采样 {len(oc_cmds)} 条 — "
                "用户手动执行的 openclaw 命令"
            )
            ev = "\n".join(f"{n}: {maybe_sanitize(ln)}" for n, ln in oc_cmds)
            out.evidence(f"{hfile} (openclaw)", ev)
        else:
            out.item("  OpenClaw 相关命令: 0 条")

        recent = lines[-20:]
        if recent:
            out.item("  最近 20 条命令:")
            ev = "\n".join(maybe_sanitize(ln) for _, ln in recent)
            out.evidence(f"{hfile} (最近)", ev)

        files_data.append({
            "path": hfile,
            "total_lines": total,
            "dangerous_count": len(dangerous),
            "dangerous": [{"line": n, "cmd": maybe_sanitize(ln)} for n, ln in dangerous],
            "openclaw_count_total": oc_total,
            "openclaw_count_sample_30": len(oc_cmds),
            "recent_count": len(recent),
        })

    out.set_data("history_files", files_data)
    return out.done()


if __name__ == "__main__":
    sys.exit(main())
