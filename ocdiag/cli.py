"""Common argparse setup for diag scripts."""

from __future__ import annotations

import argparse
from typing import Optional

from . import paths


def build_common_parser(description: str, prog: Optional[str] = None) -> argparse.ArgumentParser:
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
    return p
