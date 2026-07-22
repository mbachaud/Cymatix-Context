"""OKF bundle ingestion orchestrator.

Routes every concept through ``HelixContextManager.ingest`` — never
``upsert_doc``-direct (the historical density-gate-bypass path) — so
frontmatter tags MERGE with tagger output via the ingest seam, and all
downstream indexes (promoter_index, genes_fts, entity_graph,
path_key_index, filename_index) are populated identically to any other
ingest.

Cross-links are persisted to the inert ``okf_links`` table ONLY.
Writing to ``harmonic_links`` or ``gene_relations`` is prohibited in
Phase 1 — both are retrieval-live (Tier-5 flat per-edge boost; tie-
breaking), and doing so would ship an ungated scoring change (council
Amendment 1). Graduation happens only via the Phase-2 reviewed design.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .bundle import OkfBundle, read_bundle
from .digest import compute_bundle_digest

log = logging.getLogger("helix.okf")


@dataclass
class OkfIngestResult:
    bundle_id: str
    okf_version: Optional[str]
    digest: str
    concepts_total: int
    concepts_ingested: int
    gene_ids: List[str] = field(default_factory=list)
    links_captured: int = 0
    links_resolved: int = 0
    links_dangling: int = 0
    warnings: List[str] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)


def _concept_metadata(concept, bundle_id: str) -> Dict:
    """Ingest metadata for one concept.

    ``domains`` / ``key_values`` ride the caller-tag seam and merge with
    tagger output; ``source_kind`` carries the frontmatter ``type``
    (free-form strings pass through ``apply_metadata_hints`` lowercased);
    the ``okf_*`` keys land in ``promoter.metadata`` verbatim so the raw
    ``type`` and concept ID round-trip losslessly (council Amendment 4).
    """
    return {
        "source_id": concept.source_path,
        "domains": list(concept.tags),
        "key_values": list(concept.key_values),
        "source_kind": concept.raw_type,
        "okf_bundle_id": bundle_id,
        "okf_concept_id": concept.concept_id,
        "okf_type": concept.raw_type,
        "okf_title": concept.title,
        "okf_description": concept.description,
        "okf_frontmatter": concept.frontmatter,
    }


def _resolve_concept_gene_id(manager, concept, gene_ids: List[str]) -> Optional[str]:
    """The document a link to this concept resolves to.

    Multi-chunk concepts resolve to the deterministic PARENT gene_id
    (created by ``ingest`` for any multi-strand source); single-chunk
    concepts resolve to their only child.
    """
    if not gene_ids:
        return None
    if len(gene_ids) == 1:
        return gene_ids[0]
    parent_id = manager._make_parent_doc_id(concept.source_path)
    row = manager.genome.conn.execute(
        "SELECT 1 FROM genes WHERE gene_id = ?", (parent_id,)
    ).fetchone()
    if row is not None:
        return parent_id
    # Parent creation soft-fails inside ingest (chunks still land);
    # fall back to the first chunk so links stay resolvable.
    log.warning(
        "OKF concept %s: parent document %s missing; links resolve to first chunk",
        concept.concept_id,
        parent_id,
    )
    return gene_ids[0]


def replace_bundle_links(
    conn,
    bundle_id: str,
    rows: List[Tuple[str, str, str, str, Optional[str], str]],
) -> None:
    """Idempotently replace the okf_links rows for *bundle_id*."""
    cur = conn.cursor()
    cur.execute("DELETE FROM okf_links WHERE bundle_id = ?", (bundle_id,))
    cur.executemany(
        "INSERT INTO okf_links (bundle_id, source_concept_id, "
        "target_concept_id, resolved_source_gene_id, "
        "resolved_target_gene_id, link_text) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def ingest_bundle(
    manager,
    root: Path | str,
    bundle_id: Optional[str] = None,
) -> OkfIngestResult:
    """Ingest an OKF bundle directory through *manager*.

    Returns an ``OkfIngestResult`` with the canonical digest (computed
    from the parsed bundle before any storage — never from SQLite) and
    link-resolution counts.
    """
    bundle: OkfBundle = read_bundle(root, bundle_id=bundle_id)
    digest = compute_bundle_digest(bundle)

    result = OkfIngestResult(
        bundle_id=bundle.bundle_id,
        okf_version=bundle.okf_version,
        digest=digest,
        concepts_total=len(bundle.concepts),
        concepts_ingested=0,
        warnings=list(bundle.warnings),
        skipped_files=list(bundle.skipped_files),
    )

    concept_gene: Dict[str, Optional[str]] = {}
    for concept in bundle.concepts:
        if not concept.body.strip():
            # Nothing to store — frontmatter-only file. Accepted (the
            # bundle stays conformant); links to it will be dangling.
            log.info(
                "OKF concept %s has an empty body; no document stored",
                concept.concept_id,
            )
            concept_gene[concept.concept_id] = None
            continue
        gene_ids = manager.ingest(
            concept.body,
            content_type="text",
            metadata=_concept_metadata(concept, bundle.bundle_id),
        )
        result.gene_ids.extend(gene_ids)
        result.concepts_ingested += 1
        concept_gene[concept.concept_id] = _resolve_concept_gene_id(
            manager, concept, gene_ids
        )

    rows: List[Tuple[str, str, str, str, Optional[str], str]] = []
    for concept in bundle.concepts:
        source_gid = concept_gene.get(concept.concept_id)
        if source_gid is None:
            if concept.links:
                log.warning(
                    "OKF concept %s stored no document; its %d link(s) "
                    "are not persisted",
                    concept.concept_id,
                    len(concept.links),
                )
            continue
        for link in concept.links:
            target_gid = concept_gene.get(link.target_concept_id)
            if target_gid is None:
                # Spec §5.3: broken links are not malformed — they may be
                # not-yet-written knowledge. Logged, never fatal.
                log.info(
                    "OKF link %s -> %s is dangling (no such concept in bundle)",
                    concept.concept_id,
                    link.target_concept_id,
                )
                result.links_dangling += 1
            else:
                result.links_resolved += 1
            rows.append(
                (
                    bundle.bundle_id,
                    concept.concept_id,
                    link.target_concept_id,
                    source_gid,
                    target_gid,
                    link.link_text,
                )
            )
    result.links_captured = len(rows)
    replace_bundle_links(manager.genome.conn, bundle.bundle_id, rows)

    log.info(
        "OKF bundle %s ingested: %d/%d concepts, %d links (%d resolved, "
        "%d dangling), digest %s",
        bundle.bundle_id,
        result.concepts_ingested,
        result.concepts_total,
        result.links_captured,
        result.links_resolved,
        result.links_dangling,
        digest[:16],
    )
    return result
