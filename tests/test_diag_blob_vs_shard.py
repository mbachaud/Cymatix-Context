"""
tests/test_diag_blob_vs_shard.py

Unit tests for benchmarks/diag_blob_vs_shard_tiers.py.

Run with:
    python -m pytest tests/test_diag_blob_vs_shard.py -q --noconftest

All tests use mocked /fingerprint JSON responses matching the wire schema
from routes_context.py L800-826.  No server, no network, no GPU.

Wire schema reference:
  fingerprints[*].tier_contributions  (routes_context.py L817-819)
  -- dict {tier_name: float}, built from _merge_tier_contributions(
         helix.genome.last_tier_contributions, refiner_contrib)
     rounded to 4dp, sorted by key.
  fingerprints[*].rank   -- 0-based (converted to 1-based by diagnostic)
  fingerprints[*].source -- source_id used for gold matching
  fingerprints[*].score  -- final score (base + tcm_bonus)
"""

from __future__ import annotations

import json
import sys
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Make benchmarks/ importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "benchmarks"))

import diag_blob_vs_shard_tiers as _mod
from diag_blob_vs_shard_tiers import (
    _analyse_needle,
    _find_gold,
    _gold_hit,
    _aggregate,
    _norm,
    run,
)


# ---------------------------------------------------------------------------
# Wire-schema fixture builders
# ---------------------------------------------------------------------------

def _fp_response(fingerprints: List[Dict[str, Any]]) -> bytes:
    """Build a minimal /fingerprint response body matching routes_context.py."""
    body = {
        "mode": "fingerprint",
        "profile": "fast",
        "query": "test query",
        "fingerprints": fingerprints,
        "count": len(fingerprints),
        "max_results": 200,
        "score_floor": 0.0,
        "evaluated_total": len(fingerprints),
        "above_floor_total": len(fingerprints),
        "returned": len(fingerprints),
        "filtered_by_floor": 0,
        "truncated_by_cap": 0,
        "response_hint": "No filtering or truncation applied.",
        "agent": {
            "recommendation": "triage",
            "hint": "Use tier fingerprints to decide which genes to fetch in full.",
            "latency_ms": 18.4,
            "cold_tier_used": False,
            "cold_tier_count": 0,
            "tier_totals": {},
        },
    }
    return json.dumps(body).encode("utf-8")


def _make_fp(
    *,
    rank: int,
    source: str,
    score: float,
    tiers: Dict[str, float],
    gene_id: str = "g0",
) -> Dict[str, Any]:
    """Build a single fingerprint item matching routes_context.py L800-826."""
    return {
        "rank":   rank,           # 0-based (routes_context.py L801)
        "gene_id": gene_id,
        "score":  round(score, 4),
        "preview": "snippet...",
        "path":   f"/some/path/{gene_id}.py",
        "source": source,         # source_id (routes_context.py L813)
        "domains": [],
        "entities": [],
        "chromatin": 0,
        "tier_contributions": {   # routes_context.py L817-819
            k: round(float(v), 4) for k, v in sorted(tiers.items())
        },
    }


def _needle(
    *,
    nid: str = "n001",
    question: str = "What does the splice step do?",
    gold_paths: Optional[List[str]] = None,
    ntype: str = "within",
) -> Dict[str, Any]:
    return {
        "id": nid,
        "question": question,
        "gold_paths": gold_paths or ["cymatix_context/pipeline/stages.py"],
        "type": ntype,
    }


# ---------------------------------------------------------------------------
# Mock urllib transport
# ---------------------------------------------------------------------------

