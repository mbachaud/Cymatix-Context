"""A request must tier on its own retrieval's scores, not another thread's.

``query_docs`` publishes its score maps on the store (``last_query_scores``
and friends) and ``build_context`` reads them back after retrieval returns.
Another thread's query can publish in that gap, including its intermediate
additive-scale map, and the reader then tiers this request's candidates on
foreign scores. Under RRF that reads as a flat score profile and the request
abstains: 39 to 45 of 180 calls on a file-backed store before the fix.
"""

from __future__ import annotations

import threading

import pytest

from cymatix_context.config import GenomeConfig
from cymatix_context.context_manager import CymatixContextManager
from cymatix_context.request_scoped import LastQueryValue, own_last
from tests.conftest import make_cymatix_config, make_gene

QUERY = "how does cache invalidation ttl work"
TOPICS = [
    "cache redis ttl invalidation",
    "auth token jwt session",
    "splice compress fragment",
]


def _manager(tmp_path):
    cfg = make_cymatix_config(
        genome=GenomeConfig(path=str(tmp_path / "genome.db"), cold_start_threshold=5))
    mgr = CymatixContextManager(cfg)
    for i in range(300):
        topic = TOPICS[i % 3]
        mgr.genome.upsert_gene(make_gene(
            f"{topic} document number {i} " * 20,
            domains=topic.split(), gene_id=f"g{i:014d}",
        ))
    return mgr


def _build(mgr):
    window = mgr.build_context(QUERY, read_only=True, ignore_delivered=True)
    return list(window.expressed_gene_ids or []), dict(window.metadata or {})


def test_foreign_publication_after_retrieval_does_not_change_the_result(tmp_path):
    mgr = _manager(tmp_path)
    try:
        baseline_ids, baseline_meta = _build(mgr)
        assert baseline_ids and baseline_meta.get("budget_tier") != "abstain"

        real_query_docs = mgr.genome.query_docs

        def query_then_foreign_write(*args, **kwargs):
            docs = real_query_docs(*args, **kwargs)
            flat = {d.gene_id: 32.74 for d in docs}

            def foreign():
                # What a concurrent query's intermediate publication looks
                # like: every candidate tied on the additive scale.
                with mgr.genome._last_query_scores_lock:
                    mgr.genome.last_query_scores = dict(flat)
                    mgr.genome.last_tier_contributions = {}

            t = threading.Thread(target=foreign)
            t.start()
            t.join()
            return docs

        mgr.genome.query_docs = query_then_foreign_write
        ids, meta = _build(mgr)
        assert meta.get("budget_tier") != "abstain", meta
        assert ids == baseline_ids
    finally:
        mgr.genome.close()


def test_concurrent_identical_queries_all_match_the_single_threaded_result(tmp_path):
    mgr = _manager(tmp_path)
    try:
        baseline_ids, _ = _build(mgr)
        assert baseline_ids
        results, errors = [], []

        def work():
            try:
                for _ in range(15):
                    results.append(_build(mgr))
            except BaseException as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=work, daemon=True) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        assert not any(t.is_alive() for t in threads)
        assert not errors, errors
        assert len(results) == 180
        abstained = [m for _, m in results if m.get("budget_tier") == "abstain"]
        assert not abstained, f"{len(abstained)}/180 calls abstained: {abstained[0]}"
        assert all(ids == baseline_ids for ids, _ in results)
    finally:
        mgr.genome.close()


class _Holder:
    last_query_scores = LastQueryValue()

    def __init__(self):
        self.last_query_scores = {}


def test_own_last_returns_this_threads_publication_and_attribute_stays_global():
    h = _Holder()
    h.last_query_scores = {"mine": 1.0}
    seen = {}

    def other():
        h.last_query_scores = {"theirs": 2.0}
        seen["other_own"] = own_last(h, "last_query_scores")

    t = threading.Thread(target=other)
    t.start()
    t.join()
    # The plain attribute keeps its meaning: the most recent publication.
    assert h.last_query_scores == {"theirs": 2.0}
    assert own_last(h, "last_query_scores") == {"mine": 1.0}
    assert seen["other_own"] == {"theirs": 2.0}


def test_own_last_falls_back_to_the_attribute():
    h = _Holder()
    h.last_query_scores = {"a": 1.0}
    seen = {}

    def never_published():
        seen["v"] = own_last(h, "last_query_scores")

    t = threading.Thread(target=never_published)
    t.start()
    t.join()
    assert seen["v"] == {"a": 1.0}

    class Plain:
        last_query_scores = {"p": 3.0}

    assert own_last(Plain(), "last_query_scores") == {"p": 3.0}
    assert own_last(object(), "last_query_scores", "dflt") == "dflt"


@pytest.mark.parametrize("name", [
    "last_query_scores", "last_tier_contributions", "last_signal_timings", "last_pool_total",
])
def test_store_publications_are_thread_scoped(tmp_path, name):
    from cymatix_context.knowledge_store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "genome.db"))
    try:
        setattr(store, name, "mine")
        t = threading.Thread(target=lambda: setattr(store, name, "theirs"))
        t.start()
        t.join()
        assert getattr(store, name) == "theirs"
        assert own_last(store, name) == "mine"
    finally:
        store.close()
