"""Regression tests for `openclaw-diag <collector> --help`.

Before v1.8.2 the generic state-collector dispatch in ``ocdiag.main.main``
shared an ``add_help=False`` parser via ``parse_known_args`` — so ``-h`` /
``--help`` fell into the discarded "unknown args" bucket and the collector
just ran. These tests pin the fixed behavior: collector --help prints
argparse usage and exits 0, instead of executing the diagnostic.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag import main as ocdiag_main  # noqa: E402
from ocdiag.core import registry as ocdiag_registry  # noqa: E402


def _capture_help(argv):
    """Run main(argv) expecting an argparse SystemExit(0); return stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        with pytest.raises(SystemExit) as exc:
            ocdiag_main.main(argv)
    assert exc.value.code == 0, (
        f"expected argparse to exit 0 for {argv!r}, got {exc.value.code!r}"
    )
    return buf.getvalue()


def test_channel_help_shows_usage_not_diagnostic():
    out = _capture_help(["channel", "--help"])
    # argparse usage line carries the prog we set; a real diagnostic run
    # would print the human renderer banner instead.
    assert "usage: openclaw-diag channel" in out
    # Channel keeps its single channel-only flag.
    assert "--account" in out
    # ``--probe`` and ``--sender`` were removed in v1.9.0 when the
    # channel collector dropped config interpretation and the active
    # probe path. Pin the absence so the flags don't sneak back in.
    assert "--sender" not in out
    assert "--probe" not in out
    # Negative control: the human renderer's banner must not appear.
    assert "OPENCLAW-DIAG" not in out


def test_gateway_help_shows_usage_not_diagnostic():
    out = _capture_help(["gateway", "--help"])
    assert "usage: openclaw-diag gateway" in out
    # Common flags still present.
    assert "--format" in out
    # Channel-only flag must not leak into a non-channel collector's help.
    assert "--account" not in out
    # ``--probe`` / ``--sender`` are gone everywhere.
    assert "--sender" not in out
    assert "--probe" not in out
    assert "OPENCLAW-DIAG" not in out


def test_cron_jobs_help_shows_usage_not_diagnostic():
    out = _capture_help(["cron_jobs", "--help"])
    assert "usage: openclaw-diag cron_jobs" in out
    assert "--format" in out
    # Channel-only flag must not appear here either.
    assert "--account" not in out


def test_collector_short_h_flag_also_shows_help():
    # `-h` is registered alongside `--help` by argparse's auto help action;
    # cover it explicitly so the regression can't sneak back through `-h`.
    out = _capture_help(["channel", "-h"])
    assert "usage: openclaw-diag channel" in out


def test_channel_without_help_runs_collector():
    """Negative control: omitting --help must still execute the diagnostic.

    We only assert the call returns an int exit code (no SystemExit raised
    by argparse). This proves the help branch is gated on -h/--help and
    does not steal normal invocations.
    """
    rc = ocdiag_main.main(["channel"])
    assert isinstance(rc, int)


def test_trace_help_documents_all_messages():
    """v1.11.0 introduced --all-messages/-A; pin the flag in trace --help."""
    out = _capture_help(["trace", "--help"])
    assert "usage: openclaw-diag trace" in out
    assert "--all-messages" in out
    # Negative control: the help branch must not run the inspector.
    assert "OPENCLAW-DIAG" not in out


def test_trace_argparse_rejects_all_messages_with_msg_index(capsys):
    """v1.11.0: the argparse layer of cmd_inspector rejects
    --all-messages combined with --msg-index/--msg-id/--msg-match BEFORE
    the inspector ever runs. parser.error() emits to stderr and SystemExit(2).

    The inspector layer also enforces the same mutex (see test_trace.py)
    but a CLI user hits the argparse path first. Pin both layers.
    """
    with pytest.raises(SystemExit) as exc:
        ocdiag_main.main([
            "trace", "1234567890abcdef",
            "--all-messages", "--msg-index", "0",
        ])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--all-messages" in err
    assert "--msg-index" in err


def test_trace_argparse_rejects_all_messages_with_msg_id(capsys):
    with pytest.raises(SystemExit) as exc:
        ocdiag_main.main([
            "trace", "1234567890abcdef",
            "--all-messages", "--msg-id", "user-1",
        ])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--all-messages" in err


def test_trace_argparse_rejects_all_messages_with_msg_match(capsys):
    with pytest.raises(SystemExit) as exc:
        ocdiag_main.main([
            "trace", "1234567890abcdef",
            "--all-messages", "--msg-match", "hi",
        ])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--all-messages" in err