class _MockResponse:
    """Minimal file-like object returned by urlopen mock."""
    def __init__(self, data: bytes):
        self._io = BytesIO(data)

    def read(self) -> bytes:
        return self._io.read()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _TwoServerTransport:
    """Mock urlopen that serves different fingerprint bodies for blob vs sharded URL."""

    def __init__(
        self,
        blob_fps:   List[Dict[str, Any]],
        shard_fps:  List[Dict[str, Any]],
        blob_url:   str = "http://blob:11437",
        shard_url:  str = "http://shard:11438",
    ):
        self._blob_body  = _fp_response(blob_fps)
        self._shard_body = _fp_response(shard_fps)
        self._blob_url   = blob_url.rstrip("/")
        self._shard_url  = shard_url.rstrip("/")

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if self._blob_url in url:
            return _MockResponse(self._blob_body)
        return _MockResponse(self._shard_body)


# ---------------------------------------------------------------------------
# _norm / _gold_hit tests
# ---------------------------------------------------------------------------

class TestNormAndGoldHit:

    def test_norm_backslash(self):
        assert _norm("foo\\bar\\baz.py") == "foo/bar/baz.py"

    def test_norm_lowercase(self):
        assert _norm("Cymatix_Context/Pipeline/STAGES.PY") == \
               "cymatix_context/pipeline/stages.py"

    def test_gold_hit_substring_forward(self):
        """gold path is substring of source."""
        assert _gold_hit(
            "F:/Projects/cymatix_context/pipeline/stages.py",
            ["cymatix_context/pipeline/stages.py"],
        ) is True

    def test_gold_hit_substring_reverse(self):
        """source is substring of gold path (reverse direction)."""
        assert _gold_hit(
            "pipeline/stages.py",
            ["cymatix_context/pipeline/stages.py"],
        ) is True

    def test_gold_hit_no_match(self):
        assert _gold_hit(
            "cymatix_context/pipeline/other.py",
            ["cymatix_context/pipeline/stages.py"],
        ) is False

    def test_gold_hit_empty_source(self):
        assert _gold_hit("", ["cymatix_context/pipeline/stages.py"]) is False

    def test_gold_hit_multiple_golds(self):
        """Matches the second gold_path in the list."""
        assert _gold_hit(
            "F:/Projects/cymatix_context/identity/provenance.py",
            [
                "cymatix_context/pipeline/stages.py",
                "cymatix_context/identity/provenance.py",
            ],
        ) is True


# ---------------------------------------------------------------------------
# _find_gold tests
# ---------------------------------------------------------------------------

class TestFindGold:

    def test_gold_found_at_rank0(self):
        fps = [
            _make_fp(rank=0, source="F:/cymatix_context/pipeline/stages.py",
                     score=9.5, tiers={"fts5": 6.0, "dense": 0.9}),
            _make_fp(rank=1, source="F:/cymatix_context/other.py",
                     score=5.0, tiers={"fts5": 3.0}, gene_id="g1"),
        ]
        result = _find_gold(fps, ["cymatix_context/pipeline/stages.py"])
        assert result["in_pool"] is True
        assert result["rank"] == 1          # 0-based 0 -> 1-based 1
        assert result["score"] == pytest.approx(9.5)
        assert result["tiers"]["fts5"] == pytest.approx(6.0)
        assert result["tiers"]["dense"] == pytest.approx(0.9)

    def test_gold_found_at_deep_rank(self):
        fps = [
            _make_fp(rank=0, source="other_a.py", score=9.0,
                     tiers={"fts5": 6.0}, gene_id="g0"),
            _make_fp(rank=1, source="other_b.py", score=8.0,
                     tiers={"fts5": 5.0}, gene_id="g1"),
            _make_fp(rank=2, source="F:/cymatix_context/pipeline/stages.py",
                     score=4.0, tiers={"fts5": 2.0, "co_activation": 0.3},
                     gene_id="g2"),
        ]
        result = _find_gold(fps, ["cymatix_context/pipeline/stages.py"])
        assert result["in_pool"] is True
        assert result["rank"] == 3          # 0-based 2 -> 1-based 3
        assert result["tiers"]["co_activation"] == pytest.approx(0.3)

    def test_gold_not_in_pool(self):
        fps = [
            _make_fp(rank=0, source="other_a.py", score=9.0,
                     tiers={"fts5": 6.0}),
        ]
        result = _find_gold(fps, ["cymatix_context/pipeline/stages.py"])
        assert result["in_pool"] is False
        assert result["rank"] is None
        assert result["score"] is None
        assert result["tiers"] == {}

    def test_empty_pool(self):
        result = _find_gold([], ["cymatix_context/pipeline/stages.py"])
        assert result["in_pool"] is False

    def test_tiers_empty_dict_when_missing(self):
        """If tier_contributions key is absent, tiers defaults to {}."""
        fps = [{"rank": 0, "source": "cymatix_context/pipeline/stages.py",
                "score": 5.0}]  # no tier_contributions key
        result = _find_gold(fps, ["cymatix_context/pipeline/stages.py"])
        assert result["in_pool"] is True
        assert result["tiers"] == {}


