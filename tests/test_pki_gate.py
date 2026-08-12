"""PKI read gate (``[retrieval] pki_enabled``) — the off-switch the
2026-08-06 retrieval-layer ledger flagged as missing ("unmeasured — no
off-switch exists").

Contract: default ON keeps Tier 0 byte-identical (the ``pki`` signal is
timed on every query, rare path×key pairs contribute score); OFF skips
the ``path_key_index`` read entirely — no ``pki`` signal in
``last_signal_timings`` (the ablation-ladder observable), no ``pki``
tier contribution, other tiers untouched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from cymatix_context.config import CymatixConfig, load_config
from cymatix_context.knowledge_store import Genome
from cymatix_context.schemas import EpigeneticMarkers, Gene, PromoterTags

REPO_ROOT = Path(__file__).resolve().parents[1]

QUERY = ["alphaproj", "cymatix_port"]


def _pki_gene() -> Gene:
    """A gene whose path tokens AND kv keys both appear in QUERY, so the
    Tier-0 path-key pair (alphaproj, cymatix_port) fires with cardinality
    1 (bonus PKI_BASE / PKI_FLOOR = 5.0 > 0)."""
    content = "The alphaproj service reads cymatix_port from its config."
    g = Gene(
        gene_id=Genome.make_gene_id(content),
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=["alphaproj"], entities=["cymatix_port"]),
        epigenetics=EpigeneticMarkers(),
        key_values=["cymatix_port=11437"],
    )
    g.source_id = "alphaproj/config.toml"
    return g


def _query(store: Genome):
    return store.query_docs(QUERY, [], max_genes=5)


# ── store-level gate ───────────────────────────────────────────────────


def test_pki_on_by_default_times_signal_and_contributes():
    store = Genome(path=":memory:")
    try:
        store.upsert_gene(_pki_gene())
        _query(store)
        assert "pki" in store.last_signal_timings, (
            "default store must run Tier 0 and time the pki signal"
        )
        pki_hits = [
            gid
            for gid, tiers in store.last_tier_contributions.items()
            if "pki" in tiers
        ]
        assert pki_hits, "rare path×key pair must contribute pki score"
    finally:
        store.close()


def test_pki_disabled_skips_tier_entirely():
    store = Genome(path=":memory:", pki_enabled=False)
    try:
        store.upsert_gene(_pki_gene())
        results = _query(store)
        assert "pki" not in store.last_signal_timings, (
            "pki_enabled=False must skip Tier 0 — no signal timing"
        )
        assert not any(
            "pki" in tiers for tiers in store.last_tier_contributions.values()
        ), "no gene may carry a pki tier contribution when the gate is off"
        assert results, "other tiers must still return the gene"
    finally:
        store.close()


# ── config threading ───────────────────────────────────────────────────


def test_retrieval_config_default_is_enabled():
    assert CymatixConfig().retrieval.pki_enabled is True


def test_toml_can_disable_pki(tmp_path):
    toml = tmp_path / "cymatix.toml"
    toml.write_text("[retrieval]\npki_enabled = false\n", encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.retrieval.pki_enabled is False


# ── ablation-ladder arm ────────────────────────────────────────────────


def _load_ladder():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    path = REPO_ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py"
    spec = importlib.util.spec_from_file_location("_erb_ladder_pki", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ladder_has_no_pki_arm_with_signal_validation():
    ladder = _load_ladder()
    arm = next((a for a in ladder.ARMS if a.name == "no_pki"), None)
    assert arm is not None, "ablation ladder must carry a no_pki arm"
    assert arm.knobs == {"retrieval.pki_enabled": False}
    assert arm.validation == ladder.VALIDATION_SIGNAL
    assert arm.expect_absent == ("pki",)


def test_ladder_has_combined_no_splade_no_pki_arm():
    """B3 card (issues #333/#334) runs SPLADE off / PKI off / BOTH."""
    ladder = _load_ladder()
    arm = next(
        (a for a in ladder.ARMS if a.name == "no_splade_no_pki"), None
    )
    assert arm is not None, "B3 needs the combined splade+pki-off arm"
    assert arm.knobs == {
        "ingestion.splade_enabled": False,
        "retrieval.pki_enabled": False,
    }
    assert arm.validation == ladder.VALIDATION_SIGNAL
    assert arm.expect_absent == ("splade", "pki")


# ── hot-swap path stays in sync ────────────────────────────────────────


def test_swap_db_preserves_pki_disabled(tmp_path):
    """/admin/swap-db must rebuild the store with the configured gate.

    The 2026-08-09 receipt invalidations came from exactly this drift
    class (filename_anchor/bm25 lost on swap): a bench that disables PKI
    and then swaps a fixture bed must not get PKI silently re-enabled.
    """
    from cymatix_context.config import GenomeConfig, RetrievalConfig
    from tests.conftest import (
        MockCompressorBackend,
        make_client,
        make_cymatix_config,
    )

    db_a = str(tmp_path / "a.db")
    db_b = str(tmp_path / "b.db")
    for p in (db_a, db_b):
        ks = Genome(path=p)
        ks.upsert_doc(_pki_gene(), apply_gate=False)
        ks.close()

    config = make_cymatix_config(
        genome=GenomeConfig(path=db_a, cold_start_threshold=5),
        retrieval=RetrievalConfig(pki_enabled=False),
    )
    client = make_client(config=config, backend=MockCompressorBackend())
    app = client.app

    assert app.state.cymatix.genome._pki_enabled is False

    resp = client.post("/admin/swap-db", json={"path": db_b})
    assert resp.status_code == 200

    assert app.state.cymatix.genome._pki_enabled is False, (
        "swap-db rebuilt the store with PKI re-enabled — knob drift"
    )
