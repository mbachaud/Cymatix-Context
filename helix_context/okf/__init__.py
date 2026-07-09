"""OKF (Open Knowledge Format) ingestion adapter — Phase 1.

Reads OKF v0.1 knowledge bundles (plain directories of markdown files
with YAML frontmatter, spec snapshot pinned at upstream commit ee67a5ca
— see tests/fixtures/okf/SPEC-ee67a5ca.md) and routes every concept
through ``HelixContextManager.ingest`` so frontmatter tags merge with
tagger output instead of bypassing it.

Cross-links are captured into the inert ``okf_links`` table only —
never ``harmonic_links`` or ``gene_relations`` (both are retrieval-live;
see docs/research/2026-07-08-okf-council.md, Amendment 1).

Decision record: docs/research/2026-07-08-okf-council.md.
"""

from .bundle import OkfBundle, OkfConcept, OkfLink, read_bundle
from .digest import compute_bundle_digest
from .ingest import OkfIngestResult, ingest_bundle

__all__ = [
    "OkfBundle",
    "OkfConcept",
    "OkfIngestResult",
    "OkfLink",
    "compute_bundle_digest",
    "ingest_bundle",
    "read_bundle",
]
