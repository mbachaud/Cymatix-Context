"""Grain-size ladder: classifier assembly caps measured in characters.

The query classifier's per-class assembly caps (arithmetic 2 / factual 5 /
procedural 6 / multi_hop 8) are seat counts sized for 4000-char documents.
On the ERB 580-char bed with no seat floor, 268/470 needles delivered
exactly 8 chunks (~6.5k chars) and gold at rank ~16-21 was cut.

``[budget] classifier_cap_ref_chars`` > 0 turns a cap of N seats into a
budget of N x ref_chars content characters: ranked candidates are kept while
they fit, and never fewer than the legacy N. 0 (default) = legacy seats.
"""

import pytest

from cymatix_context.config import (
    BudgetConfig,
    ClassifierConfig,
    CymatixConfig,
    GenomeConfig,
    RetrievalConfig,
    RibosomeConfig,
    load_config,
)
from cymatix_context.context_manager import CymatixContextManager
from cymatix_context.pipeline.tier_logic import seats_for_char_cap
from tests.conftest import MockCompressorBackend, make_gene
from tests.test_abstain_tier import _stub_express


def _genes(n, chars):
    return [make_gene("x" * chars, gene_id=f"cls_gene_{i:010d}") for i in range(n)]


@pytest.mark.parametrize("seats,ref,chars,n,expected", [
    (8, 2000, 565, 40, 28),   # 16000 // 565 = 28 small grains fit
    (8, 2000, 4000, 40, 8),   # large docs: never fewer than the legacy 8
    (2, 2000, 565, 40, 7),
    (8, 2000, 565, 5, 5),     # fewer candidates than either cap
])
def test_seats_for_char_cap(seats, ref, chars, n, expected):
    assert seats_for_char_cap(_genes(n, chars), seats, ref) == expected


def test_budget_default_is_legacy():
    assert BudgetConfig().classifier_cap_ref_chars == 0


def test_loader_reads_ref_chars(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[budget]\nclassifier_cap_ref_chars = 2000\n", encoding="utf-8")
    assert load_config(str(p)).budget.classifier_cap_ref_chars == 2000


def test_negative_ref_chars_rejected():
    with pytest.raises(ValueError):
        BudgetConfig(classifier_cap_ref_chars=-1)


def _pool(n=15):
    genes = [make_gene(f"Migration cost report {i:02d}: verified total {100 + i} dollars.",
                       gene_id=f"cls_seat_{i:010d}") for i in range(n)]
    scores = {g.gene_id: (4.0 if i == 0 else 3.0) for i, g in enumerate(genes)}  # BROAD tier
    return genes, scores


@pytest.mark.parametrize("ref", [0, 100])
def test_build_context_applies_char_based_classifier_cap(ref):
    cfg = CymatixConfig(
        budget=BudgetConfig(max_genes_per_turn=16, min_delivered_docs=0,
                            expression_tokens=20000, classifier_cap_ref_chars=ref),
        classifier=ClassifierConfig(enabled=True),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        retrieval=RetrievalConfig(fusion_mode="additive"),
        ribosome=RibosomeConfig(model="mock", timeout=5),
    )
    genes, scores = _pool()
    mgr = CymatixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    try:
        for g in genes:
            mgr.genome.upsert_gene(g)
        _stub_express(mgr, candidates=genes, scores=scores)
        window = mgr.build_context("Calculate the total cost of migration.")
        legacy = 2  # arithmetic class cap
        expected = legacy if ref == 0 else seats_for_char_cap(genes, legacy, ref)
        assert expected > legacy or ref == 0
        assert window.expressed_gene_ids == [g.gene_id for g in genes[:expected]]
    finally:
        mgr.close()
