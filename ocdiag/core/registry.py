"""Collector auto-registration via decorator."""
from __future__ import annotations
from typing import Dict, List, Optional, Protocol
from .types import Report
from .context import DiagContext


class CollectorProtocol(Protocol):
    id: str
    title: str
    kind: str  # "state" | "inspector"

    def collect(self, ctx: DiagContext, **kwargs) -> Report: ...


_REGISTRY: Dict[str, CollectorProtocol] = {}


def register(cls):
    """Class decorator: instantiate and register a collector."""
    instance = cls()
    _REGISTRY[instance.id] = instance
    return cls


def get(module_id: str) -> Optional[CollectorProtocol]:
    return _REGISTRY.get(module_id)


def all_state() -> List[CollectorProtocol]:
    return [c for c in _REGISTRY.values() if c.kind == "state"]


def all_inspectors() -> List[CollectorProtocol]:
    return [c for c in _REGISTRY.values() if c.kind == "inspector"]


def all_ids() -> List[str]:
    return list(_REGISTRY.keys())


def discover() -> None:
    """Import all collector modules to trigger @register decorators."""
    import importlib
    import pkgutil
    from ocdiag import collectors, inspectors
    for pkg in (collectors, inspectors):
        for info in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(f"{pkg.__name__}.{info.name}")
