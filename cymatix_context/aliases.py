"""Canonical software-vocabulary surface for the helix knowledge store.

**Post-R3:** the canonical names are now the *real* class definitions.
``Document``, ``KnowledgeStore``, ``Compressor``, ``DocumentTags``,
``DocumentSignals``, ``LifecycleTier``, and ``DocumentAttribution``
live in their home modules as the primary class identities. The
legacy biology names (``Gene``, ``Genome``, ``Ribosome``,
``PromoterTags``, ``EpigeneticMarkers``, ``ChromatinState``,
``GeneAttribution``) remain valid as one-line aliases declared
immediately after each class definition.

This module exists for two reasons:

1. **A single import point** — ``from cymatix_context.aliases import
   Document, KnowledgeStore, Compressor, ...`` works without having
   to know which module each class lives in.
2. **Provenance documentation** — the ``_RENAME_LOG`` table at the
   bottom records every pre-R3 name and where the alias survives.

Identity holds in both directions:

    from cymatix_context.schemas import Gene
    from cymatix_context.aliases import Document
    assert Document is Gene
    assert Gene is Document
    assert Document.__name__ == "Document"   # post-R3

There is no subclassing, no wrapping, and no runtime cost to using
either name. Pydantic field names (``gene_id``, ``promoter``,
``epigenetics``, ``chromatin``, ``codons``) and SQL table/column
contracts are unchanged.

Lexicon: see ``docs/ROSETTA.md`` for the full bidirectional mapping
and the R1/R2/R3 status table.
"""

from __future__ import annotations

# ── Schemas (canonical pydantic classes — Document is the real def) ─────
from cymatix_context.schemas import (
    Document,
    DocumentAttribution,
    DocumentSignals,
    DocumentTags,
    LifecycleTier,
)

# ── Core modules (canonical class names) ────────────────────────────────
from cymatix_context.genome import KnowledgeStore
from cymatix_context.ribosome import Compressor


__all__ = [
    "Compressor",          # canonical for Ribosome
    "Document",            # canonical for Gene
    "DocumentAttribution", # canonical for GeneAttribution
    "DocumentSignals",     # canonical for EpigeneticMarkers
    "DocumentTags",        # canonical for PromoterTags
    "KnowledgeStore",      # canonical for Genome
    "LifecycleTier",       # canonical for ChromatinState
]


# Per-alias provenance, useful for code-search tools that surface
# rename history. Read once at import; not used at runtime.
#
# Format: {canonical_name: ("legacy_name", "home_module")}
_RENAME_LOG = {
    "Document":            ("Gene",              "cymatix_context.schemas"),
    "DocumentAttribution": ("GeneAttribution",   "cymatix_context.schemas"),
    "DocumentSignals":     ("EpigeneticMarkers", "cymatix_context.schemas"),
    "DocumentTags":        ("PromoterTags",      "cymatix_context.schemas"),
    "LifecycleTier":       ("ChromatinState",    "cymatix_context.schemas"),
    "KnowledgeStore":      ("Genome",            "cymatix_context.genome"),
    "Compressor":          ("Ribosome",          "cymatix_context.ribosome"),
}
