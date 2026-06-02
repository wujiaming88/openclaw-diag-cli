#!/usr/bin/env python3
"""模块 9：插件诊断（一致性 + ERROR/WARN + Hook + Channel + 外部依赖 DNS）。"""

from __future__ import annotations

import glob
import json
import os
import re
import socket
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import cli, output, trajectory
from ocdiag.jsonlog import parse_name
from ocdiag.sensitive import sanitize_text
from ocdiag.timeutil import fmt_hms


READY_RE = re.compile(r"ready\s*\((\d+)\s+plugins?:\s*([^;]+);\s*([^)]+)\)")

HOOK_HANDLER_FAIL_RE = re.compile(
    r"\[hooks\]\s+(\S+)\s+handler from\s+(\S+)\s+failed:\s+(.*)", re.DOTALL
)
HOOK_ASYNC_VIOLATION_RE = re.compile(
    r"(\S+)\s+handler from\s+(\S+)\s+returned a Promise"
)
HOOK_AFTER_TOOL_RE = re.compile(
    r"(\w+)\s+hook failed:\s+(?:tool=\S+\s+)?error=(.*)"
)
HOOK_INTERNAL_RE = re.compile(
    r"Hook error \[([^\]]+)\]:\s+(.*)"
)

CHANNEL_FAIL_RE = re.compile(
    r"\[([^\]]+)\]\s+channel startup failed:\s+(.*)", re.IGNORECASE
)

_PM_BRACKET_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)", re.DOTALL)
_PM_EXCLUDED_PREFIXES = frozenset([
    "plugins", "hooks", "ConfigManager", "plugin-manager", "PluginManager",
])
_PM_BODY_PLUGIN_RE = re.compile(r"^([a-z0-9@/_.-]+)\s+failed", re.I)
_PM_PAREN_PLUGIN_RE = re.compile(
    r"\((?:load|register|init|start|stop):\s*([a-z0-9@/_.-]+)\)", re.I
)
_PM_PLUGIN_EQ_RE = re.compile(r"\bplugin=([a-z0-9@/_.-]+)", re.I)


def msg_text(obj):
    v = obj.get("1", "")
    if isinstance(v, str) and v:
        return v
    for k in ("0", "2", "msg", "message"):
        v = obj.get(k, "")
        if isinstance(v, str) and v:
            return v
    return ""


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def dedup_messages(samples, max_unique=5):
    buckets = {}
    for ts, lvl, text in samples:
        key = text[:60]
        if key not in buckets or (ts or "") > (buckets[key][0] or ""):
            buckets[key] = (ts, lvl, text)
    return sorted(buckets.values(), key=lambda x: x[0] or "", reverse=True)[:max_unique]


def extract_plugin_from_pm_message(text):
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


