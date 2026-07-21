"""Shim — see knowledge_store.py (renamed from genome.py).

R3 Stage B.2 (issue #87) moved this module to
``cymatix_context.knowledge_store``. This file remains as a back-compat
shim that re-exports every module-level name from the new location:

    from cymatix_context.genome import Genome           # still works
    from cymatix_context.knowledge_store import KnowledgeStore  # canonical

Both names resolve to the same class object (``KnowledgeStore`` is the
real definition in ``knowledge_store.py``; ``Genome`` is the legacy
alias declared there in R3 Stage A).

The shim re-exports every non-dunder name, which covers historically-
imported private helpers like ``_kv_keys_from_list`` (used by
``scripts/backfill_path_key_index.py``).

SQL table and column names (``genes``, ``gene_id``, ``gene_attribution``,
``harmonic_links``, ``chromatin``, ``promoter``, ``epigenetics``,
``codons``) are the on-disk contract and remain untouched — only the
Python module filename and class identity changed.

Lexicon: see ``docs/ROSETTA.md``.
"""

from cymatix_context import knowledge_store as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

del _impl, _name
