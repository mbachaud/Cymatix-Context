"""CYMATIX_SCORING_EPOCH pins the wall clock that scoring reads.

The authority tier's +0.5 "created in the last 48 h" boost and
DocumentSignals.access_rate() read the current time, so a long bench ladder
drifts once the bed crosses the 48 h mark mid-run (ERB c580 indexed rerun
2026-10-18: 20 needles >= erb_411 changed delivered_chars / first-gold
rank, 0 before). Unset = time.time(), unchanged.
"""

import time

from cymatix_context.clock import scoring_now
from cymatix_context.schemas import DocumentSignals


def test_unset_is_wall_clock(monkeypatch):
    monkeypatch.delenv("CYMATIX_SCORING_EPOCH", raising=False)
    assert abs(scoring_now() - time.time()) < 5


def test_env_pins_epoch(monkeypatch):
    monkeypatch.setenv("CYMATIX_SCORING_EPOCH", "1700000000.5")
    assert scoring_now() == 1700000000.5


def test_bad_value_falls_back_to_wall_clock(monkeypatch):
    monkeypatch.setenv("CYMATIX_SCORING_EPOCH", "not-a-number")
    assert abs(scoring_now() - time.time()) < 5


def test_access_rate_uses_pinned_clock(monkeypatch):
    epi = DocumentSignals(recent_accesses=[1_000_000.0])
    monkeypatch.setenv("CYMATIX_SCORING_EPOCH", "1000010.0")
    assert epi.access_rate(window_seconds=60) > 0
    monkeypatch.setenv("CYMATIX_SCORING_EPOCH", "2000000.0")
    assert epi.access_rate(window_seconds=60) == 0


def test_authority_recency_uses_pinned_clock(monkeypatch, tmp_path):
    from cymatix_context.knowledge_store import KnowledgeStore
    from tests.conftest import make_gene

    ks = KnowledgeStore(path=str(tmp_path / "a.db"), synonym_map={})
    g = make_gene("recency probe text", gene_id="clock_gene_00000001")
    g.epigenetics.created_at = 1_000_000.0
    ks.upsert_gene(g)
    cur = ks.read_conn.cursor()

    def boost_at(epoch):
        monkeypatch.setenv("CYMATIX_SCORING_EPOCH", str(epoch))
        scores = {g.gene_id: 1.0}
        ks._apply_authority_boosts(cur, scores, ["unrelatedterm"])
        return scores[g.gene_id]

    try:
        inside = boost_at(1_000_000.0 + 3600)          # 1 h after creation
        outside = boost_at(1_000_000.0 + 72 * 3600)    # 72 h after creation
        assert inside > outside
    finally:
        ks.close()
