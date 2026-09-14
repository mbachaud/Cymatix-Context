"""General companion delivery contracts, independent of benchmark fixtures."""
import pytest
from contextlib import closing

from cymatix_context.companions import rank_companions
from cymatix_context.config import BudgetConfig, load_config
from cymatix_context.genome import Genome
from tests.conftest import make_gene


def document(text, source, position=0):
    doc = make_gene(text, domains=["manual"])
    doc.source_id = source
    doc.promoter.sequence_index = position
    return doc


def companion_budget(**overrides):
    return BudgetConfig(**{"full_text_delivery": True, "expression_tokens": 25000, **overrides})


def test_question_selects_distant_passage_without_changing_primary():
    seed = document("Orchard operations overview", "orchard.md")
    nearby = document("Harvest equipment inventory", "orchard.md", 1)
    distant = document("Irrigation valve pressure must be 42 psi", "orchard.md", 9)
    foreign = document("Irrigation valve pressure", "unselected.md")
    ranked = rank_companions("irrigation valve pressure", [seed], [nearby, distant, foreign, seed])
    assert ranked[0].gene_id == distant.gene_id
    assert {d.gene_id for d in ranked} == {nearby.gene_id, distant.gene_id}
    assert seed.content == "Orchard operations overview"


def test_empty_question_ties_are_stable_by_source_and_sequence():
    seed = document("Introduction", "guide", 2)
    near = document("First appendix", "guide", 3)
    far = document("Second appendix", "guide", 12)
    assert rank_companions("the", [seed], [far, near]) == [near, far]


def test_source_lookup_is_indexed_and_restricts_sources():
    with closing(Genome(":memory:")) as store:
        seed = document("Seed", "manual")
        tail = document("Complete appendix", "manual", 3)
        other = document("Unrelated source", "other")
        for doc in [seed, tail, other]:
            store.upsert_gene(doc, apply_gate=False)
        assert {d.gene_id for d in store.get_source_documents(["manual", "manual"])} == {seed.gene_id, tail.gene_id}
        assert store.get_source_documents([]) == []
        plan = store.read_conn.execute("EXPLAIN QUERY PLAN SELECT * FROM genes WHERE source_id IN (?)", ("manual",)).fetchall()
        assert "idx_genes_source" in str([tuple(row) for row in plan])


def test_companion_defaults_and_validation():
    config = BudgetConfig()
    assert config.full_text_delivery is False
    assert config.expression_tokens == 7000
    assert config.companion_chunks == 4
    assert config.context_max_chars == 100_000
    assert load_config("cymatix.toml").budget.companion_chunks == 4
    with pytest.raises(ValueError):
        BudgetConfig(companion_chunks=-1)
    with pytest.raises(ValueError):
        BudgetConfig(context_max_chars=0)


def test_assembly_restores_full_primary_and_appends_same_source():
    from cymatix_context.context_manager import CymatixContextManager
    from tests.conftest import make_cymatix_config
    with closing(CymatixContextManager(make_cymatix_config(budget=companion_budget()))) as manager:
        seed = document("Overview " + "detail " * 150 + "PRIMARY TAIL", "orchard.md")
        tail = document("Irrigation pressure 42 psi APPENDIX TAIL", "orchard.md", 4)
        manager.genome.upsert_gene(seed, apply_gate=False)
        manager.genome.upsert_gene(tail, apply_gate=False)
        window = manager._assemble("irrigation pressure", [seed], {seed.gene_id: "short summary"}, ignore_delivered=True)
        assert "PRIMARY TAIL" in window.expressed_context
        assert "APPENDIX TAIL" in window.expressed_context
        assert window.expressed_gene_ids == [seed.gene_id, tail.gene_id]
        assert len(window.expressed_context) <= manager.config.budget.context_max_chars


def test_assembly_skips_oversized_companion_without_truncating_seed():
    from cymatix_context.context_manager import CymatixContextManager
    from tests.conftest import make_cymatix_config
    with closing(CymatixContextManager(make_cymatix_config(budget=companion_budget(context_max_chars=700)))) as manager:
        seed = document("Seed body remains whole", "manual")
        oversized = document("pressure " * 1000, "manual", 1)
        small = document("pressure appendix fits", "manual", 2)
        for doc in [seed, oversized, small]:
            manager.genome.upsert_gene(doc, apply_gate=False)
        window = manager._assemble("pressure", [seed], {}, ignore_delivered=True)
        assert seed.content in window.expressed_context
        assert small.content in window.expressed_context
        assert oversized.gene_id not in window.expressed_gene_ids
        assert len(window.expressed_context) <= 700


def test_packet_preserves_primary_then_separate_complete_companions(monkeypatch):
    from cymatix_context.context_packet import build_context_packet
    with closing(Genome(":memory:")) as store:
        seed = document("Overview " * 500 + "PRIMARY TAIL", "manual")
        tail = document("Irrigation " * 300 + "COMPANION TAIL", "manual", 4)
        for doc in [seed, tail]:
            store.upsert_gene(doc, apply_gate=False)
        monkeypatch.setattr(store, "query_docs", lambda **kwargs: [seed])
        packet = build_context_packet("irrigation", genome=store, budget_config=companion_budget())
        assert (packet.verified + packet.stale_risk)[0].content == seed.content
        assert packet.companions[0].content == tail.content
        assert packet.delivery.delivered_gene_ids == [seed.gene_id, tail.gene_id]
        assert tail.gene_id not in packet.retrieval_scores


