"""shell_history collector — scan shell history for dangerous commands."""

from __future__ import annotations

import os
import re
import time
from typing import List, Tuple

from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report
from ..sensitive import sanitize_text


_DANGEROUS_RE = re.compile(
    r"rm\s+.*-rf|kill\s+.*-9|shutdown|reboot|mkfs|dd\s+if=|>\s*/dev/sd|"
    r"pkill|killall|iptables\s+-F|ufw\s+disable|chmod\s+777",
    re.IGNORECASE,
)
_SCTL_DANGEROUS_RE = re.compile(r"systemctl\s+(stop|disable)", re.IGNORECASE)
_OC_RE = re.compile(r"openclaw|oc ", re.IGNORECASE)


def _list_history_files() -> List[str]:
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".bash_history"),
        os.path.join(home, ".zsh_history"),
    ]
    if home != "/root":
        candidates.append("/root/.bash_history")
    return [c for c in candidates if os.path.isfile(c)]


def _read_lines(path: str) -> Tuple[List[Tuple[int, str]], str]:
    out: List[Tuple[int, str]] = []
    try:
        with open(path, "r", errors="replace") as f:
            for i, line in enumerate(f, 1):
                out.append((i, line.rstrip("\n")))
        return out, ""
    except OSError as e:
        return out, f"{type(e).__name__}: {e}"


@register
class ShellHistoryCollector:
    id = "shell_history"
    title = "命令执行历史"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)
        unmask = ctx.unmask

        def maybe_sanitize(s: str) -> str:
            return s if unmask else sanitize_text(s)

        history_files = _list_history_files()
        s_intro = report.section("10.1 历史文件")
        if not history_files:
            s_intro.warn(
                "history.none",
                "未找到 shell 历史文件 (.bash_history / .zsh_history)",
                data={"history_files": []},
            )
            report.data["history_files"] = []
            report.add_scope("shell_history", "full", "0 files")
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        files_data = []
        for hfile in history_files:
            sec = report.section(f"10.x {os.path.basename(hfile)}")
            lines, read_err = _read_lines(hfile)
            if read_err:
                sec.warn(
                    "history.read",
                    f"{os.path.basename(hfile)} — 读取失败 ({read_err})",
                    data={"path": hfile, "error": read_err},
                )
                files_data.append({
                    "path": hfile,
                    "found": False,
                    "reason": "unreadable",
                    "error": read_err,
                })
                continue

            total = len(lines)
            sec.ok(
                "history.total",
                f"{os.path.basename(hfile)} — 共 {total} 条记录",
                data={"path": hfile, "total_lines": total},
            )

            dangerous: List[Tuple[int, str]] = []
            for n, ln in lines:
                if _DANGEROUS_RE.search(ln) and "openclaw" not in ln.lower():
                    dangerous.append((n, ln))
                elif _SCTL_DANGEROUS_RE.search(ln) and "openclaw" not in ln.lower():
                    dangerous.append((n, ln))

            if dangerous:
                ev = "\n".join(
                    f"{n}: {maybe_sanitize(ln)}" for n, ln in dangerous
                )
                sec.warn(
                    "history.dangerous",
                    f"高危命令: {len(dangerous)} 条",
                    evidence=ev,
                    data={
                        "count": len(dangerous),
                        "items": [
                            {"line": n, "cmd": maybe_sanitize(ln)}
                            for n, ln in dangerous
                        ],
                    },
                )
            else:
                sec.ok(
                    "history.dangerous",
                    "高危命令: 0 条",
                    data={"count": 0},
                )

            oc_all = [(n, ln) for n, ln in lines if _OC_RE.search(ln)]
            oc_total = len(oc_all)
            oc_cmds = oc_all[-30:]
            if oc_total:
                ev = "\n".join(
                    f"{n}: {maybe_sanitize(ln)}" for n, ln in oc_cmds
                )
                sec.ok(
                    "history.openclaw",
                    f"OpenClaw 相关命令: 全文 {oc_total} 条，最近 30 条采样 {len(oc_cmds)} 条",
                    evidence=ev,
                    data={
                        "total": oc_total,
                        "sampled": len(oc_cmds),
                    },
                )
            else:
                sec.ok(
                    "history.openclaw",
                    "OpenClaw 相关命令: 0 条",
                    data={"total": 0},
                )

            recent = lines[-20:]
            if recent:
                ev = "\n".join(maybe_sanitize(ln) for _, ln in recent)
                sec.ok(
                    "history.recent",
                    f"最近 {len(recent)} 条命令",
                    evidence=ev,
                    data={"count": len(recent)},
                )

            files_data.append({
                "path": hfile,
                "total_lines": total,
                "dangerous_count": len(dangerous),
                "dangerous": [
                    {"line": n, "cmd": maybe_sanitize(ln)}
                    for n, ln in dangerous
                ],
                "openclaw_count_total": oc_total,
                "openclaw_count_sample_30": len(oc_cmds),
                "recent_count": len(recent),
            })

        report.data["history_files"] = files_data
        total_lines = sum(
            (fd.get("total_lines") or 0) for fd in files_data
        )
        report.add_scope(
            "shell_history", "full",
            f"{len(files_data)} files, {total_lines} lines",
        )
        report.elapsed_ms = (time.time() - t0) * 1000
        return report
