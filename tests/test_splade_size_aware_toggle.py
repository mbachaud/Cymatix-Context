"""Size-aware SPLADE auto-toggle (issue #164).

Tests for ``resolve_splade_enabled`` (pure resolver) and the
``KnowledgeStore.upsert_doc`` integration that consults it per upsert.

The resolver is the unit test surface; the integration test verifies the
``upsert_doc`` path honors the resolved decision by inspecting the
``splade_terms`` table after a series of upserts that cross the
threshold.
"""

from __future__ import annotations

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.schemas import Gene
from cymatix_context.storage.indexes import resolve_splade_enabled


# ── 1. resolve_splade_enabled — pure resolver ────────────────────────────


class TestResolveSpladeEnabled:
    """The pure resolver: no DB, no side effects."""

    def test_default_off_is_byte_identical_to_static(self):
        """Both thresholds 0 -> static flag wins (byte-identical to pre-#164)."""
        assert resolve_splade_enabled(True, 0) is True
        assert resolve_splade_enabled(True, 1_000_000) is True
        assert resolve_splade_enabled(False, 0) is False
        assert resolve_splade_enabled(False, 1_000_000) is False

    def test_auto_enable_below_overrides_static_off(self):
        """``current < auto_enable_below`` -> force ON even if static OFF."""
        assert resolve_splade_enabled(
            splade_enabled=False, current_gene_count=100,
            auto_enable_below=50_000,
        ) is True

    def test_auto_disable_above_overrides_static_on(self):
        """``current > auto_disable_above`` -> force OFF even if static ON."""
        assert resolve_splade_enabled(
            splade_enabled=True, current_gene_count=200_001,
            auto_disable_above=200_000,
        ) is False

    def test_gray_band_respects_static_flag(self):
        """Between thresholds -> static flag is authoritative."""
        for static in (True, False):
            assert resolve_splade_enabled(
                splade_enabled=static, current_gene_count=75_000,
                auto_enable_below=50_000,
                auto_disable_above=200_000,
            ) is static

    def test_boundary_at_enable_threshold_is_not_auto_enabled(self):
        """``current == auto_enable_below`` -> static flag wins.

        Strict inequality means the threshold is the smallest count at
        which auto-enable is NOT applied. Documented in the resolver
        docstring as the deliberate-binary behaviour at the bound.
        """
        assert resolve_splade_enabled(
            splade_enabled=False, current_gene_count=50_000,
            auto_enable_below=50_000,
        ) is False

    def test_boundary_at_disable_threshold_is_not_auto_disabled(self):
        """``current == auto_disable_above`` -> static flag wins."""
        assert resolve_splade_enabled(
            splade_enabled=True, current_gene_count=200_000,
            auto_disable_above=200_000,
        ) is True

    def test_disable_above_takes_precedence_over_enable_below(self):
        """Pathological config: enable_below > disable_above. The disable
        arm wins when ``current > disable_above`` -- the resolver's order
        of checks is documented and stable.
        """
        # Pathological: caller asked to auto-enable up to 1M and
        # auto-disable above 100K. current=500K > 100K -> OFF wins.
        assert resolve_splade_enabled(
            splade_enabled=True, current_gene_count=500_000,
            auto_enable_below=1_000_000,
            auto_disable_above=100_000,
        ) is False


# ── 2. upsert_doc integration ────────────────────────────────────────────


def _make_gene(content: str, suffix: str = "") -> Gene:
    """Fresh Gene with content-derived gene_id so successive
    ``upsert_doc`` calls insert rather than update.
    """
    payload = content + suffix
    return Gene(
        gene_id=KnowledgeStore.make_gene_id(payload),
        content=content,
        complement=f"Summary: {content[:40]}",
        codons=["chunk_0"],
        source_id=f"test://size-aware/{suffix}",
    )