def test_companions_respect_party_and_archive_boundaries():
    from cymatix_context.schemas import ChromatinState
    with closing(Genome(":memory:")) as store:
        own = document("Owner details", "shared")
        foreign = document("Private foreign details", "shared", 1)
        archived = document("Archived details", "shared", 2)
        for doc in [own, foreign, archived]:
            store.upsert_gene(doc, apply_gate=False)
        store.conn.execute("INSERT INTO parties (party_id, display_name, created_at) VALUES ('other', 'Other', 1)")
        store.conn.execute("INSERT INTO gene_attribution (gene_id, party_id, authored_at) VALUES (?, ?, 1)", (foreign.gene_id, "other"))
        store.conn.execute("UPDATE genes SET chromatin=? WHERE gene_id=?", (int(ChromatinState.HETEROCHROMATIN), archived.gene_id))
        store.conn.commit()
        assert [d.gene_id for d in store.get_source_documents(["shared"], party_id="owner")] == [own.gene_id]


def test_packet_preserves_whitespace_and_honors_small_token_budget(monkeypatch):
    from cymatix_context.context_packet import build_context_packet
    from cymatix_context.accel import estimate_tokens
    with closing(Genome(":memory:")) as store:
        seed = document("  body with exact whitespace\n\n", "manual")
        tail = document("long appendix " * 900, "manual", 1)
        for doc in [seed, tail]:
            store.upsert_gene(doc, apply_gate=False)
        monkeypatch.setattr(store, "query_docs", lambda **kwargs: [seed])
        budget = companion_budget(ribosome_tokens=0, expression_tokens=700)
        packet = build_context_packet("appendix", genome=store, budget_config=budget)
        assert (packet.verified + packet.stale_risk)[0].content == seed.content
        assert estimate_tokens(packet.model_dump_json()) <= 700
        assert not packet.companions
        assert packet.know is None


def test_sharded_lookup_routes_only_selected_sources_and_passes_party():
    import sqlite3
    from types import SimpleNamespace
    from cymatix_context.sharding import ShardedGenomeAdapter
    from unittest.mock import Mock
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE fingerprint_index (source_id TEXT, shard_name TEXT)")
        conn.executemany("INSERT INTO fingerprint_index VALUES (?, ?)", [("manual", "a"), ("manual", "a"), ("other", "b")])
        doc = document("Valid appendix", "manual")
        shard = SimpleNamespace(get_source_documents=Mock(return_value=[doc]))
        router = SimpleNamespace(main_conn=conn, _open_shard=Mock(return_value=shard))
        adapter = object.__new__(ShardedGenomeAdapter)
        adapter._router = router
        assert adapter.get_source_documents(["manual"], party_id="owner") == [doc]
        router._open_shard.assert_called_once_with("a")
        shard.get_source_documents.assert_called_once_with(["manual"], party_id="owner")
    finally:
        conn.close()


@pytest.mark.parametrize("surface", ["packet", "expressed"])
def test_token_oversized_companion_does_not_displace_smaller_one(monkeypatch, surface):
    from cymatix_context.context_packet import build_context_packet
    from cymatix_context.context_manager import CymatixContextManager
    from tests.conftest import make_cymatix_config
    budget = companion_budget(ribosome_tokens=0, expression_tokens=1000)
    with closing(CymatixContextManager(make_cymatix_config(budget=budget))) as manager:
        seed = document("Orchard overview", "manual")
        giant = document("valve pressure " * 800, "manual", 1)
        small = document("pressure details", "manual", 2)
        for doc in [seed, giant, small]:
            manager.genome.upsert_gene(doc, apply_gate=False)
        if surface == "packet":
            monkeypatch.setattr(manager.genome, "query_docs", lambda **kwargs: [seed])
            result = build_context_packet("valve pressure", genome=manager.genome, budget_config=budget)
            ids = result.delivery.delivered_gene_ids
        else:
            result = manager._assemble("valve pressure", [seed], {}, ignore_delivered=True)
            ids = result.expressed_gene_ids
        assert giant.gene_id not in ids
        assert small.gene_id in ids


def test_full_text_session_repeats_and_explicit_redelivery():
    from cymatix_context.context_manager import CymatixContextManager
    from tests.conftest import make_cymatix_config
    with closing(CymatixContextManager(make_cymatix_config(budget=companion_budget()))) as manager:
        seed = document("Primary unchanged", "manual")
        tail = document("Appendix value 42", "manual", 1)
        for doc in [seed, tail]:
            manager.genome.upsert_gene(doc, apply_gate=False)
        first = manager._assemble("appendix value", [seed], {}, session_id="test-session")
        repeat = manager._assemble("appendix value", [seed], {}, session_id="test-session")
        full = manager._assemble("appendix value", [seed], {}, session_id="test-session", ignore_delivered=True)
        assert tail.content in first.expressed_context
        assert tail.content not in repeat.expressed_context
        assert tail.content in full.expressed_context