def _capture_stdout(argv):
    """Run main(argv), expect a normal int return; capture stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ocdiag_main.main(argv)
    assert isinstance(rc, int)
    return rc, buf.getvalue()


# -----------------------------------------------------------------------------
# v1.12.0 — --help 全面改造
# -----------------------------------------------------------------------------

def test_top_level_help_has_key_sections():
    """顶层 help 必须包含工具简介、体检命令、扫描类、对象诊断、辅助命令、
    全局选项、退出码这些 section 标题；这是 agent / 用户能否快速定位的命脉。"""
    _, out = _capture_stdout(["--help"])
    assert "OpenClaw 运维诊断 CLI" in out
    assert "用法:" in out
    assert "体检命令:" in out
    assert "扫描类诊断" in out
    assert "对象诊断" in out
    assert "辅助命令:" in out
    assert "全局选项" in out
    assert "退出码:" in out
    # 退出码四档全部要列出来
    for code in ("0", "1", "2", "3"):
        assert code in out


def test_top_level_help_lists_every_state_collector_id():
    """顶层 help 用 registry 动态渲染扫描类列表，必须覆盖每一个已注册的
    state collector id —— 防止未来新增 collector 漏排。"""
    ocdiag_registry.discover()
    _, out = _capture_stdout(["--help"])
    state_ids = [c.id for c in ocdiag_registry.all_state()]
    # 至少应有 14 个 state collector，避免 registry 空导致断言假阳性通过。
    assert len(state_ids) >= 10, f"unexpectedly few state collectors: {state_ids}"
    for sid in state_ids:
        assert sid in out, f"collector id {sid!r} missing from top-level --help"


def test_top_level_help_lists_inspectors():
    """对象诊断段必须列出 trace/extract/panorama 三个 inspector。"""
    _, out = _capture_stdout(["--help"])
    for iid in ("trace", "extract", "panorama"):
        assert iid in out, f"inspector {iid!r} missing from top-level --help"


def test_trace_help_documents_previously_bare_flags():
    """v1.12.0 给 trace 的 5 个 flag 补了 help —— 不能再是裸 flag。
    断言对应中文 help 片段出现，而不只是 flag 名（flag 名在 usage 行也会出现）。"""
    out = _capture_help(["trace", "--help"])
    # 命令专属分组标题
    assert "trace 选项" in out
    # 5 个之前没有 help 的 flag，每个都要带可读说明
    assert "不读取 trajectory.jsonl" in out          # --no-trajectory
    assert "不关联 openclaw 应用日志" in out         # --no-log
    assert "完整 meta 信息" in out                    # --show-tool-metas
    assert "插件快照" in out                          # --show-plugin-snapshot
    assert "强制脱敏" in out                          # --mask
    # description 一句话简介必须出现
    assert "追踪一条用户消息的完整生命周期" in out


def test_collector_help_shows_global_options_with_descriptions():
    """collector 的 --help 应能看到「全局选项」分组，且 --config / --unmask
    等 flag 不再光秃秃，每个都带中文 help 文本。"""
    out = _capture_help(["gateway", "--help"])
    assert "全局选项" in out
    assert "openclaw.json 配置文件路径" in out      # --config
    assert "不脱敏" in out                           # --unmask
    assert "关闭 ANSI 颜色" in out                  # --no-color
    assert "OpenClaw 日志目录" in out                # --log-dir
    assert "sessions 根目录" in out                  # --sessions-base
    assert "OpenClaw 主目录" in out                  # --openclaw-home


def test_collector_help_description_includes_one_liner():
    """gateway --help 顶部 description 要把 _COMMAND_DESC 里的一句话拼上，
    用户/agent 不必再去 list 查 collector 是干嘛的。"""
    out = _capture_help(["gateway", "--help"])
    # description 行通常长这样: "Gateway 状态 (gateway) — 分析 Gateway 进程..."
    assert "(gateway)" in out
    assert "Gateway 进程生命周期" in out


def test_list_pretty_includes_command_descriptions():
    """`openclaw-diag list` 的 pretty 输出要在每个 id 后面带描述片段。"""
    rc, out = _capture_stdout(["list"])
    assert rc == 0
    # 取一条扫描类 + 一条对象类各验一句话片段
    assert "Gateway 进程生命周期" in out                 # gateway
    assert "追踪一条用户消息的完整生命周期" in out       # trace


def test_extract_help_documents_options_and_groups():
    """extract --help 应有 description / 命令分组 / 每个 flag 的中文 help。"""
    out = _capture_help(["extract", "--help"])
    assert "usage: openclaw-diag extract" in out
    assert "extract 选项" in out
    assert "可读格式" in out  # description 一句话片段
    # 各 flag 的 help 文案
    assert "只打印每文件的记录条数统计" in out          # --summary
    assert "导出全部版本" in out                        # --all
    assert "只列出匹配到的文件" in out                  # --list
    assert "按记录类型过滤" in out                      # --types
    # 全局选项分组与 --config help 都得在
    assert "全局选项" in out
    assert "openclaw.json 配置文件路径" in out


def test_panorama_help_documents_options_and_groups():
    """panorama --help 同样要有完整说明。"""
    out = _capture_help(["panorama", "--help"])
    assert "usage: openclaw-diag panorama" in out
    assert "panorama 选项" in out
    assert "全景诊断" in out  # description 片段
    assert "脱敏 tool 参数" in out                       # --mask
    assert "选择第 N 个 run" in out                      # --run-index
    assert "包含 session 内全部 run" in out              # --all-runs
    assert "只用 sessionId / runIds 关联" in out         # --strict-correlation
    assert "全局选项" in out


def test_list_json_each_entry_has_description_field():
    """`openclaw-diag list --format json` 每项必须含 description 字段，
    便于 agent / 脚本不必硬编码描述表也能渲染目录。"""
    rc, out = _capture_stdout(["list", "--format", "json"])
    assert rc == 0
    payload = json.loads(out)
    assert "state_collectors" in payload
    assert "object_inspectors" in payload
    for entry in payload["state_collectors"]:
        assert "description" in entry
        # 至少 doctor / gateway 这种已登记的 id 必须有非空描述
        if entry["id"] in ("doctor", "gateway", "channel", "trace"):
            assert entry["description"], (
                f"description must not be empty for known id {entry['id']!r}"
            )
    for entry in payload["object_inspectors"]:
        assert "description" in entry
        assert entry["description"], (
            f"inspector {entry['id']!r} description should not be empty"
        )


def test_gateway_without_help_runs_without_attribute_error():
    """Regression guard for the v1.8.3 split.

    `gateway`'s parser no longer registers --probe/--sender/--account.
    `_build_context` must read them via getattr-with-default so the
    args namespace lacking those attributes does not raise AttributeError.
    """
    try:
        rc = ocdiag_main.main(["gateway"])
    except AttributeError as e:
        pytest.fail(f"gateway run raised AttributeError: {e}")
    assert isinstance(rc, int)
