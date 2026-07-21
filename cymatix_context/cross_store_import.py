"""
Horizontal Document Transfer (cross-store import) -- KnowledgeStore export/import.

Enables portable knowledge store files (.helix) that can be transferred
between Helix instances, seeding new projects with institutional
memory from mature ones.

Bio analogue (legacy term: HGT):
    Horizontal gene transfer is the movement of genetic material
    between organisms that is not via vertical transmission
    (parent to offspring). Bacteria use it to share antibiotic
    resistance documents. We use it to share project knowledge.

Export format (.helix):
    A JSON file containing:
    - header: metadata (source, timestamp, document count, version)
    - documents: list of Document objects (full fidelity, including signals)
    - promoter_index: list of (gene_id, tag_type, tag_value) tuples

    Content-addressed document IDs ensure deduplication on import --
    identical content produces identical IDs across instances.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from .genome import Genome
from .schemas import Gene

log = logging.getLogger("helix.hgt")

HELIX_FORMAT_VERSION = 1


def export_genome(
    genome: Genome,
    output_path: str,
    description: str = "",
    include_heterochromatin: bool = False,
) -> Dict:
    """
    Export the knowledge store to a portable .helix file.

    Args:
        knowledge store: Source knowledge store to export
        output_path: Path to write the .helix file
        description: Human-readable description of this knowledge store snapshot
        include_heterochromatin: Include stale/compacted documents (default: skip them)

    Returns:
        Export summary dict with document count and file size
    """
    cur = genome.conn.cursor()

    # Fetch documents
    if include_heterochromatin:
        rows = cur.execute("SELECT * FROM genes").fetchall()
    else:
        rows = cur.execute(
            "SELECT * FROM genes WHERE chromatin < 2"
        ).fetchall()

    genes = [genome._row_to_gene(r) for r in rows]

    # Fetch tags index
    gene_ids = {g.gene_id for g in genes}
    index_rows = cur.execute("SELECT gene_id, tag_type, tag_value FROM promoter_index").fetchall()
    promoter_index = [
        {"gene_id": r[0], "tag_type": r[1], "tag_value": r[2]}
        for r in index_rows if r[0] in gene_ids
    ]

    # Per-record transit checksums, computed over the content exactly as
    # written. The importer recomputes the same digest and compares —
    # symmetric by construction, so records whose gene_id is NOT a
    # content address (stable IDs like ``presence:<participant>``,
    # euchromatin-compressed genes whose content was rewritten in place)
    # round-trip cleanly instead of being rejected as tampered.
    gene_checksums = {g.gene_id: Genome.make_gene_id(g.content) for g in genes}

    # Build export
    export = {
        "helix_format_version": HELIX_FORMAT_VERSION,
        "header": {
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "description": description,
            "gene_count": len(genes),
            "promoter_tag_count": len(promoter_index),
            "source_path": genome.path,
            "include_heterochromatin": include_heterochromatin,
        },
        "genes": [g.model_dump() for g in genes],
        "gene_checksums": gene_checksums,
        "promoter_index": promoter_index,
    }

    # Write
    out = Path(output_path)
    out.write_text(json.dumps(export, indent=2, default=str), encoding="utf-8")

    file_size = out.stat().st_size
    log.info("Exported %d genes to %s (%d bytes)", len(genes), output_path, file_size)

    return {
        "genes": len(genes),
        "promoter_tags": len(promoter_index),
        "file_size": file_size,
        "path": str(out.resolve()),
    }


def import_genome(
    genome: Genome,
    input_path: str,
    merge_strategy: str = "skip_existing",
) -> Dict:
    """
    Import documents from a .helix file into the knowledge store.

    Args:
        knowledge store: Target knowledge store to import into
        input_path: Path to the .helix file
        merge_strategy: How to handle duplicate document IDs
            - "skip_existing": Keep the existing document (default, safe)
            - "overwrite": Replace existing documents with imported ones
            - "newest": Keep whichever document was accessed more recently

    Returns:
        Import summary with counts of imported, skipped, and total documents
    """
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))

    version = data.get("helix_format_version", 0)
    if version != HELIX_FORMAT_VERSION:
        log.warning("Format version mismatch: expected %d, got %d", HELIX_FORMAT_VERSION, version)

    genes_data = data.get("genes", [])
    header = data.get("header", {})
    # Transit checksums written by export_genome (same digest recomputed
    # here — symmetric with export). Older .helix files predate the map;
    # for those, fall back to the legacy content-address check, which is
    # only valid when the gene_id actually is a content address.
    gene_checksums = data.get("gene_checksums")

    imported = 0
    skipped = 0
    overwritten = 0

    tampered = 0
    for gene_dict in genes_data:
        gene = Gene.model_validate(gene_dict)

        # Integrity verification: .helix files may have been edited in
        # transit. Recompute the content digest and skip the row if it
        # doesn't match what the exporter recorded.
        actual = Genome.make_gene_id(gene.content)
        expected = (
            gene_checksums.get(gene.gene_id)
            if gene_checksums is not None
            else gene.gene_id  # legacy files: gene_id is the content address
        )
        if expected != actual:
            log.warning(
                "Skipping tampered gene: id=%s expected=%s actual=%s",
                gene.gene_id, expected, actual,
            )
            tampered += 1
            continue

        existing = genome.get_doc(gene.gene_id)

        if existing is not None:
            if merge_strategy == "skip_existing":
                skipped += 1
                continue
            elif merge_strategy == "newest":
                if existing.epigenetics.last_accessed >= gene.epigenetics.last_accessed:
                    skipped += 1
                    continue
            # overwrite or newest-wins: fall through to upsert
            overwritten += 1
            genome.upsert_doc(gene, apply_gate=False)
            continue

        # apply_gate=False: the density gate belongs to original ingest —
        # the exported lifecycle tier is preserved as-is on import (see
        # upsert_doc's docstring, which names cross-store imports as an
        # apply_gate=False caller).
        genome.upsert_doc(gene, apply_gate=False)
        imported += 1

    log.info(
        "Imported %d genes from %s (skipped=%d, overwritten=%d, tampered=%d)",
        imported, input_path, skipped, overwritten, tampered,
    )

    return {
        "imported": imported,
        "skipped": skipped,
        "overwritten": overwritten,
        "tampered": tampered,
        "total_in_file": len(genes_data),
        "source": header.get("description", ""),
        "source_exported_at": header.get("exported_at", ""),
    }


def genome_diff(genome: Genome, helix_path: str) -> Dict:
    """
    Compare a knowledge store against a .helix file without modifying anything.

    Returns counts of documents that are new, shared, or only in the file.
    Useful for previewing an import before committing.
    """
    data = json.loads(Path(helix_path).read_text(encoding="utf-8"))
    file_ids = {g["gene_id"] for g in data.get("genes", [])}

    cur = genome.conn.cursor()
    db_ids = {r[0] for r in cur.execute("SELECT gene_id FROM genes").fetchall()}

    shared = file_ids & db_ids
    only_in_file = file_ids - db_ids
    only_in_db = db_ids - file_ids

    return {
        "shared": len(shared),
        "only_in_file": len(only_in_file),
        "only_in_genome": len(only_in_db),
        "total_in_file": len(file_ids),
        "total_in_genome": len(db_ids),
    }
