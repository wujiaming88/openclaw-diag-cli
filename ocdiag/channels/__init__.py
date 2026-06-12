"""Channel-log collector internals.

The ``channel`` command (see ``ocdiag/collectors/channel.py``) is a
pure log scanner: it surfaces ERROR/WARNING entries from IM-channel
subsystems plus a curated set of INFO-level "silent drop / gating"
phrases. This package holds the helpers it relies on:

  * :mod:`log_utils` — JSON-line iteration with the gateway
    console-relay self-pollution guard, plus subsystem / channel
    classification primitives.
  * :mod:`signals` — the source-mined attention/benign phrase catalog
    and the :func:`signals.classify` helper.

Nothing here is exposed as a CLI surface. The collector is a single
command (``channel``); the per-helper split exists so the catalog and
the parsing primitives can evolve independently.
"""
