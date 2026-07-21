"""OKF bundle ingestion through HelixContextManager.

Council hard constraints exercised here:

- The adapter routes exclusively through ``HelixContextManager.ingest``
  (frontmatter merges with tagger output via the caller-tag seam).
- Cross-links land ONLY in the inert ``okf_links`` table. Writes to
  ``harmonic_links`` and ``gene_relations`` are prohibited (both are
  retrieval-live); the only gene_relations rows after a bundle ingest
  are the standard CHUNK_OF edges every multi-chunk ingest creates.
"""

import json
from pathlib import Path

import pytest

from cymatix_context.context_manager import HelixContextManager
from cymatix_context.okf import ingest_bundle, read_bundle

from tests.conftest import make_helix_config

pytest.importorskip("spacy")

OKF_FIXTURES = Path(__file__).parent / "fixtures" / "okf"


@pytest.fixture
def manager():
    return HelixContextManager(make_helix_config())


def _links_rows(genome, bundle_id):
    return [
        tuple(r)
        for r in genome.conn.execute(
            "SELECT source_concept_id, target_concept_id, "
            "resolved_source_gene_id, resolved_target_gene_id, link_text "
            "FROM okf_links WHERE bundle_id = ? "
            "ORDER BY source_concept_id, target_concept_id",
            (bundle_id,),
        )
    ]


class TestConceptIngestion:
    def test_type_only_bundle_ingests(self, manager):
        result = ingest_bundle(manager, OKF_FIXTURES / "type_only")
        assert result.concepts_ingested == 1
        assert result.warnings == []
        assert len(result.digest) == 64

        row = manager.genome.conn.execute(
            "SELECT promoter, source_id, source_kind FROM genes "
            "WHERE gene_id = ?",
            (result.gene_ids[0],),
        ).fetchone()
        promoter = json.loads(row[0])
        # Lossless round-trip fields (council Amendment 4).
        assert promoter["metadata"]["okf_type"] == "Note"
        assert promoter["metadata"]["okf_concept_id"] == "note"
        assert promoter["metadata"]["okf_bundle_id"] == "type_only"
        # source_id = bundle-relative POSIX concept path.
        assert row[1] == "note.md"
        # type → source_kind through the ingest path's canonical taxonomy:
        # "note" is a provenance synonym and normalizes to "session_note".
        # The raw type stays lossless in okf_type; the digest records the
        # same mapping via the same function.
        assert row[2] == "session_note"

    def test_frontmatter_tags_reach_promoter_index(self, manager):
        result = ingest_bundle(manager, OKF_FIXTURES / "degraded")
        assert result.concepts_ingested == 4
        # empty_type.md degrades (no type) but its tags still merge.
        tag_rows = {
            r[0]
            for r in manager.genome.conn.execute(
                "SELECT tag_value FROM promoter_index WHERE tag_type = 'domain'"
            )
        }
        assert {"degraded", "still-tagged"} <= tag_rows

    def test_vendored_bundle_end_to_end(self, manager):
        result = ingest_bundle(manager, OKF_FIXTURES / "crypto_bitcoin")
        assert result.concepts_total == 5
        assert result.concepts_ingested == 5
        assert result.warnings == []

        expected_links = sum(
            len(c.links)
            for c in read_bundle(OKF_FIXTURES / "crypto_bitcoin").concepts
        )
        assert result.links_captured == expected_links
        assert result.links_resolved + result.links_dangling == expected_links
        rows = _links_rows(manager.genome, "crypto_bitcoin")
        assert len(rows) == expected_links

    def test_frontmatter_type_free_form_source_kind(self, manager):
        result = ingest_bundle(manager, OKF_FIXTURES / "crypto_bitcoin")
        kinds = {
            r[0]
            for r in manager.genome.conn.execute(
                "SELECT DISTINCT source_kind FROM genes WHERE gene_id IN "
                f"({','.join('?' * len(result.gene_ids))})",
                result.gene_ids,
            )
        }
        assert "bigquery table" in kinds
        assert "bigquery dataset" in kinds


