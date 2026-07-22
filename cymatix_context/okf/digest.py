"""Canonical bundle digest — the OKF determinism claim, exactly scoped.

Council Amendment 2 (docs/research/2026-07-08-okf-council.md): for a
fixed adapter version, pinned spaCy model version, and OKF spec
version, ingesting the same bundle yields a byte-identical canonical
digest, across runs and platforms. The digest is sha256 over a
canonical JSON serialization (sorted keys, LF newlines, UTF-8,
POSIX-normalized forward-slash paths) of, per concept:

    gene_id (= sha256(content)[:16]), content_hash,
    type→taxonomy mapping, title, description,
    sorted(domains), sorted(entities), sorted(key_values)

plus the bundle cross-link edge set as a sorted list of
(source_concept_id, target_concept_id) pairs.

Excluded BY CONSTRUCTION (they are never inputs to this function):
embeddings (SEMA, BGE-M3), SPLADE term weights, wall-clock fields
(last_seen, last_verified_at, signals created_at/last_accessed), and
any REAL-valued score. The digest is computed purely from the parsed
bundle — never from the SQLite file or raw rows.

The digest fields are frontmatter-derivable: ``domains`` are the
frontmatter tags and ``entities`` are the adapter-supplied entities
(none in v0.1 — the tagger's spaCy entities merge into the *store* but
deliberately not into the *digest*, which is why the claim can pin the
spaCy model version as a precondition without the digest breaking
first in practice).

ADAPTER_VERSION participates in the digest: bump it on any change to
the concept→field mapping so two hosts on different adapter code can
never report a spuriously equal digest.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from .bundle import OkfBundle

# Bump on any change to the mapping below (fields, normalization,
# link-capture rules). Part of the digest payload.
ADAPTER_VERSION = "1"

OKF_SPEC_PIN = "ee67a5ca"


def _source_kind(raw_type: Optional[str]) -> Optional[str]:
    """The type→taxonomy mapping the ingest path applies.

    Mirrors ``identity.provenance._normalize_kind_hint`` — free-form
    strings pass through lowercased; content-type synonyms map onto the
    provenance taxonomy. Imported (not reimplemented) so the digest can
    never drift from what ``apply_metadata_hints`` actually stores.
    """
    from ..identity.provenance import _normalize_kind_hint

    return _normalize_kind_hint(raw_type)


def bundle_digest_payload(bundle: OkfBundle) -> Dict[str, Any]:
    """The canonical (pre-hash) payload. Exposed for tests and tooling."""
    concepts: List[Dict[str, Any]] = []
    for c in sorted(bundle.concepts, key=lambda c: c.concept_id):
        body_bytes = c.body.encode("utf-8")
        content_hash = hashlib.sha256(body_bytes).hexdigest()
        concepts.append(
            {
                "concept_id": c.concept_id,
                "gene_id": content_hash[:16],
                "content_hash": content_hash,
                "type": {
                    "raw": c.raw_type,
                    "source_kind": _source_kind(c.raw_type),
                },
                "title": c.title,
                "description": c.description,
                "domains": sorted(c.tags),
                "entities": [],  # adapter supplies none from v0.1 frontmatter
                "key_values": sorted(c.key_values),
            }
        )

    edges = sorted(
        {
            (c.concept_id, link.target_concept_id)
            for c in bundle.concepts
            for link in c.links
        }
    )

    return {
        "adapter_version": ADAPTER_VERSION,
        "okf_spec_pin": OKF_SPEC_PIN,
        "okf_version": bundle.okf_version,
        "concepts": concepts,
        "links": [list(edge) for edge in edges],
    }


def compute_bundle_digest(bundle: OkfBundle) -> str:
    """sha256 hex digest of the canonical JSON serialization."""
    payload = bundle_digest_payload(bundle)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
