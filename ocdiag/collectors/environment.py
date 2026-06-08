"""environment collector — OpenClaw / Node version, Gateway env vars."""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import subprocess
import time
from typing import List, Optional, Tuple

from .. import paths, trajectory
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict
from ..sensitive import safe_val, sanitize_text


def _run(cmd, timeout: int = 5):
    try:
        r = subprocess.run(
            cmd, shell=isinstance(cmd, str), capture_output=True,
            text=True, timeout=timeout, check=False,
        )
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, "", ""


def _detect_oc_version() -> Optional[str]:
    rc, stdout, _ = _run(["openclaw", "--version"])
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
                except (OSError, ValueError):
                    pass
    pkg = "/usr/lib/node_modules/openclaw/package.json"
    if os.path.isfile(pkg):
        try:
            with open(pkg) as f:
                return json.load(f).get("version")
        except (OSError, ValueError):
            pass
    return None


def _detect_node_version() -> Optional[str]:
    rc, stdout, _ = _run(["node", "--version"])
    if rc == 0 and stdout:
        return stdout.strip()
    return None


def _gateway_systemctl_status() -> str:
    _, stdout, _ = _run(["systemctl", "--user", "status", "openclaw-gateway"])
    return stdout


def _gateway_pid() -> Optional[str]:
    _, stdout, _ = _run(["pgrep", "-f", "openclaw.*gateway"])
    pid = stdout.splitlines()[0].strip() if stdout else ""
    if pid:
        return pid
    rc, stdout, _ = _run([
        "systemctl", "--user", "show", "openclaw-gateway.service",
        "--property=MainPID",
    ])
    if rc == 0 and "=" in stdout:
        v = stdout.strip().split("=", 1)[1]
        if v and v != "0":
            return v
    return None


def _parse_proc_environ(pid: str) -> Optional[List[Tuple[str, str]]]:
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
        s = entry.decode("utf-8", errors="replace")
        eq = s.find("=")
        if eq <= 0:
            continue
        k, v = s[:eq], s[eq + 1:]
        pairs.append((k, safe_val(k, v)))
    return sorted(pairs)


def _section_versions(s: Section, ctx: DiagContext) -> dict:
    data: dict = {}
    oc_version = _detect_oc_version()
    data["oc_version"] = oc_version
    if oc_version:
        s.ok("version.openclaw", f"OpenClaw 版本: {oc_version}",
             data={"version": oc_version})
    else:
        s.warn(
            "version.openclaw",
            "OpenClaw 版本: 无法确定",
            evidence="openclaw --version: 命令未找到或无输出",
            data={
                "found": False,
                "reason": "command_not_found",
                "checked": "openclaw --version + pnpm/global node_modules",
            },
        )

    service_file = paths.SERVICE_FILE
    svc_version: Optional[str] = None
    if oc_version and os.path.isfile(service_file):
        try:
            with open(service_file) as f:
                m = re.search(r"openclaw@([0-9]+\.[0-9]+\.[0-9]+)", f.read())
            if m:
                svc_version = m.group(1)
        except OSError:
            pass
        if svc_version:
            cli_clean_m = re.search(r"[0-9]+\.[0-9]+\.[0-9]+", oc_version)
            cli_clean = cli_clean_m.group(0) if cli_clean_m else ""
            if cli_clean and cli_clean != svc_version:
                s.warn(
                    "version.consistency",
                    f"版本不一致: CLI={cli_clean} vs Gateway service={svc_version}",
                    detail=(
                        "原因: pnpm 升级后 service 文件未重生，"
                        "Gateway 实际跑的是旧版本\n"
                        "修复: 在目标机器上执行 "
                        "`openclaw gateway install --force` 然后 "
                        "`openclaw gateway restart`"
                    ),
                    data={"cli": cli_clean, "service": svc_version},
                )
            else:
                s.ok(
                    "version.consistency",
                    f"版本一致: CLI={cli_clean} = service={svc_version}",
                    data={"cli": cli_clean, "service": svc_version},
                )
    data["service_version"] = svc_version
    return data


def _section_node(s: Section) -> dict:
    data: dict = {}
    node_ver = _detect_node_version()
    data["node_version"] = node_ver
    if node_ver:
        major = node_ver.lstrip("v").split(".", 1)[0]
        s.ok(
            "node.version",
            f"Node.js 版本: {node_ver} (major: {major})",
            data={"version": node_ver, "major": major},
        )
    else:
        s.warn(
            "node.version",
            "Node.js: 未找到",
            evidence="node --version: 命令未找到",
        )
    return data


