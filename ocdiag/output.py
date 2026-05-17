"""Output renderer: human-readable text with optional JSON mode.

Usage:
    out = Output(module="gateway", json_mode=False)
    out.section("模块 4：Gateway 状态")
    out.item("端口 18789: 监听中")
    out.evidence("ss -tlnp", "...")
    out.set_data("port", 18789)  # for JSON mode
    out.done()
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, TextIO


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
        self._lines: List[str] = []
        self._data: Dict[str, Any] = {}
        self._status = "ok"
        self._error_msg: Optional[str] = None

    # ── human-readable ──
    def emit(self, text: str = "") -> None:
        if not self.json_mode:
            self._lines.append(text)

    def section(self, title: str) -> None:
        self.emit("")
        self.emit(f"── {title} ──")
        self.emit("")

    def subsection(self, title: str) -> None:
        self.emit("")
        self.emit(f"  ── {title} ──")
        self.emit("")

    def item(self, text: str) -> None:
        self.emit(f"  • {text}")

    def line(self, text: str = "") -> None:
        self.emit(text)

    def evidence(self, source: str, data: str) -> None:
        self.emit(f"     [{source}]")
        if data is None:
            return
        for raw in str(data).split("\n")[:100]:
            self.emit(f"     {raw}")

    # ── JSON mode ──
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

    # ── finish ──
    def done(self) -> int:
        if self.json_mode:
            payload: Dict[str, Any] = {
                "module": self.module,
                "status": self._status,
                "data": self._data,
            }
            if self._error_msg:
                payload["error"] = self._error_msg
            self.stream.write(json.dumps(payload, ensure_ascii=False))
            self.stream.write("\n")
        else:
            for ln in self._lines:
                self.stream.write(ln + "\n")
        try:
            self.stream.flush()
        except Exception:
            pass
        return 0 if self._status == "ok" else 1


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


def section(title: str) -> None:
    current().section(title)


def item(text: str) -> None:
    current().item(text)


def evidence(source: str, data: str) -> None:
    current().evidence(source, data)