class TestUpsertDocAutoToggleIntegration:
    """End-to-end through ``upsert_doc``: thresholds drive whether
    ``splade_terms`` rows land for a given upsert.
    """

    def test_static_on_default_behaviour_unchanged(self, tmp_path):
        """Default (both thresholds 0) - splade_enabled=True always writes."""
        ks = KnowledgeStore(
            path=str(tmp_path / "g.db"), synonym_map={},
            splade_enabled=True,
        )
        try:
            ks.upsert_doc(
                _make_gene("alpha content one", "a"), apply_gate=False,
                splade_sparse={"t1": 1.0},
            )
            ks.upsert_doc(
                _make_gene("beta content two", "b"), apply_gate=False,
                splade_sparse={"t2": 1.0},
            )
            rows = ks.conn.execute(
                "SELECT gene_id FROM splade_terms"
            ).fetchall()
            assert len(rows) == 2
        finally:
            ks.close()

    def test_auto_disable_above_skips_splade_when_corpus_grows(self, tmp_path):
        """When ``current_gene_count > auto_disable_above`` the SPLADE
        write is skipped even though the static flag is True.

        ``current_gene_count`` includes the gene being upserted (the
        ``INSERT INTO genes`` runs before the index-population stage in
        ``upsert_doc``). So with threshold=2:

          - Upsert #1: count=1, 1 > 2 false -> SPLADE ON
          - Upsert #2: count=2, 2 > 2 false -> SPLADE ON
          - Upsert #3: count=3, 3 > 2 true  -> SPLADE OFF

        Result: 2 distinct gene_ids land SPLADE rows; the third is
        auto-disabled.
        """
        ks = KnowledgeStore(
            path=str(tmp_path / "g.db"), synonym_map={},
            splade_enabled=True,
            splade_auto_disable_above_genes=2,
        )
        try:
            ks.upsert_doc(
                _make_gene("alpha", "a"), apply_gate=False,
                splade_sparse={"t1": 1.0},
            )
            ks.upsert_doc(
                _make_gene("beta", "b"), apply_gate=False,
                splade_sparse={"t2": 1.0},
            )
            ks.upsert_doc(
                _make_gene("gamma", "c"), apply_gate=False,
                splade_sparse={"t3": 1.0},
            )

            distinct = ks.conn.execute(
                "SELECT COUNT(DISTINCT gene_id) FROM splade_terms"
            ).fetchone()[0]
            assert distinct == 2, (
                f"expected the 3rd upsert's SPLADE to be auto-disabled; "
                f"got {distinct} distinct gene_ids in splade_terms"
            )
        finally:
            ks.close()

    def test_auto_enable_below_forces_splade_when_corpus_small(self, tmp_path):
        """When ``current_gene_count < auto_enable_below`` SPLADE writes
        land even though the static flag is False.

        ``current_gene_count`` includes the gene being upserted. So with
        threshold=4 and static_off:

          - Upsert #1: count=1, 1 < 4 true  -> forced ON
          - Upsert #2: count=2, 2 < 4 true  -> forced ON
          - Upsert #3: count=3, 3 < 4 true  -> forced ON
          - Upsert #4: count=4, 4 < 4 false -> static (False) wins -> OFF

        Result: first three upserts land SPLADE; the fourth does not.
        """
        ks = KnowledgeStore(
            path=str(tmp_path / "g.db"), synonym_map={},
            splade_enabled=False,    # statically OFF
            splade_auto_enable_below_genes=4,
        )
        try:
            ks.upsert_doc(
                _make_gene("alpha", "a"), apply_gate=False,
                splade_sparse={"t1": 1.0},
            )
            ks.upsert_doc(
                _make_gene("beta", "b"), apply_gate=False,
                splade_sparse={"t2": 1.0},
            )
            ks.upsert_doc(
                _make_gene("gamma", "c"), apply_gate=False,
                splade_sparse={"t3": 1.0},
            )
            ks.upsert_doc(
                _make_gene("delta", "d"), apply_gate=False,
                splade_sparse={"t4": 1.0},
            )

            distinct = ks.conn.execute(
                "SELECT COUNT(DISTINCT gene_id) FROM splade_terms"
            ).fetchone()[0]
            assert distinct == 3, (
                f"first three upserts should land SPLADE rows via "
                f"auto-enable-below; got {distinct}"
            )
        finally:
            ks.close()

    def test_static_off_with_thresholds_zero_writes_nothing(self, tmp_path):
        """splade_enabled=False with both thresholds 0 -> SPLADE never
        writes (the byte-identical pre-#164 negative-path control).

        Pre-#164 behaviour skipped creating the ``splade_terms`` table
        entirely when SPLADE was off; that is preserved when both
        thresholds are 0. We assert either the table is absent OR (if
        present for some unrelated reason) it has zero rows.
        """
        ks = KnowledgeStore(
            path=str(tmp_path / "g.db"), synonym_map={},
            splade_enabled=False,
        )
        try:
            for s in ("a", "b", "c"):
                ks.upsert_doc(
                    _make_gene(f"content-{s}", s), apply_gate=False,
                    splade_sparse={"t": 1.0},
                )
            has_table = ks.conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='splade_terms'"
            ).fetchone()[0]
            if has_table:
                row = ks.conn.execute(
                    "SELECT COUNT(*) FROM splade_terms"
                ).fetchone()
                assert row[0] == 0
            # The pre-#164 path (both thresholds 0) skips table
            # creation entirely; that's the byte-identical control.
        finally:
            ks.close()


# ── 3. config plumbing ────────────────────────────────────────────────────


class TestConfigPlumbing:
    """The two thresholds round-trip through the dataclass + TOML loader."""

    def test_defaults_are_off(self):
        from cymatix_context.config import IngestionConfig
        cfg = IngestionConfig()
        assert cfg.splade_auto_enable_below_genes == 0
        assert cfg.splade_auto_disable_above_genes == 0

    def test_loader_reads_overrides(self, tmp_path):
        from cymatix_context.config import load_config

        path = tmp_path / "helix.toml"
        path.write_text(
            "[ingestion]\n"
            "splade_enabled = true\n"
            "splade_auto_enable_below_genes = 5000\n"
            "splade_auto_disable_above_genes = 250000\n",
            encoding="utf-8",
        )

        cfg = load_config(str(path))
        assert cfg.ingestion.splade_auto_enable_below_genes == 5000
        assert cfg.ingestion.splade_auto_disable_above_genes == 250000

    def test_knowledge_store_stores_thresholds(self, tmp_path):
        ks = KnowledgeStore(
            path=str(tmp_path / "g.db"), synonym_map={},
            splade_enabled=True,
            splade_auto_enable_below_genes=7,
            splade_auto_disable_above_genes=70,
        )
        try:
            assert ks._splade_auto_enable_below == 7
            assert ks._splade_auto_disable_above == 70
        finally:
            ks.close()
