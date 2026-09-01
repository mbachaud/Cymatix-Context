"""Phase 0.1 (serve-side) + 0.5 of the 2026-08-12 refinement campaign:
delivery-visibility block.

Motivation (docs/benchmarks/2026-08-12-refinement-campaign-plan.md):

* The 2026-07-30 Gemma expression campaign showed the served surfaces
  expose no signal that the assembly cap (5, not the token budget)
  truncated delivery — every arm looked identical from outside.
* The ablation ladder computes delivered-gold from
  ``window.expressed_gene_ids``; serving the delivered ids in final
  order lets any harness compute delivered-gold / pool_delivery_gap
  exactly by joining with gold (gold is unknowable at serve time).

Two surfaces are covered:

* ``/context/packet`` (and ``build_context_packet``): the packet
  envelope gains an optional top-level ``delivery`` block describing
  the packet's own delivery (its assembly cap is the ``max_genes``
  request parameter; the packet path has no token budget).
* The pipeline (``build_context`` → ``ContextWindow``): the window
  metadata gains a ``delivery`` dict where the classifier assembly cap
  and the ``[budget]`` token-budget eviction actually bind; the
  ``/context`` route serves it as a top-level ``delivery`` key.
"""

import pytest
from pydantic import ValidationError

from cymatix_context.config import (
    BudgetConfig,
    CymatixConfig,
    GenomeConfig,
    RibosomeConfig,
)
from cymatix_context.context_manager import CymatixContextManager
from cymatix_context.schemas import ContextPacket

from tests.conftest import (
    MockCompressorBackend,
    make_client,
    make_cymatix_config,
    make_gene,
)


# Starts with a wh-word, < 15 words, no arithmetic operator characters —
# rule-classifies as ``factual`` (assembly_max_genes_cap=5, the proven
# Gemma binding constraint at retrieval/query_classifier.py:193).
FACTUAL_QUERY = "what is the checkpoint interval value"


def _seed_topic_genes(genome, n: int) -> list[str]:
    """Seed ``n`` near-identical documents on one topic.

    Same domains/entities on every doc keeps retrieval scores nearly
    uniform so the score-ratio tier stays BROAD (no tier trim) and the
    classifier cap is the first constraint to bind.
    """
    ids = []
    for i in range(n):
        gene = make_gene(
            (
                "The checkpoint interval value controls the flush cadence "
                f"of the knowledge store writer. Note {i}: same topic, "
                "distinct document."
            ),
            domains=["checkpoint", "interval"],
            entities=["value"],
            gene_id=f"ckptgene{i:08d}",
        )
        genome.upsert_gene(gene)
        ids.append(gene.gene_id)
    return ids


# ── Pipeline surface (window.metadata["delivery"]) ────────────────────


def _make_manager(budget: BudgetConfig) -> CymatixContextManager:
    cfg = CymatixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=budget,
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
    )
    mgr = CymatixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    return mgr


@pytest.fixture
def manager():
    mgr = _make_manager(BudgetConfig(max_genes_per_turn=12, min_delivered_docs=0))  # legacy-mechanism tests ([w24-floor-flip])
    yield mgr
    mgr.close()


@pytest.fixture
def tiny_budget_manager():
    """Budget so small that assembly's eviction loop must fire."""
    mgr = _make_manager(
        BudgetConfig(max_genes_per_turn=12, ribosome_tokens=1, expression_tokens=1, min_delivered_docs=0)  # legacy-mechanism tests ([w24-floor-flip])
    )
    yield mgr
    mgr.close()


class TestWindowDeliveryRecording:
    """Phase 0.5 — which constraint actually truncated delivery."""

    def test_assembly_cap_binds_on_factual_query(self, manager):
        """Gemma replay in miniature: >cap candidates + factual query
        → the classifier cap of 5 (not the budget) truncates delivery."""
        _seed_topic_genes(manager.genome, 7)
        win = manager.build_context(FACTUAL_QUERY)
        delivery = (win.metadata or {}).get("delivery")
        assert delivery is not None
        assert delivery["cap_binding"] == "assembly_cap"
        assert delivery["delivered_count"] == 5
        assert delivery["delivered_gene_ids"] == list(win.expressed_gene_ids)
        assert delivery["pool_size"] > 5

    def test_token_budget_binds_under_tiny_budget(self, tiny_budget_manager):
        """Budget eviction below the cap → cap_binding == token_budget."""
        _seed_topic_genes(tiny_budget_manager.genome, 6)
        win = tiny_budget_manager.build_context(FACTUAL_QUERY)
        delivery = (win.metadata or {}).get("delivery")
        assert delivery is not None
        assert delivery["cap_binding"] == "token_budget"
        assert delivery["delivered_count"] < 5  # trimmed below the factual cap
        assert delivery["delivered_gene_ids"] == list(win.expressed_gene_ids)

    def test_none_when_nothing_binds(self, manager):
        """Fewer candidates than the cap and a roomy budget → none."""
        _seed_topic_genes(manager.genome, 3)
        win = manager.build_context(FACTUAL_QUERY)
        delivery = (win.metadata or {}).get("delivery")
        assert delivery is not None
        assert delivery["cap_binding"] == "none"
        assert delivery["delivered_count"] == len(win.expressed_gene_ids)
        assert delivery["delivered_gene_ids"] == list(win.expressed_gene_ids)


