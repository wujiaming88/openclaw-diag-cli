"""sys_health collector — DNS / network / CPU / memory / disk / process / time."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import List, Optional

from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(cmd, timeout: int = 8, shell: bool = False):
    try:
        r = subprocess.run(
            cmd, shell=shell, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, "", ""


def _dns_targets_from_config(ctx: DiagContext) -> List[str]:
    targets = set()
    cfg = ctx.config
    providers = cfg.get("models", {}).get("providers", {}) or {}
    for _, pv in providers.items():
        if not isinstance(pv, dict):
            continue
        url = pv.get("baseUrl", "") or pv.get("baseURL", "")
        m = re.match(r"https?://([^/:]+)", url)
        if m:
            targets.add(m.group(1))
    channels = cfg.get("channels", {}) or {}
    for ch_name, ch_cfg in channels.items():
        if not isinstance(ch_cfg, dict):
            continue
        if "feishu" in ch_name or "lark" in ch_name:
            targets.add("open.feishu.cn")
        if "telegram" in ch_name:
            targets.add("api.telegram.org")
        if "discord" in ch_name:
            targets.add("discord.com")
        for key in ("webhook", "baseUrl", "url"):
            url = ch_cfg.get(key, "")
            if url:
                m = re.match(r"https?://([^/:]+)", url)
                if m:
                    targets.add(m.group(1))
    gw = cfg.get("gateway", {}) or {}
    for key in ("trustedProxyUrl", "controlUrl"):
        url = gw.get(key, "")
        if url:
            m = re.match(r"https?://([^/:]+)", url)
            if m:
                targets.add(m.group(1))
    return sorted(
        t for t in targets
        if not t.startswith("127.") and t not in ("localhost", "0.0.0.0")
    )


def _detect_oc_pid() -> Optional[str]:
    rc, stdout, _ = _run(["pgrep", "-f", "openclaw.*gateway"])
    if rc == 0 and stdout.strip():
        return stdout.splitlines()[0].strip()
    rc, stdout, _ = _run([
        "systemctl", "--user", "show", "openclaw-gateway.service",
        "--property=MainPID",
    ])
    if rc == 0 and "=" in stdout:
        v = stdout.strip().split("=", 1)[1]
        if v and v != "0":
            return v
    return None


def _section_dns(s: Section, targets: List[str]) -> List[dict]:
    has_dig = _have("dig")
    has_getent = _have("getent")
    if not targets:
        targets = ["dns.google"]
    if not has_dig and not has_getent:
        s.warn("dns.tools_missing", "dig/getent 均未安装，跳过 DNS 测试")
        return []
    results: List[dict] = []
    for h in targets:
        if not h:
            continue
        ip = ""
        start_ns = time.time_ns()
        if has_dig:
            rc, stdout, _ = _run(
                ["timeout", "2", "dig", "+short", "+time=2", "+tries=1", h],
                timeout=4,
            )
            if rc == 0:
                for ln in stdout.splitlines():
                    if re.match(r"^\d+\.", ln):
                        ip = ln.strip()
                        break
        elif has_getent:
            rc, stdout, _ = _run(
                ["timeout", "2", "getent", "hosts", h], timeout=4,
            )
            if rc == 0 and stdout:
                ip = stdout.split()[0]
        elapsed_ms = (time.time_ns() - start_ns) // 1_000_000
        if ip:
            s.ok(
                f"dns.{h}",
                f"{h}: {ip} ({elapsed_ms}ms)",
                data={"host": h, "ip": ip, "elapsed_ms": elapsed_ms},
            )
            results.append({"host": h, "ip": ip, "elapsed_ms": elapsed_ms})
        else:
            s.warn(
                f"dns.{h}",
                f"{h}: FAILED (timeout 2s)",
                data={"host": h, "ip": None, "elapsed_ms": elapsed_ms},
            )
            results.append({"host": h, "ip": None, "elapsed_ms": elapsed_ms})
    return results


def _section_network(s: Section, targets: List[str]) -> dict:
    out_data: dict = {}
    if _have("iptables"):
        rc, stdout, _ = _run(["iptables", "-L", "-n"], timeout=5)
        ipt_count = sum(
            1 for ln in stdout.splitlines() if "DROP" in ln or "REJECT" in ln
        ) if rc == 0 else 0
        out_data["iptables_drop_reject_count"] = ipt_count
        s.ok(
            "network.iptables",
            f"iptables: {ipt_count} 条 DROP/REJECT 规则",
            data={"count": ipt_count},
        )
    else:
        s.ok("network.iptables", "iptables: 未安装")
    if _have("curl"):
        first = targets[0] if targets else ""
        if first:
            rc, stdout, _ = _run([
                "curl", "-so", "/dev/null",
                "-w", "%{http_code} %{time_connect}s",
                "--connect-timeout", "3", "--max-time", "5",
                f"https://{first}",
            ], timeout=8)
            curl_out = stdout.strip() or "FAILED"
            out_data["curl_test"] = {"host": first, "result": curl_out}
            ok = bool(stdout.strip()) and not curl_out.startswith("000")
            (s.ok if ok else s.warn)(
                "network.curl",
                f"{first}:443 连接: {curl_out}",
                data={"host": first, "result": curl_out},
            )
    else:
        s.warn("network.curl_missing", "curl: 未安装，跳过连通性测试")
    return out_data


def _section_cpu(s: Section, oc_pid: Optional[str]) -> dict:
    rc, stdout, _ = _run(["nproc"])
    try:
        ncpu = int(stdout.strip()) if rc == 0 else 1
    except ValueError:
        ncpu = 1
    rc, stdout, _ = _run(["uptime"])
    load_line = "unknown"
    if rc == 0:
        m = re.search(r"load average:\s*(.*)", stdout)
        if m:
            load_line = m.group(1).strip()
    load1: Optional[float] = None
    try:
        load1 = float(re.split(r"[, ]+", load_line)[0])
    except (ValueError, IndexError):
        pass
    overload = load1 is not None and load1 > ncpu * 2
    msg = f"核心数: {ncpu} | 负载: {load_line}"
    if overload:
        msg += f" — 1 分钟负载超过核心数 {ncpu} 的 2 倍"
        s.warn(
            "cpu.load",
            msg,
            data={"ncpu": ncpu, "load1": load1, "load_line": load_line},
        )
    else:
        s.ok(
            "cpu.load",
            msg,
            data={"ncpu": ncpu, "load1": load1, "load_line": load_line},
        )
    proc_data: Optional[dict] = None
    if oc_pid:
        rc, stdout, _ = _run([
            "ps", "-p", oc_pid, "-o", "pid,pcpu,pmem,rss,args", "--no-headers",
        ])
        if rc == 0 and stdout.strip():
            parts = stdout.split(None, 4)
            if len(parts) >= 5:
                pcpu, pmem, rss = parts[1], parts[2], parts[3]
                try:
                    rss_mb = int(int(rss) / 1024)
                except ValueError:
                    rss_mb = 0
                proc_data = {"pid": oc_pid, "cpu_pct": pcpu,
                             "mem_pct": pmem, "rss_mb": rss_mb}
                s.ok(
                    "cpu.process",
                    f"OpenClaw 进程(PID={oc_pid}): CPU={pcpu}% MEM={pmem}% RSS={rss_mb}MB",
                    data=proc_data,
                )
        if proc_data is None:
            s.warn(
                "cpu.process",
                f"OpenClaw 进程(PID={oc_pid}): 无法读取 ps 信息",
            )
    else:
        s.warn("cpu.process", "OpenClaw 进程: 未运行")
    return {
        "cpu_count": ncpu,
        "load_average": load_line,
        "openclaw_proc": proc_data,
    }


def _section_memory(s: Section) -> dict:
    data: dict = {}
    if _have("free"):
        rc, stdout, _ = _run(["free", "-m"])
        if rc == 0:
            for line in stdout.splitlines():
                if line.startswith("Mem:"):
                    p = line.split()
                    if len(p) >= 7:
                        try:
                            total = int(p[1])
                            used = int(p[2])
                            avail = int(p[6])
                            data["memory"] = {
                                "total_mb": total,
                                "used_mb": used,
                                "available_mb": avail,
                            }
                            avail_pct = (avail * 100 / total) if total else 0
                            msg = (
                                f"内存: 总 {total/1024:.0f}GB | "
                                f"已用 {used/1024:.1f}GB | "
                                f"可用 {avail/1024:.1f}GB"
                            )
                            if avail_pct < 5:
                                s.fail("memory.usage", msg, data=data["memory"])
                            elif avail_pct < 15:
                                s.warn("memory.usage", msg, data=data["memory"])
                            else:
                                s.ok("memory.usage", msg, data=data["memory"])
                        except ValueError:
                            pass
                if line.startswith("Swap:"):
                    p = line.split()
                    if len(p) >= 3:
                        try:
                            total = int(p[1])
                            used = int(p[2])
                            pct = (used * 100 / total) if total > 0 else 0
                            data["swap"] = {
                                "total_mb": total, "used_mb": used, "pct": pct,
                            }
                            msg = (
                                f"Swap: 总 {total/1024:.0f}GB | "
                                f"已用 {used/1024:.1f}GB ({pct:.1f}%)"
                            )
                            if pct > 50:
                                s.warn("memory.swap", msg, data=data["swap"])
                            else:
                                s.ok("memory.swap", msg, data=data["swap"])
                        except ValueError:
                            pass
    else:
        s.warn("memory.free_missing", "free: 未安装")

    oom_count = 0
    if _have("journalctl"):
        rc, stdout, _ = _run(
            ["journalctl", "-k", "--since", "7 days ago", "--no-pager"],
            timeout=10,
        )
        if rc == 0:
            oom_count = sum(
                1 for ln in stdout.splitlines()
                if re.search(r"oom-killer|killed process|out of memory", ln, re.I)
            )
    elif _have("dmesg"):
        rc, stdout, _ = _run(["dmesg"])
        if rc == 0:
            oom_count = sum(
                1 for ln in stdout.splitlines()
                if re.search(r"oom-killer|killed process|out of memory", ln, re.I)
            )
    data["oom_count_7d"] = oom_count
    msg = f"OOM kill(7天内): {oom_count} 次"
    if oom_count > 0:
        s.warn("memory.oom", msg, data={"oom_count_7d": oom_count})
    else:
        s.ok("memory.oom", msg, data={"oom_count_7d": oom_count})
    return data


def _section_disk_space(s: Section, ctx: DiagContext) -> List[dict]:
    paths_to_check = [str(ctx.openclaw_home), str(ctx.log_dir), "/"]
    results: List[dict] = []
    for p in paths_to_check:
        if not os.path.isdir(p):
            s.warn(f"disk.{p}", f"{p}: 路径不存在")
            continue
        rc, stdout, _ = _run(["df", "-h", p])
        if rc != 0:
            s.warn(f"disk.{p}", f"{p}: df 读取失败")
            continue
        lines = stdout.splitlines()
        if len(lines) < 2:
            s.warn(f"disk.{p}", f"{p}: df 读取失败")
            continue
        parts = lines[1].split()
        if len(parts) < 5:
            s.warn(f"disk.{p}", f"{p}: df 读取失败")
            continue
        pct = parts[4]
        used = parts[2]
        total = parts[1]
        try:
            pct_n = int(pct.rstrip("%"))
        except ValueError:
            pct_n = 0
        msg = f"{p}: {pct} ({used}/{total})"
        d = {"path": p, "used": used, "total": total, "pct": pct,
             "pct_n": pct_n}
        if pct_n >= 95:
            s.fail(f"disk.{p}", msg + "  [告警: 超过 95%]", data=d)
        elif pct_n >= 85:
            s.warn(f"disk.{p}", msg + "  [告警: 超过 85%]", data=d)
        else:
            s.ok(f"disk.{p}", msg, data=d)
        results.append({"path": p, "used": used, "total": total, "pct": pct})
    return results


def _section_disk_io(s: Section) -> dict:
    out_data: dict = {}
    iowait_pct: str = "?"
    if _have("iostat"):
        rc, stdout, _ = _run(["iostat", "-c", "1", "2"], timeout=5)
        if rc == 0:
            for ln in stdout.splitlines():
                if ln.strip().startswith(" "):
                    parts = ln.split()
                    if len(parts) >= 4:
                        iowait_pct = parts[3]
        if iowait_pct != "?":
            s.ok("io.iowait", f"iowait: {iowait_pct}%",
                 data={"iowait_pct": iowait_pct})
        else:
            s.warn("io.iowait", "iowait: iostat 无输出")
    else:
        try:
            with open("/proc/stat") as f:
                for ln in f:
                    if ln.startswith("cpu "):
                        parts = ln.split()
                        nums = [int(x) for x in parts[1:]]
                        total = sum(nums)
                        iw = nums[4] if len(nums) > 4 else 0
                        iowait_pct = (
                            f"{(iw * 100 / total):.2f}" if total > 0 else "0"
                        )
                        break
        except OSError:
            pass
        s.ok(
            "io.iowait",
            f"iowait (累计): {iowait_pct}% (iostat 未安装)",
            data={"iowait_pct": iowait_pct},
        )
    out_data["iowait_pct"] = iowait_pct
    disk_err = 0
    if _have("dmesg"):
        rc, stdout, _ = _run(["dmesg"])
        if rc == 0:
            disk_err = sum(
                1 for ln in stdout.splitlines()
                if re.search(
                    r"I/O error|Buffer I/O error|end_request.*I/O|ata.*error",
                    ln, re.I,
                )
            )
    out_data["disk_errors_dmesg"] = disk_err
    msg = f"磁盘错误(dmesg): {disk_err} 条"
    if disk_err > 0:
        s.warn("io.disk_errors", msg, data={"count": disk_err})
    else:
        s.ok("io.disk_errors", msg, data={"count": disk_err})
    return out_data


def _section_process(s: Section, oc_pid: Optional[str]) -> dict:
    out_data: dict = {}
    if oc_pid and os.path.isdir(f"/proc/{oc_pid}"):
        rc, stdout, _ = _run(["ps", "-p", oc_pid, "-o", "etime=,rss="])
        etime = "?"
        rss_mb = 0
        if rc == 0 and stdout.strip():
            parts = stdout.split()
            if len(parts) >= 2:
                etime = parts[0]
                try:
                    rss_mb = int(int(parts[1]) / 1024)
                except ValueError:
                    rss_mb = 0
        try:
            fd_count = len(os.listdir(f"/proc/{oc_pid}/fd"))
        except OSError:
            fd_count = 0
        fd_limit: str = "?"
        try:
            with open(f"/proc/{oc_pid}/limits") as f:
                for ln in f:
                    if ln.startswith("Max open files"):
                        parts = ln.split()
                        if len(parts) >= 4:
                            fd_limit = parts[3]
                        break
        except OSError:
            pass
        s.ok(
            "process.gateway",
            f"Gateway 进程: PID={oc_pid} | uptime={etime} | RSS={rss_mb}MB",
            data={"pid": oc_pid, "etime": etime, "rss_mb": rss_mb},
        )
        if fd_limit not in ("?", "0"):
            try:
                fd_pct = (fd_count * 100 / int(fd_limit))
                msg = f"文件描述符: {fd_count}/{fd_limit} ({fd_pct:.2f}%)"
                if fd_pct > 80:
                    s.warn("process.fd", msg,
                           data={"count": fd_count, "limit": fd_limit})
                else:
                    s.ok("process.fd", msg,
                         data={"count": fd_count, "limit": fd_limit})
            except ValueError:
                s.ok(
                    "process.fd",
                    f"文件描述符: {fd_count}/{fd_limit}",
                    data={"count": fd_count, "limit": fd_limit},
                )
        else:
            s.ok(
                "process.fd",
                f"文件描述符: {fd_count} (limit={fd_limit})",
                data={"count": fd_count, "limit": fd_limit},
            )
        out_data["process"] = {
            "pid": oc_pid, "etime": etime, "rss_mb": rss_mb,
            "fd_count": fd_count, "fd_limit": fd_limit,
        }
    else:
        s.warn("process.gateway", "Gateway 进程: 未运行")

    rc, stdout, _ = _run(["ps", "-eo", "stat", "--no-headers"])
    zombie_count = (
        sum(1 for ln in stdout.splitlines() if ln.strip().startswith("Z"))
        if rc == 0 else 0
    )
    out_data["zombie_count"] = zombie_count
    msg = f"僵尸进程: {zombie_count}"
    if zombie_count > 5:
        s.warn("process.zombie", msg, data={"count": zombie_count})
    else:
        s.ok("process.zombie", msg, data={"count": zombie_count})
    return out_data


def _section_time_sync(s: Section) -> dict:
    out_data: dict = {}
    seen_any = False
    if _have("timedatectl"):
        rc, stdout, _ = _run(["timedatectl"])
        if rc == 0 and stdout:
            ntp_sync_m = re.search(
                r"System clock synchronized:\s*(yes|no)", stdout,
            )
            sync_status_m = re.search(r"NTP service:\s*(\S+)", stdout)
            ntp_sync = ntp_sync_m.group(1) if ntp_sync_m else "unknown"
            sync_status = sync_status_m.group(1) if sync_status_m else "unknown"
            out_data["ntp"] = {"service": sync_status, "synchronized": ntp_sync}
            msg = f"NTP 同步: service={sync_status} synchronized={ntp_sync}"
            if ntp_sync == "no":
                s.warn("time.timedatectl", msg, data=out_data["ntp"])
            else:
                s.ok("time.timedatectl", msg, data=out_data["ntp"])
            seen_any = True
    if _have("ntpstat"):
        rc, stdout, _ = _run(["ntpstat"])
        ntpstat_out = " | ".join(stdout.splitlines()[:2]) if stdout else ""
        if ntpstat_out:
            ok = rc == 0
            (s.ok if ok else s.warn)(
                "time.ntpstat", f"ntpstat: {ntpstat_out}",
            )
            seen_any = True
    if _have("chronyc"):
        rc, stdout, _ = _run(["chronyc", "tracking"])
        if rc == 0:
            extracted = [
                ln for ln in stdout.splitlines()
                if "System time" in ln or "Last offset" in ln
            ]
            if extracted:
                s.ok(
                    "time.chrony", f"chrony: {' | '.join(extracted)}",
                )
                seen_any = True
    if not seen_any:
        s.warn(
            "time.tools_missing",
            "时间同步: 无法检测（timedatectl/ntpstat/chronyc 均不可用）",
        )
    return out_data


@register
class SysHealthCollector:
    id = "sys_health"
    title = "系统健康检查"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)
        targets = _dns_targets_from_config(ctx)
        if not targets:
            targets = ["dns.google"]
        oc_pid = _detect_oc_pid()
        report.data["openclaw_pid"] = oc_pid

        s_dns = report.section("1.1 DNS 解析")
        report.data["dns"] = _section_dns(s_dns, targets)

        s_net = report.section("1.2 网络连通性")
        report.data.update(_section_network(s_net, targets))

        s_cpu = report.section("1.3 CPU")
        report.data.update(_section_cpu(s_cpu, oc_pid))

        s_mem = report.section("1.4 内存")
        report.data.update(_section_memory(s_mem))

        s_disk = report.section("1.5 磁盘空间")
        report.data["disk"] = _section_disk_space(s_disk, ctx)

        s_io = report.section("1.6 磁盘 I/O")
        report.data.update(_section_disk_io(s_io))

        s_proc = report.section("1.7 进程状态")
        report.data.update(_section_process(s_proc, oc_pid))

        s_time = report.section("1.8 时间同步")
        report.data.update(_section_time_sync(s_time))

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