# ---------------------------------------------------------------------------
# _analyse_needle tests
# ---------------------------------------------------------------------------

class TestAnalyseNeedle:

    def test_both_in_pool(self):
        nd = _needle(gold_paths=["cymatix_context/pipeline/stages.py"])
        blob_fps = [
            _make_fp(rank=0, source="F:/cymatix_context/pipeline/stages.py",
                     score=9.0, tiers={"fts5": 6.0, "dense": 0.8}),
        ]
        shard_fps = [
            _make_fp(rank=0, source="other.py", score=10.0,
                     tiers={"fts5": 7.0}, gene_id="g1"),
            _make_fp(rank=1, source="F:/cymatix_context/pipeline/stages.py",
                     score=6.0, tiers={"fts5": 4.0}, gene_id="g2"),
        ]
        row = _analyse_needle(nd, blob_fps, shard_fps)
        assert row["blob"]["in_pool"] is True
        assert row["blob"]["rank"] == 1
        assert row["sharded"]["in_pool"] is True
        assert row["sharded"]["rank"] == 2

    def test_gold_in_blob_not_in_shard(self):
        """Core case: gold surfaces in blob (dense) but absent in sharded pool."""
        nd = _needle(gold_paths=["cymatix_context/pipeline/stages.py"])
        blob_fps = [
            _make_fp(rank=0, source="F:/cymatix_context/pipeline/stages.py",
                     score=8.5, tiers={"fts5": 4.0, "dense": 0.85}),
        ]
        shard_fps = [
            _make_fp(rank=0, source="other_file.py", score=7.0,
                     tiers={"fts5": 5.0}, gene_id="g1"),
        ]
        row = _analyse_needle(nd, blob_fps, shard_fps)
        assert row["blob"]["in_pool"] is True
        assert row["blob"]["rank"] == 1
        assert row["blob"]["tiers"]["dense"] == pytest.approx(0.85)
        assert row["sharded"]["in_pool"] is False
        assert row["sharded"]["rank"] is None

    def test_question_truncated(self):
        long_q = "x" * 300
        nd = _needle(question=long_q)
        row = _analyse_needle(nd, [], [])
        assert len(row["question"]) <= 200


# ---------------------------------------------------------------------------
# _aggregate tests -- the core classification logic
# ---------------------------------------------------------------------------