def scan_logs(today_logs):
    plugin_level_counts = defaultdict(Counter)
    plugin_error_samples = defaultdict(list)
    gateway_starts = []
    hook_errors = []
    subsystem_level_counts = defaultdict(Counter)
    subsystem_error_samples = defaultdict(list)
    plugin_diag_messages = []

    for logf in today_logs:
        try:
            fh = open(logf, "r", errors="replace")
        except OSError:
            # Best-effort: if today's log is unreadable, skip it; the parent
            # caller still surfaces "no log data" via the empty plugin_diag
            # output. (We don't fail the whole module for one missing file.)
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    # Expected: log files are JSONL; non-JSON lines are emitted
                    # by Node before logger init. Drop those lines silently.
                    continue
                plugin, sub = parse_name(o)
                lvl = o.get("_meta", {}).get("logLevelName", "")
                ts = o.get("time") or o.get("_meta", {}).get("date", "")
                text = msg_text(o)

                if plugin:
                    plugin_level_counts[plugin][lvl] += 1
                    if lvl in ("WARN", "ERROR", "FATAL"):
                        plugin_error_samples[plugin].append((ts, lvl, text))

                if sub:
                    if sub == "gateway":
                        m = READY_RE.search(text)
                        if m:
                            gateway_starts.append((
                                parse_ts(ts), ts, m.group(2).strip(), m.group(3).strip(),
                            ))
                    elif sub == "plugins":
                        plugin_diag_messages.append((ts, lvl, text))
                        target = extract_plugin_from_pm_message(text)
                        if target:
                            plugin_level_counts[target][lvl] += 1
                            if lvl in ("WARN", "ERROR", "FATAL"):
                                plugin_error_samples[target].append((ts, lvl, text))
                    elif "/" in sub and not sub.startswith(("gateway", "agent", "skills")):
                        subsystem_level_counts[sub][lvl] += 1
                        if lvl in ("WARN", "ERROR", "FATAL"):
                            subsystem_error_samples[sub].append((ts, lvl, text))

                    m = CHANNEL_FAIL_RE.search(text)
                    if m:
                        pid = m.group(1)
                        plugin_error_samples[pid].append(
                            (ts, "ERROR", f"channel startup failed: {m.group(2)}")
                        )
                        plugin_level_counts[pid]["ERROR"] += 1

                if sub != "plugins" and not plugin:
                    target = extract_plugin_from_pm_message(text)
                    if target:
                        plugin_level_counts[target][lvl] += 1
                        if lvl in ("WARN", "ERROR", "FATAL"):
                            plugin_error_samples[target].append((ts, lvl, text))

                m = HOOK_HANDLER_FAIL_RE.search(text)
                if m:
                    hook_errors.append((ts, m.group(2), m.group(1), m.group(3).strip()[:200]))
                    continue
                m = HOOK_ASYNC_VIOLATION_RE.search(text)
                if m:
                    hook_errors.append((ts, m.group(2), m.group(1), "returned Promise in sync hook"))
                    continue
                m = HOOK_AFTER_TOOL_RE.search(text)
                if m:
                    hook_errors.append((ts, "(unknown)", m.group(1), m.group(2).strip()[:200]))
                    continue
                m = HOOK_INTERNAL_RE.search(text)
                if m:
                    hook_errors.append((ts, "(internal)", m.group(1), m.group(2).strip()[:200]))

    return dict(
        plugin_level_counts=plugin_level_counts,
        plugin_error_samples=plugin_error_samples,
        gateway_starts=gateway_starts,
        hook_errors=hook_errors,
        subsystem_level_counts=subsystem_level_counts,
        subsystem_error_samples=subsystem_error_samples,
        plugin_diag_messages=plugin_diag_messages,
    )


def load_configured(config_path):
    """Return {plugin_id: enabled_bool}. Status reported as second return."""
    configured = {}
    status = {"found": True}
    if not config_path or not os.path.isfile(config_path):
        return configured, {"found": False, "reason": "config_not_found",
                            "checked": config_path or ""}
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        entries = cfg.get("plugins", {}).get("entries", {}) or {}
        for k, v in entries.items():
            if isinstance(v, dict):
                configured[k] = bool(v.get("enabled", False))
    except (OSError, json.JSONDecodeError) as e:
        return configured, {"found": False, "reason": "config_unreadable",
                            "checked": config_path, "error": str(e)[:200]}
    return configured, status


def load_extensions(oc_home):
    extensions = []
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
            except Exception:
                pass
        extensions.append((d, ver))
    return extensions


def section_state(out, scan, configured, extensions):
    out.progress(1, 5, "状态一致性")
    out.subsection("9.1 插件状态一致性")
    gateway_starts = scan["gateway_starts"]
    latest_start = gateway_starts[-1] if gateway_starts else None
    loaded_set = set()
    if latest_start:
        _, _, names_str, dur_str = latest_start
        loaded_list = [n.strip() for n in names_str.split(",")]
        loaded_set = set(loaded_list)
        out.item(f"已加载: {names_str} (启动 {dur_str})")
    else:
        out.item("今日日志中未发现 Gateway ready 行（今日未重启，一致性检查跳过）")

    config_enabled = set(k for k, v in configured.items() if v)
    config_disabled = set(k for k, v in configured.items() if not v)

    missing: set = set()
    extra: set = set()
    if loaded_set:
        missing = config_enabled - loaded_set
        extra = loaded_set - config_enabled - config_disabled
        if missing:
            for p in sorted(missing):
                out.item(f"{p}: 配置已启用但未出现在 loaded 列表")
        if extra:
            for p in sorted(extra):
                out.item(f"{p}: 已加载但非显式配置（非显式配置）")
        if config_disabled:
            out.item(f"已禁用: {', '.join(sorted(config_disabled))}")
        if not missing and not extra and not config_disabled:
            out.item("配置与实际加载状态一致")
    else:
        if config_enabled:
            out.item(f"配置已启用: {', '.join(sorted(config_enabled))}")
        if config_disabled:
            out.item(f"配置已禁用: {', '.join(sorted(config_disabled))}")

    if extensions:
        out.item(f"外部扩展: {', '.join(f'{n} ({v})' for n, v in extensions)}")

    out.set_data("state", {
        "loaded": sorted(loaded_set),
        "missing": sorted(missing),
        "extra": sorted(extra),
        "disabled": sorted(config_disabled),
        "extensions": [{"name": n, "version": v} for n, v in extensions],
    })


