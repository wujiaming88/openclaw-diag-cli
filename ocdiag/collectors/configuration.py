"""configuration collector — flatten openclaw.json with sensitive masking."""

from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional

from .. import trajectory
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section
from ..sensitive import is_sensitive_key, mask


_INSTALLS_RE = re.compile(r"^plugins\.installs\.[^.]+\.([^.]+)$")


def _format_value(val) -> str:
    if isinstance(val, str):
        return val if val else '""'
    if isinstance(val, bool):
        return "true" if val else "false"
    if val is None:
        return "null"
    return str(val)


def _emit_config(out: List[str], obj, prefix: str = "") -> None:
    if isinstance(obj, dict):
        if not obj:
            out.append(f"{prefix} = {{}}")
            return
        for k, v in obj.items():
            new_key = f"{prefix}.{k}" if prefix else k
            m = _INSTALLS_RE.match(new_key)
            if m and m.group(1) not in ("source", "version", "installPath"):
                continue
            if (
                new_key.startswith("models.providers.")
                and k == "models"
                and isinstance(v, list)
            ):
                for md in v:
                    if isinstance(md, dict):
                        mid = md.get("id", "?")
                        cw = md.get("contextWindow", "?")
                        mt = md.get("maxTokens", "?")
                        r_str = "true" if md.get("reasoning", False) else "false"
                        out.append(
                            f"{new_key}.{mid} = (contextWindow={cw}, "
                            f"maxTokens={mt}, reasoning={r_str})"
                        )
                continue
            _emit_config(out, v, new_key)
    elif isinstance(obj, list):
        if not obj:
            out.append(f"{prefix} = []")
            return
        all_scalar = all(isinstance(x, (str, int, float, bool)) for x in obj)
        if all_scalar:
            n = len(obj)
            if n <= 5:
                out.append(
                    f"{prefix} = [{n} 项]: "
                    + ", ".join(str(x) for x in obj)
                )
            else:
                out.append(
                    f"{prefix} = [{n} 项]: "
                    + ", ".join(str(x) for x in obj[:3])
                    + ", ... (完整列表省略)"
                )
        else:
            for i, v in enumerate(obj):
                _emit_config(out, v, f"{prefix}[{i}]")
    else:
        if is_sensitive_key(prefix):
            if isinstance(obj, str) and obj:
                display = mask(obj)
            elif isinstance(obj, (int, float, bool)):
                display = str(obj)
            elif obj is None:
                display = "null"
            else:
                display = "****"
        else:
            display = _format_value(obj)
        if len(str(display)) > 500:
            display = str(display)[:500] + "..."
        out.append(f"{prefix} = {display}")


def _section_trajectory_runtime_config(s: Section, ctx: DiagContext) -> dict:
    data: dict = {}
    files = ctx.trajectory_files()
    if not files:
        s.ok("trajectory.runtime", "未发现 trajectory 文件 — 跳过")
        return data
    runs = ctx.collect_runs()
    runs.sort(key=lambda r: r.started_ts_ms, reverse=True)
    if not runs:
        s.ok("trajectory.runtime", "最近无 trajectory run — 跳过")
        return data
    latest = runs[0]
    rt_cfg = latest.runtime_config or {}
    data["trajectory_runtime_config"] = rt_cfg

    detail_lines = [
        f"最新 run（{latest.session_id[:8]}#{latest.run_id[:8]}）的 runtime config:",
    ]
    if not rt_cfg:
        detail_lines.append("  （trace.metadata.config.runtime 为空）")
    else:
        for k in sorted(rt_cfg.keys()):
            v = rt_cfg[k]
            sv = str(v)
            if len(sv) > 200:
                sv = sv[:200] + "..."
            detail_lines.append(f"  {k} = {sv}")

    snapshots: dict = {}
    for r in runs[:50]:
        sv = r.skills_snapshot_version
        if sv is None:
            continue
        snapshots[sv] = snapshots.get(sv, 0) + 1
    data["skills_snapshot_versions"] = [
        {"snapshotVersion": k, "count": v} for k, v in snapshots.items()
    ]
    if len(snapshots) > 1:
        for sv, cnt in sorted(snapshots.items(), key=lambda x: -x[1])[:5]:
            detail_lines.append(f"  skills.snapshotVersion {sv}: {cnt} 个 run")
        s.warn(
            "trajectory.snapshot_drift",
            f"skills.snapshotVersion 漂移（最近 50 run 中 {len(snapshots)} 个不同版本）",
            detail="\n".join(detail_lines),
            data={"snapshots": snapshots},
        )
    elif snapshots:
        sv = next(iter(snapshots))
        detail_lines.append(
            f"  skills.snapshotVersion: {sv}（最近 50 run 一致）",
        )
        s.ok(
            "trajectory.snapshot",
            f"skills.snapshotVersion 一致: {sv}",
            detail="\n".join(detail_lines),
            data={"snapshotVersion": sv},
        )
    else:
        s.ok(
            "trajectory.runtime",
            "已加载最新 run 的 runtime config",
            detail="\n".join(detail_lines),
        )
    return data


@register
class ConfigurationCollector:
    id = "configuration"
    title = "配置"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)
        config_path = str(ctx.config_path)
        report.data["config_path"] = config_path
        report.add_scope("config", "current")

        s_load = report.section("3.1 加载")
        if not os.path.isfile(config_path):
            s_load.fail(
                "config.found",
                f"配置文件未找到: {config_path}",
                detail=(
                    "下一步：\n"
                    "  1) 确认 OpenClaw 已经初始化（运行过 `openclaw` 即会生成配置）\n"
                    "  2) 用 OPENCLAW_CONFIG=/path/to/openclaw.json 或 --config 指向正确路径\n"
                    "  3) 在容器/远端诊断时，用 OPENCLAW_HOME=/path 整体覆盖"
                ),
                evidence=f"{config_path}: <文件缺失>",
                data={"path": config_path},
            )
            report.data["found"] = False
            report.error = "配置文件未找到"
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        report.data["found"] = True
        try:
            with open(config_path) as f:
                config = json.load(f)
            s_load.ok(
                "config.json_valid",
                "JSON 语法: 有效",
                data={"path": config_path},
            )
            report.data["json_valid"] = True
        except (json.JSONDecodeError, OSError) as e:
            s_load.fail(
                "config.json_valid",
                "JSON 语法: 无效",
                evidence=f"{config_path}: {e}",
                data={"error": str(e)},
            )
            report.data["json_valid"] = False
            report.data["parse_error"] = str(e)
            report.error = "配置 JSON 无效"
            report.elapsed_ms = (time.time() - t0) * 1000
            return report

        flat: List[str] = []
        _emit_config(flat, config)
        s_flat = report.section("3.2 展平")
        s_flat.ok(
            "config.flattened",
            f"配置项: {len(flat)} 行（已脱敏）",
            detail="\n".join(flat),
            data={"lines": len(flat)},
        )
        report.data["flattened"] = flat

        s_traj = report.section("3.3 Trajectory 最新 runtime config")
        report.data.update(
            _section_trajectory_runtime_config(s_traj, ctx),
        )
        try:
            traj_runs_count = len(ctx.collect_runs())
        except Exception:
            traj_runs_count = 0
        report.add_scope(
            "trajectory", "full",
            f"{traj_runs_count} runs" if traj_runs_count else None,
        )

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
