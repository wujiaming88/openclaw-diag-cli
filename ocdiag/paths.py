"""Filesystem paths used by the diag scripts. Override via environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v else default


HOME = os.path.expanduser("~")

OPENCLAW_HOME = _env_path("OPENCLAW_HOME", os.path.join(HOME, ".openclaw"))
CONFIG = _env_path("OPENCLAW_CONFIG", os.path.join(OPENCLAW_HOME, "openclaw.json"))
CRON_JOBS = _env_path("OPENCLAW_CRON_JOBS", os.path.join(OPENCLAW_HOME, "cron", "jobs.json"))
CRON_STATE = _env_path("OPENCLAW_CRON_STATE", os.path.join(OPENCLAW_HOME, "cron", "jobs-state.json"))
CRON_RUNS_DIR = _env_path("OPENCLAW_CRON_RUNS", os.path.join(OPENCLAW_HOME, "cron", "runs"))
SESSIONS_BASE = _env_path("OPENCLAW_SESSIONS", os.path.join(OPENCLAW_HOME, "agents"))
EXTENSIONS_DIR = _env_path("OPENCLAW_EXTENSIONS", os.path.join(OPENCLAW_HOME, "extensions"))

LOG_DIR = _env_path("OPENCLAW_LOG_DIR", "/tmp/openclaw")
SERVICE_FILE = _env_path(
    "OPENCLAW_SERVICE_FILE",
    os.path.join(HOME, ".config", "systemd", "user", "openclaw-gateway.service"),
)
SERVICE_ENV_FILE = _env_path(
    "OPENCLAW_SERVICE_ENV_FILE",
    os.path.join(HOME, ".config", "systemd", "user", "openclaw-gateway.service.d", "env.conf"),
)


def home() -> str:
    return HOME


def config_path() -> str:
    return CONFIG


def log_dir() -> str:
    return LOG_DIR


def sessions_base() -> str:
    return SESSIONS_BASE
