"""Common argparse setup for diag scripts.

When invoked via the dispatcher (`openclaw-diag <id>`), the dispatcher exports
OPENCLAW_DIAG_PROG="openclaw-diag <id>" before running the script so argparse
uses that as `prog`. When you run the script directly (e.g.
`python3 diag/01_sys_health.py`), argparse falls back to the script basename.
"""

from __future__ import annotations

import argparse
import os
from typing import Optional

from . import paths


def build_common_parser(description: str, prog: Optional[str] = None) -> argparse.ArgumentParser:
    if prog is None:
        prog = os.environ.get("OPENCLAW_DIAG_PROG") or None
    p = argparse.ArgumentParser(
        prog=prog,
        description=description,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--config",
        default=paths.CONFIG,
        help="Path to openclaw.json",
    )
    p.add_argument(
        "--log-dir",
        default=paths.LOG_DIR,
        help="Directory containing openclaw-*.log files",
    )
    p.add_argument(
        "--sessions-base",
        default=paths.SESSIONS_BASE,
        help="Base directory containing per-agent session data",
    )
    p.add_argument(
        "--openclaw-home",
        default=paths.OPENCLAW_HOME,
        help="OpenClaw home directory (~/.openclaw)",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON output")
    p.add_argument("--no-color", action="store_true", help="Disable colored output")
    p.add_argument(
        "--unmask",
        action="store_true",
        help="Disable default sanitization of secrets in free-form text "
             "(shell history / plugin errors / systemd / sessions). Off by default.",
    )
    return p
