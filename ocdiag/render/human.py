"""Human / terminal renderer for v2 Reports.

Visually mirrors the v1 output style (heavy bar banner, ✓/⚠/✗ glyphs per
section, footer with verdict summary). Pure structure-driven: it reads
``check.verdict`` directly — no regex inference of any kind.
"""

from __future__ import annotations

import datetime
import sys
import time
import unicodedata
from typing import List, Optional, TextIO

from .. import __version__
from ..core.types import Check, Report, Section, Verdict
from . import ansi


LINE_WIDTH = 76
_HEAVY_BAR = "━" * LINE_WIDTH


def _vwidth(s: str) -> int:
    """Visible terminal width: ANSI stripped, East-Asian wide chars count 2."""
    s = ansi.strip_ansi(s)
    n = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            n += 2
        elif ord(ch) >= 0x1F000:
            n += 2
        else:
            n += 1
    return n


def _now_string() -> str:
    now = datetime.datetime.now().astimezone()
    off = now.utcoffset()
    if off is None:
        tz = "UTC"
    else:
        total = int(off.total_seconds())
        sign = "+" if total >= 0 else "-"
        hh = abs(total) // 3600
        mm = (abs(total) % 3600) // 60
        tz = f"{sign}{hh:02d}" if mm == 0 else f"{sign}{hh:02d}{mm:02d}"
    return now.strftime("%Y-%m-%d %H:%M:%S ") + tz


_GLYPH = {Verdict.OK: "✓", Verdict.WARN: "⚠", Verdict.FAIL: "✗"}
_COLOR = {Verdict.OK: ansi.GREEN, Verdict.WARN: ansi.YELLOW, Verdict.FAIL: ansi.RED}


class HumanRenderer:
    def __init__(
        self,
        no_color: bool = False,
        stream: Optional[TextIO] = None,
    ):
        self.no_color = no_color
        self.stream = stream or sys.stdout

    # ── color helpers ──
    def _use_color(self) -> bool:
        if self.no_color:
            return False
        try:
            return self.stream.isatty()
        except Exception:
            return False

    def _c(self, text: str, code: str) -> str:
        return ansi.colorize(text, code, self._use_color())

    def _bar(self) -> str:
        return self._c(_HEAVY_BAR, ansi.DIM)

    # ── banner ──
    def _banner_lines(self, report: Report) -> List[str]:
        logo = self._c("🦞  OPENCLAW-DIAG", ansi.BOLD_WHITE)
        ver = self._c(f"v{__version__}", ansi.CYAN)
        mod_label = self._c("Module", ansi.DIM)
        time_label = self._c("Time", ansi.DIM)
        mod_id = self._c(report.module_id, ansi.BOLD_WHITE)
        return [
            self._bar(),
            f"  {logo}  ·  {ver}",
            f"  {mod_label}    {mod_id}  ·  {report.title}",
            f"  {time_label}      {_now_string()}",
            self._bar(),
        ]

    # ── verdict line ──
    def _verdict_line(self, report: Report) -> str:
        s = report.summary
        p, w, f, total = s["pass"], s["warn"], s["fail"], s["total"]
        if total == 0:
            return "  " + self._c("·  IDLE", ansi.DIM) + "    模块未产生检查项"
        if f > 0:
            label = (
                self._c(_GLYPH[Verdict.FAIL], ansi.RED)
                + "  "
                + self._c("FAIL", ansi.RED + ansi.BOLD)
            )
            tail = f"{total} 项检查 · {p} 通过 · {w} 警告 · {f} 错误"
        elif w > 0:
            label = (
                self._c(_GLYPH[Verdict.WARN], ansi.YELLOW)
                + "  "
                + self._c("WARN", ansi.YELLOW + ansi.BOLD)
            )
            tail = f"{total} 项检查 · {p} 通过 · {w} 警告 · 0 错误"
        else:
            label = (
                self._c(_GLYPH[Verdict.OK], ansi.GREEN)
                + "  "
                + self._c("PASS", ansi.GREEN + ansi.BOLD)
            )
            tail = f"{total} 项检查 · 全部通过"
        return f"  {label}   {tail}"

    # ── sections ──
    def _section_header(self, section: Section) -> str:
        v = section.verdict
        glyph = self._c(_GLYPH[v], _COLOR[v] + ansi.BOLD)
        title = self._c(section.title, ansi.BOLD_WHITE)
        left = f"  {title}"
        pad = max(1, LINE_WIDTH - _vwidth(left) - _vwidth(glyph))
        return left + (" " * pad) + glyph

    def _check_line(self, check: Check) -> List[str]:
        v = check.verdict
        bullet = self._c(_GLYPH[v], _COLOR[v])
        out = [f"  {bullet} {check.message}"]
        if check.detail:
            for ln in check.detail.split("\n"):
                out.append(f"      {ln}")
        if check.evidence:
            tag = self._c("[evidence]", ansi.DIM)
            out.append(f"     {tag}")
            for raw in check.evidence.split("\n")[:200]:
                out.append(f"     {raw}")
        return out

    def _render_section(self, section: Section) -> List[str]:
        if not section.checks and not section.title:
            return []
        lines: List[str] = []
        if section.title:
            lines.append(self._section_header(section))
            lines.append("")
        for chk in section.checks:
            lines.extend(self._check_line(chk))
        return lines

    # ── footer ──
    def _footer_lines(self, report: Report) -> List[str]:
        s = report.summary
        p, w, f = s["pass"], s["warn"], s["fail"]
        if f > 0:
            tag = self._c(
                f"✗ {f} error" + ("s" if f != 1 else ""), ansi.RED + ansi.BOLD,
            )
        elif w > 0:
            tag = self._c(
                f"⚠ {w} warning" + ("s" if w != 1 else ""), ansi.YELLOW + ansi.BOLD,
            )
        else:
            tag = self._c("✓ all checks passed", ansi.GREEN + ansi.BOLD)
        elapsed_ms = int(report.elapsed_ms)
        if elapsed_ms < 1000:
            elapsed_s = f"{elapsed_ms} ms"
        else:
            elapsed_s = f"{elapsed_ms / 1000:.1f}s"
        run = self._c(f"Run {elapsed_s}", ansi.CYAN)
        tool = self._c("openclaw-diag", ansi.BOLD_WHITE)
        ver = self._c(f"v{__version__}", ansi.DIM)
        return [
            self._bar(),
            f"  {tag}  ·  {run}  ·  {tool} {ver}",
            self._bar(),
        ]

    # ── full render ──
    def render(self, report: Report) -> str:
        lines: List[str] = []
        lines.extend(self._banner_lines(report))
        lines.append(self._verdict_line(report))
        lines.append(self._bar())
        lines.append("")
        for sec in report.sections:
            block = self._render_section(sec)
            if block:
                lines.extend(block)
                lines.append("")
        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")
        lines.extend(self._footer_lines(report))
        if report.error:
            lines.append("")
            lines.append(self._c(f"  ! error: {report.error}", ansi.RED))
        return "\n".join(lines)

    def write(self, report: Report) -> None:
        try:
            self.stream.write(self.render(report) + "\n")
            self.stream.flush()
        except BrokenPipeError:
            try:
                import os
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
            except Exception:
                pass


def render(report: Report, no_color: bool = False) -> str:
    return HumanRenderer(no_color=no_color).render(report)
