"""Shim — see compressor.py (renamed from ribosome.py).

R3 Stage B (issue #87) moved this module to ``helix_context.compressor``.
This file remains as a back-compat re-export so existing imports keep
working without modification:

    from helix_context.ribosome import Ribosome      # still works
    from helix_context.compressor import Compressor  # canonical

Both names resolve to the same class object (``Compressor`` is the real
definition in ``compressor.py``; ``Ribosome`` is the legacy alias
declared there in R3 Stage A).

The shim re-exports every module-level name from compressor, including
single-underscore "private" names (``_parse_json``, ``_EXPRESS_SYSTEM``,
``_splice_system``, etc.) that training scripts and tests historically
reach for. Dunder names are excluded.

Lexicon: see ``docs/ROSETTA.md``.
"""

from helix_context import compressor as _impl

# Re-export every non-dunder attribute. Imports performed at module-import
# time so ``from helix_context.ribosome import X`` resolves immediately
# without a runtime attribute lookup.
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

del _impl, _name