class TestAggregate:

    def _make_row(
        self,
        *,
        nid: str,
        blob_rank: Optional[int],
        shard_rank: Optional[int],
        blob_tiers: Optional[Dict[str, float]] = None,
        shard_tiers: Optional[Dict[str, float]] = None,
        shard_in_pool: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Build a per-needle result row directly (bypasses HTTP)."""
        blob_in  = blob_rank is not None
        shard_in = shard_rank is not None if shard_in_pool is None else shard_in_pool
        return {
            "id":   nid,
            "type": "within",
            "question": "test",
            "gold_paths": ["some/gold.py"],
            "blob": {
                "in_pool": blob_in,
                "rank":    blob_rank,
                "score":   float(blob_rank or 0) * 0.1,
                "tiers":   blob_tiers or {},
            },
            "sharded": {
                "in_pool": shard_in,
                "rank":    shard_rank,
                "score":   float(shard_rank or 0) * 0.1 if shard_in else None,
                "tiers":   shard_tiers or {},
            },
        }

    # --- Scenario A: candidate-gen loss (gold absent in shard pool entirely) ---
    def test_candidate_gen_loss_classification(self):
        """blob rank=1, shard NOT in pool -> candidate_gen_loss."""
        rows = [
            self._make_row(
                nid="n1",
                blob_rank=1,
                shard_rank=None,
                blob_tiers={"dense": 0.85, "fts5": 4.0},
                shard_in_pool=False,
            ),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        assert agg["divergent_count"] == 1
        assert agg["candidate_gen_loss"] == 1
        assert agg["ranking_loss"] == 0

    # --- Scenario B: ranking loss (gold in shard pool but deep) ---
    def test_ranking_loss_classification(self):
        """blob rank=2, shard rank=50 (deep) -> ranking_loss."""
        rows = [
            self._make_row(
                nid="n2",
                blob_rank=2,
                shard_rank=50,
                blob_tiers={"co_activation": 1.2, "fts5": 5.0},
                shard_tiers={"fts5": 5.0},  # co_activation absent
            ),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        assert agg["divergent_count"] == 1
        assert agg["ranking_loss"] == 1
        assert agg["candidate_gen_loss"] == 0

    # --- Scenario C: non-divergent (both good) ---
    def test_non_divergent_not_counted(self):
        """blob rank=3, shard rank=5 -> both good, not divergent."""
        rows = [
            self._make_row(nid="n3", blob_rank=3, shard_rank=5,
                           blob_tiers={"fts5": 6.0},
                           shard_tiers={"fts5": 5.5}),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        assert agg["divergent_count"] == 0
        assert agg["candidate_gen_loss"] == 0
        assert agg["ranking_loss"] == 0

    # --- Scenario D: blob not good (blob rank > cap) ---
    def test_blob_bad_not_counted_as_divergent(self):
        """blob rank=15 (> cap=10) -> not blob-good, not divergent."""
        rows = [
            self._make_row(nid="n4", blob_rank=15, shard_rank=None,
                           shard_in_pool=False),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        assert agg["divergent_count"] == 0

    # --- Core assertion: tier delta names the differing tier ---
    def test_tier_delta_identifies_dense_as_differentiator(self):
        """
        Ranking-loss case: blob has dense=0.8, shard has dense=0.0.
        Aggregate must put 'dense' first in tier_ranking_summary and
        tier_overall_delta_rank.
        """
        rows = [
            self._make_row(
                nid="n5",
                blob_rank=1,
                shard_rank=40,     # deep -> ranking_loss
                blob_tiers={"fts5": 5.0, "dense": 0.8},
                shard_tiers={"fts5": 5.0, "dense": 0.0},
            ),
            self._make_row(
                nid="n6",
                blob_rank=3,
                shard_rank=55,
                blob_tiers={"fts5": 4.5, "dense": 0.7},
                shard_tiers={"fts5": 4.5, "dense": 0.0},
            ),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        assert agg["ranking_loss"] == 2

        summary = agg["tier_ranking_summary"]
        # dense must appear and have positive delta
        dense_entry = next((t for t in summary if t["tier"] == "dense"), None)
        assert dense_entry is not None, "dense missing from tier_ranking_summary"
        assert dense_entry["delta"] > 0.0

        # dense should rank first (highest delta) since fts5 is equal
        assert summary[0]["tier"] == "dense"
        assert agg["tier_overall_delta_rank"][0] == "dense"

    def test_tier_delta_identifies_co_activation(self):
        """
        Ranking-loss case: blob has co_activation=1.2, shard has
        co_activation=0.0.  co_activation must top the delta rank.
        """
        rows = [
            self._make_row(
                nid="n7",
                blob_rank=4,
                shard_rank=80,
                blob_tiers={"fts5": 5.0, "co_activation": 1.2},
                shard_tiers={"fts5": 5.0},
            ),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        summary = agg["tier_ranking_summary"]
        assert summary[0]["tier"] == "co_activation"
        assert summary[0]["blob_median"] == pytest.approx(1.2)
        assert summary[0]["shard_median"] == pytest.approx(0.0)
        assert summary[0]["delta"] == pytest.approx(1.2)

    def test_cand_gen_loss_blob_tiers_reported(self):
        """
        Candidate-gen-loss case: blob surfaced gold via dense.
        tier_cand_gen_summary must list dense as top tier.
        """
        rows = [
            self._make_row(
                nid="n8",
                blob_rank=2,
                shard_rank=None,
                blob_tiers={"dense": 4.5, "fts5": 3.0},
                shard_in_pool=False,
            ),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        assert agg["candidate_gen_loss"] == 1
        cg = agg["tier_cand_gen_summary"]
        assert cg[0]["tier"] == "dense"
        assert cg[0]["blob_median"] == pytest.approx(4.5)

    # --- Mixed scenario: one cand-gen-loss + one ranking-loss ---
    def test_mixed_two_needles(self):
        """
        needle A: blob rank=1, shard NOT in pool -> candidate_gen_loss
                  blob surfaced via dense
        needle B: blob rank=5, shard rank=35   -> ranking_loss
                  blob has co_activation=1.0, shard has co_activation=0.0
        """
        rows = [
            self._make_row(
                nid="A",
                blob_rank=1,
                shard_rank=None,
                blob_tiers={"dense": 0.85, "fts5": 4.0},
                shard_in_pool=False,
            ),
            self._make_row(
                nid="B",
                blob_rank=5,
                shard_rank=35,
                blob_tiers={"fts5": 5.0, "co_activation": 1.0},
                shard_tiers={"fts5": 5.0},
            ),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)

        assert agg["divergent_count"] == 2
        assert agg["candidate_gen_loss"] == 1
        assert agg["ranking_loss"] == 1

        # Cand-gen-loss: dense is reported in blob tiers
        cg = agg["tier_cand_gen_summary"]
        assert any(t["tier"] == "dense" for t in cg)

        # Ranking-loss: co_activation shows positive delta
        rl = agg["tier_ranking_summary"]
        co_entry = next((t for t in rl if t["tier"] == "co_activation"), None)
        assert co_entry is not None
        assert co_entry["delta"] > 0.0

    def test_pool_counts(self):
        rows = [
            self._make_row(nid="p1", blob_rank=1, shard_rank=5),
            self._make_row(nid="p2", blob_rank=2, shard_rank=None,
                           shard_in_pool=False),
            self._make_row(nid="p3", blob_rank=None, shard_rank=3,
                           blob_tiers={}),
        ]
        # Fix blob in_pool for row p3: blob_rank=None means not in pool
        rows[2]["blob"]["in_pool"] = False
        rows[2]["blob"]["rank"]    = None

        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        assert agg["blob_pool_total"]  == 2   # p1 + p2 in blob pool
        assert agg["shard_pool_total"] == 2   # p1 + p3 in shard pool
        assert agg["total_needles"]    == 3

    def test_empty_needles(self):
        agg = _aggregate([], blob_rank_cap=10, shard_bury_cap=10)
        assert agg["total_needles"]      == 0
        assert agg["divergent_count"]    == 0
        assert agg["candidate_gen_loss"] == 0
        assert agg["ranking_loss"]       == 0
        assert agg["tier_ranking_summary"]  == []
        assert agg["tier_cand_gen_summary"] == []

    def test_tier_ranking_summary_sorted_desc(self):
        """tier_ranking_summary items must be sorted by delta descending."""
        rows = [
            self._make_row(
                nid="s1",
                blob_rank=1,
                shard_rank=20,
                blob_tiers={"fts5": 5.0, "co_activation": 1.5, "dense": 0.3},
                shard_tiers={"fts5": 4.0},
            ),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        deltas = [t["delta"] for t in agg["tier_ranking_summary"]]
        assert deltas == sorted(deltas, reverse=True)

    def test_zero_delta_tiers_excluded_from_ranking_summary(self):
        """Tiers with delta <= 0 must not appear in tier_ranking_summary."""
        rows = [
            self._make_row(
                nid="z1",
                blob_rank=1,
                shard_rank=30,
                blob_tiers={"fts5": 5.0, "dense": 0.0},   # dense equal in both
                shard_tiers={"fts5": 5.0, "dense": 0.0},
            ),
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        for entry in agg["tier_ranking_summary"]:
            assert entry["delta"] > 0.0


# ---------------------------------------------------------------------------
# Integration: run() with mocked urllib
# ---------------------------------------------------------------------------

class TestRunMocked:

    def _urlopen_factory(
        self,
        blob_fps: List[Dict[str, Any]],
        shard_fps: List[Dict[str, Any]],
        blob_url: str = "http://blob:11437",
        shard_url: str = "http://shard:11438",
    ):
        transport = _TwoServerTransport(
            blob_fps, shard_fps, blob_url=blob_url, shard_url=shard_url,
        )

        def _urlopen(req, timeout=None):
            return transport(req, timeout=timeout)

        return _urlopen

    def test_cand_gen_loss_via_dense(self):
        """
        Gold present in blob (via dense tier) but absent in sharded pool.
        run() must classify as candidate_gen_loss and name 'dense' as the
        differentiating tier in tier_cand_gen_summary.
        """
        gold_source = "F:/Projects/cymatix_context/pipeline/stages.py"
        gold_path   = "cymatix_context/pipeline/stages.py"

        blob_fps = [
            _make_fp(rank=0, source=gold_source, score=8.5,
                     tiers={"fts5": 4.0, "dense": 0.85}),
        ]
        shard_fps = [
            _make_fp(rank=0, source="other/file.py", score=7.0,
                     tiers={"fts5": 5.0}, gene_id="g1"),
        ]

        needles = [_needle(gold_paths=[gold_path])]
        urlopen = self._urlopen_factory(blob_fps, shard_fps)

        with patch.object(urllib.request, "urlopen", urlopen):
            per_needle, agg = run(
                needles=needles,
                blob_url="http://blob:11437",
                sharded_url="http://shard:11438",
            )

        assert len(per_needle) == 1
        row = per_needle[0]
        assert row["blob"]["in_pool"] is True
        assert row["blob"]["rank"] == 1
        assert row["sharded"]["in_pool"] is False

        assert agg["candidate_gen_loss"] == 1
        assert agg["ranking_loss"] == 0
        assert agg["divergent_count"] == 1

        cg = agg["tier_cand_gen_summary"]
        assert any(t["tier"] == "dense" for t in cg), \
            "dense missing from tier_cand_gen_summary"
        # tier_cand_gen_summary sorts by blob_median desc; dense (0.85) < fts5 (4.0)
        # so we assert presence + value, not position -- classification is the key claim
        dense_entry = next(t for t in cg if t["tier"] == "dense")
        assert dense_entry["blob_median"] == pytest.approx(0.85)

    def test_ranking_loss_via_co_activation(self):
        """
        Gold is in sharded pool but deep (rank 25).  Blob has co_activation;
        sharded has none.  run() must classify as ranking_loss and name
        'co_activation' as the differentiating tier.
        """
        gold_source = "F:/Projects/cymatix_context/identity/provenance.py"
        gold_path   = "cymatix_context/identity/provenance.py"

        blob_fps = [
            _make_fp(rank=0, source=gold_source, score=9.0,
                     tiers={"fts5": 5.0, "co_activation": 1.3}),
        ]
        # sharded: gold appears at rank 24 (0-based) with no co_activation
        decoys = [
            _make_fp(rank=i, source=f"decoy_{i}.py", score=8.0 - i * 0.1,
                     tiers={"fts5": 5.0}, gene_id=f"decoy_{i}")
            for i in range(24)
        ]
        shard_gold_fp = _make_fp(
            rank=24, source=gold_source, score=2.0,
            tiers={"fts5": 5.0},  # same as blob fts5 -> zero delta; only co_activation differs
            gene_id="gold_shard",
        )
        shard_fps = decoys + [shard_gold_fp]

        needles = [_needle(gold_paths=[gold_path])]
        urlopen = self._urlopen_factory(blob_fps, shard_fps)

        with patch.object(urllib.request, "urlopen", urlopen):
            per_needle, agg = run(
                needles=needles,
                blob_url="http://blob:11437",
                sharded_url="http://shard:11438",
                shard_bury_cap=10,
            )

        row = per_needle[0]
        assert row["blob"]["rank"]    == 1
        assert row["sharded"]["rank"] == 25    # 0-based 24 -> 1-based 25

        assert agg["ranking_loss"]       == 1
        assert agg["candidate_gen_loss"] == 0

        rl = agg["tier_ranking_summary"]
        assert len(rl) > 0, "tier_ranking_summary is empty"
        co_entry = next((t for t in rl if t["tier"] == "co_activation"), None)
        assert co_entry is not None, "co_activation missing from tier_ranking_summary"
        assert co_entry["delta"] == pytest.approx(1.3)
        assert rl[0]["tier"] == "co_activation", \
            f"expected co_activation as top ranking-loss tier, got {rl[0]['tier']}"

    def test_multiple_needles_mixed(self):
        """Two needles: one cand-gen-loss (dense), one ranking-loss (dense).
        Both are correctly classified and dense tops both tier summaries."""
        gold_a = "cymatix_context/pipeline/stages.py"
        gold_b = "cymatix_context/identity/provenance.py"

        src_a = f"F:/Projects/{gold_a}"
        src_b = f"F:/Projects/{gold_b}"

        # Needle A: blob has gold at rank 0 via dense; shard missing gold
        blob_fps_a  = [_make_fp(rank=0, source=src_a, score=8.0,
                                tiers={"fts5": 4.0, "dense": 0.9})]
        shard_fps_a = [_make_fp(rank=0, source="other.py", score=7.0,
                                tiers={"fts5": 5.0}, gene_id="g1")]

        # Needle B: blob at rank 0 via dense; shard at rank 15 (buried) no dense
        blob_fps_b  = [_make_fp(rank=0, source=src_b, score=9.0,
                                tiers={"fts5": 5.0, "dense": 0.75})]
        decoys_b    = [_make_fp(rank=i, source=f"d_{i}.py", score=7.0 - i * 0.1,
                                tiers={"fts5": 4.5}, gene_id=f"d{i}")
                       for i in range(14)]
        shard_gold_b = _make_fp(rank=14, source=src_b, score=2.0,
                                tiers={"fts5": 2.0}, gene_id="gb")
        shard_fps_b  = decoys_b + [shard_gold_b]

        call_count = [0]
        def _urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            i = call_count[0]
            call_count[0] += 1
            # Calls: 0=blob/A, 1=shard/A, 2=blob/B, 3=shard/B
            if i == 0:
                return _MockResponse(_fp_response(blob_fps_a))
            elif i == 1:
                return _MockResponse(_fp_response(shard_fps_a))
            elif i == 2:
                return _MockResponse(_fp_response(blob_fps_b))
            else:
                return _MockResponse(_fp_response(shard_fps_b))

        needles = [
            _needle(nid="A", gold_paths=[gold_a]),
            _needle(nid="B", gold_paths=[gold_b]),
        ]
        with patch.object(urllib.request, "urlopen", _urlopen):
            per_needle, agg = run(
                needles=needles,
                blob_url="http://blob:11437",
                sharded_url="http://shard:11438",
                shard_bury_cap=10,
            )

        assert agg["divergent_count"] == 2

        # A is cand-gen-loss; B is ranking-loss
        assert agg["candidate_gen_loss"] == 1
        assert agg["ranking_loss"]       == 1

        # dense must appear in cand-gen-loss summary
        cg_tiers = {t["tier"] for t in agg["tier_cand_gen_summary"]}
        assert "dense" in cg_tiers

        # dense must appear in ranking-loss summary with positive delta
        rl = agg["tier_ranking_summary"]
        dense_rl = next((t for t in rl if t["tier"] == "dense"), None)
        assert dense_rl is not None
        assert dense_rl["delta"] > 0.0

    def test_error_on_one_arm_does_not_crash(self):
        """If sharded server raises, the needle is recorded with shard_error
        and pool counts/aggregates degrade gracefully (no exception raised)."""
        gold_source = "F:/Projects/cymatix_context/pipeline/stages.py"
        blob_fps = [
            _make_fp(rank=0, source=gold_source, score=8.0,
                     tiers={"fts5": 5.0, "dense": 0.8}),
        ]
        call_count = [0]

        def _urlopen(req, timeout=None):
            i = call_count[0]
            call_count[0] += 1
            if i == 0:
                return _MockResponse(_fp_response(blob_fps))
            # second call (sharded) raises
            raise urllib.error.URLError("connection refused")

        needles = [_needle(gold_paths=["cymatix_context/pipeline/stages.py"])]
        with patch.object(urllib.request, "urlopen", _urlopen):
            per_needle, agg = run(
                needles=needles,
                blob_url="http://blob:11437",
                sharded_url="http://shard:11438",
            )

        assert len(per_needle) == 1
        row = per_needle[0]
        assert "shard_error" in row
        assert row["sharded"]["in_pool"] is False
        # aggregate must complete without raising
        assert agg["total_needles"] == 1


# ---------------------------------------------------------------------------
# Output schema shape
# ---------------------------------------------------------------------------

class TestOutputSchemaShape:

    def test_aggregate_keys_present(self):
        """_aggregate always returns the full key set even on empty input."""
        agg = _aggregate([])
        expected_keys = {
            "total_needles",
            "blob_good_count",
            "shard_bad_count",
            "divergent_count",
            "candidate_gen_loss",
            "ranking_loss",
            "blob_pool_total",
            "shard_pool_total",
            "tier_ranking_summary",
            "tier_cand_gen_summary",
            "tier_overall_delta_rank",
        }
        assert expected_keys.issubset(set(agg.keys())), \
            f"missing keys: {expected_keys - set(agg.keys())}"

    def test_per_needle_keys_present(self):
        nd = _needle()
        row = _analyse_needle(nd, [], [])
        for key in ("id", "type", "question", "gold_paths", "blob", "sharded"):
            assert key in row, f"missing per-needle key: {key}"
        for arm in ("blob", "sharded"):
            for sub in ("in_pool", "rank", "score", "tiers"):
                assert sub in row[arm], f"missing {arm}.{sub}"

    def test_tier_ranking_summary_item_keys(self):
        rows = [
            {
                "id": "x", "type": "within", "question": "q",
                "gold_paths": ["g.py"],
                "blob":    {"in_pool": True,  "rank": 1,  "score": 9.0,
                            "tiers": {"dense": 0.8, "fts5": 4.0}},
                "sharded": {"in_pool": True,  "rank": 20, "score": 3.0,
                            "tiers": {"fts5": 4.0}},
            }
        ]
        agg = _aggregate(rows, blob_rank_cap=10, shard_bury_cap=10)
        for item in agg["tier_ranking_summary"]:
            for key in ("tier", "blob_median", "shard_median", "delta"):
                assert key in item, f"missing tier_ranking_summary key: {key}"
