"""plugin_diag collector — plugin status, errors, hooks, channels, deps, trajectory drift."""

from __future__ import annotations

import glob
import json
import os
import re
import socket
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import trajectory
from ..core.context import DiagContext
from ..core.registry import register
from ..core.types import Report, Section, Verdict
from ..jsonlog import parse_name
from ..sensitive import sanitize_text
from ..timeutil import fmt_hms


READY_RE = re.compile(r"ready\s*\((\d+)\s+plugins?:\s*([^;]+);\s*([^)]+)\)")

HOOK_HANDLER_FAIL_RE = re.compile(
    r"\[hooks\]\s+(\S+)\s+handler from\s+(\S+)\s+failed:\s+(.*)", re.DOTALL,
)
HOOK_ASYNC_VIOLATION_RE = re.compile(
    r"(\S+)\s+handler from\s+(\S+)\s+returned a Promise",
)
HOOK_AFTER_TOOL_RE = re.compile(
    r"(\w+)\s+hook failed:\s+(?:tool=\S+\s+)?error=(.*)",
)
HOOK_INTERNAL_RE = re.compile(
    r"Hook error \[([^\]]+)\]:\s+(.*)",
)

CHANNEL_FAIL_RE = re.compile(
    r"\[([^\]]+)\]\s+channel startup failed:\s+(.*)", re.IGNORECASE,
)

_PM_BRACKET_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)", re.DOTALL)
_PM_EXCLUDED_PREFIXES = frozenset([
    "plugins", "hooks", "ConfigManager", "plugin-manager", "PluginManager",
])
_PM_BODY_PLUGIN_RE = re.compile(r"^([a-z0-9@/_.-]+)\s+failed", re.I)
_PM_PAREN_PLUGIN_RE = re.compile(
    r"\((?:load|register|init|start|stop):\s*([a-z0-9@/_.-]+)\)", re.I,
)
_PM_PLUGIN_EQ_RE = re.compile(r"\bplugin=([a-z0-9@/_.-]+)", re.I)

_URL_IN_VAL_RE = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.\-]*)(?::\d+)?")


def _msg_text(obj: Dict[str, Any]) -> str:
    v = obj.get("1", "")
    if isinstance(v, str) and v:
        return v
    for k in ("0", "2", "msg", "message"):
        v = obj.get(k, "")
        if isinstance(v, str) and v:
            return v
    return ""


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _dedup_messages(samples, max_unique: int = 5):
    buckets: Dict[str, Tuple[Optional[str], str, str]] = {}
    for ts, lvl, text in samples:
        key = text[:60]
        if key not in buckets or (ts or "") > (buckets[key][0] or ""):
            buckets[key] = (ts, lvl, text)
    return sorted(
        buckets.values(), key=lambda x: x[0] or "", reverse=True,
    )[:max_unique]


def _extract_plugin_from_pm_message(text: str) -> Optional[str]:
    m = _PM_BRACKET_RE.match(text)
    if not m:
        return None
    prefix, body = m.group(1), m.group(2)
    if prefix not in _PM_EXCLUDED_PREFIXES:
        return prefix
    m2 = _PM_BODY_PLUGIN_RE.match(body)
    if m2:
        return m2.group(1)
    m3 = _PM_PAREN_PLUGIN_RE.search(body)
    if m3:
        return m3.group(1)
    m4 = _PM_PLUGIN_EQ_RE.search(text)
    if m4:
        return m4.group(1)
    return None