# ── Served surfaces (/context/packet and /context) ───────────────────


@pytest.fixture
def client():
    c = make_client(
        make_cymatix_config(budget=BudgetConfig(max_genes_per_turn=12, min_delivered_docs=0))  # legacy cap-binding route test ([w24-floor-flip])
    )
    yield c


class TestPacketDeliveryBlock:
    """Phase 0.1 serve-side — delivery block on the packet envelope."""

    def test_delivery_block_present_and_ordered(self, client):
        """delivered_gene_ids reproduce the packet's final item order."""
        genome = client.app.state.cymatix.genome
        _seed_topic_genes(genome, 3)
        resp = client.post("/context/packet", json={
            "query": "checkpoint interval value",
            "task_type": "explain",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "delivery" in data
        d = data["delivery"]
        assert d is not None
        items = list(data["verified"]) + list(data["stale_risk"])
        assert d["delivered_gene_ids"] == [it["gene_id"] for it in items]
        assert d["delivered_count"] == len(items)
        assert d["pool_size"] >= d["delivered_count"]
        assert d["cap_binding"] in ("assembly_cap", "token_budget", "none")

    def test_assembly_cap_when_max_genes_binds(self, client):
        """>cap candidates and max_genes=2 → the packet's count cap
        (derived from its ``max_genes`` parameter; the store returns up
        to ``max_genes * 2`` candidates) is reported as binding."""
        genome = client.app.state.cymatix.genome
        _seed_topic_genes(genome, 6)
        resp = client.post("/context/packet", json={
            "query": FACTUAL_QUERY,
            "max_genes": 2,
        })
        assert resp.status_code == 200
        d = resp.json()["delivery"]
        assert d["cap_binding"] == "assembly_cap"
        assert d["delivered_count"] >= 2
        assert d["pool_size"] > d["delivered_count"]

    def test_degrades_to_none_without_candidates(self):
        """Absent-data path: zeros + "none"; know/miss contract untouched.

        Builder-level: a store with zero matches (endpoint-level, a
        fully unmatched query raises PromoterMismatch inside the store —
        pre-existing behavior, unrelated to this block)."""
        from cymatix_context.context_packet import build_context_packet

        class _EmptyGenome:
            last_query_scores: dict = {}
            last_tier_contributions: dict = {}
            main_conn = None

            def query_docs(self, **kwargs):
                return []

        packet = build_context_packet(
            "zebra unicorn moonbeam",
            task_type="explain",
            genome=_EmptyGenome(),
        )
        assert packet.delivery is not None
        assert packet.delivery.cap_binding == "none"
        assert packet.delivery.delivered_count == 0
        assert packet.delivery.delivered_gene_ids == []
        assert packet.delivery.pool_size == 0
        # know/miss contract untouched: zero-candidate packet ships miss.
        assert packet.know is None
        assert packet.miss is not None
        # The dumped payload still validates as a ContextPacket — no
        # extra=forbid consumer breaks.
        ContextPacket(**packet.model_dump())

    def test_context_route_serves_pipeline_delivery(self, client):
        """The /context (continue) response carries the window's
        delivery block — the surface where cap/budget actually bind."""
        genome = client.app.state.cymatix.genome
        _seed_topic_genes(genome, 7)
        resp = client.post("/context", json={"query": FACTUAL_QUERY})
        assert resp.status_code == 200
        # /context returns a Continue-compatible one-element list.
        data = resp.json()[0]
        d = data.get("delivery")
        assert d is not None
        assert d["cap_binding"] == "assembly_cap"
        assert d["delivered_count"] == 5
        assert len(d["delivered_gene_ids"]) == 5
        assert d["pool_size"] > 5


# ── Schema contract ───────────────────────────────────────────────────


class TestDeliveryBlockSchema:
    def test_packet_defaults_to_no_delivery(self):
        p = ContextPacket(task_type="explain", query="q")
        assert p.delivery is None
        assert p.model_dump()["delivery"] is None

    def test_delivery_block_is_extra_forbid(self):
        from cymatix_context.schemas import DeliveryBlock
        with pytest.raises(ValidationError):
            DeliveryBlock(
                pool_size=1,
                delivered_count=1,
                delivered_gene_ids=["a"],
                cap_binding="none",
                bogus_field=1,
            )

    def test_delivery_block_rejects_unknown_cap_binding(self):
        from cymatix_context.schemas import DeliveryBlock
        with pytest.raises(ValidationError):
            DeliveryBlock(cap_binding="wat")

    def test_packet_round_trips_with_delivery(self):
        from cymatix_context.schemas import DeliveryBlock
        p = ContextPacket(
            task_type="explain",
            query="q",
            delivery=DeliveryBlock(
                pool_size=3,
                delivered_count=2,
                delivered_gene_ids=["a", "b"],
                cap_binding="assembly_cap",
            ),
        )
        dumped = p.model_dump()
        p2 = ContextPacket(**dumped)
        assert p2.delivery is not None
        assert p2.delivery.cap_binding == "assembly_cap"
        assert p2.delivery.delivered_gene_ids == ["a", "b"]
