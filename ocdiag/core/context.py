"""Shared diagnostic context — immutable, passed to all collectors."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Grace window applied to ``mtime_prefilter`` floors. Absorbs filesystem
# clock skew, delayed flushes, and ``touch``-style mtime updates that don't
# correspond to new run records — better to scan a slightly-too-old file
# than to miss a run that legitimately fell within the requested window.
_MTIME_PREFILTER_GRACE_MS = 2 * 3600 * 1000  # 2h


@dataclass
class DiagContext:
    openclaw_home: Path
    config_path: Path
    log_dir: Path
    sessions_base: Path
    unmask: bool = False
    no_color: bool = False
    json_mode: bool = False
    # Optional account substring used by the ``channel`` collector to
    # filter the log scan — matched against the channel-prefix portion
    # of the message body (e.g. ``default`` matches
    # ``feishu[default]: ...``). ``None`` → no filter.
    account_id: Optional[str] = None
    _config_cache: Optional[Dict[str, Any]] = field(default=None, repr=False)
    # Per-invocation trajectory caches. Used by ``trajectory_files`` and
    # ``collect_runs`` to dedup the expensive disk scan + JSONL parse across
    # all collectors that share a single ``DiagContext`` (notably the ``all``
    # command, where 9 collectors otherwise each rescan ~hundreds of MB).
    # Scoped to one ctx by design — never module-global, never reused across
    # CLI invocations.
    #
    # Cache key is ``(since_ms, limit_per_file, populate_raw, mtime_prefilter)``
    # — the 4th dimension isolates prefiltered (subset) results from the
    # complete-set keys used by the superset reuse path in ``collect_runs``.
    _traj_files_cache: Optional[List[str]] = field(default=None, repr=False)
    _trajectory_cache: Optional[
        Dict[Tuple[Optional[int], Optional[int], bool, bool], List[Any]]
    ] = field(default=None, repr=False)

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
        mtime_prefilter: bool = False,
    ) -> List[Any]:
        """Memoized ``trajectory.collect_runs`` keyed on its arg tuple.

        The no-window callers (performance.py x2, configuration,
        run_health) all share key
        ``(None, None, False, False)`` — the first call populates the cache,
        the rest are hits. Windowed callers (gateway 24h,
        cron_jobs/recent_errors 7d, environment 14d, plugin_diag 7d→30d via
        ``mtime_prefilter``) each have a distinct ``since_ms``; in the ``all``
        command they reuse the cached full scan via the superset path below
        (zero disk I/O), prefilter requests included. ``populate_raw`` MUST be
        in the key — a raw-less cached entry would silently miss raw fields
        for a raw caller. ``mtime_prefilter`` is keyed too: a fresh prefilter
        DISK scan is a SUBSET (drops undated runs in old files) and must not
        pollute the complete-set keys; a prefilter request served from the
        full cache instead yields the superset (keeps undated runs).

        Two reuse paths, in order:

        1) Exact key hit → return shallow copy.
        2) Superset reuse: any windowed request (``since_ms`` is not None,
           ``limit_per_file is None``), REGARDLESS of ``mtime_prefilter``,
           when a full-scan entry under
           ``(None, None, populate_raw, False)`` already exists, is served
           by filtering that cached list IN MEMORY using the EXACT predicate
           from ``trajectory.collect_runs``: keep iff
           ``(not r.started_ts_ms) or (r.started_ts_ms >= since_ms)``. For a
           non-prefilter request the filtered list is set-equal to a fresh
           ``trajectory.collect_runs(files, since_ms=...)``; for a prefilter
           request it is a SUPERSET of the prefilter disk scan (keeps undated
           runs the mtime floor would have skipped). We cache it under the
           exact requested key to dedup repeat lookups. A prefilter request
           only reaches Path 3 when NO full scan is cached (standalone path).
        3) Disk scan fall-through. When ``mtime_prefilter=True`` and
           ``since_ms`` is not None, only files whose mtime is at or after
           ``since_ms - _MTIME_PREFILTER_GRACE_MS`` are scanned (the grace
           absorbs clock skew / writer flush lag); otherwise the full
           memoized file list is scanned.

        IMPORTANT: callers commonly do ``runs.sort(...)`` IN PLACE on the
        returned list (see e.g. ``performance._section_trajectory_perf``).
        Returning the cached list object directly would let one caller mutate
        what the next caller sees. We always return a SHALLOW COPY so the
        cached list stays pristine; ``Run`` instances themselves are shared
        (collectors only read their fields).
        """
        if self._trajectory_cache is None:
            self._trajectory_cache = {}
        key = (since_ms, limit_per_file, populate_raw, mtime_prefilter)

        # Path 1: exact hit.
        cached = self._trajectory_cache.get(key)
        if cached is not None:
            return list(cached)

        # Path 2: superset reuse — any windowed query (no per-file limit) can
        # be served from a cached full scan with the same ``populate_raw``,
        # INCLUDING prefilter requests. We only reuse an EXISTING full cache;
        # a prefilter request with no full cache falls through to the Path 3
        # fast disk scan (the standalone path). When a prefilter request is
        # served here it yields the SUPERSET of a prefilter disk scan (keeps
        # undated runs the mtime floor would skip) — all current windowed
        # consumers tolerate that (gateway counts dated runs only; plugin_diag
        # takes the top-30 dated runs). Predicate mirrors
        # trajectory.collect_runs exactly, INCLUDING keeping undated runs
        # (started_ts_ms == 0).
        if (
            since_ms is not None
            and limit_per_file is None
        ):
            full_key = (None, None, populate_raw, False)
            full_cached = self._trajectory_cache.get(full_key)
            if full_cached is not None:
                filtered = [
                    r for r in full_cached
                    if (not r.started_ts_ms) or (r.started_ts_ms >= since_ms)
                ]
                self._trajectory_cache[key] = filtered
                return list(filtered)

        # Path 3: disk scan. Optionally narrow file list via mtime prefilter.
        from .. import trajectory  # local import to avoid any future cycle
        if mtime_prefilter and since_ms is not None:
            # Per-since_ms file list; not memoized (would balloon for varied
            # windows). The mtime walk is cheap relative to the JSONL parses
            # it spares.
            floor_ms = since_ms - _MTIME_PREFILTER_GRACE_MS
            files = trajectory.discover_trajectory_files(
                str(self.sessions_base),
                mtime_floor_ms=floor_ms,
            )
        else:
            files = self.trajectory_files()
        cached = trajectory.collect_runs(
            files,
            since_ms=since_ms,
            limit_per_file=limit_per_file,
            populate_raw=populate_raw,
        )
        self._trajectory_cache[key] = cached
        return list(cached)