def _scan_logs(log_files: List[str]) -> Dict[str, Any]:
    plugin_level_counts: Dict[str, Counter] = defaultdict(Counter)
    plugin_error_samples: Dict[str, List] = defaultdict(list)
    gateway_starts: List[Tuple] = []
    hook_errors: List[Tuple] = []
    subsystem_level_counts: Dict[str, Counter] = defaultdict(Counter)
    subsystem_error_samples: Dict[str, List] = defaultdict(list)
    plugin_diag_messages: List[Tuple] = []

    for logf in log_files:
        try:
            fh = open(logf, "r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                plugin, sub = parse_name(o)
                lvl = o.get("_meta", {}).get("logLevelName", "")
                ts = o.get("time") or o.get("_meta", {}).get("date", "")
                text = _msg_text(o)

                if plugin:
                    plugin_level_counts[plugin][lvl] += 1
                    if lvl in ("WARN", "ERROR", "FATAL"):
                        plugin_error_samples[plugin].append((ts, lvl, text))

                if sub:
                    if sub == "gateway":
                        m = READY_RE.search(text)
                        if m:
                            gateway_starts.append((
                                _parse_ts(ts), ts,
                                m.group(2).strip(), m.group(3).strip(),
                            ))
                    elif sub == "plugins":
                        plugin_diag_messages.append((ts, lvl, text))
                        target = _extract_plugin_from_pm_message(text)
                        if target:
                            plugin_level_counts[target][lvl] += 1
                            if lvl in ("WARN", "ERROR", "FATAL"):
                                plugin_error_samples[target].append(
                                    (ts, lvl, text),
                                )
                    elif "/" in sub and not sub.startswith(
                        ("gateway", "agent", "skills"),
                    ):
                        subsystem_level_counts[sub][lvl] += 1
                        if lvl in ("WARN", "ERROR", "FATAL"):
                            subsystem_error_samples[sub].append(
                                (ts, lvl, text),
                            )

                    m = CHANNEL_FAIL_RE.search(text)
                    if m:
                        pid = m.group(1)
                        plugin_error_samples[pid].append(
                            (ts, "ERROR",
                             f"channel startup failed: {m.group(2)}"),
                        )
                        plugin_level_counts[pid]["ERROR"] += 1

                if sub != "plugins" and not plugin:
                    target = _extract_plugin_from_pm_message(text)
                    if target:
                        plugin_level_counts[target][lvl] += 1
                        if lvl in ("WARN", "ERROR", "FATAL"):
                            plugin_error_samples[target].append(
                                (ts, lvl, text),
                            )

                m = HOOK_HANDLER_FAIL_RE.search(text)
                if m:
                    hook_errors.append(
                        (ts, m.group(2), m.group(1), m.group(3).strip()[:200]),
                    )
                    continue
                m = HOOK_ASYNC_VIOLATION_RE.search(text)
                if m:
                    hook_errors.append((
                        ts, m.group(2), m.group(1),
                        "returned Promise in sync hook",
                    ))
                    continue
                m = HOOK_AFTER_TOOL_RE.search(text)
                if m:
                    hook_errors.append((
                        ts, "(unknown)", m.group(1),
                        m.group(2).strip()[:200],
                    ))
                    continue
                m = HOOK_INTERNAL_RE.search(text)
                if m:
                    hook_errors.append((
                        ts, "(internal)", m.group(1),
                        m.group(2).strip()[:200],
                    ))

    return {
        "plugin_level_counts": plugin_level_counts,
        "plugin_error_samples": plugin_error_samples,
        "gateway_starts": gateway_starts,
        "hook_errors": hook_errors,
        "subsystem_level_counts": subsystem_level_counts,
        "subsystem_error_samples": subsystem_error_samples,
        "plugin_diag_messages": plugin_diag_messages,
    }


def _load_configured(config_path: str) -> Tuple[Dict[str, bool], Dict[str, Any]]:
    configured: Dict[str, bool] = {}
    if not config_path or not os.path.isfile(config_path):
        return configured, {
            "found": False, "reason": "config_not_found",
            "checked": config_path or "",
        }
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        entries = cfg.get("plugins", {}).get("entries", {}) or {}
        for k, v in entries.items():
            if isinstance(v, dict):
                configured[k] = bool(v.get("enabled", False))
    except (OSError, json.JSONDecodeError) as e:
        return configured, {
            "found": False, "reason": "config_unreadable",
            "checked": config_path, "error": str(e)[:200],
        }
    return configured, {"found": True}


def _load_extensions(oc_home: str) -> List[Tuple[str, str]]:
    extensions: List[Tuple[str, str]] = []
    ext_dir = os.path.join(oc_home, "extensions") if oc_home else ""
    if not (ext_dir and os.path.isdir(ext_dir)):
        return extensions
    for d in sorted(os.listdir(ext_dir)):
        full = os.path.join(ext_dir, d)
        if not os.path.isdir(full):
            continue
        pkg = os.path.join(full, "package.json")
        ver = "?"
        if os.path.isfile(pkg):
            try:
                with open(pkg) as f:
                    ver = json.load(f).get("version", "?")
            except (OSError, json.JSONDecodeError):
                pass
        extensions.append((d, ver))
    return extensions


def _walk_urls(val: Any, out_set: Set[str]) -> None:
    if isinstance(val, str):
        for m in _URL_IN_VAL_RE.finditer(val):
            out_set.add(m.group(1).lower())
    elif isinstance(val, dict):
        for v in val.values():
            _walk_urls(v, out_set)
    elif isinstance(val, list):
        for v in val:
            _walk_urls(v, out_set)


def _section_state(
    s: Section, scan: Dict[str, Any], configured: Dict[str, bool],
    extensions: List[Tuple[str, str]],
) -> dict:
    gateway_starts = scan["gateway_starts"]
    latest_start = gateway_starts[-1] if gateway_starts else None
    loaded_set: Set[str] = set()
    body_lines: List[str] = []
    if latest_start:
        _, _, names_str, dur_str = latest_start
        loaded_list = [n.strip() for n in names_str.split(",")]
        loaded_set = set(loaded_list)
        body_lines.append(f"已加载: {names_str} (启动 {dur_str})")
    else:
        body_lines.append(
            "今日日志中未发现 Gateway ready 行（今日未重启，一致性检查跳过）",
        )

    config_enabled = {k for k, v in configured.items() if v}
    config_disabled = {k for k, v in configured.items() if not v}

    missing: Set[str] = set()
    extra: Set[str] = set()
    if loaded_set:
        missing = config_enabled - loaded_set
        extra = loaded_set - config_enabled - config_disabled
        for p in sorted(missing):
            body_lines.append(f"{p}: 配置已启用但未出现在 loaded 列表")
        for p in sorted(extra):
            body_lines.append(f"{p}: 已加载但非显式配置（非显式配置）")
        if config_disabled:
            body_lines.append(f"已禁用: {', '.join(sorted(config_disabled))}")
        if not missing and not extra and not config_disabled:
            body_lines.append("配置与实际加载状态一致")
    else:
        if config_enabled:
            body_lines.append(f"配置已启用: {', '.join(sorted(config_enabled))}")
        if config_disabled:
            body_lines.append(f"配置已禁用: {', '.join(sorted(config_disabled))}")

    if extensions:
        body_lines.append(
            "外部扩展: " + ", ".join(f"{n} ({v})" for n, v in extensions),
        )

    state_data = {
        "loaded": sorted(loaded_set),
        "missing": sorted(missing),
        "extra": sorted(extra),
        "disabled": sorted(config_disabled),
        "extensions": [{"name": n, "version": v} for n, v in extensions],
    }

    if missing:
        s.warn(
            "plugin.state",
            f"插件未加载（配置启用但 loaded 列表缺失）: {len(missing)} 个",
            evidence="\n".join(body_lines),
            data=state_data,
        )
    elif extra:
        s.warn(
            "plugin.state",
            f"插件状态不一致（loaded 中含非显式配置）: {len(extra)} 个",
            evidence="\n".join(body_lines),
            data=state_data,
        )
    else:
        s.ok(
            "plugin.state",
            f"插件状态一致 (loaded={len(loaded_set)}, "
            f"配置启用={len(config_enabled)}, 禁用={len(config_disabled)})",
            evidence="\n".join(body_lines),
            data=state_data,
        )
    return {"state": state_data}


def _section_errors(
    s: Section, scan: Dict[str, Any], configured: Dict[str, bool],
    unmask: bool,
) -> dict:
    def _scrub(t: str) -> str:
        return t if unmask else sanitize_text(t)

    plugin_level_counts = scan["plugin_level_counts"]
    plugin_error_samples = scan["plugin_error_samples"]
    plugin_diag_messages = scan["plugin_diag_messages"]

    all_plugins = sorted(
        set(configured.keys()) | set(plugin_level_counts.keys()),
    )
    if not all_plugins and not plugin_diag_messages:
        s.ok(
            "plugin.errors",
            "（今日无插件日志数据）",
            data={"plugin_errors": {}},
        )
        return {"plugin_errors": {}}

    errors_payload: Dict[str, Any] = {}
    body_lines: List[str] = []
    total_error = 0

    def rank(p):
        c = plugin_level_counts.get(p, Counter())
        return (
            -(c.get("ERROR", 0) + c.get("FATAL", 0)),
            -c.get("WARN", 0), p,
        )

    for p in sorted(all_plugins, key=rank):
        c = plugin_level_counts.get(p, Counter())
        err = c.get("ERROR", 0) + c.get("FATAL", 0)
        warn = c.get("WARN", 0)
        total = sum(c.values())
        total_error += err
        note = ""
        if p in configured and not configured[p]:
            note = " [disabled]"
        elif p not in configured and p in plugin_level_counts:
            note = " [auto/extension]"
        body_lines.append(
            f"{p}: {err} ERROR, {warn} WARN, {total} total{note}",
        )
        samples = plugin_error_samples.get(p, [])
        sample_payload = []
        if samples:
            for ts, lvl, text in _dedup_messages(samples, max_unique=5):
                tag = {
                    "ERROR": "E", "FATAL": "F", "WARN": "W",
                }.get(lvl, "?")
                snippet = _scrub(text.replace("\n", " "))[:200]
                body_lines.append(f"  [{tag}] {fmt_hms(ts)}: {snippet}")
                sample_payload.append({
                    "ts": ts, "level": lvl, "msg": _scrub(text[:300]),
                })
        if err > 0 or warn > 0 or sample_payload:
            errors_payload[p] = {
                "error_count": err, "warn_count": warn,
                "total": total, "samples": sample_payload,
            }

    pm_errors = [
        (ts, lvl, t) for ts, lvl, t in plugin_diag_messages
        if lvl in ("ERROR", "FATAL")
    ]
    pm_warns = [
        (ts, lvl, t) for ts, lvl, t in plugin_diag_messages if lvl == "WARN"
    ]
    if pm_errors or pm_warns:
        total_error += len(pm_errors)
        body_lines.append(
            f"[plugin-manager]: {len(pm_errors)} ERROR, "
            f"{len(pm_warns)} WARN, {len(plugin_diag_messages)} total",
        )

    payload = {
        "plugin_errors": errors_payload,
        "total_error_count": total_error,
        "pm_errors": len(pm_errors),
        "pm_warns": len(pm_warns),
    }

    if total_error > 20:
        s.fail(
            "plugin.errors",
            f"插件 ERROR 总数 {total_error} (>20)",
            evidence="\n".join(body_lines),
            data=payload,
        )
    elif total_error > 0:
        s.warn(
            "plugin.errors",
            f"插件 ERROR 总数 {total_error}",
            evidence="\n".join(body_lines),
            data=payload,
        )
    else:
        s.ok(
            "plugin.errors",
            "插件 ERROR=0",
            evidence="\n".join(body_lines) if body_lines else None,
            data=payload,
        )
    return payload


def _section_hooks(s: Section, scan: Dict[str, Any]) -> dict:
    hook_errors = scan["hook_errors"]
    if not hook_errors:
        s.ok(
            "plugin.hooks",
            "今日无 Hook 执行异常",
            data={"hook_errors": {"total": 0, "by_plugin": {}}},
        )
        return {"hook_errors": {"total": 0, "by_plugin": {}}}

    by_plugin: Dict[str, List[Tuple]] = defaultdict(list)
    for ts, plugin_id, hook_name, error_msg in hook_errors:
        by_plugin[plugin_id].append((ts, hook_name, error_msg))

    body_lines = [f"共 {len(hook_errors)} 次 Hook 执行异常:"]
    by_plugin_payload: Dict[str, Any] = {}
    for plugin_id in sorted(by_plugin, key=lambda p: -len(by_plugin[p])):
        entries = by_plugin[plugin_id]
        by_hook: Dict[str, List[Tuple]] = defaultdict(list)
        for ts, hook_name, err in entries:
            by_hook[hook_name].append((ts, err))
        body_lines.append(f"  {plugin_id}: {len(entries)} 次")
        hooks_payload: Dict[str, Any] = {}
        for hook_name in sorted(by_hook, key=lambda h: -len(by_hook[h])):
            hook_entries = by_hook[hook_name]
            body_lines.append(
                f"    hook={hook_name}: {len(hook_entries)} 次",
            )
            last = hook_entries[-1]
            body_lines.append(
                f"      最近: {fmt_hms(last[0])} {last[1][:100]}",
            )
            hooks_payload[hook_name] = {
                "count": len(hook_entries),
                "last_ts": last[0],
                "last_msg": last[1][:300],
            }
        by_plugin_payload[plugin_id] = {
            "count": len(entries), "hooks": hooks_payload,
        }
    payload = {
        "hook_errors": {
            "total": len(hook_errors),
            "by_plugin": by_plugin_payload,
        },
    }
    s.warn(
        "plugin.hooks",
        f"Hook 执行异常 {len(hook_errors)} 次",
        evidence="\n".join(body_lines),
        data=payload,
    )
    return payload


def _section_channels(s: Section, scan: Dict[str, Any]) -> dict:
    subsystem_level_counts = scan["subsystem_level_counts"]
    subsystem_error_samples = scan["subsystem_error_samples"]
    if not subsystem_level_counts:
        s.ok(
            "plugin.channels",
            "（今日无 Channel 子系统日志）",
            data={"channels": {}},
        )
        return {"channels": {}}

    channel_groups: Dict[str, List[str]] = defaultdict(list)
    for sub in subsystem_level_counts:
        channel_groups[sub.split("/")[0]].append(sub)

    channels_payload: Dict[str, Any] = {}
    body_lines: List[str] = []
    total_err = 0
    for prefix in sorted(channel_groups):
        subs = channel_groups[prefix]
        prefix_err = sum(
            subsystem_level_counts[ss].get("ERROR", 0)
            + subsystem_level_counts[ss].get("FATAL", 0)
            for ss in subs
        )
        prefix_warn = sum(
            subsystem_level_counts[ss].get("WARN", 0) for ss in subs
        )
        prefix_total = sum(
            sum(subsystem_level_counts[ss].values()) for ss in subs
        )
        total_err += prefix_err
        body_lines.append(
            f"{prefix}: {prefix_err} ERROR, {prefix_warn} WARN, "
            f"{prefix_total} total ({len(subs)} subsystems)",
        )
        if prefix_err > 0 or prefix_warn > 20:
            for sub in sorted(
                subs,
                key=lambda x: -(subsystem_level_counts[x].get("ERROR", 0)),
            ):
                sc = subsystem_level_counts[sub]
                sub_err = sc.get("ERROR", 0) + sc.get("FATAL", 0)
                sub_warn = sc.get("WARN", 0)
                if sub_err > 0 or sub_warn > 10:
                    short_sub = sub.split("/", 1)[1] if "/" in sub else sub
                    body_lines.append(
                        f"  {short_sub}: {sub_err}E {sub_warn}W",
                    )
                    samples = subsystem_error_samples.get(sub, [])
                    if samples:
                        for ts, _lvl, text in _dedup_messages(
                            samples, max_unique=2,
                        ):
                            body_lines.append(
                                f"    [{fmt_hms(ts)}] "
                                f"{text.replace(chr(10), ' ')[:150]}",
                            )
        channels_payload[prefix] = {
            "error_count": prefix_err,
            "warn_count": prefix_warn,
            "total": prefix_total,
            "subsystems": sorted(subs),
        }

    payload = {"channels": channels_payload}
    if total_err > 0:
        s.warn(
            "plugin.channels",
            f"Channel 子系统 ERROR: {total_err}",
            evidence="\n".join(body_lines),
            data=payload,
        )
    else:
        s.ok(
            "plugin.channels",
            f"Channel 子系统: {len(channels_payload)} 类，ERROR=0",
            evidence="\n".join(body_lines),
            data=payload,
        )
    return payload


def _section_deps(s: Section, config_path: str) -> dict:
    if not (config_path and os.path.isfile(config_path)):
        s.ok(
            "plugin.deps",
            "未发现已启用插件的外部依赖配置",
            data={"plugin_deps": {}, "plugin_deps_status": {
                "found": False, "reason": "config_not_found",
                "checked": config_path or "",
            }},
        )
        return {"plugin_deps": {}}

    plugin_deps: Dict[str, Set[str]] = {}
    try:
        with open(config_path) as f:
            cfg_all = json.load(f)
        entries = cfg_all.get("plugins", {}).get("entries", {}) or {}
        for pid, pconf in entries.items():
            if not isinstance(pconf, dict):
                continue
            if not pconf.get("enabled", False):
                continue
            hosts: Set[str] = set()
            _walk_urls(pconf, hosts)
            hosts = {
                h for h in hosts
                if not h.startswith(("127.", "localhost", "0.0.0.0"))
            }
            plugin_deps[pid] = hosts
    except (OSError, json.JSONDecodeError) as e:
        s.warn(
            "plugin.deps",
            f"配置读取/解析失败: {type(e).__name__}",
            data={
                "plugin_deps_status": {
                    "found": False, "reason": "config_unreadable",
                    "checked": config_path, "error": str(e)[:200],
                },
            },
        )
        return {"plugin_deps": {}}

    if not plugin_deps:
        s.ok(
            "plugin.deps",
            "未发现已启用插件的外部依赖配置",
            data={"plugin_deps": {}},
        )
        return {"plugin_deps": {}}

    deps_payload: Dict[str, Any] = {}
    body_lines: List[str] = [
        f"扫描: {len(plugin_deps)} 个已启用插件的配置",
    ]
    no_dep: List[str] = []
    dep_lines: List[str] = []
    failures = 0
    for pid in sorted(plugin_deps):
        hosts = plugin_deps[pid]
        host_results: List[Dict[str, Any]] = []
        if not hosts:
            no_dep.append(pid)
            deps_payload[pid] = {"hosts": []}
            continue
        for host in sorted(hosts):
            start = datetime.now()
            try:
                socket.setdefaulttimeout(3)
                socket.gethostbyname(host)
                elapsed = (datetime.now() - start).total_seconds() * 1000
                dep_lines.append(
                    f"  {pid} → {host}: 可达 ({elapsed:.0f}ms)",
                )
                host_results.append({
                    "host": host, "reachable": True,
                    "elapsed_ms": round(elapsed, 1),
                })
            except (socket.gaierror, socket.herror, OSError):
                dep_lines.append(
                    f"  {pid} → {host}: FAILED (DNS 解析失败)",
                )
                host_results.append({
                    "host": host, "reachable": False, "elapsed_ms": None,
                })
                failures += 1
        deps_payload[pid] = {"hosts": host_results}
    if dep_lines:
        body_lines.append("发现外部端点:")
        body_lines.extend(dep_lines)
    if no_dep:
        body_lines.append(
            f"无外部依赖的插件: {', '.join(no_dep)}",
        )

    payload = {
        "plugin_deps": deps_payload,
        "dns_failure_count": failures,
    }
    if failures > 0:
        s.warn(
            "plugin.deps",
            f"插件外部依赖 DNS 解析失败 {failures} 次",
            evidence="\n".join(body_lines),
            data=payload,
        )
    else:
        s.ok(
            "plugin.deps",
            f"插件外部依赖检查: {len(plugin_deps)} 个插件，DNS 全部可达",
            evidence="\n".join(body_lines),
            data=payload,
        )
    return payload


def _section_trajectory(
    s: Section, sessions_base: str, configured: Dict[str, bool],
) -> dict:
    files = trajectory.discover_trajectory_files(sessions_base)
    if not files:
        s.ok(
            "plugin.trajectory",
            "未发现 trajectory 文件 — 跳过 trajectory 插件分析",
            data={"trajectory_plugins": {"found": False}},
        )
        return {"trajectory_plugins": {"found": False}}

    runs = trajectory.collect_runs(files)
    runs.sort(key=lambda r: r.started_ts_ms or 0, reverse=True)
    recent = [r for r in runs[:30] if r.plugin_entries]
    if not recent:
        s.ok(
            "plugin.trajectory",
            "最近 trajectory 中无 plugin metadata",
            data={"trajectory_plugins": {"found": True, "samples": 0}},
        )
        return {"trajectory_plugins": {"found": True, "samples": 0}}

    latest = recent[0]
    last_plugins = latest.plugin_entries
    plugin_errors_now = [
        p for p in last_plugins
        if p.get("error") and p.get("activated")
    ]
    plugin_disabled_with_reason = [
        p for p in last_plugins
        if p.get("error") and not p.get("activated")
    ]

    body_lines = [
        f"最新 run: {latest.session_id[:8]}#{latest.run_id[:8]} | "
        f"插件 entries={len(last_plugins)} | "
        f"importedRuntimePluginIds={len(latest.imported_runtime_plugin_ids)}",
    ]
    if plugin_errors_now:
        body_lines.append(
            f"FATAL: {len(plugin_errors_now)} 个插件 activated 但 error 非空:",
        )
        for p in plugin_errors_now[:10]:
            body_lines.append(
                f"    {p.get('id')}: error={p.get('error')} "
                f"(source={p.get('activationSource')})",
            )
    else:
        body_lines.append("最新 run: 0 个 activated 插件出错")
    if plugin_disabled_with_reason:
        body_lines.append(
            f"禁用插件（含 activationReason）: "
            f"{len(plugin_disabled_with_reason)} 个 "
            f"({', '.join(p.get('id', '?') for p in plugin_disabled_with_reason[:6])})",
        )

    drift_disabled_active: List[Dict] = []
    drift_active_disabled: List[Dict] = []
    for p in last_plugins:
        pid = p.get("id")
        if not pid:
            continue
        cfg_enabled = configured.get(pid)
        runtime_enabled = p.get("enabled")
        if cfg_enabled is True and runtime_enabled is False:
            drift_disabled_active.append(p)
        elif cfg_enabled is False and runtime_enabled is True:
            drift_active_disabled.append(p)

    if drift_disabled_active or drift_active_disabled:
        body_lines.append(
            f"插件配置 vs trajectory 漂移: "
            f"{len(drift_disabled_active) + len(drift_active_disabled)} 项",
        )
        for p in drift_disabled_active[:5]:
            body_lines.append(
                f"    {p.get('id')}: config 启用但 trajectory enabled=false "
                f"(reason={p.get('activationReason') or '?'})",
            )
        for p in drift_active_disabled[:5]:
            body_lines.append(
                f"    {p.get('id')}: config 禁用但 trajectory enabled=true",
            )
    else:
        body_lines.append("config vs trajectory 漂移: 无")

    entry_ids = {p.get("id") for p in last_plugins}
    imported_unused = [
        pid for pid in latest.imported_runtime_plugin_ids
        if pid and pid not in entry_ids
    ]
    if imported_unused:
        body_lines.append(
            f"imported 但未在 entries 中: {len(imported_unused)} 个",
        )

    summary = {
        "found": True,
        "samples": len(recent),
        "latest_run": {
            "sessionId": latest.session_id,
            "runId": latest.run_id,
            "ts_ms": latest.started_ts_ms,
        },
        "plugin_count_latest": len(last_plugins),
        "plugin_errors_recent": [
            {
                "id": p.get("id"), "error": p.get("error"),
                "activated": p.get("activated"),
                "activationReason": p.get("activationReason"),
            }
            for p in plugin_errors_now
        ],
        "plugin_drift": {
            "config_enabled_runtime_disabled": [
                {
                    "id": p.get("id"), "enabled": p.get("enabled"),
                    "activated": p.get("activated"),
                    "reason": p.get("activationReason"),
                }
                for p in drift_disabled_active
            ],
            "config_disabled_runtime_enabled": [
                {
                    "id": p.get("id"), "enabled": p.get("enabled"),
                    "activated": p.get("activated"),
                }
                for p in drift_active_disabled
            ],
        },
        "imported_unused": imported_unused,
    }
    payload = {"trajectory_plugins": summary}

    drift_count = len(drift_disabled_active) + len(drift_active_disabled)
    if plugin_errors_now:
        s.fail(
            "plugin.trajectory",
            f"最新 run 中 {len(plugin_errors_now)} 个 activated 插件出错",
            evidence="\n".join(body_lines),
            data=payload,
        )
    elif drift_count > 0:
        s.warn(
            "plugin.trajectory",
            f"插件配置 vs trajectory 漂移: {drift_count} 项",
            evidence="\n".join(body_lines),
            data=payload,
        )
    else:
        s.ok(
            "plugin.trajectory",
            f"Trajectory 插件状态健康（{len(recent)} 个最近 run，"
            f"{len(last_plugins)} 个 entries）",
            evidence="\n".join(body_lines),
            data=payload,
        )
    return payload


@register
class PluginDiagCollector:
    id = "plugin_diag"
    title = "插件诊断"
    kind = "state"

    def collect(self, ctx: DiagContext, **_) -> Report:
        t0 = time.time()
        report = Report(module_id=self.id, title=self.title)

        today = datetime.now().strftime("%Y-%m-%d")
        log_dir = str(ctx.log_dir)
        today_logs = sorted(
            glob.glob(os.path.join(log_dir, f"openclaw-{today}.log")),
        )

        scan = _scan_logs(today_logs)
        configured, configured_status = _load_configured(str(ctx.config_path))
        extensions = _load_extensions(str(ctx.openclaw_home))

        if not configured_status.get("found", True):
            report.data["configured_status"] = configured_status

        s_state = report.section("9.1 插件状态一致性")
        report.data.update(_section_state(s_state, scan, configured, extensions))

        s_err = report.section("9.2 插件错误/警告")
        report.data.update(_section_errors(s_err, scan, configured, ctx.unmask))

        s_hooks = report.section("9.3 Hook 执行状态")
        report.data.update(_section_hooks(s_hooks, scan))

        s_chan = report.section("9.4 Channel 子系统")
        report.data.update(_section_channels(s_chan, scan))

        s_deps = report.section("9.5 插件外部依赖")
        report.data.update(_section_deps(s_deps, str(ctx.config_path)))

        s_traj = report.section("9.6 Trajectory 插件快照 + 漂移")
        report.data.update(
            _section_trajectory(s_traj, str(ctx.sessions_base), configured),
        )

        report.elapsed_ms = (time.time() - t0) * 1000
        return report
