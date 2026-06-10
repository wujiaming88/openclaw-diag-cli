"""Channel diagnostic internals.

Loaded by the ``channel`` collector (see ``ocdiag/collectors/channel.py``).
This package houses variant detection, per-variant rule tables, and the
active probe / secret-resolver path. It is NOT a CLI surface — there is
exactly one user-visible command (``channel``); the layered split is
internal so each variant can grow its own L1 / L2-L3 / L5 specifics
without bloating one file.
"""