def _section_resources(s: Section, ctx: DiagContext) -> dict:
    data: dict = {}
    rc, stdout, _ = _run(["free", "-m"])
    mem_avail = ""
    if rc == 0:
        for line in stdout.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                if len(parts) >= 7:
                    mem_avail = parts[6]
                    break
    data["memory_available_mb"] = mem_avail
    if mem_avail:
        s.ok("resources.memory", f"可用内存: {mem_avail} MB",
             data={"available_mb": mem_avail})
    else:
        s.warn(
            "resources.memory",
            "可用内存: 无法获取（free 不可用）",
            data={"found": False, "reason": "free_unavailable"},
        )

    rc, stdout, _ = _run(["df", "-m", str(ctx.openclaw_home)])
    disk_avail = ""
    if rc == 0:
        lines = stdout.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 4:
                disk_avail = parts[3]
    data["disk_available_mb"] = disk_avail
    if disk_avail:
        s.ok(
            "resources.disk",
            f"磁盘可用 ({ctx.openclaw_home}): {disk_avail} MB",
            data={"available_mb": disk_avail, "path": str(ctx.openclaw_home)},
        )
    else:
        s.warn(
            "resources.disk",
            f"磁盘可用 ({ctx.openclaw_home}): 无法获取",
            data={"found": False, "reason": "df_unavailable"},
        )
    return data


def _section_gateway(s: Section, ctx: DiagContext) -> dict:
    data: dict = {}
    gw_status = _gateway_systemctl_status()
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
        data["gateway_state"] = active_state
        data["gateway_main_pid"] = main_pid
        if active_state == "active":
            s.ok(
                "gateway.service",
                f"Gateway 服务: 运行中 (PID {main_pid or '?'}, since {since or '?'})",
                data={"state": active_state, "pid": main_pid, "since": since},
            )
        else:
            s.warn(
                "gateway.service",
                f"Gateway 服务: {active_state or 'unknown'}",
                evidence="\n".join(gw_status.splitlines()[:5]),
                data={"state": active_state},
            )
    else:
        _, pids, _ = _run(["pgrep", "-f", "openclaw-gatewa"])
        pids_clean = " ".join(pids.splitlines()[:5]) if pids else ""
        if pids_clean:
            s.ok(
                "gateway.service",
                f"Gateway 进程: 已发现 (PIDs: {pids_clean})",
                data={"pids": pids_clean.split()},
            )
        else:
            s.warn(
                "gateway.service",
                "Gateway 进程: 未通过 systemctl 或 pgrep 检测到",
                evidence="pgrep -f openclaw-gatewa: 无输出",
                data={"pids": []},
            )
        data["gateway_pids"] = pids_clean.split() if pids_clean else []

    port = 18789
    cfg_port = ctx.config.get("gateway", {}).get("port") if ctx.config else None
    if cfg_port:
        try:
            port = int(cfg_port)
        except (TypeError, ValueError):
            pass
    rc, stdout, _ = _run(["ss", "-tlnp"])
    listening = (
        any(f":{port} " in ln for ln in stdout.splitlines()) if rc == 0 else False
    )
    data["port"] = port
    data["port_listening"] = listening
    if listening:
        s.ok("gateway.port", f"端口 {port}: 监听中",
             data={"port": port, "listening": True})
    else:
        s.warn(
            "gateway.port",
            f"端口 {port}: 未监听",
            evidence=f"ss -tlnp | grep :{port}: 无输出",
            data={"port": port, "listening": False},
        )
    return data


