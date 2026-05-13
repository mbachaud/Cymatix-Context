"""Shim — see persistence.py (renamed from replication.py).

R3 Stage B.4 (issue #87) moved this module to
``helix_context.persistence``. This file remains as a back-compat
shim:

    from helix_context.replication import ReplicationManager   # still works
    from helix_context.persistence import ReplicationManager   # canonical

The class identifier (``ReplicationManager``) keeps its current name
in Stage B; if a method rename is wanted (``replicate`` ->
``persist`` etc.) that lands in Stage C.

Lexicon: see ``docs/ROSETTA.md``.
"""

from helix_context import persistence as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

del _impl, _name
