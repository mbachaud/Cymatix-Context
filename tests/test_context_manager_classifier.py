"""Integration tests: classifier wiring inside HelixContextManager.build_context()."""

import pytest

from helix_context.config import (
    BudgetConfig,
    ClassifierConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
)
from helix_context.context_manager import HelixContextManager
from tests.conftest import make_gene
from tests.conftest import MockCompressorBackend


@pytest.fixture
def manager():
    """Manager with mock backend + in-memory genome, seeded so build_context
    has candidates and reaches the metadata-emission tail of the pipeline."""
    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=4, splice_aggressiveness=0.5),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=True),
    )
    mgr = HelixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    # Seed a handful of genes so the empty-candidate early-return path
    # in build_context() is not taken; the classifier metadata block
    # lives below that early-return.
    for i, (content, doms, ents) in enumerate([
        ("Calculate the total cost of cloud migration projects",
         ["finance", "migration"], ["cost", "calculate", "total"]),
        ("Migration cost spreadsheet with monthly totals",
         ["finance", "migration"], ["cost", "total", "migration"]),
        ("Hello world greeting examples",
         ["greeting"], ["hello", "there"]),
        ("General notes about hello there phrasing",
         ["greeting"], ["hello"]),
    ]):
        mgr.genome.upsert_gene(
            make_gene(content, domains=doms, entities=ents,
                      gene_id=f"seed_gene_{i:010d}"),
        )
    yield mgr
    mgr.close()


def test_arithmetic_query_emits_classifier_metadata(manager):
    win = manager.build_context("Calculate the total cost of migration.")
    meta = (win.metadata or {}).get("classifier")
    assert meta is not None
    assert meta["class"] == "arithmetic"
    assert meta["assembly_max_genes_cap"] == 2
    assert meta["decoder_selected"] == "minimal"
    assert meta["override_applied"] is False
    assert "candidate_pool_size" in meta
    assert "max_genes_effective" in meta


def test_decoder_override_wins_but_classifier_still_logged(manager):
    win = manager.build_context(
        "Calculate the total cost of migration.",
        decoder_override="full",
    )
    meta = (win.metadata or {}).get("classifier")
    assert meta is not None
    assert meta["class"] == "arithmetic"            # classifier still ran
    assert meta["override_applied"] is True
    assert meta["decoder_selected"] == "minimal"   # what classifier *would* have picked


def test_classifier_disabled_skips_metadata(manager):
    manager.config.classifier.enabled = False
    win = manager.build_context("Calculate the total cost.")
    assert "classifier" not in (win.metadata or {})


def test_default_class_is_no_op_for_max_genes(manager):
    """A `default`-classified query produces identical max_genes_effective
    to a baseline run with the classifier disabled."""
    q = "Hello there."  # falls to default

    manager.config.classifier.enabled = False
    win_off = manager.build_context(q)

    manager.config.classifier.enabled = True
    win_on = manager.build_context(q)

    assert (win_off.metadata or {}).get("genes_expressed") == \
           (win_on.metadata or {}).get("genes_expressed")
