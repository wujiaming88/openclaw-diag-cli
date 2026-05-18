#!/usr/bin/env python3
"""模块 1：系统健康检查（DNS、网络、CPU、内存、磁盘、IO、进程、时间同步）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output, paths


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(cmd, timeout=8, shell=False):
    try:
        r = subprocess.run(
            cmd, shell=shell, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, "", ""


def dns_targets_from_config(config_path: str) -> List[str]:
    targets = set()
    if not os.path.isfile(config_path):
        return []
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except Exception:
        return []
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
    return sorted(t for t in targets if not t.startswith("127.")
                  and t not in ("localhost", "0.0.0.0"))


def detect_oc_pid() -> Optional[str]:
    rc, stdout, _ = run(["pgrep", "-f", "openclaw.*gateway"])
    if rc == 0 and stdout.strip():
        return stdout.splitlines()[0].strip()
    rc, stdout, _ = run(["systemctl", "--user", "show", "openclaw-gateway.service",
                         "--property=MainPID"])
    if rc == 0 and "=" in stdout:
        v = stdout.strip().split("=", 1)[1]
        if v and v != "0":
            return v
    return None


def section_dns(out: output.Output, targets: List[str]) -> None:
    out.line("  ── 1.1 DNS 解析 ──")
    out.line("")
    has_dig = have("dig")
    has_getent = have("getent")
    if not targets:
        targets = ["dns.google"]
    results = []
    for h in targets:
        if not h:
            continue
        ip = ""
        start_ns = time.time_ns()
        if has_dig:
            rc, stdout, _ = run(["timeout", "2", "dig", "+short", "+time=2", "+tries=1", h], timeout=4)
            if rc == 0:
                for ln in stdout.splitlines():
                    if re.match(r"^\d+\.", ln):
                        ip = ln.strip()
                        break
        elif has_getent:
            rc, stdout, _ = run(["timeout", "2", "getent", "hosts", h], timeout=4)
            if rc == 0 and stdout:
                ip = stdout.split()[0]
        elapsed_ms = (time.time_ns() - start_ns) // 1_000_000
        if ip:
            out.item(f"{h}: {ip} ({elapsed_ms}ms)")
            results.append({"host": h, "ip": ip, "elapsed_ms": elapsed_ms})
        else:
            out.item(f"{h}: FAILED (timeout 2s)")
            results.append({"host": h, "ip": None, "elapsed_ms": elapsed_ms})
    if not has_dig and not has_getent:
        out.item("dig/getent 均未安装，跳过 DNS 测试")
    out.line("")
    out.set_data("dns", results)


def section_network(out: output.Output, targets: List[str]) -> None:
    out.line("  ── 1.2 网络连通性 ──")
    out.line("")
    if have("iptables"):
        rc, stdout, _ = run(["iptables", "-L", "-n"], timeout=5)
        ipt_count = sum(1 for ln in stdout.splitlines() if "DROP" in ln or "REJECT" in ln) if rc == 0 else 0
        out.item(f"iptables: {ipt_count} 条 DROP/REJECT 规则")
        out.set_data("iptables_drop_reject_count", ipt_count)
    else:
        out.item("iptables: 未安装")
    if have("curl"):
        first = targets[0] if targets else ""
        if first:
            rc, stdout, _ = run([
                "curl", "-so", "/dev/null",
                "-w", "%{http_code} %{time_connect}s",
                "--connect-timeout", "3", "--max-time", "5",
                f"https://{first}",
            ], timeout=8)
            curl_out = stdout.strip() or "FAILED"
            out.item(f"{first}:443 连接: {curl_out}")
            out.set_data("curl_test", {"host": first, "result": curl_out})
    else:
        out.item("curl: 未安装，跳过连通性测试")
    out.line("")


def section_cpu(out: output.Output, oc_pid: Optional[str]) -> None:
    out.line("  ── 1.3 CPU ──")
    out.line("")
    rc, stdout, _ = run(["nproc"])
    try:
        ncpu = int(stdout.strip()) if rc == 0 else 1
    except Exception:
        ncpu = 1
    rc, stdout, _ = run(["uptime"])
    load_line = "unknown"
    if rc == 0:
        m = re.search(r"load average:\s*(.*)", stdout)
        if m:
            load_line = m.group(1).strip()
    out.item(f"核心数: {ncpu} | 负载: {load_line}")
    out.set_data("cpu_count", ncpu)
    out.set_data("load_average", load_line)
    try:
        load1 = float(re.split(r"[, ]+", load_line)[0])
        if load1 > ncpu * 2:
            out.item(f"注意: 1 分钟负载 {load1} 超过核心数 {ncpu} 的 2 倍")
    except Exception:
        pass

    if oc_pid:
        rc, stdout, _ = run(["ps", "-p", oc_pid, "-o", "pid,pcpu,pmem,rss,args", "--no-headers"])
        if rc == 0 and stdout.strip():
            parts = stdout.split(None, 4)
            if len(parts) >= 5:
                pcpu, pmem, rss = parts[1], parts[2], parts[3]
                try:
                    rss_mb = int(int(rss) / 1024)
                except Exception:
                    rss_mb = 0
                out.item(f"OpenClaw 进程(PID={oc_pid}): CPU={pcpu}% MEM={pmem}% RSS={rss_mb}MB")
                out.set_data("openclaw_proc", {"pid": oc_pid, "cpu_pct": pcpu, "mem_pct": pmem, "rss_mb": rss_mb})
        else:
            out.item(f"OpenClaw 进程(PID={oc_pid}): 无法读取 ps 信息")
    else:
        out.item("OpenClaw 进程: 未运行")
    out.line("")


def section_memory(out: output.Output) -> None:
    out.line("  ── 1.4 内存 ──")
    out.line("")
    if have("free"):
        rc, stdout, _ = run(["free", "-m"])
        if rc == 0:
            for line in stdout.splitlines():
                if line.startswith("Mem:"):
                    p = line.split()
                    if len(p) >= 7:
                        try:
                            total = int(p[1]); used = int(p[2]); avail = int(p[6])
                            out.item(
                                f"内存: 总 {total/1024:.0f}GB | 已用 {used/1024:.1f}GB | 可用 {avail/1024:.1f}GB"
                            )
                            out.set_data("memory", {"total_mb": total, "used_mb": used, "available_mb": avail})
                        except Exception:
                            pass
                if line.startswith("Swap:"):
                    p = line.split()
                    if len(p) >= 3:
                        try:
                            total = int(p[1]); used = int(p[2])
                            pct = (used * 100 / total) if total > 0 else 0
                            out.item(
                                f"Swap: 总 {total/1024:.0f}GB | 已用 {used/1024:.1f}GB ({pct:.1f}%)"
                            )
                            out.set_data("swap", {"total_mb": total, "used_mb": used, "pct": pct})
                        except Exception:
                            pass
    else:
        out.item("free: 未安装")

    oom_count = 0
    if have("journalctl"):
        rc, stdout, _ = run(["journalctl", "-k", "--since", "7 days ago", "--no-pager"], timeout=10)
        if rc == 0:
            oom_count = sum(1 for ln in stdout.splitlines()
                            if re.search(r"oom-killer|killed process|out of memory", ln, re.I))
    elif have("dmesg"):
        rc, stdout, _ = run(["dmesg"])
        if rc == 0:
            oom_count = sum(1 for ln in stdout.splitlines()
                            if re.search(r"oom-killer|killed process|out of memory", ln, re.I))
    out.item(f"OOM kill(7天内): {oom_count} 次")
    out.set_data("oom_count_7d", oom_count)
    out.line("")


def section_disk_space(out: output.Output) -> None:
    out.line("  ── 1.5 磁盘空间 ──")
    out.line("")
    paths_to_check = [paths.OPENCLAW_HOME, "/tmp/openclaw", "/"]
    results = []
    for p in paths_to_check:
        if os.path.isdir(p):
            rc, stdout, _ = run(["df", "-h", p])
            if rc == 0:
                lines = stdout.splitlines()
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 5:
                        pct = parts[4]
                        used = parts[2]; total = parts[1]
                        warn = ""
                        try:
                            pct_n = int(pct.rstrip("%"))
                            if pct_n >= 90:
                                warn = " [告警: 超过 90%]"
                        except Exception:
                            pass
                        out.item(f"{p}: {pct} ({used}/{total}){warn}")
                        results.append({"path": p, "used": used, "total": total, "pct": pct})
                        continue
            out.item(f"{p}: df 读取失败")
        else:
            out.item(f"{p}: 路径不存在")
    out.set_data("disk", results)
    out.line("")


def section_disk_io(out: output.Output) -> None:
    out.line("  ── 1.6 磁盘 I/O ──")
    out.line("")
    if have("iostat"):
        rc, stdout, _ = run(["iostat", "-c", "1", "2"], timeout=5)
        iowait = ""
        if rc == 0:
            for ln in stdout.splitlines():
                if ln.strip().startswith(" "):
                    parts = ln.split()
                    if len(parts) >= 4:
                        iowait = parts[3]
        if iowait:
            out.item(f"iowait: {iowait}%")
            out.set_data("iowait_pct", iowait)
        else:
            out.item("iowait: iostat 无输出")
    else:
        iowait_pct = "?"
        try:
            with open("/proc/stat") as f:
                for ln in f:
                    if ln.startswith("cpu "):
                        parts = ln.split()
                        nums = [int(x) for x in parts[1:]]
                        total = sum(nums)
                        iw = nums[4] if len(nums) > 4 else 0
                        iowait_pct = f"{(iw * 100 / total):.2f}" if total > 0 else "0"
                        break
        except OSError:
            pass
        out.item(f"iowait (累计): {iowait_pct}% (iostat 未安装)")
        out.set_data("iowait_pct", iowait_pct)

    disk_err = 0
    if have("dmesg"):
        rc, stdout, _ = run(["dmesg"])
        if rc == 0:
            disk_err = sum(1 for ln in stdout.splitlines()
                           if re.search(r"I/O error|Buffer I/O error|end_request.*I/O|ata.*error", ln, re.I))
    out.item(f"磁盘错误(dmesg): {disk_err} 条")
    out.set_data("disk_errors_dmesg", disk_err)
    out.line("")


def section_process(out: output.Output, oc_pid: Optional[str]) -> None:
    out.line("  ── 1.7 进程状态 ──")
    out.line("")
    if oc_pid and os.path.isdir(f"/proc/{oc_pid}"):
        rc, stdout, _ = run(["ps", "-p", oc_pid, "-o", "etime=,rss="])
        etime = "?"
        rss_mb = 0
        if rc == 0 and stdout.strip():
            parts = stdout.split()
            if len(parts) >= 2:
                etime = parts[0]
                try:
                    rss_mb = int(int(parts[1]) / 1024)
                except Exception:
                    rss_mb = 0
        try:
            fd_count = len(os.listdir(f"/proc/{oc_pid}/fd"))
        except OSError:
            fd_count = 0
        fd_limit = "?"
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
        out.item(f"Gateway 进程: PID={oc_pid} | uptime={etime} | RSS={rss_mb}MB")
        if fd_limit not in ("?", "0"):
            try:
                fd_pct = (fd_count * 100 / int(fd_limit))
                out.item(f"文件描述符: {fd_count}/{fd_limit} ({fd_pct:.2f}%)")
            except Exception:
                out.item(f"文件描述符: {fd_count}/{fd_limit}")
        else:
            out.item(f"文件描述符: {fd_count} (limit={fd_limit})")
        out.set_data("process", {
            "pid": oc_pid, "etime": etime, "rss_mb": rss_mb,
            "fd_count": fd_count, "fd_limit": fd_limit,
        })
    else:
        out.item("Gateway 进程: 未运行")

    rc, stdout, _ = run(["ps", "-eo", "stat", "--no-headers"])
    zombie_count = sum(1 for ln in stdout.splitlines() if ln.strip().startswith("Z")) if rc == 0 else 0
    out.item(f"僵尸进程: {zombie_count}")
    out.set_data("zombie_count", zombie_count)
    out.line("")


def section_time_sync(out: output.Output) -> None:
    out.line("  ── 1.8 时间同步 ──")
    out.line("")
    tsync_ok = False
    if have("timedatectl"):
        rc, stdout, _ = run(["timedatectl"])
        if rc == 0 and stdout:
            ntp_sync_m = re.search(r"System clock synchronized:\s*(yes|no)", stdout)
            sync_status_m = re.search(r"NTP service:\s*(\S+)", stdout)
            ntp_sync = ntp_sync_m.group(1) if ntp_sync_m else "unknown"
            sync_status = sync_status_m.group(1) if sync_status_m else "unknown"
            out.item(f"NTP 同步: service={sync_status} synchronized={ntp_sync}")
            out.set_data("ntp", {"service": sync_status, "synchronized": ntp_sync})
            tsync_ok = True
    if have("ntpstat"):
        rc, stdout, _ = run(["ntpstat"])
        ntpstat_out = " | ".join(stdout.splitlines()[:2]) if stdout else ""
        if ntpstat_out:
            out.item(f"ntpstat: {ntpstat_out}")
            tsync_ok = True
    if have("chronyc"):
        rc, stdout, _ = run(["chronyc", "tracking"])
        if rc == 0:
            extracted = [ln for ln in stdout.splitlines()
                         if "System time" in ln or "Last offset" in ln]
            if extracted:
                out.item(f"chrony: {' | '.join(extracted)}")
                tsync_ok = True
    if not tsync_ok:
        out.item("时间同步: 无法检测（timedatectl/ntpstat/chronyc 均不可用）")


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 1：系统健康检查",
    )
    args = parser.parse_args()

    out = output.init("sys_health", json_mode=args.json, no_color=args.no_color)
    out.section("模块 1：系统健康检查")
    out.line("")

    targets = dns_targets_from_config(args.config)
    if not targets:
        targets = ["dns.google"]

    oc_pid = detect_oc_pid()
    out.set_data("openclaw_pid", oc_pid)

    out.progress(1, 8, "DNS 解析")
    section_dns(out, targets)
    out.progress(2, 8, "网络连通性")
    section_network(out, targets)
    out.progress(3, 8, "CPU")
    section_cpu(out, oc_pid)
    out.progress(4, 8, "内存")
    section_memory(out)
    out.progress(5, 8, "磁盘空间")
    section_disk_space(out)
    out.progress(6, 8, "磁盘 I/O")
    section_disk_io(out)
    out.progress(7, 8, "进程状态")
    section_process(out, oc_pid)
    out.progress(8, 8, "时间同步")
    section_time_sync(out)

    return out.done()


if __name__ == "__main__":
    sys.exit(main())