class TestLinkPersistence:
    def test_dangling_links_recorded_with_null_target(self, manager):
        ingest_bundle(manager, OKF_FIXTURES / "degraded")
        rows = _links_rows(manager.genome, "degraded")
        by_target = {r[1]: r for r in rows}

        dangling = by_target["missing/concept"]
        assert dangling[0] == "dangling"
        assert dangling[2] is not None  # source always resolves
        assert dangling[3] is None      # dangling target → NULL

        resolved = by_target["no_frontmatter"]
        assert resolved[3] is not None
        exists = manager.genome.conn.execute(
            "SELECT 1 FROM genes WHERE gene_id = ?", (resolved[3],)
        ).fetchone()
        assert exists is not None

    def test_multi_chunk_concept_resolves_to_parent(self, manager, tmp_path):
        paragraphs = "\n\n".join(
            f"Paragraph {i}: " + ("substantive sentence content. " * 30)
            for i in range(12)
        )
        (tmp_path / "big.md").write_text(
            f"---\ntype: Note\n---\n{paragraphs}", encoding="utf-8"
        )
        (tmp_path / "small.md").write_text(
            "---\ntype: Note\n---\nSee [big](/big.md).", encoding="utf-8"
        )
        result = ingest_bundle(manager, tmp_path, bundle_id="multi")
        assert len(result.gene_ids) > 2, "big.md must chunk into >= 2 strands"

        (row,) = _links_rows(manager.genome, "multi")
        target_gid = row[3]
        assert target_gid == manager._make_parent_doc_id("big.md")
        parent = manager.genome.conn.execute(
            "SELECT is_fragment FROM genes WHERE gene_id = ?", (target_gid,)
        ).fetchone()
        assert parent is not None

    def test_reingest_is_idempotent(self, manager):
        ingest_bundle(manager, OKF_FIXTURES / "degraded")
        first = _links_rows(manager.genome, "degraded")
        ingest_bundle(manager, OKF_FIXTURES / "degraded")
        second = _links_rows(manager.genome, "degraded")
        assert first == second

    def test_bundles_keep_separate_link_namespaces(self, manager):
        ingest_bundle(manager, OKF_FIXTURES / "degraded")
        ingest_bundle(manager, OKF_FIXTURES / "type_only")
        assert _links_rows(manager.genome, "type_only") == []
        assert len(_links_rows(manager.genome, "degraded")) == 2


class TestInertness:
    """Amendment 1: okf_links has zero retrieval readers; harmonic_links
    and gene_relations receive NO OKF link writes."""

    def test_no_harmonic_links_written(self, manager, tmp_path):
        ingest_bundle(manager, OKF_FIXTURES / "crypto_bitcoin")
        count = manager.genome.conn.execute(
            "SELECT COUNT(*) FROM harmonic_links"
        ).fetchone()[0]
        assert count == 0

    def test_gene_relations_identical_to_plain_ingest(self, manager):
        """OKF adds NOTHING to gene_relations beyond what a plain ingest
        of the same bodies produces (CHUNK_OF parent edges + entity-graph
        auto-links are standard ingest machinery, not OKF link writes)."""
        from tests.conftest import make_helix_config as _mkcfg

        bundle = read_bundle(OKF_FIXTURES / "crypto_bitcoin")
        ingest_bundle(manager, OKF_FIXTURES / "crypto_bitcoin")

        mgr_plain = HelixContextManager(_mkcfg())
        for c in bundle.concepts:
            mgr_plain.ingest(
                c.body,
                content_type="text",
                metadata={
                    "source_id": c.source_path,
                    "domains": list(c.tags),
                    "key_values": list(c.key_values),
                    "source_kind": c.raw_type,
                },
            )

        def _relations(genome):
            return sorted(
                tuple(r)
                for r in genome.conn.execute(
                    "SELECT gene_id_a, gene_id_b, relation FROM gene_relations"
                )
            )

        assert _relations(manager.genome) == _relations(mgr_plain.genome)
        assert (
            mgr_plain.genome.conn.execute(
                "SELECT COUNT(*) FROM okf_links"
            ).fetchone()[0]
            == 0
        )

    def test_okf_links_has_no_nontest_readers(self):
        """Grep-level guard: no retrieval/scoring module reads okf_links.

        Only the okf package (writer), storage DDL, and the CLI surface
        may mention the table outside tests.
        """
        import cymatix_context

        pkg_root = Path(cymatix_context.__file__).parent
        offenders = []
        for py in pkg_root.rglob("*.py"):
            rel = py.relative_to(pkg_root).as_posix()
            if "okf_links" not in py.read_text(encoding="utf-8", errors="ignore"):
                continue
            if not (
                rel.startswith("okf/")
                or rel == "storage/ddl.py"
                or rel.startswith("cli/")
            ):
                offenders.append(rel)
        assert offenders == [], f"unexpected okf_links readers: {offenders}"


class TestEmptyBody:
    def test_frontmatter_only_concept_accepted_without_genes(
        self, manager, tmp_path
    ):
        (tmp_path / "stub.md").write_text(
            "---\ntype: Note\n---\n", encoding="utf-8"
        )
        (tmp_path / "real.md").write_text(
            "---\ntype: Note\n---\nActual content here.", encoding="utf-8"
        )
        result = ingest_bundle(manager, tmp_path, bundle_id="stubbed")
        assert result.concepts_total == 2
        assert result.concepts_ingested == 1
