"""Shared diagnostic context — immutable, passed to all collectors."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class DiagContext:
    openclaw_home: Path
    config_path: Path
    log_dir: Path
    sessions_base: Path
    unmask: bool = False
    no_color: bool = False
    json_mode: bool = False
    _config_cache: Optional[Dict[str, Any]] = field(default=None, repr=False)

    @classmethod
    def default(cls, **overrides) -> "DiagContext":
        home = Path(os.environ.get("OPENCLAW_HOME", os.path.expanduser("~/.openclaw")))
        return cls(
            openclaw_home=home,
            config_path=Path(os.environ.get("OPENCLAW_CONFIG", str(home / "openclaw.json"))),
            log_dir=Path(os.environ.get("OPENCLAW_LOG_DIR", "/tmp/openclaw")),
            sessions_base=Path(os.environ.get("OPENCLAW_SESSIONS", str(home / "agents"))),
            **overrides,
        )

    @property
    def config(self) -> Dict[str, Any]:
        if self._config_cache is None:
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self._config_cache = json.load(f)
            except (OSError, ValueError):
                self._config_cache = {}
        return self._config_cache
