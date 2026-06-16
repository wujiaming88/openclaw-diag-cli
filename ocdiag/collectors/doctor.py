"""doctor collector — environment self-check.

Reports four things and rolls them up into a v2 verdict:
  - Node.js (version + presence) — read from ``OCDIAG_NODE_VERSION`` env var
    populated by the Node launcher; absent ⇒ skipped (ok).
  - Python interpreter version (>= 3.8 required).
  - openclaw.json readability.
  - Sessions directory presence.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Optional

from .. import __version__
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict


def _section_node(s: Section) -> None:
    raw = os.environ.get("OCDIAG_NODE_VERSION")
    if not raw:
        # Try probing PATH directly so `python3 -m ocdiag.main doctor` from a
        # shell still gets a verdict on Node.
        node_bin = shutil.which("node")
        if node_bin is None:
            s.ok(
                "doctor.node",
                "Node 检查跳过（OCDIAG_NODE_VERSION 未注入；从 npx 启动以核对版本）",
                data={"skipped": True},
            )
            return
        try:
            r = subprocess.run(
                [node_bin, "--version"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            raw = (r.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired):
            s.warn(
                "doctor.node",
                "Node 二进制无法执行",
                data={"binary": node_bin},
            )
            return
    if not raw:
        s.warn("doctor.node", "Node 版本未知", data={})
        return
    normalized = raw.lstrip("v")
    try:
        major = int(normalized.split(".", 1)[0])
    except (ValueError, IndexError):
        s.warn(
            "doctor.node",
            f"Node 版本无法解析: {raw}",
            data={"raw": raw},
        )
        return
    if major >= 18:
        s.ok(
            "doctor.node",
            f"Node v{normalized} (需要 >=18)",
            data={"version": normalized, "required": ">=18", "ok": True},
        )
    else:
        s.fail(
            "doctor.node",
            f"Node v{normalized} 过低 (需要 >=18)",
            data={"version": normalized, "required": ">=18", "ok": False},
        )


def _section_python(s: Section) -> None:
    v = sys.version_info
    version = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 8):
        s.ok(
            "doctor.python",
            f"Python {version} ({sys.executable})",
            data={"version": version, "executable": sys.executable},
        )
    else:
        s.fail(
            "doctor.python",
            f"Python {version} 过低 (需要 >=3.8)",
            data={"version": version, "executable": sys.executable},
        )


def _section_ocdiag(s: Section) -> None:
    s.ok(
        "doctor.ocdiag",
        f"ocdiag 包可用 (v{__version__})",
        data={"version": __version__},
    )


def _section_openclaw(s: Section, ctx: DiagContext) -> None:
    cfg = ctx.config_path
    if cfg.is_file():
        # Reading via DiagContext caches the parsed json; if it's empty the
        # file existed but failed to parse.
        parsed = ctx.config
        if parsed:
            s.ok(
                "doctor.config",
                f"openclaw.json 可读 ({cfg})",
                data={"path": str(cfg), "readable": True},
            )
        else:
            s.warn(
                "doctor.config",
                f"openclaw.json 存在但解析失败 ({cfg})",
                data={"path": str(cfg), "readable": False},
            )
    else:
        s.warn(
            "doctor.config",
            f"openclaw.json 未找到 ({cfg}) — 安装 OpenClaw 后会自动生成",
            data={"path": str(cfg), "exists": False},
        )

    sessions_base = ctx.sessions_base
    if sessions_base.is_dir():
        s.ok(
            "doctor.sessions",
            f"Sessions 目录存在 ({sessions_base})",
            data={"path": str(sessions_base), "exists": True},
        )
    else:
        s.warn(
            "doctor.sessions",
            f"Sessions 目录未找到 ({sessions_base})",
            data={"path": str(sessions_base), "exists": False},
        )


@register
class DoctorCollector:
    id = "doctor"
    title = "环境自检"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)
        report.add_scope("doctor", "current")

        s_node = report.section("Doctor · Node.js")
        _section_node(s_node)

        s_python = report.section("Doctor · Python")
        _section_python(s_python)

        s_ocdiag = report.section("Doctor · ocdiag")
        _section_ocdiag(s_ocdiag)

        s_openclaw = report.section("Doctor · OpenClaw")
        _section_openclaw(s_openclaw, ctx)

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
