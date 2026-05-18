"""Output renderer: banner + per-subsection verdict + footer.

Public API stays back-compatible with v1: collectors continue to call
``section() / subsection() / item() / line() / evidence()`` plus the JSON
helpers (``set_data`` / ``update_data`` / ``add_data_item`` / ``fail``).

What's new in v2:
  - Every module starts with a heavy 3-row banner and ends with a footer line.
  - Each subsection auto-derives a ✓/⚠/✗ verdict from its accumulated text.
  - Module-level verdict (PASS / WARN / FAIL) is rendered just under the banner.
  - JSON payloads gain top-level ``status`` (already existed), ``summary``
    (pass/warn/fail/total), and ``elapsed_ms`` fields.

Rendering is buffered: events are appended as they arrive, then assembled at
``done()`` so verdicts can look back at every text fragment in their section.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
import unicodedata
from typing import Any, Dict, List, Optional, TextIO, Tuple

from . import __version__


LINE_WIDTH = 76
_HEAVY_BAR = "━" * LINE_WIDTH

# Braille-dot spinner. Single-color, plain unicode — no extra deps.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# ANSI
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_RED = "\x1b[31m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"
_BOLD_WHITE = "\x1b[1;37m"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Lines emitted as raw "── X ──" via line() get auto-promoted to subsections so
# collectors written before subsection() existed still get verdict markers.
_SUBSECTION_LINE_RE = re.compile(r"^\s*──\s+(.+?)\s+──\s*$")

# Verdict keyword detectors. Conservative: default ✓; only escalate when text
# carries a clear non-zero failure / warning signal. Negative phrasing
# ("0 条", "未出现", "近 24h 无") is filtered first so noise like
# "无重启/停止记录" doesn't bleed into WARN.
_NEGATIVE_PATTERNS = [
    r"\b0\s*(?:次|条|个|项|条记录|errors?|warnings?)\b",
    # Plugin-manager style "0 ERROR, 0 WARN, 0 total" lines: uppercase
    # WARN/ERROR with explicit zero count must not be matched by _WARN_RE.
    r"\b0\s+(?:WARN|ERROR|FATAL)\b",
    # All-clear threshold descriptions: "WARN<=20", "ERROR=0", "所有插件 ERROR=0 且 WARN<=20".
    # These describe healthy bounds, not real warnings.
    r"WARN\s*<=\s*\d+",
    r"ERROR\s*=\s*0",
    r"未出现",
    r"未发现",
    r"无异常",
    r"无重启",
    r"无 WS 相关",
    r"无 (?:hook|Hook) 执行异常",
    r"全部通过",
    r"近 \d+h 无",
    r"近 \d+ 天无",
    r"今日无",
    r"无（未配置",
    r"无外部依赖",
]
_NEG_RE = re.compile("|".join(_NEGATIVE_PATTERNS))

_FAIL_RE = re.compile(
    r"FATAL"
    r"|崩溃"
    r"|连续失败\s*(?:[3-9]|\d{2,})"   # 3..9 or 10+
    r"|OOM kill\(\d{0,2}天?内?\):\s*[1-9]"
    r"|检测到\s*[1-9]\d*\s*个\s*stuck"
    r"|JsonFileReadError"
    r"|HTTP\s+5\d\d"
    r"|配置文件未找到"
    r"|jobs\.json 解析失败"
    r"|JSON 语法:\s*无效"
)

_WARN_RE = re.compile(
    r"\bWARN\b|警告"
    r"|\b[1-9]\d*\s*(?:WARN|ERROR|FATAL)\b"  # forward: '5 ERROR', '18 WARN'
    # Reverse Chinese form emitted by recent_errors etc.: 'ERROR 级别: 22 条',
    # 'FATAL 级别: 1 条', 'Journalctl ERROR 级别 ...'
    r"|(?:ERROR|FATAL)\s*级别[^：:]{0,8}[:：]\s*[1-9]"
    # Common Chinese label-then-count phrases
    r"|工具调用错误[:：]\s*[1-9]"
    # Chinese failure/error phrases used by performance / shell_history etc.
    r"|\b失败[:：]\s*\S"               # "失败: <something>"
    r"|共\s*[1-9]\d*\s*次错误"      # "共 3 次错误"
    r"|错误数[:：]\s*[1-9]"             # "错误数: 3"
    r"|连续失败"
    r"|HTTP\s+4\d\d"
    # Note: model stopReason "aborted" / "stop=aborted" is a normal stopReason
    # field value (user-initiated cancel of a single model call) and not a
    # system warning. Do not match those here — collectors that need to
    # surface real abort-related anomalies should emit a clear text signal
    # (e.g. "频繁中止: N 次").
    r"|model fallback decision"
    r"|disabling CardKit"
    r"|高危命令:\s*[1-9]"
    r"|gateway closed \([^0]\d{2,3}\)"  # 1006/1011/1012 etc, not 1000
    r"|gateway timeout"
    r"|FailoverError"
    r"|超时未"
    r"|failed:\s*\S"
    r"|FAILED \(timeout"
    r"|未安装"
    r"|未运行"
    r"|无法读取"
    r"|无法解析"
    r"|读取失败"
    r"|解析失败"
    r"|连接失败"
    r"|总异常:\s*[1-9]"
    r"|异常:\s*[1-9]\d*\s*个"
    r"|cache read dropped"
    r"|deprecated"
)

_TS_RE = re.compile(r"^\[?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _vwidth(s: str) -> int:
    """Visible terminal width: ANSI stripped, East-Asian wide chars count 2."""
    s = _strip_ansi(s)
    n = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            n += 2
        elif ord(ch) >= 0x1F000:
            n += 2  # most BMP-supplement emojis render double-wide
        else:
            n += 1
    return n


def _verdict_glyph(v: str) -> str:
    return {"ok": "✓", "warn": "⚠", "fail": "✗"}.get(v, "✓")


def _verdict_ansi(v: str) -> str:
    return {"ok": _GREEN, "warn": _YELLOW, "fail": _RED}.get(v, _GREEN)


def _quiet_exit_on_broken_pipe() -> None:
    """When the downstream pipe (head/grep/less) closes early, redirect stdout
    to /dev/null so subsequent writes do not raise, then return cleanly.

    Mirrors Python's recommended SIGPIPE handling for CLIs.
    """
    try:
        import os as _os
        devnull = _os.open(_os.devnull, _os.O_WRONLY)
        _os.dup2(devnull, sys.stdout.fileno())
    except Exception:
        pass


def _now_string() -> str:
    """Local time with offset, e.g. ``2026-05-18 12:54:00 +08``."""
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


# ── Event types (internal) ──
# ("banner",)               module banner placeholder
# ("subsection", title)     start a new subsection
# ("item", text)            bullet item
# ("rawline", text)         raw line, no bullet
# ("blank",)                blank line
# ("evidence", source, data)


class Output:
    def __init__(
        self,
        module: str,
        json_mode: bool = False,
        no_color: bool = False,
        stream: Optional[TextIO] = None,
    ):
        self.module = module
        self.json_mode = json_mode
        self.no_color = no_color
        self.stream = stream or sys.stdout
        self._events: List[Tuple] = []
        self._title_zh: Optional[str] = None
        self._data: Dict[str, Any] = {}
        self._status = "ok"
        self._error_msg: Optional[str] = None
        self._t0 = time.time()
        # Spinner state for progress(). Tracked per-Output so concurrent
        # collectors (unused today, but cheap to support) don't share frames.
        self._spinner_idx = 0
        self._progress_max_width = 0

    # ── color helper ──
    def _use_color(self) -> bool:
        if self.no_color:
            return False
        try:
            return self.stream.isatty()
        except Exception:
            return False

    def _c(self, text: str, code: str) -> str:
        if not self._use_color():
            return text
        return f"{code}{text}{_RESET}"

    # ── public API (back-compat) ──
    def emit(self, text: str = "") -> None:
        self.line(text)

    def section(self, title: str) -> None:
        # Captures the module title for the banner row.
        clean = title.strip()
        # Strip a leading "模块 N：" prefix; the module id is already rendered
        # by the banner so we don't repeat it.
        m = re.match(r"^模块\s*\d+\s*[:：]\s*(.+)$", clean)
        self._title_zh = m.group(1).strip() if m else clean
        self._events.append(("banner",))

    def subsection(self, title: str) -> None:
        self._events.append(("subsection", title.strip()))

    def item(self, text: str) -> None:
        self._events.append(("item", text))

    def line(self, text: str = "") -> None:
        if not text or not text.strip():
            self._events.append(("blank",))
            return
        m = _SUBSECTION_LINE_RE.match(text)
        if m:
            self._events.append(("subsection", m.group(1).strip()))
            return
        self._events.append(("rawline", text))

    def evidence(self, source: str, data: str) -> None:
        self._events.append(("evidence", source, data))

    # ── JSON helpers ──
    def set_data(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update_data(self, mapping: Dict[str, Any]) -> None:
        self._data.update(mapping)

    def add_data_item(self, key: str, value: Any) -> None:
        if key not in self._data or not isinstance(self._data[key], list):
            self._data[key] = []
        self._data[key].append(value)

    def fail(self, message: str) -> None:
        self._status = "error"
        self._error_msg = message

    # ── progress indicator ──
    def progress(self, step: int, total: int, label: str) -> None:
        """Render a transient progress line on stderr.

        Silent when stderr is not a TTY or when JSON mode is active. Uses
        \\r to overwrite the prior frame so collectors that update progress
        rapidly do not flood the terminal. Cleared by done() before the
        final banner is written to stdout.
        """
        if self.json_mode:
            return
        try:
            if not sys.stderr.isatty():
                return
        except Exception:
            return
        frame = _SPINNER_FRAMES[self._spinner_idx % len(_SPINNER_FRAMES)]
        self._spinner_idx += 1
        line = f"  {frame} [{step}/{total}] {label}"
        width = _vwidth(line)
        if width > self._progress_max_width:
            self._progress_max_width = width
        try:
            sys.stderr.write("\r" + line)
            sys.stderr.flush()
        except (BrokenPipeError, OSError):
            pass

    def _clear_progress(self) -> None:
        """Erase the current progress line from stderr, if any was written."""
        if self._progress_max_width <= 0:
            return
        try:
            if not sys.stderr.isatty():
                return
        except Exception:
            return
        try:
            sys.stderr.write("\r" + (" " * self._progress_max_width) + "\r")
            sys.stderr.flush()
        except (BrokenPipeError, OSError):
            pass
        self._progress_max_width = 0

    # ── verdict / grouping ──
    def _group_subsections(self) -> List[Dict[str, Any]]:
        """Walk events and split them into subsection blocks.

        The first block (before any explicit subsection) is the "intro" block
        with title=None — it still contributes to module verdict but renders
        without a header line.
        """
        sections: List[Dict[str, Any]] = []
        current: Dict[str, Any] = {"title": None, "events": []}
        for ev in self._events:
            kind = ev[0]
            if kind == "banner":
                continue
            if kind == "subsection":
                if current["title"] is not None or current["events"]:
                    sections.append(current)
                current = {"title": ev[1], "events": []}
                continue
            current["events"].append(ev)
        if current["title"] is not None or current["events"]:
            sections.append(current)
        return sections

    def _section_text(self, section: Dict[str, Any]) -> str:
        parts: List[str] = []
        for ev in section["events"]:
            if ev[0] in ("item", "rawline"):
                parts.append(ev[1])
            elif ev[0] == "evidence":
                parts.append(ev[2] or "")
        return "\n".join(parts)

    def _verdict_for_section(self, section: Dict[str, Any]) -> str:
        # Per-line evaluation. Negative tokens (zero-counts, "未出现", etc.)
        # are *stripped* rather than short-circuiting the line, so a mixed
        # line like 'tools: 16 ERROR, 0 WARN, 16 total' still surfaces the
        # nonzero ERROR signal.
        text = self._section_text(section)
        worst = "ok"
        for raw in text.split("\n"):
            line = raw.strip()
            if not line:
                continue
            # Strip out only the negative tokens; what remains is the
            # "non-clean" portion of the line that should be evaluated.
            stripped = _NEG_RE.sub("", line)
            if not stripped.strip():
                # Whole line was zero-counts / pure negative phrasing.
                continue
            if _FAIL_RE.search(stripped):
                return "fail"
            if _WARN_RE.search(stripped):
                worst = "warn"
        return worst

    # ── rendering ──
    def _bar_line(self) -> str:
        return self._c(_HEAVY_BAR, _DIM) if self._use_color() else _HEAVY_BAR

    def _banner_lines(self) -> List[str]:
        title = self._title_zh or self.module
        logo = self._c(f"🦞  OPENCLAW-DIAG", _BOLD_WHITE)
        ver = self._c(f"v{__version__}", _CYAN)
        # Row 1
        row1 = f"  {logo}  ·  {ver}"
        # Row 2: module id + zh title
        mod_label = self._c("Module", _DIM)
        time_label = self._c("Time", _DIM)
        mod_id = self._c(self.module, _BOLD_WHITE)
        row2 = f"  {mod_label}    {mod_id}  ·  {title}"
        row3 = f"  {time_label}      {_now_string()}"
        return [self._bar_line(), row1, row2, row3, self._bar_line()]

    def _verdict_line(self, p: int, w: int, f: int, total: int) -> str:
        if total == 0:
            return self._pad("  " + self._c("·  IDLE", _DIM) +
                              "    模块未产生检查项")
        if f > 0:
            label = self._c(_verdict_glyph("fail"), _RED) + "  " + \
                    self._c("FAIL", _RED + _BOLD)
            tail = f"{total} 项检查 · {p} 通过 · {w} 警告 · {f} 错误"
        elif w > 0:
            label = self._c(_verdict_glyph("warn"), _YELLOW) + "  " + \
                    self._c("WARN", _YELLOW + _BOLD)
            tail = f"{total} 项检查 · {p} 通过 · {w} 警告 · 0 错误"
        else:
            label = self._c(_verdict_glyph("ok"), _GREEN) + "  " + \
                    self._c("PASS", _GREEN + _BOLD)
            tail = f"{total} 项检查 · 全部通过"
        return f"  {label}   {tail}"

    def _pad(self, s: str) -> str:
        return s

    def _format_subsection_header(self, title: str, verdict: str) -> str:
        glyph = _verdict_glyph(verdict)
        glyph_c = self._c(glyph, _verdict_ansi(verdict) + _BOLD)
        title_c = self._c(title, _BOLD_WHITE)
        left = f"  {title_c}"
        # pad to LINE_WIDTH so the glyph hugs the right edge
        pad = max(1, LINE_WIDTH - _vwidth(left) - _vwidth(glyph))
        return left + (" " * pad) + glyph_c

    def _format_item(self, text: str) -> str:
        # Preserve original leading whitespace if any; strip a single leading
        # space to keep the bullet aligned.
        if text.startswith("  ") and not text.startswith("  •"):
            # Indented sub-item (e.g. cron's "  [1] ..."): keep as-is, just
            # indent under the subsection.
            return f"  {text}"
        bullet = self._c("·", _DIM)
        return f"  {bullet} {text}"

    def _format_evidence(self, source: str, data: str) -> List[str]:
        out: List[str] = []
        tag = self._c(f"[{source}]", _DIM)
        out.append(f"     {tag}")
        if data is None:
            return out
        for raw in str(data).split("\n")[:200]:
            # Highlight ISO timestamps inside evidence lines
            if self._use_color() and _TS_RE.match(raw.lstrip()):
                out.append(self._c(f"     {raw}", _DIM))
            else:
                out.append(f"     {raw}")
        return out

    def _format_rawline(self, text: str) -> str:
        # Pass through but strip pre-existing top-level "section" decorators
        # ("── ... ──" already promoted to subsection) and keep alignment.
        return text

    def _render_subsection(
        self, section: Dict[str, Any], verdict: str
    ) -> List[str]:
        lines: List[str] = []
        if section["title"] is not None:
            lines.append(self._format_subsection_header(section["title"], verdict))
            lines.append("")
        prev_blank = True
        for ev in section["events"]:
            kind = ev[0]
            if kind == "blank":
                if not prev_blank:
                    lines.append("")
                    prev_blank = True
                continue
            if kind == "item":
                lines.append(self._format_item(ev[1]))
            elif kind == "rawline":
                lines.append(self._format_rawline(ev[1]))
            elif kind == "evidence":
                lines.extend(self._format_evidence(ev[1], ev[2]))
            prev_blank = False
        # Trim trailing blanks within a section
        while lines and lines[-1] == "":
            lines.pop()
        return lines

    def _footer_lines(self, elapsed_ms: int, p: int, w: int, f: int) -> List[str]:
        if f > 0:
            tag = self._c(f"✗ {f} error" + ("s" if f != 1 else ""), _RED + _BOLD)
        elif w > 0:
            tag = self._c(f"⚠ {w} warning" + ("s" if w != 1 else ""),
                           _YELLOW + _BOLD)
        else:
            tag = self._c("✓ all checks passed", _GREEN + _BOLD)
        if elapsed_ms < 1000:
            elapsed_s = f"{elapsed_ms} ms"
        else:
            elapsed_s = f"{elapsed_ms / 1000:.1f}s"
        run = self._c(f"Run {elapsed_s}", _CYAN)
        tool = self._c(f"openclaw-diag", _BOLD_WHITE)
        ver = self._c(f"v{__version__}", _DIM)
        body = f"  {tag}  ·  {run}  ·  {tool} {ver}"
        return [self._bar_line(), body, self._bar_line()]

    def _render_full(self) -> List[str]:
        lines: List[str] = []
        lines.extend(self._banner_lines())
        sections = self._group_subsections()
        # Compute per-section verdicts
        verdicts = [self._verdict_for_section(s) for s in sections]
        # If a section has no items at all, skip it from the verdict tally so
        # decorative-only blocks don't inflate "全部通过".
        counted = [
            v for s, v in zip(sections, verdicts)
            if any(ev[0] in ("item", "rawline", "evidence") for ev in s["events"])
        ]
        passed = sum(1 for v in counted if v == "ok")
        warned = sum(1 for v in counted if v == "warn")
        failed = sum(1 for v in counted if v == "fail")
        total = len(counted)
        lines.append(self._verdict_line(passed, warned, failed, total))
        lines.append(self._bar_line())
        lines.append("")
        for s, v in zip(sections, verdicts):
            block = self._render_subsection(s, v)
            if block:
                lines.extend(block)
                lines.append("")
        # drop trailing blank before footer
        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")
        elapsed_ms = int((time.time() - self._t0) * 1000)
        lines.extend(self._footer_lines(elapsed_ms, passed, warned, failed))
        # Promote module status if the verdict pushed it past "ok".
        if self._status == "ok" and failed > 0:
            self._status = "error"
        elif self._status == "ok" and warned > 0:
            self._status = "warn"
        self._summary = {
            "pass": passed, "warn": warned, "fail": failed, "total": total,
        }
        self._elapsed_ms = elapsed_ms
        return lines

    # ── finish ──
    def done(self) -> int:
        # Wipe any in-flight progress line on stderr before we render banner /
        # JSON to stdout, so the user sees clean output (no leftover spinner).
        self._clear_progress()
        if not self._title_zh:
            self._title_zh = self.module
        if self.json_mode:
            # Re-use grouping to produce summary even in JSON mode.
            sections = self._group_subsections()
            verdicts = [self._verdict_for_section(s) for s in sections]
            counted = [
                v for s, v in zip(sections, verdicts)
                if any(ev[0] in ("item", "rawline", "evidence") for ev in s["events"])
            ]
            p = sum(1 for v in counted if v == "ok")
            w = sum(1 for v in counted if v == "warn")
            f = sum(1 for v in counted if v == "fail")
            elapsed_ms = int((time.time() - self._t0) * 1000)
            # Compute fine-grained verdict (ok | warn | fail) and a backward
            # compatible legacy status (ok | error). Existing jq pipelines
            # that gate on `status != "ok"` keep working: warnings stay "ok"
            # at the legacy field, escalating to "error" only on real fail.
            inferred_verdict: str
            if self._status in ("error", "fail"):
                inferred_verdict = "fail"
            elif f > 0:
                inferred_verdict = "fail"
            elif w > 0:
                inferred_verdict = "warn"
            else:
                inferred_verdict = "ok"
            legacy_status = "error" if inferred_verdict == "fail" else "ok"
            # Mirror onto self._status so the exit code path also sees it.
            self._status = legacy_status
            payload: Dict[str, Any] = {
                "module": self.module,
                "status": legacy_status,         # backward-compat: ok | error
                "verdict": inferred_verdict,     # new 3-state: ok | warn | fail
                "summary": {"pass": p, "warn": w, "fail": f, "total": len(counted)},
                "elapsed_ms": elapsed_ms,
                "data": self._data,
            }
            if self._error_msg:
                payload["error"] = self._error_msg
            try:
                self.stream.write(json.dumps(payload, ensure_ascii=False))
                self.stream.write("\n")
            except BrokenPipeError:
                # Downstream (head/grep/less) closed stdout. Exit cleanly.
                _quiet_exit_on_broken_pipe()
        else:
            try:
                for ln in self._render_full():
                    self.stream.write(ln + "\n")
            except BrokenPipeError:
                _quiet_exit_on_broken_pipe()
        try:
            self.stream.flush()
        except (BrokenPipeError, OSError):
            pass
        # Exit code semantics:
        #   ok   -> 0  (clean)
        #   warn -> 0  (informational, must not fail CI / `openclaw-diag all`)
        #   fail -> 1  (real error)
        # An explicit fail() call wins regardless of inferred verdict.
        return 1 if self._status in ("error", "fail") else 0


# Module-level convenience for scripts that just want functional API.
_active: Optional[Output] = None


def init(module: str, json_mode: bool = False, no_color: bool = False) -> Output:
    global _active
    _active = Output(module, json_mode=json_mode, no_color=no_color)
    return _active


def current() -> Output:
    if _active is None:
        raise RuntimeError("output.init() must be called first")
    return _active


def emit(text: str = "") -> None:
    current().emit(text)


def progress(step: int, total: int, label: str) -> None:
    current().progress(step, total, label)


# ── helpers for object inspectors that render their own text ──

def _isatty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except Exception:
        return False


def render_banner(
    module_id: str,
    title_zh: str,
    *,
    no_color: bool = False,
    stream: Optional[TextIO] = None,
) -> str:
    """Banner rendering decoupled from the Output buffer — used by trace and
    extract since they keep their own line-by-line formatters.
    """
    s = stream or sys.stdout
    use_color = (not no_color) and _isatty(s)

    def c(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if use_color else text

    bar = c(_HEAVY_BAR, _DIM) if use_color else _HEAVY_BAR
    logo = c("🦞  OPENCLAW-DIAG", _BOLD_WHITE)
    ver = c(f"v{__version__}", _CYAN)
    mod_label = c("Module", _DIM)
    time_label = c("Time", _DIM)
    mod_id = c(module_id, _BOLD_WHITE)
    rows = [
        bar,
        f"  {logo}  ·  {ver}",
        f"  {mod_label}    {mod_id}  ·  {title_zh}",
        f"  {time_label}      {_now_string()}",
        bar,
    ]
    return "\n".join(rows)


def render_footer(
    elapsed_ms: int,
    *,
    status: str = "ok",
    no_color: bool = False,
    stream: Optional[TextIO] = None,
) -> str:
    s = stream or sys.stdout
    use_color = (not no_color) and _isatty(s)

    def c(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if use_color else text

    bar = c(_HEAVY_BAR, _DIM) if use_color else _HEAVY_BAR
    if status == "fail":
        tag = c("✗ inspection failed", _RED + _BOLD)
    elif status == "warn":
        tag = c("⚠ inspection complete", _YELLOW + _BOLD)
    else:
        tag = c("✓ inspection complete", _GREEN + _BOLD)
    if elapsed_ms < 1000:
        elapsed_s = f"{elapsed_ms} ms"
    else:
        elapsed_s = f"{elapsed_ms / 1000:.1f}s"
    run = c(f"Run {elapsed_s}", _CYAN)
    tool = c("openclaw-diag", _BOLD_WHITE)
    ver = c(f"v{__version__}", _DIM)
    return "\n".join([bar, f"  {tag}  ·  {run}  ·  {tool} {ver}", bar])


def section(title: str) -> None:
    current().section(title)


def item(text: str) -> None:
    current().item(text)


def evidence(source: str, data: str) -> None:
    current().evidence(source, data)
