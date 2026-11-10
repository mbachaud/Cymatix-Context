"""Fresh-ingest determinism: same content in, same expressed_context out.

The compaction-recall bench (benchmarks/compaction_recall/) rebuilt fresh
stores twice at one commit with PYTHONHASHSEED=0 and differed on 2/834
needles, both in a chain ingested incrementally across boundaries. Root
cause: ``KnowledgeStore.corpus_size()`` memoized the document count for a
60 s wall-clock TTL and in-process writes never invalidated it. The IDF
lexical-anchor tier (``idf = log(N / df)``, gated at a boost > 1.0) then
used the size from the first query, while the store kept growing, and whether
it refreshed depended on how long ingest had taken. Prebuilt beds never
showed it: their size does not change between queries.
"""

from __future__ import annotations

import time

from cymatix_context.config import GenomeConfig
from cymatix_context.context_manager import CymatixContextManager
from tests.conftest import make_cymatix_config, make_gene, requires_spacy_model

N_EARLY = 10
N_LATE = 290
# "team4" tags 60 of the 300 docs: with the true size its IDF boost is
# 1.5 * ln(300 / 60) ~= 2.4 (fires); with a size memoized after the first
# 10 docs it is floored to 100, 1.5 * ln(100 / 60) ~= 0.77 (gated off).
QUERY = "which module handles team4"


def _docs() -> list[str]:
    return [
        f"Note {i}: the widget{i} module number {i} lives in plant{i % 7} "
        f"and handles batch{i} for team{i % 5}."
        for i in range(N_EARLY + N_LATE)
    ]


def _manager(tmp_path, name: str) -> CymatixContextManager:
    path = tmp_path / name / "genome.db"
    path.parent.mkdir()
    cfg = make_cymatix_config(genome=GenomeConfig(path=str(path), cold_start_threshold=5))
    return CymatixContextManager(cfg)


def _query(mgr: CymatixContextManager) -> str:
    return mgr.build_context(QUERY, read_only=True, ignore_delivered=True).expressed_context


def _ingest(mgr: CymatixContextManager, docs: list[str]) -> None:
    for d in docs:
        mgr.ingest(d)


@requires_spacy_model
def test_read_only_query_does_not_change_a_later_answer(tmp_path, monkeypatch):
    """A store queried mid-ingest answers like one that was never queried."""
    monkeypatch.setenv("CYMATIX_DISABLE_LEARN", "1")
    docs = _docs()
    cold = _manager(tmp_path, "cold")
    warm = _manager(tmp_path, "warm")
    try:
        _ingest(cold, docs)
        _ingest(warm, docs[:N_EARLY])
        _query(warm)
        _ingest(warm, docs[N_EARLY:])
        assert _query(warm) == _query(cold)
    finally:
        cold.close()
        warm.close()


@requires_spacy_model
def test_answer_does_not_depend_on_elapsed_wall_clock(tmp_path, monkeypatch):
    """Same operations, same content; only the clock between queries differs.

    This is the bench's shape: both runs did ingest -> query -> ingest ->
    query, and one of them was slow enough to cross the 60 s memo TTL.
    """
    monkeypatch.setenv("CYMATIX_DISABLE_LEARN", "1")
    real_time = time.time
    skew = {"s": 0.0}
    monkeypatch.setattr(time, "time", lambda: real_time() + skew["s"])
    docs = _docs()
    fast = _manager(tmp_path, "fast")
    slow = _manager(tmp_path, "slow")
    try:
        answers = {}
        for name, mgr in (("fast", fast), ("slow", slow)):
            _ingest(mgr, docs[:N_EARLY])
            _query(mgr)
            _ingest(mgr, docs[N_EARLY:])
            skew["s"] = 120.0 if name == "slow" else 0.0
            answers[name] = _query(mgr)
            skew["s"] = 0.0
        assert answers["fast"] == answers["slow"]
    finally:
        fast.close()
        slow.close()


def test_corpus_size_tracks_in_process_writes(genome):
    """The IDF denominator sees this store's own upserts and deletes at once."""
    a, b = make_gene("alpha doc about retrieval"), make_gene("beta doc about ranking")
    genome.upsert_doc(a)
    assert genome.corpus_size() == 1
    genome.upsert_doc(b)
    assert genome.corpus_size() == 2
    genome.delete_gene(a.gene_id)
    assert genome.corpus_size() == 1
