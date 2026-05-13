"""Shim — see fragments.py (renamed from codons.py).

R3 Stage B.3 (issue #87) moved this module to
``helix_context.fragments``. This file remains as a back-compat shim
that re-exports every module-level name from the new location:

    from helix_context.codons import Codon, CodonChunker, CodonEncoder, RawStrand  # still works
    from helix_context.fragments import Codon, CodonChunker, CodonEncoder, RawStrand  # canonical

Both import surfaces resolve to the same classes. The class names
themselves (``Codon``, ``CodonChunker``, ``CodonEncoder``,
``RawStrand``) keep their biology framing for now — Stage C handles
any rename of those identifiers.

Pydantic field names that contain ``codons`` (e.g. ``Gene.codons:
List[str]``) are the SQL contract and remain untouched.

Lexicon: see ``docs/ROSETTA.md``.
"""

from helix_context import fragments as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

del _impl, _name