def section_errors(out, scan, configured, unmask=False):
    def _scrub(s: str) -> str:
        return s if unmask else sanitize_text(s)

    out.progress(2, 5, "错误/警告")
    out.subsection("9.2 插件错误/警告")
    plugin_level_counts = scan["plugin_level_counts"]
    plugin_error_samples = scan["plugin_error_samples"]
    plugin_diag_messages = scan["plugin_diag_messages"]

    all_plugins = sorted(set(configured.keys()) | set(plugin_level_counts.keys()))
    if not all_plugins and not plugin_diag_messages:
        out.item("（今日无插件日志数据）")
        out.set_data("plugin_errors", {})
        return

    errors_payload = {}

    def rank(p):
        c = plugin_level_counts.get(p, Counter())
        return (-(c.get("ERROR", 0) + c.get("FATAL", 0)), -c.get("WARN", 0), p)

    has_issues = False
    for p in sorted(all_plugins, key=rank):
        c = plugin_level_counts.get(p, Counter())
        err = c.get("ERROR", 0) + c.get("FATAL", 0)
        warn = c.get("WARN", 0)
        total = sum(c.values())
        if err > 0:
            has_issues = True
        elif warn > 20:
            has_issues = True
        note = ""
        if p in configured and not configured[p]:
            note = " [disabled]"
        elif p not in configured and p in plugin_level_counts:
            note = " [auto/extension]"
        out.item(f"{p}: {err} ERROR, {warn} WARN, {total} total{note}")
        samples = plugin_error_samples.get(p, [])
        sample_payload = []
        if samples:
            for ts, lvl, text in dedup_messages(samples, max_unique=999):
                tag = {"ERROR": "E", "FATAL": "F", "WARN": "W"}.get(lvl, "?")
                snippet = _scrub(text.replace("\n", " "))
                out.item(f"  [{tag}] {fmt_hms(ts)}: {snippet}")
                sample_payload.append({
                    "ts": ts, "level": lvl, "msg": _scrub(text[:300]),
                })
        if err > 0 or warn > 0 or sample_payload:
            errors_payload[p] = {
                "error_count": err,
                "warn_count": warn,
                "total": total,
                "samples": sample_payload,
            }

    pm_errors = [(ts, lvl, text) for ts, lvl, text in plugin_diag_messages if lvl in ("ERROR", "FATAL")]
    pm_warns = [(ts, lvl, text) for ts, lvl, text in plugin_diag_messages if lvl == "WARN"]
    if pm_errors or pm_warns:
        has_issues = True
        out.item(f"[plugin-manager]: {len(pm_errors)} ERROR, {len(pm_warns)} WARN, "
                 f"{len(plugin_diag_messages)} total")
        for ts, _lvl, text in dedup_messages(pm_errors, max_unique=999):
            out.item(f"  [E] {fmt_hms(ts)}: {_scrub(text.replace(chr(10),' '))}")
        for ts, _lvl, text in dedup_messages(pm_warns, max_unique=999):
            out.item(f"  [W] {fmt_hms(ts)}: {_scrub(text.replace(chr(10),' '))}")
    elif plugin_diag_messages:
        out.item(f"[plugin-manager]: 0 ERROR, 0 WARN, {len(plugin_diag_messages)} total")

    if not has_issues:
        out.item("所有插件 ERROR=0 且 WARN<=20")

    out.set_data("plugin_errors", errors_payload)


