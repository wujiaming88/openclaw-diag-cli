#!/usr/bin/env python3
"""模块 2：基础环境（版本一致性、Gateway 进程、端口、环境变量）。"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output, paths
from ocdiag.sensitive import safe_val


def run(cmd, timeout=5):
    try:
        r = subprocess.run(
            cmd, shell=isinstance(cmd, str), capture_output=True,
            text=True, timeout=timeout, check=False,
        )
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, "", ""


def detect_oc_version() -> Optional[str]:
    rc, stdout, _ = run(["openclaw", "--version"])
    if rc == 0 and stdout:
        return stdout.splitlines()[0].strip()

    home = os.path.expanduser("~")
    pnpm_root = os.path.join(home, ".local", "share", "pnpm")
    if os.path.isdir(pnpm_root):
        for root, _, files in os.walk(pnpm_root):
            if "package.json" in files and root.endswith(os.sep + "openclaw"):
                try:
                    with open(os.path.join(root, "package.json")) as f:
                        return json.load(f).get("version")
                except Exception:
                    pass

    pkg = "/usr/lib/node_modules/openclaw/package.json"
    if os.path.isfile(pkg):
        try:
            with open(pkg) as f:
                return json.load(f).get("version")
        except Exception:
            pass
    return None


def detect_node_version() -> Optional[str]:
    rc, stdout, _ = run(["node", "--version"])
    if rc == 0 and stdout:
        return stdout.strip()
    return None


def gateway_systemctl_status() -> str:
    _, stdout, _ = run(["systemctl", "--user", "status", "openclaw-gateway"])
    return stdout


def gateway_pid() -> Optional[str]:
    _, stdout, _ = run(["pgrep", "-f", "openclaw.*gateway"])
    pid = stdout.splitlines()[0].strip() if stdout else ""
    if pid:
        return pid
    rc, stdout, _ = run(["systemctl", "--user", "show", "openclaw-gateway.service",
                         "--property=MainPID"])
    if rc == 0 and "=" in stdout:
        v = stdout.strip().split("=", 1)[1]
        if v and v != "0":
            return v
    return None


def parse_proc_environ(pid: str) -> Optional[list]:
    p = f"/proc/{pid}/environ"
    if not os.access(p, os.R_OK):
        return None
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except (PermissionError, FileNotFoundError, OSError):
        return None
    pairs = []
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        try:
            s = entry.decode("utf-8", errors="replace")
        except Exception:
            continue
        eq = s.find("=")
        if eq <= 0:
            continue
        k, v = s[:eq], s[eq + 1:]
        pairs.append((k, safe_val(k, v)))
    return sorted(pairs)


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 2：采集 OpenClaw 基础环境",
        prog="02_environment",
    )
    args = parser.parse_args()
    out = output.init("environment", json_mode=args.json, no_color=args.no_color)
    out.section("模块 2：基础环境")

    oc_version = detect_oc_version()
    if oc_version:
        out.item(f"OpenClaw 版本: {oc_version}")
    else:
        out.item("OpenClaw 版本: 无法确定")
        out.evidence("openclaw --version", "命令未找到或无输出")
    out.set_data("oc_version", oc_version)

    service_file = paths.SERVICE_FILE
    svc_version = None
    if oc_version and os.path.isfile(service_file):
        try:
            with open(service_file) as f:
                m = re.search(r"openclaw@([0-9]+\.[0-9]+\.[0-9]+)", f.read())
            if m:
                svc_version = m.group(1)
        except OSError:
            pass
        if svc_version:
            cli_clean = re.search(r"[0-9]+\.[0-9]+\.[0-9]+", oc_version)
            cli_clean = cli_clean.group(0) if cli_clean else ""
            if cli_clean and cli_clean != svc_version:
                out.item(f"版本不一致: CLI={cli_clean} vs Gateway service={svc_version}")
                out.item("     原因: pnpm 升级后 service 文件未重生，Gateway 实际跑的是旧版本")
                out.item("     修复: 在目标机器上执行 `openclaw gateway install --force` 然后 `openclaw gateway restart`")
            else:
                out.item(f"版本一致: CLI={cli_clean} = service={svc_version}")
    out.set_data("service_version", svc_version)

    node_ver = detect_node_version()
    if node_ver:
        major = node_ver.lstrip("v").split(".", 1)[0]
        out.item(f"Node.js 版本: {node_ver} (major: {major})")
    else:
        out.item("Node.js: 未找到")
        out.evidence("node --version", "命令未找到")
    out.set_data("node_version", node_ver)

    rc, stdout, _ = run(["free", "-m"])
    mem_avail = ""
    if rc == 0:
        for line in stdout.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                if len(parts) >= 7:
                    mem_avail = parts[6]
                    break
    if mem_avail:
        out.item(f"可用内存: {mem_avail} MB")
    out.set_data("memory_available_mb", mem_avail)

    rc, stdout, _ = run(["df", "-m", paths.OPENCLAW_HOME])
    disk_avail = ""
    if rc == 0:
        lines = stdout.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 4:
                disk_avail = parts[3]
    if disk_avail:
        out.item(f"磁盘可用 ({paths.OPENCLAW_HOME}): {disk_avail} MB")
    out.set_data("disk_available_mb", disk_avail)

    gw_status = gateway_systemctl_status()
    if gw_status:
        active_state = ""
        main_pid = ""
        since = ""
        m = re.search(r"Active:\s+(\S+)", gw_status)
        if m:
            active_state = m.group(1)
        m = re.search(r"Main PID:\s+(\d+)", gw_status)
        if m:
            main_pid = m.group(1)
        m = re.search(r"since\s+(.*)", gw_status)
        if m:
            since = m.group(1).splitlines()[0].strip()
        if active_state == "active":
            out.item(f"Gateway 服务: 运行中 (PID {main_pid or '?'}, since {since or '?'})")
        else:
            out.item(f"Gateway 服务: {active_state or 'unknown'}")
            out.evidence("systemctl --user status openclaw-gateway", "\n".join(gw_status.splitlines()[:5]))
        out.set_data("gateway_state", active_state)
        out.set_data("gateway_main_pid", main_pid)
    else:
        _, pids, _ = run(["pgrep", "-f", "openclaw-gatewa"])
        pids_clean = " ".join(pids.splitlines()[:5]) if pids else ""
        if pids_clean:
            out.item(f"Gateway 进程: 已发现 (PIDs: {pids_clean})")
        else:
            out.item("Gateway 进程: 未通过 systemctl 或 pgrep 检测到")
            out.evidence("pgrep -f openclaw-gatewa", "无输出")
        out.set_data("gateway_pids", pids_clean.split() if pids_clean else [])

    port = 18789
    if os.path.isfile(args.config):
        try:
            with open(args.config) as f:
                cfg = json.load(f)
            cfg_port = cfg.get("gateway", {}).get("port")
            if cfg_port:
                port = int(cfg_port)
        except Exception:
            pass
    rc, stdout, _ = run(["ss", "-tlnp"])
    listening = any(f":{port} " in ln for ln in stdout.splitlines()) if rc == 0 else False
    if listening:
        out.item(f"端口 {port}: 监听中")
    else:
        out.item(f"端口 {port}: 未监听")
        out.evidence(f"ss -tlnp | grep :{port}", "<无输出>")
    out.set_data("port", port)
    out.set_data("port_listening", listening)

    out.line("")
    out.line("  ── Gateway 进程环境变量（OpenClaw 实际运行环境） ──")
    out.line("")
    pid = gateway_pid()
    env_pairs = parse_proc_environ(pid) if pid else None
    if pid and env_pairs is not None:
        out.item(f"Gateway PID: {pid}")
        out.line("")
        out.item(f"共 {len(env_pairs)} 个环境变量")
        out.line("")
        for k, v in env_pairs:
            out.item(f"{k} = {v}")
        out.set_data("gateway_env", [{"key": k, "value": v} for k, v in env_pairs])
    elif pid:
        out.item(f"无法读取 /proc/{pid}/environ（权限不足？）")
    else:
        out.item("Gateway 进程未运行，跳过")

    if os.path.isfile(paths.SERVICE_ENV_FILE):
        out.line("")
        out.line(f"  ── Systemd 服务环境变量配置 ({paths.SERVICE_ENV_FILE}) ──")
        out.line("")
        try:
            with open(paths.SERVICE_ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("["):
                        continue
                    if line.startswith("Environment="):
                        raw = line[len("Environment="):].strip()
                        # systemd 支持单行多键：Environment="A=1" "B=2" 或 Environment=A=1 B=2
                        try:
                            tokens = shlex.split(raw, posix=True)
                        except ValueError:
                            tokens = [raw]
                        for tok in tokens:
                            if "=" not in tok:
                                out.item(tok)
                                continue
                            key, val = tok.split("=", 1)
                            out.item(f"{key} = {safe_val(key, val)}")
        except OSError as e:
            out.item(f"读取失败: {e}")

    if os.path.isfile(service_file):
        out.line("")
        out.line(f"  ── Systemd 服务文件 ({service_file}) ──")
        out.line("")
        try:
            with open(service_file) as f:
                for line in f:
                    out.item(line.rstrip("\n"))
        except OSError:
            pass

    return out.done()


if __name__ == "__main__":
    sys.exit(main())