def _section_env_vars(s: Section, unmask: bool) -> dict:
    data: dict = {}
    pid = _gateway_pid()
    env_pairs = _parse_proc_environ(pid) if pid else None
    if pid and env_pairs is not None:
        details = "\n".join(f"{k} = {v}" for k, v in env_pairs)
        s.ok(
            "env.gateway",
            f"Gateway PID: {pid} — 共 {len(env_pairs)} 个环境变量",
            detail=details,
            data={"pid": pid, "env": [{"key": k, "value": v}
                                      for k, v in env_pairs]},
        )
        data["gateway_env"] = [{"key": k, "value": v} for k, v in env_pairs]
    elif pid:
        s.warn(
            "env.gateway",
            f"无法读取 /proc/{pid}/environ（权限不足？）",
            data={"found": False, "reason": "proc_unreadable"},
        )
    else:
        s.warn(
            "env.gateway",
            "Gateway 进程未运行，跳过",
            data={"found": False, "reason": "process_not_running"},
        )

    if os.path.isfile(paths.SERVICE_ENV_FILE):
        items: List[str] = []
        try:
            with open(paths.SERVICE_ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("["):
                        continue
                    if line.startswith("Environment="):
                        raw = line[len("Environment="):].strip()
                        try:
                            tokens = shlex.split(raw, posix=True)
                        except ValueError:
                            tokens = [raw]
                        for tok in tokens:
                            if "=" not in tok:
                                items.append(tok)
                                continue
                            key, val = tok.split("=", 1)
                            items.append(f"{key} = {safe_val(key, val)}")
        except OSError as e:
            items.append(f"读取失败: {e}")
        if items:
            s.ok(
                "env.systemd_env_file",
                f"Systemd 环境文件 ({paths.SERVICE_ENV_FILE}) — {len(items)} 项",
                detail="\n".join(items),
                data={"path": paths.SERVICE_ENV_FILE, "items": items},
            )

    if os.path.isfile(paths.SERVICE_FILE):
        try:
            with open(paths.SERVICE_FILE) as f:
                content = f.read()
            display = content if unmask else sanitize_text(content)
            s.ok(
                "env.systemd_unit",
                f"Systemd 服务文件 ({paths.SERVICE_FILE})",
                detail=display,
                data={"path": paths.SERVICE_FILE},
            )
        except OSError as e:
            s.warn(
                "env.systemd_unit",
                f"无法读取 {paths.SERVICE_FILE}: {e}",
            )
    return data


def _section_trajectory_versions(s: Section, ctx: DiagContext) -> dict:
    data: dict = {}
    files = ctx.trajectory_files()
    if not files:
        s.ok(
            "trajectory.versions",
            "未发现 trajectory 文件 — 跳过版本漂移分析",
            data={"version_history": []},
        )
        data["version_history"] = []
        return data

    runs = ctx.collect_runs(
        since_ms=trajectory.ms_ago(14 * 86400 * 1000),
    )
    if not runs:
        s.ok("trajectory.versions", "最近 14d 无 trajectory run")
        return data

    version_seen: dict = {}
    node_seen: dict = {}
    invocation_seen: set = set()
    for r in runs:
        v = r.harness_version or "?"
        slot = version_seen.setdefault(v, [0, 0])
        slot[0] += 1
        if r.started_ts_ms > slot[1]:
            slot[1] = r.started_ts_ms
        n = r.harness_node or "?"
        nslot = node_seen.setdefault(n, [0, 0])
        nslot[0] += 1
        if r.started_ts_ms > nslot[1]:
            nslot[1] = r.started_ts_ms
        if r.invocation:
            invocation_seen.add(tuple(r.invocation))

    lines: List[str] = []
    lines.append(f"OpenClaw harness.version (14d, {len(runs)} run):")
    for ver, (cnt, ts) in sorted(version_seen.items(), key=lambda x: -x[1][0]):
        ts_str = ""
        if ts:
            ts_str = (
                " 最后出现 "
                + datetime.datetime.fromtimestamp(ts / 1000).strftime(
                    "%Y-%m-%d %H:%M",
                )
            )
        lines.append(f"  {ver}: {cnt} 个 run{ts_str}")

    n_versions = len(version_seen)
    n_nodes = len(node_seen)
    n_invocations = len(invocation_seen)

    detail_lines = list(lines)
    detail_lines.append("Node runtime 版本:")
    for ver, (cnt, _ts) in sorted(node_seen.items(), key=lambda x: -x[1][0]):
        detail_lines.append(f"  {ver}: {cnt} 个 run")
    detail = "\n".join(detail_lines)

    if n_versions > 1 or n_nodes > 1 or n_invocations > 1:
        msgs = []
        if n_versions > 1:
            msgs.append(f"OpenClaw {n_versions} 个不同版本")
        if n_nodes > 1:
            msgs.append(f"Node {n_nodes} 个不同版本")
        if n_invocations > 1:
            msgs.append(f"{n_invocations} 种不同 invocation")
        s.warn(
            "trajectory.drift",
            f"14d 内出现版本漂移: {' / '.join(msgs)}",
            detail=detail,
        )
    else:
        s.ok("trajectory.versions", f"14d 版本一致 ({len(runs)} run)",
             detail=detail)

    data["version_history"] = [
        {"version": v, "count": c, "last_ts_ms": t}
        for v, (c, t) in sorted(version_seen.items(), key=lambda x: -x[1][0])
    ]
    data["node_version_history"] = [
        {"node": n, "count": c, "last_ts_ms": t}
        for n, (c, t) in sorted(node_seen.items(), key=lambda x: -x[1][0])
    ]
    data["invocation_variants"] = [list(inv) for inv in invocation_seen]
    return data


@register
class EnvironmentCollector:
    id = "environment"
    title = "基础环境"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)

        s_ver = report.section("2.1 OpenClaw 版本")
        report.data.update(_section_versions(s_ver, ctx))

        s_node = report.section("2.2 Node 运行时")
        report.data.update(_section_node(s_node))

        s_res = report.section("2.3 资源")
        report.data.update(_section_resources(s_res, ctx))

        s_gw = report.section("2.4 Gateway 服务")
        report.data.update(_section_gateway(s_gw, ctx))

        s_env = report.section("2.5 进程环境变量")
        report.data.update(_section_env_vars(s_env, ctx.unmask))

        s_traj = report.section("2.6 Trajectory 版本漂移 (14d)")
        report.data.update(
            _section_trajectory_versions(s_traj, ctx),
        )

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