def section_hooks(out, scan):
    out.progress(3, 5, "Hook 状态")
    out.subsection("9.3 Hook 执行状态")
    hook_errors = scan["hook_errors"]
    if not hook_errors:
        out.item("今日无 Hook 执行异常")
        out.set_data("hook_errors", {"total": 0, "by_plugin": {}})
        return

    by_plugin = defaultdict(list)
    for ts, plugin_id, hook_name, error_msg in hook_errors:
        by_plugin[plugin_id].append((ts, hook_name, error_msg))

    out.item(f"共 {len(hook_errors)} 次 Hook 执行异常:")
    out.line("")
    by_plugin_payload = {}
    for plugin_id in sorted(by_plugin, key=lambda p: -len(by_plugin[p])):
        entries = by_plugin[plugin_id]
        by_hook = defaultdict(list)
        for ts, hook_name, err in entries:
            by_hook[hook_name].append((ts, err))
        out.item(f"  {plugin_id}: {len(entries)} 次")
        hooks_payload = {}
        for hook_name in sorted(by_hook, key=lambda h: -len(by_hook[h])):
            hook_entries = by_hook[hook_name]
            out.item(f"    hook={hook_name}: {len(hook_entries)} 次")
            last = hook_entries[-1]
            out.item(f"      最近: {fmt_hms(last[0])} {last[1][:100]}")
            hooks_payload[hook_name] = {
                "count": len(hook_entries),
                "last_ts": last[0],
                "last_msg": last[1][:300],
            }
        by_plugin_payload[plugin_id] = {
            "count": len(entries),
            "hooks": hooks_payload,
        }
    out.set_data("hook_errors", {
        "total": len(hook_errors),
        "by_plugin": by_plugin_payload,
    })


def section_channels(out, scan):
    out.progress(4, 5, "Channel 子系统")
    out.subsection("9.4 Channel 子系统")
    subsystem_level_counts = scan["subsystem_level_counts"]
    subsystem_error_samples = scan["subsystem_error_samples"]
    if not subsystem_level_counts:
        out.item("（今日无 Channel 子系统日志）")
        out.set_data("channels", {})
        return

    channel_groups = defaultdict(list)
    for sub in subsystem_level_counts:
        channel_groups[sub.split("/")[0]].append(sub)

    channels_payload = {}
    for prefix in sorted(channel_groups):
        subs = channel_groups[prefix]
        total_err = sum(
            subsystem_level_counts[s].get("ERROR", 0) + subsystem_level_counts[s].get("FATAL", 0)
            for s in subs
        )
        total_warn = sum(subsystem_level_counts[s].get("WARN", 0) for s in subs)
        total_all = sum(sum(subsystem_level_counts[s].values()) for s in subs)
        out.item(f"{prefix}: {total_err} ERROR, {total_warn} WARN, {total_all} total ({len(subs)} subsystems)")
        if total_err > 0 or total_warn > 20:
            for sub in sorted(subs, key=lambda s: -(subsystem_level_counts[s].get("ERROR", 0))):
                sc = subsystem_level_counts[sub]
                sub_err = sc.get("ERROR", 0) + sc.get("FATAL", 0)
                sub_warn = sc.get("WARN", 0)
                if sub_err > 0 or sub_warn > 10:
                    short_sub = sub.split("/", 1)[1] if "/" in sub else sub
                    out.item(f"  {short_sub}: {sub_err}E {sub_warn}W")
                    samples = subsystem_error_samples.get(sub, [])
                    if samples:
                        for ts, _lvl, text in dedup_messages(samples, max_unique=2):
                            out.item(f"    [{fmt_hms(ts)}] {text.replace(chr(10),' ')}")
        channels_payload[prefix] = {
            "error_count": total_err,
            "warn_count": total_warn,
            "total": total_all,
            "subsystems": sorted(subs),
        }
    out.set_data("channels", channels_payload)


_URL_IN_VAL_RE = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.\-]*)(?::\d+)?")


def walk_urls(val, out_set):
    if isinstance(val, str):
        for m in _URL_IN_VAL_RE.finditer(val):
            out_set.add(m.group(1).lower())
    elif isinstance(val, dict):
        for v in val.values():
            walk_urls(v, out_set)
    elif isinstance(val, list):
        for v in val:
            walk_urls(v, out_set)


