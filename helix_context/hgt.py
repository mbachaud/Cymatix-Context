"""Shim — see cross_store_import.py (renamed from hgt.py).

R3 Stage B.5 (issue #87) moved this module to
``helix_context.cross_store_import``. This file remains as a back-
compat shim so the historical biology acronym (HGT — horizontal
gene transfer) still resolves at import time:

    from helix_context.hgt import export_genome, import_genome, genome_diff  # legacy
    from helix_context.cross_store_import import export_genome, import_genome, genome_diff  # canonical

Per docs/ROSETTA.md the canonical name for this concept is
"cross-store import" — moving documents from one helix instance to
another. Function names themselves (``export_genome`` /
``import_genome`` / ``genome_diff``) keep their current names in
Stage B; renaming the function-level surface is Stage C work.

Lexicon: see ``docs/ROSETTA.md``.
"""

from helix_context import cross_store_import as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

del _impl, _name
