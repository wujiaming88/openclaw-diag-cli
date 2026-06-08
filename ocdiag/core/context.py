"""Shared diagnostic context — immutable, passed to all collectors."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
    # Per-invocation trajectory caches. Used by ``trajectory_files`` and
    # ``collect_runs`` to dedup the expensive disk scan + JSONL parse across
    # all collectors that share a single ``DiagContext`` (notably the ``all``
    # command, where 9 collectors otherwise each rescan ~hundreds of MB).
    # Scoped to one ctx by design — never module-global, never reused across
    # CLI invocations.
    _traj_files_cache: Optional[List[str]] = field(default=None, repr=False)
    _trajectory_cache: Optional[Dict[Tuple[Optional[int], Optional[int], bool],
                                     List[Any]]] = field(default=None, repr=False)

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

    def trajectory_files(self) -> List[str]:
        """Return the mtime-sorted list of trajectory files under
        ``sessions_base``. Memoized per ctx — first call walks the dir, the
        rest are O(1).

        Lazy import of ``ocdiag.trajectory`` keeps ``core.context`` cycle-safe
        (collectors import context, trajectory does not need context, but the
        deferred import preserves that guarantee even if trajectory grows new
        deps).
        """
        if self._traj_files_cache is None:
            from .. import trajectory  # local import to avoid any future cycle
            self._traj_files_cache = trajectory.discover_trajectory_files(
                str(self.sessions_base),
            )
        return self._traj_files_cache

    def collect_runs(
        self,
        *,
        since_ms: Optional[int] = None,
        limit_per_file: Optional[int] = None,
        populate_raw: bool = False,
    ) -> List[Any]:
        """Memoized ``trajectory.collect_runs`` keyed on its arg tuple.

        The five no-window callers (performance.py x2, configuration,
        plugin_diag, run_health) all share key ``(None, None, False)`` — the
        first call populates the cache, the rest are hits. Windowed callers
        (gateway 24h, cron_jobs/recent_errors 7d, environment 14d) each have
        a distinct ``since_ms`` and only call once, so they don't dedup but
        also don't risk returning the wrong window. ``populate_raw`` MUST be
        in the key — a raw-less cached entry would silently miss raw fields
        for a raw caller.

        IMPORTANT: callers commonly do ``runs.sort(...)`` IN PLACE on the
        returned list (see e.g. ``performance._section_trajectory_perf``).
        Returning the cached list object directly would let one caller mutate
        what the next caller sees. We always return a SHALLOW COPY so the
        cached list stays pristine; ``Run`` instances themselves are shared
        (collectors only read their fields).
        """
        if self._trajectory_cache is None:
            self._trajectory_cache = {}
        key = (since_ms, limit_per_file, populate_raw)
        cached = self._trajectory_cache.get(key)
        if cached is None:
            from .. import trajectory  # local import to avoid any future cycle
            files = self.trajectory_files()
            cached = trajectory.collect_runs(
                files,
                since_ms=since_ms,
                limit_per_file=limit_per_file,
                populate_raw=populate_raw,
            )
            self._trajectory_cache[key] = cached
        return list(cached)
