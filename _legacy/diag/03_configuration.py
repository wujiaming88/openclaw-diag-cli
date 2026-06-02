#!/usr/bin/env python3
"""模块 3：采集 openclaw.json 配置（含敏感字段脱敏，模型 providers 折叠）。"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output, trajectory
from ocdiag.sensitive import is_sensitive_key, mask


_INSTALLS_RE = re.compile(r"^plugins\.installs\.[^.]+\.([^.]+)$")


def format_value(val):
    if isinstance(val, str):
        return val if val else '""'
    if isinstance(val, bool):
        return "true" if val else "false"
    if val is None:
        return "null"
    return str(val)


def emit_config(out: output.Output, data: list, obj, prefix: str = "") -> None:
    if isinstance(obj, dict):
        if not obj:
            data.append(f"{prefix} = {{}}")
            return
        for k, v in obj.items():
            new_key = f"{prefix}.{k}" if prefix else k
            m = _INSTALLS_RE.match(new_key)
            if m and m.group(1) not in ("source", "version", "installPath"):
                continue
            if new_key.startswith("models.providers.") and k == "models" and isinstance(v, list):
                for md in v:
                    if isinstance(md, dict):
                        mid = md.get("id", "?")
                        cw = md.get("contextWindow", "?")
                        mt = md.get("maxTokens", "?")
                        r_str = "true" if md.get("reasoning", False) else "false"
                        data.append(
                            f"{new_key}.{mid} = (contextWindow={cw}, "
                            f"maxTokens={mt}, reasoning={r_str})"
                        )
                continue
            emit_config(out, data, v, new_key)
    elif isinstance(obj, list):
        if not obj:
            data.append(f"{prefix} = []")
            return
        all_scalar = all(isinstance(x, (str, int, float, bool)) for x in obj)
        if all_scalar:
            n = len(obj)
            if n <= 5:
                data.append(f"{prefix} = [{n} 项]: {', '.join(str(x) for x in obj)}")
            else:
                data.append(f"{prefix} = [{n} 项]: {', '.join(str(x) for x in obj[:3])}, ... (完整列表省略)")
        else:
            for i, v in enumerate(obj):
                emit_config(out, data, v, f"{prefix}[{i}]")
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
            display = format_value(obj)
        if len(str(display)) > 500:
            display = str(display)[:500] + "..."
        data.append(f"{prefix} = {display}")


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 3：采集 OpenClaw 配置（含敏感字段脱敏）",
    )
    args = parser.parse_args()

    out = output.init("configuration", json_mode=args.json, no_color=args.no_color)
    out.section("模块 3：配置")

    config_path = args.config
    if not os.path.isfile(config_path):
        out.item(f"配置文件未找到: {config_path}")
        out.line("  下一步：")
        out.line("    1) 确认 OpenClaw 已经初始化（运行过 `openclaw` 即会生成配置）")
        out.line("    2) 用 OPENCLAW_CONFIG=/path/to/openclaw.json 或 --config 指向正确路径")
        out.line("    3) 在容器/远端诊断时，用 OPENCLAW_HOME=/path 整体覆盖")
        out.evidence(config_path, "<文件缺失>")
        out.set_data("config_path", config_path)
        out.set_data("found", False)
        out.fail("配置文件未找到")
        return out.done()

    out.set_data("config_path", config_path)
    out.set_data("found", True)

    out.progress(1, 2, "读取配置")
    try:
        with open(config_path) as f:
            config = json.load(f)
        out.item("JSON 语法: 有效")
        out.set_data("json_valid", True)
    except (json.JSONDecodeError, OSError) as e:
        out.item("JSON 语法: 无效")
        out.evidence(config_path, str(e))
        out.set_data("json_valid", False)
        out.set_data("parse_error", str(e))
        out.fail("配置 JSON 无效")
        return out.done()

    out.line("")
    out.progress(2, 2, "展平输出")
    flat: list = []
    emit_config(out, flat, config)
    for line in flat:
        out.item(line)
    out.set_data("flattened", flat)

    out.line("")
    out.line("  ── Trajectory: 最近 run 的 effective runtime config ──")
    out.line("")
    section_trajectory_runtime_config(out, args.sessions_base)

    return out.done()


def section_trajectory_runtime_config(out: output.Output, sessions_base: str) -> None:
    """Show ``trace.metadata.config.runtime`` from the most recent trajectory.

    Informational only — no verdict change. Tracks
    ``skills.snapshotVersion`` over the last 50 runs to flag rapid churn.
    """
    files = trajectory.discover_trajectory_files(sessions_base)
    if not files:
        out.item("未发现 trajectory 文件 — 跳过 effective runtime config")
        return
    runs = trajectory.collect_runs(files)
    runs.sort(key=lambda r: r.started_ts_ms, reverse=True)
    if not runs:
        out.item("最近无 trajectory run — 跳过")
        return
    latest = runs[0]
    rt_cfg = latest.runtime_config or {}
    out.item(f"最新 run（{latest.session_id[:8]}#{latest.run_id[:8]}）的 runtime config:")
    if not rt_cfg:
        out.item("  （trace.metadata.config.runtime 为空）")
    else:
        for k in sorted(rt_cfg.keys()):
            v = rt_cfg[k]
            sv = str(v)
            if len(sv) > 200:
                sv = sv[:200] + "..."
            out.item(f"    {k} = {sv}")

    snapshots: dict = {}
    for r in runs[:50]:
        sv = r.skills_snapshot_version
        if sv is None:
            continue
        snapshots[sv] = snapshots.get(sv, 0) + 1
    if len(snapshots) > 1:
        out.item(f"  skills.snapshotVersion 漂移（最近 50 run 中 {len(snapshots)} 个不同版本）:")
        for sv, cnt in sorted(snapshots.items(), key=lambda x: -x[1])[:5]:
            out.item(f"    {sv}: {cnt} 个 run")
    elif snapshots:
        sv = next(iter(snapshots))
        out.item(f"  skills.snapshotVersion: {sv}（最近 50 run 一致）")

    out.set_data("trajectory_runtime_config", rt_cfg)
    out.set_data("skills_snapshot_versions",
                 [{"snapshotVersion": k, "count": v} for k, v in snapshots.items()])


if __name__ == "__main__":
    sys.exit(main())