def section_deps(out, config_path, unmask=False):
    out.progress(5, 5, "外部依赖")
    out.subsection("9.5 插件外部依赖")
    plugin_deps = {}
    if not (config_path and os.path.isfile(config_path)):
        out.item("未发现已启用插件的外部依赖配置")
        out.set_data("plugin_deps", {})
        out.set_data("plugin_deps_status",
                     {"found": False, "reason": "config_not_found",
                      "checked": config_path or ""})
        return
    try:
        with open(config_path) as f:
            cfg_all = json.load(f)
        entries = cfg_all.get("plugins", {}).get("entries", {}) or {}
        for pid, pconf in entries.items():
            if not isinstance(pconf, dict):
                continue
            if not pconf.get("enabled", False):
                continue
            hosts = set()
            walk_urls(pconf, hosts)
            hosts = {h for h in hosts if not h.startswith(("127.", "localhost", "0.0.0.0"))}
            plugin_deps[pid] = hosts
    except (OSError, json.JSONDecodeError) as e:
        out.item(f"配置读取/解析失败: {type(e).__name__}")
        out.set_data("plugin_deps", {})
        out.set_data("plugin_deps_status",
                     {"found": False, "reason": "config_unreadable",
                      "checked": config_path, "error": str(e)[:200]})
        return

    if not plugin_deps:
        out.item("未发现已启用插件的外部依赖配置")
        out.set_data("plugin_deps", {})
        return

    out.item(f"扫描: {len(plugin_deps)} 个已启用插件的配置")
    deps_payload: dict = {}
    found_any = False
    no_dep = []
    dep_lines = []
    for pid in sorted(plugin_deps):
        hosts = plugin_deps[pid]
        host_results = []
        if not hosts:
            no_dep.append(pid)
            deps_payload[pid] = {"hosts": []}
            continue
        found_any = True
        for host in sorted(hosts):
            start = datetime.now()
            try:
                socket.setdefaulttimeout(3)
                socket.gethostbyname(host)
                elapsed = (datetime.now() - start).total_seconds() * 1000
                dep_lines.append(f"  {pid} → {host}: 可达 ({elapsed:.0f}ms)")
                host_results.append({"host": host, "reachable": True, "elapsed_ms": round(elapsed, 1)})
            except Exception:
                dep_lines.append(f"  {pid} → {host}: FAILED (DNS 解析失败)")
                host_results.append({"host": host, "reachable": False, "elapsed_ms": None})
        deps_payload[pid] = {"hosts": host_results}
    if found_any:
        out.item("发现外部端点:")
        for ln in dep_lines:
            out.item(ln)
    if no_dep:
        out.item(f"无外部依赖的插件: {', '.join(no_dep)}")
    out.set_data("plugin_deps", deps_payload)


def main() -> int:
    parser = cli.build_common_parser(
        description="模块 9：插件诊断",
    )
    args = parser.parse_args()
    out = output.init("plugin_diag", json_mode=args.json, no_color=args.no_color)
    out.section("模块 9：插件诊断")

    today = datetime.now().strftime("%Y-%m-%d")
    today_logs = sorted(glob.glob(os.path.join(args.log_dir, f"openclaw-{today}.log")))

    scan = scan_logs(today_logs)
    configured, configured_status = load_configured(args.config)
    extensions = load_extensions(args.openclaw_home)
    if not configured_status.get("found", True):
        out.item(f"配置加载失败: {configured_status.get('reason')} "
                 f"({configured_status.get('checked')})")
        out.set_data("configured_status", configured_status)

    section_state(out, scan, configured, extensions)
    section_errors(out, scan, configured, unmask=args.unmask)
    section_hooks(out, scan)
    section_channels(out, scan)
    section_deps(out, args.config, unmask=args.unmask)
    section_trajectory_plugins(out, args.sessions_base, configured)

    return out.done()


