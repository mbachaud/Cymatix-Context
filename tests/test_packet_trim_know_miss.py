"""The full-text budget trim must keep the exactly-one-of know/miss contract.

Every /context/packet response carries exactly one of ``know`` or ``miss``
(wiki/Agent-Contract.md). The ``[budget] full_text_delivery`` trim loop
used to clear both after popping primary items, shipping neither.
"""
from contextlib import closing

from cymatix_context.config import BudgetConfig
from cymatix_context.genome import Genome
from tests.conftest import make_gene


def _doc(text, source):
    doc = make_gene(text, domains=["manual"])
    doc.source_id = source
    return doc


def _trimming_store(store, monkeypatch, scores):
    docs = [_doc(f"irrigation valve {name} " + "pressure " * 400, f"{name}.md")
            for name in ("first", "second", "third")]
    for doc in docs:
        store.upsert_gene(doc, apply_gate=False)

    def query_docs(**kwargs):
        store.last_query_scores = {d.gene_id: s for d, s in zip(docs, scores)}
        store.last_tier_contributions = {}
        return list(docs)

    monkeypatch.setattr(store, "query_docs", query_docs)
    return docs


def _budget():
    return BudgetConfig(full_text_delivery=True, ribosome_tokens=0, expression_tokens=1200)


def _delivered(packet):
    return [i.gene_id for i in (*packet.verified, *packet.stale_risk, *packet.companions)]


def test_primary_trim_keeps_exactly_one_of_know_or_miss(monkeypatch):
    from cymatix_context.context_packet import build_context_packet
    with closing(Genome(":memory:")) as store:
        _trimming_store(store, monkeypatch, scores=[0.9, 0.5, 0.2])
        packet = build_context_packet("irrigation valve pressure", genome=store, budget_config=_budget())
        assert packet.delivery.cap_binding == "token_budget"
        assert len(_delivered(packet)) < 3
        assert (packet.know is None) != (packet.miss is None)


def test_trimmed_know_points_at_a_delivered_document(monkeypatch):
    from cymatix_context.context_packet import build_context_packet
    from cymatix_context.schemas import KnowBlock
    from cymatix_context.scoring import know_decision

    def always_know(*, top_gene, top_score, score_gap, coordinate_confidence, **_):
        return KnowBlock(confidence=0.9, top_score=top_score, score_gap=score_gap,
                         lexical_dense_agree=False, gene_id_match=top_gene.gene_id,
                         coordinate_confidence=min(1.0, max(0.0, coordinate_confidence)))

    monkeypatch.setattr(know_decision, "decide_know_or_miss", always_know)
    with closing(Genome(":memory:")) as store:
        # Rank-0 document scores lowest, so it sorts to the tail and is trimmed first.
        docs = _trimming_store(store, monkeypatch, scores=[0.1, 0.5, 0.9])
        packet = build_context_packet("irrigation valve pressure", genome=store, budget_config=_budget())
        delivered = _delivered(packet)
        assert docs[0].gene_id not in delivered
        assert packet.miss is None
        assert packet.know is not None
        assert packet.know.gene_id_match in delivered
        assert packet.know.top_score == max(
            s for gid, s in zip([d.gene_id for d in docs], [0.1, 0.5, 0.9]) if gid in delivered)
