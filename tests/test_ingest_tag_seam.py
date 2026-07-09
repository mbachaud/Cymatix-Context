"""Ingest tag seam — caller-supplied domains/entities/key_values merge.

Covers the two council-required properties of the structured-source seam
in ``HelixContextManager.ingest`` (OKF Phase 1, Commit 1):

1. **Equivalence** — tags supplied via ``metadata`` produce identical
   promoter_index / genes_fts / path_key_index rows to the same tags
   produced by the tagger itself.
2. **Bench-neutrality** — the seam is a provable no-op when none of the
   recognized keys is present in metadata, so every existing ingest
   caller (bench beds, the #239 rig, CLI/HTTP/MCP) is untouched.
"""

import json

import pytest

from helix_context.context_manager import (
    HelixContextManager,
    _merge_caller_tags,
)
from helix_context.schemas import Gene, PromoterTags

from tests.conftest import make_gene, make_helix_config

pytest.importorskip("spacy")


# Content rich enough that the CpuTagger produces non-empty domains,
# entities, and key_values on its own (spaCy NER + tech dictionary +
# key=value regex all fire). Single paragraph → single strand → the
# document keeps its content-addressed gene_id in both managers.
_CONTENT = (
    "The Redis cache layer behind the FastAPI service is maintained by "
    "Grace Hopper at Microsoft. Connection settings: port=6379 and "
    "model=bge-m3. Python clients reconnect through the sqlite fallback."
)
_SOURCE = "docs/runbooks/cache-layer.md"


def _fresh_manager() -> HelixContextManager:
    return HelixContextManager(make_helix_config())


def _index_rows(genome, gene_id):
    """Snapshot the three seam-relevant indexes for one document."""
    cur = genome.conn.cursor()
    promoter = sorted(
        tuple(r)
        for r in cur.execute(
            "SELECT tag_type, tag_value FROM promoter_index WHERE gene_id = ?",
            (gene_id,),
        )
    )
    fts = [
        tuple(r)
        for r in cur.execute(
            "SELECT content, complement FROM genes_fts WHERE gene_id = ?",
            (gene_id,),
        )
    ]
    path_key = sorted(
        tuple(r)
        for r in cur.execute(
            "SELECT path_token, kv_key FROM path_key_index WHERE gene_id = ?",
            (gene_id,),
        )
    )
    return promoter, fts, path_key


def _stored_tags(genome, gene_id):
    row = genome.conn.execute(
        "SELECT promoter, key_values FROM genes WHERE gene_id = ?", (gene_id,)
    ).fetchone()
    promoter = json.loads(row[0])
    key_values = json.loads(row[1]) if row[1] else []
    return promoter["domains"], promoter["entities"], key_values


def _strip_tagger_tags(mgr: HelixContextManager) -> None:
    """Wrap the manager's tagger so it emits no tags of its own.

    Everything else the tagger produces (complement, intent, summary,
    codons) is untouched, so the only difference between the two ingest
    runs is *where* the tags came from.
    """
    real_pack = mgr._cpu_tagger.pack

    def stripped_pack(*args, **kwargs):
        gene = real_pack(*args, **kwargs)
        gene.promoter.domains = []
        gene.promoter.entities = []
        gene.key_values = []
        return gene

    mgr._cpu_tagger.pack = stripped_pack


class TestEquivalence:
    def test_yaml_tags_produce_identical_index_rows(self):
        # Run A: tagger produces the tags.
        mgr_tagger = _fresh_manager()
        assert mgr_tagger._cpu_tagger is not None, "CpuTagger required"
        (gid_a,) = mgr_tagger.ingest(
            _CONTENT, content_type="text", metadata={"path": _SOURCE}
        )
        domains, entities, key_values = _stored_tags(mgr_tagger.genome, gid_a)
        assert domains and entities and key_values, (
            "fixture content must make the tagger emit all three tag kinds"
        )

        # Run B: tagger muted, the exact same tags arrive via metadata.
        mgr_yaml = _fresh_manager()
        _strip_tagger_tags(mgr_yaml)
        (gid_b,) = mgr_yaml.ingest(
            _CONTENT,
            content_type="text",
            metadata={
                "path": _SOURCE,
                "domains": domains,
                "entities": entities,
                "key_values": key_values,
            },
        )

        assert gid_a == gid_b  # content-addressed: same content, same id
        assert _index_rows(mgr_tagger.genome, gid_a) == _index_rows(
            mgr_yaml.genome, gid_b
        )
        assert _stored_tags(mgr_yaml.genome, gid_b) == (
            domains,
            entities,
            key_values,
        )


class TestBenchNeutrality:
    def test_seam_is_noop_without_caller_tag_keys(self, monkeypatch):
        """With the seam disabled entirely, plain ingest output is identical."""
        mgr_seam = _fresh_manager()
        (gid_1,) = mgr_seam.ingest(
            _CONTENT, content_type="text", metadata={"path": _SOURCE}
        )

        from helix_context import context_manager as cm

        monkeypatch.setattr(cm, "_merge_caller_tags", lambda gene, metadata: None)
        mgr_off = _fresh_manager()
        (gid_2,) = mgr_off.ingest(
            _CONTENT, content_type="text", metadata={"path": _SOURCE}
        )

        assert gid_1 == gid_2
        assert _stored_tags(mgr_seam.genome, gid_1) == _stored_tags(
            mgr_off.genome, gid_2
        )
        assert _index_rows(mgr_seam.genome, gid_1) == _index_rows(
            mgr_off.genome, gid_2
        )

    def test_helper_leaves_gene_untouched_without_keys(self):
        for metadata in (None, {}, {"path": "a/b.md", "source_id": "a/b.md"}):
            gene = make_gene(
                "content", domains=["alpha"], entities=["Beta"]
            )
            gene.key_values = ["k=v"]
            before = gene.model_dump()
            _merge_caller_tags(gene, metadata)
            assert gene.model_dump() == before


class TestMergeSemantics:
    def _gene(self) -> Gene:
        gene = make_gene(
            "content", domains=["cache", "redis"], entities=["Redis"]
        )
        gene.key_values = ["port=6379"]
        return gene

    def test_caller_values_prepend_and_dedupe_case_insensitively(self):
        gene = self._gene()
        _merge_caller_tags(
            gene,
            {
                "domains": ["Sales", "CACHE"],
                "entities": ["BigQuery", "redis"],
                "key_values": ["region=eu", "port=6379"],
            },
        )
        # Caller-first ordering; "CACHE"/"redis" collapse onto existing
        # values case-insensitively (first occurrence wins).
        assert gene.promoter.domains == ["Sales", "CACHE", "redis"]
        assert gene.promoter.entities == ["BigQuery", "redis"]
        # key_values dedupe exactly (values may be case-significant).
        assert gene.key_values == ["region=eu", "port=6379"]

    def test_key_values_case_sensitive_dedupe(self):
        gene = self._gene()
        _merge_caller_tags(gene, {"key_values": ["PORT=6379"]})
        assert gene.key_values == ["PORT=6379", "port=6379"]

    def test_values_coerced_to_stripped_strings(self):
        gene = make_gene("content")
        _merge_caller_tags(
            gene, {"domains": [" sales ", 42, ""], "entities": None}
        )
        assert gene.promoter.domains == ["sales", "42"]
        assert gene.promoter.entities == []

    def test_partial_keys_merge_only_those(self):
        gene = self._gene()
        _merge_caller_tags(gene, {"domains": ["sales"]})
        assert gene.promoter.domains == ["sales", "cache", "redis"]
        assert gene.promoter.entities == ["Redis"]
        assert gene.key_values == ["port=6379"]