def section_trajectory_plugins(
    out: output.Output, sessions_base: str, configured: dict,
) -> None:
    """Trajectory-derived plugin snapshot + drift detection.

    Source events: ``trace.metadata.plugins.entries`` and
    ``trace.metadata.plugins.importedRuntimePluginIds`` from the most-recent
    10 runs across all trajectory files.
    """
    out.subsection("9.6 Trajectory: 插件状态快照 + 漂移检测")
    files = trajectory.discover_trajectory_files(sessions_base)
    if not files:
        out.item("未发现 trajectory 文件 — 跳过 trajectory 插件分析")
        out.set_data("trajectory_plugins", {"found": False})
        return
    runs = trajectory.collect_runs(files)
    runs.sort(key=lambda r: r.started_ts_ms, reverse=True)
    recent = [r for r in runs[:30] if r.plugin_entries]
    if not recent:
        out.item("最近 trajectory 中无 plugin metadata（trigger 都用了 ext-only?）")
        out.set_data("trajectory_plugins", {"found": True, "samples": 0})
        return

    latest = recent[0]
    last_plugins = latest.plugin_entries
    # Real plugin errors = activated=True but `error` field still set. The
    # `error` field doubles as activation-reason for *disabled* plugins
    # ("not in allowlist", "bundled (disabled by default)"); those are
    # informational, not failures. Only count real activation-time errors.
    plugin_errors_now = [
        p for p in last_plugins
        if p.get("error") and p.get("activated")
    ]
    plugin_disabled_with_reason = [
        p for p in last_plugins
        if p.get("error") and not p.get("activated")
    ]
    out.item(f"最新 run 的插件状态（{latest.session_id[:8]}#{latest.run_id[:8]}）")
    out.item(f"  共 {len(last_plugins)} 个插件 entries | "
             f"importedRuntimePluginIds={len(latest.imported_runtime_plugin_ids)}")
    if plugin_errors_now:
        out.item(f"FATAL: 最新 run 中有 {len(plugin_errors_now)} 个插件 activated 但 error 非空:")
        for p in plugin_errors_now[:10]:
            err = p.get("error")
            out.item(f"    {p['id']}: error={err} "
                     f"(source={p.get('activationSource')})")
    else:
        out.item("  最新 run: 0 个 activated 插件出错")
    if plugin_disabled_with_reason:
        out.item(f"  禁用插件（含 activationReason）: {len(plugin_disabled_with_reason)} 个 "
                 f"({', '.join(p['id'] for p in plugin_disabled_with_reason[:6])})")

    # Drift detection: compare config (configured) with the trajectory's own
    # `enabled` field (NOT `activated`). `activated=false` is a normal per-run
    # state for plugins that simply weren't reached by any hook this run, while
    # `enabled=false` reflects the runtime configuration choice. Comparing
    # against `enabled` is what matches the user's intent of "config drifted
    # from runtime".
    drift_disabled_active = []   # configured=enabled but trajectory enabled=false
    drift_active_disabled = []   # trajectory enabled=true but config=disabled
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
        out.item(f"警告：插件配置 vs trajectory 漂移: "
                 f"{len(drift_disabled_active)+len(drift_active_disabled)} 项")
        for p in drift_disabled_active[:5]:
            out.item(f"    {p.get('id')}: config 启用但 trajectory enabled=false "
                     f"(reason={p.get('activationReason') or '?'})")
        for p in drift_active_disabled[:5]:
            out.item(f"    {p.get('id')}: config 禁用但 trajectory enabled=true")
    else:
        out.item("  config vs trajectory 漂移: 无")

    # Imported but not in entries (loaded into runtime but never exposed).
    entry_ids = {p.get("id") for p in last_plugins}
    imported_unused = [
        pid for pid in latest.imported_runtime_plugin_ids
        if pid and pid not in entry_ids
    ]
    if len(imported_unused) > 5:
        out.item(f"警告：imported 但未出现在 entries 的插件: {len(imported_unused)} 个")
    elif imported_unused:
        out.item(f"  imported 但未在 entries 中: {len(imported_unused)} 个 "
                 f"({', '.join(imported_unused[:5])})")
    else:
        out.item("  imported_runtime_plugin_ids: 全部在 entries 中")

    out.set_data("trajectory_plugins", {
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
                "id": p.get("id"),
                "error": p.get("error"),
                "activated": p.get("activated"),
                "activationReason": p.get("activationReason"),
            }
            for p in plugin_errors_now
        ],
        "plugin_drift": {
            # JSON keys reflect the actual semantics: comparison is between
            # config `enabled` and trajectory `enabled` (NOT `activated`).
            # Each entry surfaces both `enabled` and `activated` so consumers
            # can disambiguate.
            "config_enabled_runtime_disabled": [
                {
                    "id": p.get("id"),
                    "enabled": p.get("enabled"),
                    "activated": p.get("activated"),
                    "reason": p.get("activationReason"),
                }
                for p in drift_disabled_active
            ],
            "config_disabled_runtime_enabled": [
                {
                    "id": p.get("id"),
                    "enabled": p.get("enabled"),
                    "activated": p.get("activated"),
                }
                for p in drift_active_disabled
            ],
        },
        "imported_unused": imported_unused,
    })


if __name__ == "__main__":
    sys.exit(main())
