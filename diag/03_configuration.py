#!/usr/bin/env python3
"""模块 3：采集 openclaw.json 配置（含敏感字段脱敏，模型 providers 折叠）。"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output
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
                        pn = new_key.split(".")[2] if new_key.count(".") >= 2 else "?"
                        data.append(f"{pn}/{mid} (contextWindow={cw}, maxTokens={mt}, reasoning={r_str})")
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
        prog="03_configuration",
    )
    args = parser.parse_args()

    out = output.init("configuration", json_mode=args.json, no_color=args.no_color)
    out.section("模块 3：配置")

    config_path = args.config
    if not os.path.isfile(config_path):
        out.item(f"配置文件未找到: {config_path}")
        out.evidence(config_path, "<文件缺失>")
        out.set_data("config_path", config_path)
        out.set_data("found", False)
        out.fail("配置文件未找到")
        return out.done()

    out.set_data("config_path", config_path)
    out.set_data("found", True)

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
    flat: list = []
    emit_config(out, flat, config)
    for line in flat:
        out.item(line)
    out.set_data("flattened", flat)

    return out.done()


if __name__ == "__main__":
    sys.exit(main())
