"""Regression tests for the **Node launcher** — the real npm entry point.

Why this file exists
--------------------
`bin/openclaw-diag.js` is what `npm i -g openclaw-diag-cli` / `npx` actually
run. It intercepts a few paths (`--help`, no-args banner, `skill-install
--help`, python-not-found) with its own text and only delegates the rest to
the Python dispatcher (`ocdiag.main`).

Through v1.12.1 the Python `--help` was translated to English, but the Node
launcher still printed its own *Chinese* static help — so `openclaw-diag
--help` (the thing users see) was still Chinese. The existing suite only
exercised the Python dispatcher, so the regression slipped through.

These tests pin the launcher's user-facing behavior directly by spawning the
JS entry with `node`, and guard against any Chinese (CJK) text creeping back
into the npm entry point.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "bin" / "openclaw-diag.js"

# CJK Unified Ideographs — any hit means stray Chinese in the npm entry point.
_CJK = re.compile(r"[\u4e00-\u9fff]")

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not on PATH"
)


def _run_launcher(args):
    """Spawn `node bin/openclaw-diag.js <args>` and capture (rc, stdout+stderr)."""
    proc = subprocess.run(
        ["node", str(LAUNCHER), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


@requires_node
def test_launcher_help_is_english_and_delegates_to_python():
    """`--help` must show the rich English Python help, not the old Node text.

    `Exit codes:` and `Object diagnostics` only exist in the Python
    dispatcher's `--help`, so their presence proves the launcher delegates
    instead of printing its own (previously Chinese) help.
    """
    rc, out = _run_launcher(["--help"])
    assert rc == 0
    assert "OpenClaw operations diagnostics CLI" in out
    assert "Health checks:" in out
    assert "Object diagnostics" in out  # proves delegation to Python --help
    assert "Exit codes:" in out
    assert not _CJK.search(out), f"unexpected Chinese in --help: {out!r}"


@requires_node
def test_launcher_short_h_flag_matches_help():
    rc, out = _run_launcher(["-h"])
    assert rc == 0
    assert "OpenClaw operations diagnostics CLI" in out
    assert not _CJK.search(out)


@requires_node
def test_launcher_no_args_banner_is_english():
    rc, out = _run_launcher([])
    assert rc == 0
    assert "Common commands:" in out
    assert "openclaw-diag --help" in out
    assert not _CJK.search(out), f"unexpected Chinese in no-args banner: {out!r}"


@requires_node
def test_launcher_skill_install_help_is_english():
    rc, out = _run_launcher(["skill-install", "--help"])
    assert rc == 0
    assert "install the openclaw-diag skill" in out
    assert "Install targets" in out
    assert not _CJK.search(out), f"unexpected Chinese in skill-install --help: {out!r}"


@requires_node
def test_launcher_version_is_plain():
    """`--version` prints the bare version, no banner, no Chinese."""
    rc, out = _run_launcher(["--version"])
    assert rc == 0
    assert re.match(r"^\d+\.\d+\.\d+", out.strip())
    assert not _CJK.search(out)
