#!/usr/bin/env python3
"""模块 4：Gateway 状态（进程、端口、生命周期、WS 错误码统一视图）。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output, recent_logs
from ocdiag.jsonlog import get_log_subsystem, parse_log_msg


def run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, "", ""


# ── 4.1: process & port ──

def section_process_port(out: output.Output, args, port: int) -> None:
    rc, stdout, stderr = run(["systemctl", "--user", "status", "openclaw-gateway"])
    svc_status = (stdout or "") + (stderr or "")
    if "Active:" in svc_status:
        for ln in svc_status.splitlines():
            if "Active:" in ln:
                out.item(f"Systemd: {ln.strip()}")
            if "Main PID:" in ln:
                out.item(ln.strip())
    else:
        out.item("Systemd: 未以 systemd 管理或无法获取状态")

    rc, pids, _ = run(["pgrep", "-f", "openclaw-gatewa"])
    pid_list = pids.splitlines()[:5] if pids else []
    if pid_list:
        rc, ps_out, _ = run(["ps", "-p", ",".join(pid_list),
                             "-o", "pid,ppid,etime,%mem,rss,args", "--no-headers"])
        if rc == 0 and ps_out.strip():
            out.item("进程: " + " | ".join(ps_out.strip().splitlines()))
    out.set_data("pids", pid_list)

    rc, ss_out, _ = run(["ss", "-tlnp", f"sport = :{port}"])
    listening = port in [int(p) for p in re.findall(rf":{port}\b", ss_out)]
    rc, http_out, _ = run([
        "curl", "-s", "-m5", "-o", "/dev/null", "-w", "%{http_code}",
        f"http://127.0.0.1:{port}/",
    ])
    gw_http = http_out.strip() or "000"
    out.item(f"端口 {port} 监听: {'是' if listening else '否'} | HTTP 健康检查: {gw_http}")
    out.set_data("port", port)
    out.set_data("port_listening", listening)
    out.set_data("http_health_code", gw_http)


# ── 4.2: 24h restart events ──

def section_restart_events(out: output.Output) -> None:
    rc, raw, _ = run([
        "journalctl", "--user", "-u", "openclaw-gateway",
        "--since", "24 hours ago", "--no-pager",
    ], timeout=15)
    if not raw:
        out.item("24h 启停事件: 无 — 近 24h 无重启记录")
        out.set_data("restart_events", [])
        return

    lifecycle = [ln for ln in raw.splitlines()
                 if re.search(r"Started openclaw|Stopped openclaw|Main process exited|"
                              r"SIGTERM|SIGKILL|OOM Killer", ln, re.I)]
    restart_count = sum(1 for ln in raw.splitlines()
                        if re.search(r"Started openclaw", ln, re.I))

    if not lifecycle:
        out.item(f"24h 启停事件: {restart_count} 次启动 — 近 24h 无重启/停止记录")
        out.set_data("restart_count_24h", restart_count)
        return

    rc, json_out, _ = run([
        "journalctl", "--user", "-u", "openclaw-gateway",
        "--since", "24 hours ago", "--no-pager", "-o", "json",
    ], timeout=15)

    seen = set()
    results = []
    for line in json_out.splitlines() if json_out else []:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("MESSAGE", "") or ""
        ts_us = int(obj.get("__REALTIME_TIMESTAMP", 0) or 0)
        ts_str = datetime.fromtimestamp(ts_us / 1_000_000).strftime("%m月 %d %H:%M:%S") if ts_us else ""
        pid = obj.get("_PID", "")
        syslog_id = obj.get("SYSLOG_IDENTIFIER", "")
        is_systemd = syslog_id == "systemd"
        if re.search(r"Started", msg, re.I):
            etype = "启动"
        elif re.search(r"Stopped|stop", msg, re.I):
            etype = "停止"
        elif re.search(r"SIGTERM", msg, re.I):
            etype = "SIGTERM"
        elif re.search(r"SIGKILL", msg, re.I):
            etype = "SIGKILL"
        elif re.search(r"Main process exited", msg, re.I):
            m2 = re.search(r"code=(\w+)", msg)
            m3 = re.search(r"status=(\d+)", msg)
            code_info = f" code={m2.group(1)}" if m2 else ""
            status_info = f" status={m3.group(1)}" if m3 else ""
            etype = f"进程退出{code_info}{status_info}"
        elif re.search(r"OOM", msg, re.I):
            etype = "OOM"
        else:
            continue
        key = f"{ts_str}|{etype}"
        if key in seen:
            continue
        seen.add(key)
        if is_systemd or not pid:
            results.append((ts_us, f"[{ts_str}] {etype}"))
        else:
            results.append((ts_us, f"[{ts_str}] PID={pid} {etype}"))

    results.sort()
    out.item(f"24h 启停事件: {restart_count} 次启动")
    if results:
        out.evidence("journalctl", "\n".join(line for _, line in results))
    out.set_data("restart_count_24h", restart_count)
    out.set_data("restart_events", [line for _, line in results])


# ── 4.3: model API connectivity ──

def section_model_api(out: output.Output, args) -> None:
    if not os.path.isfile(args.config):
        return
    try:
        with open(args.config) as f:
            cfg = json.load(f)
    except Exception:
        return
    models = cfg.get("models", {}) or {}
    all_cfgs = {}
    if isinstance(models.get("configs"), dict):
        all_cfgs.update(models["configs"])
    if isinstance(models.get("providers"), dict):
        all_cfgs.update(models["providers"])
    seen_urls = set()
    api_results = []
    for name, v in all_cfgs.items():
        if not isinstance(v, dict):
            continue
        base_url = v.get("baseURL") or v.get("baseUrl")
        if not base_url:
            continue
        url_key = base_url.split("/v1", 1)[0].rstrip("/")
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        rc, stdout, _ = run([
            "curl", "-s", "-m5", "-o", "/dev/null", "-w", "%{http_code}", url_key,
        ])
        api_http = stdout.strip() or "000"
        out.item(f"模型 API [{url_key}]: HTTP {api_http}")
        api_results.append({"url": url_key, "http_code": api_http})
    out.set_data("model_api", api_results)


# ── 4.4: WS lifecycle analysis ──

VALID_SUBSYSTEMS = ("feishu/core/lark-client", "feishu/channel/monitor", "gateway/health-monitor")


def parse_ws_ts(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def extract_account(msg: str) -> str:
    m = re.search(r"feishu\[([^\]]+)\]", msg)
    if m:
        return m.group(1)
    m = re.search(r"accountId=([A-Za-z0-9_.-]+)", msg)
    if m:
        return m.group(1)
    m = re.search(r"account\s+([A-Za-z0-9_.-]+)", msg)
    if m:
        return m.group(1)
    return ""


def section_ws_lifecycle(out: output.Output, app_log: str) -> None:
    if not app_log or not os.path.isfile(app_log):
        return
    keyword_re = re.compile(
        r"ws ready|WS ready|websocket|ws error|ws close|ws reconnect|"
        r"health.monitor|channel.*connect|expired.*discard|starting.*WebSocket|"
        r"event-dispatch is ready|stopping feishu|stopped feishu|starting feishu|"
        r"disconnecting WebSocket",
        re.IGNORECASE,
    )
    events = []
    expired = []
    ws_errors = []
    health_count = 0
    try:
        with open(app_log, errors="replace") as f:
            for raw in f:
                if not keyword_re.search(raw):
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    obj = None
                if obj is None:
                    low = raw.lower()
                    if "expired" in low and "discard" in low:
                        m = re.match(r"\[?(\d{4}-\d{2}-\d{2}T?\d{2}:\d{2}:\d{2})", raw)
                        ts = m.group(1) if m else ""
                        expired.append((ts, raw[:200]))
                    continue
                ts_raw = obj.get("time", "")
                ts_dt = parse_ws_ts(ts_raw)
                ts_str = ts_raw[11:19] if ts_raw else ""
                sub = get_log_subsystem(obj)
                msg = parse_log_msg(obj)
                low = msg.lower()
                is_valid_subsystem = (
                    sub in VALID_SUBSYSTEMS or sub.startswith("gateway/channels/")
                )
                is_event_dispatch_ready = (not sub) and "event-dispatch is ready" in low
                if not is_valid_subsystem and not is_event_dispatch_ready:
                    continue
                if "health-monitor" in sub or "health-monitor" in low:
                    health_count += 1
                    continue
                account = extract_account(msg)
                kind = None
                if "expired" in low and "discard" in low:
                    expired.append((ts_str, msg[:200]))
                    continue
                if "event-dispatch is ready" in low:
                    kind = "ready"
                elif re.search(r"starting feishu\[[^\]]+\]\s*\(mode:\s*websocket\)", msg, re.I):
                    kind = "init"
                elif re.search(r"feishu\[[^\]]+\]:\s*starting WebSocket connection", msg, re.I):
                    kind = "start_ws"
                elif re.search(r"feishu\[[^\]]+\]:\s*WebSocket client started", msg, re.I):
                    kind = "client_up"
                elif re.search(r"websocket started for account", msg, re.I):
                    kind = "monitor_up"
                elif (re.search(r"^stopping feishu\[[^\]]+\]\s*$", msg.strip(), re.I) or
                      re.search(r"\|\s*stopping feishu\[", msg, re.I)):
                    kind = "stopping"
                elif (re.search(r"^stopped feishu\[[^\]]+\]\s*$", msg.strip(), re.I) or
                      re.search(r"\|\s*stopped feishu\[", msg, re.I)):
                    kind = "stopped"
                elif "disconnecting websocket" in low:
                    kind = "disconnecting"
                elif any(x in low for x in ["ws close", "ws error", "closed before connect", "connection lost"]):
                    kind = "ws_error"
                    code_m = re.search(r"code[=: ]+(\d+)", msg)
                    reason_m = re.search(r"reason[=: ]+([^\s,)]+)", msg)
                    detail = []
                    if code_m: detail.append(f"code={code_m.group(1)}")
                    if reason_m: detail.append(f"reason={reason_m.group(1)}")
                    ws_errors.append((ts_str, account or "?", " ".join(detail) or msg[:120]))
                elif "reconnect" in low and "websocket" in low:
                    kind = "reconnect"
                else:
                    continue
                events.append((ts_dt, ts_str, account, kind, msg))
    except OSError:
        out.item("Channel WS: 读取应用日志失败")
        return

    if not events and not expired:
        out.item("Channel WS: 今日无 WS 相关事件记录")
        return

    by_account = defaultdict(list)
    ready_events = []
    for e in events:
        if e[3] == "ready":
            ready_events.append(e)
        elif e[2]:
            by_account[e[2]].append(e)
        else:
            by_account["?"].append(e)

    ready_events.sort(key=lambda x: (x[0] or datetime.min))
    candidates = []
    for acc, evs in by_account.items():
        if acc == "?":
            continue
        for ev in evs:
            if ev[3] == "init" and ev[0] is not None:
                candidates.append([ev[0], acc, False])

    for r in ready_events:
        r_ts = r[0]
        if r_ts is None:
            by_account["?"].append(r)
            continue
        best = None
        best_dt = 999999
        for c in candidates:
            if c[2]:
                continue
            try:
                delta = (r_ts - c[0]).total_seconds()
            except Exception:
                continue
            if 0 <= delta <= 30 and delta < best_dt:
                best_dt = delta
                best = c
        if best is not None:
            best[2] = True
            by_account[best[1]].append(r)
        else:
            by_account["?"].append(r)

    for acc in by_account:
        by_account[acc].sort(key=lambda x: (x[0] or datetime.min, x[1]))

    cycle_summaries = []
    per_account_cycles = defaultdict(int)

    def flush(acc, cur_list):
        if not cur_list:
            return
        kinds = [k for _, _, _, k, _ in cur_list]
        t0 = cur_list[0][1]
        if "ready" in kinds:
            try:
                s_dt = next(e[0] for e in cur_list if e[3] in ("init", "start_ws"))
                r_dt = next(e[0] for e in cur_list if e[3] == "ready")
                dur = (r_dt - s_dt).total_seconds() if (s_dt and r_dt) else None
            except StopIteration:
                dur = None
            if any(k in kinds for k in ("stopping", "disconnecting")) and any(
                k in kinds for k in ("init", "start_ws")
            ):
                label = "重连→就绪"
            elif "init" in kinds or "start_ws" in kinds:
                label = "建连→就绪"
            else:
                label = "就绪"
            extra = f" (耗时 {dur:.1f}s)" if dur is not None and dur >= 0 else ""
            cycle_summaries.append((t0, acc, f"{label}{extra}", True))
        elif any(k in kinds for k in ("init", "start_ws", "client_up", "monitor_up")):
            cycle_summaries.append((t0, acc, "建连（未见 ready）", False))
        elif any(k in kinds for k in ("stopping", "stopped", "disconnecting")):
            cycle_summaries.append((t0, acc, "停止", None))
        elif "ws_error" in kinds:
            cycle_summaries.append((t0, acc, "错误", False))
        per_account_cycles[acc] += 1

    for acc, evs in by_account.items():
        if acc == "?":
            continue
        cur = []
        for e in evs:
            if not cur:
                cur.append(e)
                continue
            prev_dt = cur[-1][0]
            cur_dt = e[0]
            try:
                gap = (cur_dt - prev_dt).total_seconds() if (prev_dt and cur_dt) else 0
            except Exception:
                gap = 0
            if gap > 60:
                flush(acc, cur)
                cur = [e]
            else:
                cur.append(e)
        flush(acc, cur)

    cycle_summaries.sort(key=lambda x: x[0])

    total_ready = sum(1 for _, _, _, k, _ in events if k == "ready")
    total_attempts = sum(1 for _, _, _, k, _ in events if k == "init")
    total_stops = sum(1 for _, _, _, k, _ in events if k == "stopping")
    total_errors = len(ws_errors)

    def attempt_times(evs):
        ts_list = [e[0] for e in evs if e[3] == "init" and e[0] is not None]
        if not ts_list:
            ts_list = [e[0] for e in evs if e[3] == "start_ws" and e[0] is not None]
        ts_list.sort()
        return ts_list

    freq_flags = []
    for acc, evs in by_account.items():
        if acc == "?":
            continue
        attempts = attempt_times(evs)
        for i in range(len(attempts)):
            window = [t for t in attempts[i:] if (t - attempts[i]).total_seconds() <= 300]
            if len(window) >= 3:
                freq_flags.append((acc, attempts[i].strftime("%H:%M:%S"), len(window)))
                break

    intervals = []
    for acc, evs in by_account.items():
        if acc == "?":
            continue
        attempts = attempt_times(evs)
        for a, b in zip(attempts, attempts[1:]):
            intervals.append((b - a).total_seconds())
    avg_interval = sum(intervals) / len(intervals) if intervals else None

    body = []
    body.append(f"概览: {total_attempts} 次建连尝试, {total_ready} 次就绪, {total_stops} 次停止/断开, {total_errors} 次错误")
    per_acc_str = ", ".join(f"{a}={n}" for a, n in sorted(per_account_cycles.items()))
    if per_acc_str:
        body.append(f"各账号生命周期片段数: {per_acc_str}")
    if avg_interval is not None:
        body.append(f"平均重连间隔: {avg_interval:.1f}s")
    if freq_flags:
        for acc, t0, n in freq_flags:
            body.append(f"频繁重连: {acc} 在 {t0} 起 5 分钟内建连 {n} 次")
    if cycle_summaries:
        body.append("")
        body.append("生命周期时间线:")
        for ts_str, acc, summary, _ok in cycle_summaries:
            body.append(f"     [{ts_str}] feishu[{acc}]: {summary}")
    if ws_errors:
        body.append("")
        body.append(f"WS 错误明细: {len(ws_errors)} 条")
        for ts_str, acc, detail in ws_errors[:10]:
            body.append(f"     [{ts_str}] feishu[{acc}]: {detail}")
        if len(ws_errors) > 10:
            body.append(f"     ... 共 {len(ws_errors)} 条")
    if expired:
        body.append("")
        body.append(f"过期丢弃: {len(expired)} 条消息")
        for ts_str, msg in expired[:10]:
            body.append(f"     [{ts_str}] {msg}")
        if len(expired) > 10:
            body.append(f"     ... 共 {len(expired)} 条")
    if health_count > 0:
        body.append("")
        body.append(f"health-monitor: {health_count} 条心跳记录（未展开）")

    if body:
        out.item("Channel WS 状态 — WebSocket 消息通道的连接、断连和消息丢失记录:")
        out.evidence("应用日志", "\n".join(body))
    out.set_data("ws_summary", {
        "attempts": total_attempts,
        "ready": total_ready,
        "stops": total_stops,
        "errors": total_errors,
        "avg_interval_s": avg_interval,
        "freq_reconnect": [{"account": a, "from": t, "count": n} for a, t, n in freq_flags],
        "expired_count": len(expired),
        "health_count": health_count,
    })


# ── 4.5: gateway error codes (auth + WS close) ──

KNOWN_REASONS = [
    ("HTTP", 401, r"trusted_proxy_user_missing", "可信代理用户缺失，auth.mode=trustedProxy 时需在请求头设置 X-Remote-User"),
    ("HTTP", 401, r"unauthorized", "内部 API 认证失败，通常是 auth.mode 配置问题"),
    ("HTTP", 401, r".*", "内部 API 认证失败"),
    ("WS", 1008, r"pairing required", "节点未配对，需要执行 openclaw devices list 并批准配对请求"),
    ("WS", 1008, r"not_paired", "节点未配对"),
    ("WS", 1008, r"slow consumer", "客户端消费消息太慢，被服务端主动断开"),
    ("WS", 1008, r"connect challenge missing", "连接握手缺少 nonce，认证流程异常"),
    ("WS", 1008, r"connect challenge timeout", "连接握手超时，可能是网络延迟或服务端未响应"),
    ("WS", 1008, r"connect failed", "连接认证失败，可能是密钥/证书不匹配"),
    ("WS", 1008, r"Missing callSid", "语音通话缺少 callSid（Twilio 集成问题）"),
    ("WS", 1008, r"Unknown call", "未知的语音通话 ID"),
    ("WS", 1008, r"Start timeout", "语音会话启动超时"),
    ("WS", 1006, r".*", "连接异常断开，未收到 close frame — 通常是网络中断、进程崩溃或超时"),
    ("WS", 1001, r".*", "端点正在离开（服务器关闭或客户端断开）"),
    ("WS", 1011, r".*", "服务器内部错误导致关闭"),
    ("WS", 1012, r".*", "服务器正在重启"),
    ("WS", 1013, r".*", "服务器暂时不可用，请稍后重试"),
    ("WS", 1000, r".*", "正常关闭"),
]


def explain(kind, code, reason):
    for k, c, pat, expl in KNOWN_REASONS:
        if k == kind and c == code and re.search(pat, reason or "", re.I):
            return expl
    return None


def classify_err(msg: str):
    low = msg.lower()
    if "trusted_proxy_user_missing" in low:
        return ("HTTP", 401, "trusted_proxy_user_missing")
    if "unauthorized" in low:
        return ("HTTP", 401, "unauthorized")
    m = re.search(r"gateway closed\s*\((\d+)\)\s*:\s*(.*)", msg, re.I)
    if m:
        return ("WS", int(m.group(1)), m.group(2).strip())
    m = re.search(r"(?:ws close|ws error|closed).*?code[=: ]*(\d{4})", msg, re.I)
    if m:
        code = int(m.group(1))
        m2 = re.search(r"reason[=: ]*([^\s,)]+.*?)(?:\s*$|\s*[,|)])", msg, re.I)
        return ("WS", code, (m2.group(1).strip() if m2 else ""))
    if re.search(r"closed before connect|abnormal clos", msg, re.I):
        return ("WS", 1006, "abnormal closure")
    return None


VALID_GATEWAY_PREFIXES = ("gateway/", "feishu/core/lark-client", "feishu/channel/")


def section_gateway_errors(out: output.Output, app_log: str) -> None:
    if not app_log or not os.path.isfile(app_log):
        return
    keyword_re = re.compile(
        r"unauthorized|trusted_proxy_user_missing|gateway closed|ws close|ws error|closed before",
        re.IGNORECASE,
    )
    events = []
    try:
        with open(app_log, errors="replace") as f:
            for raw in f:
                if not keyword_re.search(raw):
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                sub = get_log_subsystem(obj)
                if not sub:
                    continue
                if not any(sub == p or sub.startswith(p) for p in VALID_GATEWAY_PREFIXES):
                    continue
                msg = parse_log_msg(obj)
                ts = obj.get("time", "")[:19]
                r = classify_err(msg)
                if r is None:
                    continue
                kind, code, reason = r
                events.append((ts, kind, code, reason or "(no reason)"))
    except OSError:
        return

    if not events:
        out.item("Gateway 错误码: 0 条")
        out.set_data("gateway_errors", {"total": 0})
        return

    total = len(events)
    auth_count = sum(1 for e in events if e[1] == "HTTP")
    ws_count = sum(1 for e in events if e[1] == "WS")
    body = [f"共 {total} 条（认证 {auth_count} 条, WS 关闭 {ws_count} 条）", ""]

    combo = defaultdict(lambda: {"count": 0, "timeline": []})
    for ts, kind, code, reason in events:
        combo[(kind, code, reason)]["count"] += 1
        combo[(kind, code, reason)]["timeline"].append(ts)

    structured = []
    for (kind, code, reason), data in sorted(combo.items(), key=lambda x: -x[1]["count"]):
        count = data["count"]
        timeline = data["timeline"]
        expl = explain(kind, code, reason)
        body.append(f"{kind} {code}: {reason} ({count} 次)")
        if expl:
            body.append(f"     {expl}")
        shown = timeline[-5:] if len(timeline) > 5 else timeline
        for t in shown:
            body.append(f"     [{t}]")
        if len(timeline) > 5:
            body.append(f"     ... 共 {len(timeline)} 条，仅显示最近 5 条")
        body.append("")
        structured.append({
            "kind": kind, "code": code, "reason": reason,
            "count": count, "explanation": expl,
        })

    out.item("Gateway 错误码 — 认证 401 + WS 关闭码统一视图:")
    out.evidence("应用日志", "\n".join(body))
    out.set_data("gateway_errors", {
        "total": total,
        "auth_count": auth_count,
        "ws_count": ws_count,
        "by_reason": structured,
    })


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 4：Gateway 状态采集",
        prog="04_gateway",
    )
    args = parser.parse_args()

    out = output.init("gateway", json_mode=args.json, no_color=args.no_color)
    out.section("模块 4：Gateway 状态")

    port = 18789
    if os.path.isfile(args.config):
        try:
            with open(args.config) as f:
                cfg = json.load(f)
            cp = cfg.get("gateway", {}).get("port")
            if cp:
                port = int(cp)
        except Exception:
            pass

    section_process_port(out, args, port)
    section_restart_events(out)
    section_model_api(out, args)

    app_log = recent_logs.latest_app_log(args.log_dir)
    if app_log:
        section_ws_lifecycle(out, app_log)
        section_gateway_errors(out, app_log)
    else:
        out.item("Channel WS: 未找到应用日志文件")
        out.item("Gateway 错误码: 未找到应用日志文件")

    return out.done()


if __name__ == "__main__":
    sys.exit(main())
