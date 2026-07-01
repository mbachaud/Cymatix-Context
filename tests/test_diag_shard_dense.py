"""
tests/test_diag_shard_dense.py

Unit tests for benchmarks/diag_shard_dense.py.

Run with:
    python -m pytest tests/test_diag_shard_dense.py -q --noconftest

All tests use mocked /fingerprint + /context/packet JSON responses
(matching the real wire schema from routes_context.py).  No server,
no network, no GPU.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

# Make the benchmarks/ directory importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "benchmarks"))

import diag_shard_dense as _mod
from diag_shard_dense import (
    DEFAULT_QUERIES,
    _BGE_DENSE_TIER,
    _DENSE_TIERS,
    _extract_from_packet,
    _extract_tiers_from_fingerprint,
    run_diagnostic,
)

# ---------------------------------------------------------------------------
# Fixture helpers: minimal wire-schema responses
# ---------------------------------------------------------------------------

def _fingerprint_response(
    *,
    gene_ids: list[str] | None = None,
    tier_maps: list[dict] | None = None,
    tier_totals: dict | None = None,
) -> dict:
    """Build a minimal /fingerprint response matching routes_context.py L855-882."""
    gene_ids = gene_ids or ["gene_a", "gene_b"]
    tier_maps = tier_maps or [{} for _ in gene_ids]
    fingerprints = []
    for i, (gid, tmap) in enumerate(zip(gene_ids, tier_maps)):
        tc = {k: round(float(v), 4) for k, v in tmap.items()}
        fingerprints.append({
            "rank": i,
            "gene_id": gid,
            "score": sum(tc.values()),
            "preview": f"preview for {gid}",
            "path": f"/some/path/{gid}.md",
            "source": f"source_{gid}",
            "domains": [],
            "entities": [],
            "chromatin": 0,
            "tier_contributions": tc,
        })
    computed_totals: dict = {}
    for fp in fingerprints:
        for t, v in fp["tier_contributions"].items():
            computed_totals[t] = computed_totals.get(t, 0.0) + v
    return {
        "mode": "fingerprint",
        "profile": "fast",
        "query": "test query",
        "fingerprints": fingerprints,
        "count": len(fingerprints),
        "max_results": 10,
        "score_floor": 0.0,
        "evaluated_total": len(fingerprints),
        "above_floor_total": len(fingerprints),
        "returned": len(fingerprints),
        "filtered_by_floor": 0,
        "truncated_by_cap": 0,
        "response_hint": "No filtering or truncation applied.",
        "agent": {
            "recommendation": "triage",
            "hint": "Use tier fingerprints.",
            "latency_ms": 12.3,
            "cold_tier_used": False,
            "cold_tier_count": 0,
            "tier_totals": {
                k: round(v, 4)
                for k, v in (tier_totals or computed_totals).items()
            },
        },
    }


def _packet_response(
    *,
    found: bool = True,
    confidence: float = 0.82,
    lexical_dense_agree: bool = False,
    top_score: float = 6.0,
    score_gap: float = 2.0,
    source_ids: list[str] | None = None,
) -> dict:
    """Build a minimal /context/packet response matching routes_context.py L583-607."""
    source_ids = source_ids or ["F:/Projects/helix-context/README.md"]
    verified = [
        {
            "kind": "gene",
            "gene_id": f"gene_{i}",
            "title": f"doc {i}",
            "content": "content snippet",
            "relevance_score": 0.8,
            "source_id": src,
            "status": "verified",
            "citations": [],
        }
        for i, src in enumerate(source_ids)
    ]
    payload: dict = {
        "task_type": "explain",
        "query": "test query",
        "verified": verified,
        "stale_risk": [],
        "contradictions": [],
        "refresh_targets": [],
        "notes": [],
        "response_mode": "packet",
        "coordinate_confidence": confidence,
        "file_coverage": 0.75,
    }
    if found:
        payload["know"] = {
            "found": True,
            "confidence": confidence,
            "top_score": top_score,
            "score_gap": score_gap,
            "lexical_dense_agree": lexical_dense_agree,
            "gene_id_match": "gene_0",
            "coordinate_confidence": confidence,
            "soft_stale": False,
        }
    else:
        payload["miss"] = {
            "miss": True,
            "reason": "sparse",
            "top_score": top_score,
            "ratio": 1.1,
            "escalate_to": ["grep"],
            "refresh_targets": [],
            "do_not_answer_from_genome": True,
        }
    return payload


# ---------------------------------------------------------------------------
# Transport-level mock: avoids monkeypatching httpx.Client itself
# ---------------------------------------------------------------------------

class _MockTransport(httpx.BaseTransport):
    """Minimal transport that serves pre-built JSON for /fingerprint and /context/packet."""

    def __init__(self, fp_resp: dict, pkt_resp: dict):
        self._fp = fp_resp
        self._pkt = pkt_resp

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        data = self._fp if "/fingerprint" in url else self._pkt
        body = json.dumps(data).encode()
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})


class _AltTransport(httpx.BaseTransport):
    """Transport that alternates fp responses on each /fingerprint call."""

    def __init__(self, fp_responses: list[dict], pkt_resp: dict):
        self._fps = fp_responses
        self._pkt = pkt_resp
        self._count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/fingerprint" in url:
            data = self._fps[self._count % len(self._fps)]
            self._count += 1
        else:
            data = self._pkt
        body = json.dumps(data).encode()
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})


class _ErrorTransport(httpx.BaseTransport):
    """Returns HTTP 500 for /fingerprint, 200 for packet."""

    def __init__(self, pkt_resp: dict):
        self._pkt = pkt_resp

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if "/fingerprint" in str(request.url):
            return httpx.Response(500, content=b'{"error":"boom"}',
                                  headers={"content-type": "application/json"})
        body = json.dumps(self._pkt).encode()
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})


def _patched_run(transport: httpx.BaseTransport, helix_url: str,
                 queries: list[dict], **kw) -> dict:
    """Run run_diagnostic with the given transport injected via patch."""
    _real_client = httpx.Client

    def _client_with_transport(*args, **kwargs):
        # Strip any transport kwarg callers may pass, inject ours
        kwargs.pop("transport", None)
        return _real_client(*args, transport=transport, **kwargs)

    with patch.object(_mod.httpx, "Client", _client_with_transport):
        return run_diagnostic(helix_url, queries, **kw)


# ===========================================================================
# _extract_tiers_from_fingerprint tests
# ===========================================================================

class TestExtractTiersFromFingerprint:

    def test_dense_tier_present_in_one_result(self):
        """dense_fired=True when 'dense' key has non-zero score in any result."""
        resp = _fingerprint_response(
            gene_ids=["g1", "g2"],
            tier_maps=[
                {"fts5": 6.0, "dense": 0.87},
                {"tag_exact": 3.0},
            ],
        )
        tiers, dense_fired = _extract_tiers_from_fingerprint(resp)
        assert dense_fired is True
        assert "dense" in tiers
        assert "fts5" in tiers
        assert "tag_exact" in tiers

    def test_dense_tier_absent_returns_false(self):
        """dense_fired=False when no result has 'dense' contribution."""
        resp = _fingerprint_response(
            gene_ids=["g1", "g2"],
            tier_maps=[
                {"fts5": 6.0, "tag_exact": 3.0},
                {"tag_prefix": 2.5, "fts5": 4.0},
            ],
        )
        tiers, dense_fired = _extract_tiers_from_fingerprint(resp)
        assert dense_fired is False
        assert "dense" not in tiers

    def test_dense_zero_score_not_counted(self):
        """A 'dense' key with score=0.0 must NOT set dense_fired=True."""
        resp = _fingerprint_response(
            gene_ids=["g1"],
            tier_maps=[{"fts5": 5.0, "dense": 0.0}],
        )
        tiers, dense_fired = _extract_tiers_from_fingerprint(resp)
        assert dense_fired is False
        # dense should not be in tiers (zero score excluded)
        assert "dense" not in tiers

    def test_dense_via_tier_totals_only(self):
        """dense_fired=True even if per-fingerprint tier_contributions lacks 'dense'
        but agent.tier_totals includes it (refiner-path dense contrib)."""
        resp = _fingerprint_response(
            gene_ids=["g1"],
            tier_maps=[{"fts5": 6.0}],
            tier_totals={"fts5": 6.0, "dense": 0.42},
        )
        tiers, dense_fired = _extract_tiers_from_fingerprint(resp)
        assert dense_fired is True
        assert "dense" in tiers

    def test_empty_fingerprints(self):
        """Empty fingerprints list: no tiers fired, no dense."""
        resp = _fingerprint_response(gene_ids=[], tier_maps=[])
        resp["fingerprints"] = []
        resp["count"] = 0
        resp["agent"]["tier_totals"] = {}
        tiers, dense_fired = _extract_tiers_from_fingerprint(resp)
        assert tiers == []
        assert dense_fired is False

    def test_splade_and_sema_boost_do_not_set_dense_fired(self):
        """splade and sema_boost appear in tiers list but do NOT set dense_fired.

        The BGE-M3 'dense' key is the unambiguous gate; splade/sema_boost are
        supplementary semantic signals that can exist without BGE-M3.
        """
        resp = _fingerprint_response(
            gene_ids=["g1"],
            tier_maps=[{"fts5": 4.0, "splade": 1.2, "sema_boost": 0.6}],
        )
        tiers, dense_fired = _extract_tiers_from_fingerprint(resp)
        assert "splade" in tiers
        assert "sema_boost" in tiers
        # splade/sema_boost alone must NOT claim BGE-M3 dense fired
        assert dense_fired is False

    def test_all_tier_names_collected(self):
        """All distinct non-zero tier names from all results are collected."""
        resp = _fingerprint_response(
            gene_ids=["g1", "g2", "g3"],
            tier_maps=[
                {"fts5": 6.0, "tag_exact": 3.0, "dense": 0.5},
                {"fts5": 4.0, "harmonic": 2.0},
                {"tag_prefix": 1.5, "cymatics": 0.3},
            ],
        )
        tiers, dense_fired = _extract_tiers_from_fingerprint(resp)
        assert set(tiers) == {"fts5", "tag_exact", "dense", "harmonic", "tag_prefix", "cymatics"}
        assert dense_fired is True

    def test_tiers_sorted_alphabetically(self):
        """Returned tiers list is sorted."""
        resp = _fingerprint_response(
            gene_ids=["g1"],
            tier_maps=[{"fts5": 6.0, "tag_exact": 3.0, "dense": 0.9, "harmonic": 2.0}],
        )
        tiers, _ = _extract_tiers_from_fingerprint(resp)
        assert tiers == sorted(tiers)


# ===========================================================================
# _extract_from_packet tests
# ===========================================================================

class TestExtractFromPacket:

    def test_know_block_extraction(self):
        """Know block fields are correctly extracted."""
        resp = _packet_response(
            found=True,
            confidence=0.91,
            lexical_dense_agree=True,
        )
        info = _extract_from_packet(resp)
        assert info["know_found"] is True
        assert info["confidence"] == pytest.approx(0.91)
        assert info["lexical_dense_agree"] is True

    def test_miss_block_no_know(self):
        """Miss block: know_found=False, lexical_dense_agree=None."""
        resp = _packet_response(found=False)
        info = _extract_from_packet(resp)
        assert info["know_found"] is False
        assert info["lexical_dense_agree"] is None

    def test_top_sources_extracted(self):
        """top_sources list comes from verified items."""
        resp = _packet_response(
            found=True,
            source_ids=[
                "F:/Projects/helix-context/README.md",
                "F:/Projects/BookKeeper/docs/api.md",
                "F:/Projects/Education/curriculum.md",
            ],
        )
        info = _extract_from_packet(resp)
        assert len(info["top_sources"]) == 3
        assert "F:/Projects/helix-context/README.md" in info["top_sources"]

    def test_empty_packet(self):
        """Empty / minimal packet does not raise."""
        info = _extract_from_packet({})
        assert info["know_found"] is False
        assert info["top_sources"] == []
        assert info["lexical_dense_agree"] is None

    def test_lexical_dense_agree_false(self):
        """lexical_dense_agree=False is faithfully propagated."""
        resp = _packet_response(found=True, lexical_dense_agree=False)
        info = _extract_from_packet(resp)
        assert info["lexical_dense_agree"] is False


# ===========================================================================
# run_diagnostic integration tests (mock via transport injection)
# ===========================================================================

class TestRunDiagnosticMocked:

    def test_dense_present_in_unsharded_response(self):
        """When /fingerprint returns dense tier, dense_fired=True for all queries."""
        fp = _fingerprint_response(
            gene_ids=["g1", "g2"],
            tier_maps=[
                {"fts5": 6.0, "tag_exact": 3.0, "dense": 0.88},
                {"fts5": 4.0, "dense": 0.55},
            ],
        )
        pkt = _packet_response(found=True, lexical_dense_agree=True)

        queries = DEFAULT_QUERIES[:3]
        result = _patched_run(
            _MockTransport(fp, pkt),
            "http://localhost:11437",
            queries,
            label="test_unsharded",
        )

        assert result["label"] == "test_unsharded"
        assert result["aggregate"]["n"] == 3
        assert result["aggregate"]["n_dense_fired"] == 3
        assert result["aggregate"]["pct_dense_fired"] == 100.0
        for row in result["queries"]:
            assert row["dense_fired"] is True
            assert "dense" in row["tiers_fired"]
            assert "fts5" in row["tiers_fired"]

    def test_dense_absent_in_sharded_response(self):
        """When /fingerprint has no dense tier, dense_fired=False for all queries."""
        fp = _fingerprint_response(
            gene_ids=["g1", "g2"],
            tier_maps=[
                {"fts5": 6.0, "tag_exact": 3.0},
                {"fts5": 4.0, "tag_prefix": 2.0},
            ],
        )
        pkt = _packet_response(found=True, lexical_dense_agree=False)

        queries = DEFAULT_QUERIES[:3]
        result = _patched_run(
            _MockTransport(fp, pkt),
            "http://localhost:11438",
            queries,
            label="test_sharded",
        )

        assert result["aggregate"]["n_dense_fired"] == 0
        assert result["aggregate"]["pct_dense_fired"] == 0.0
        for row in result["queries"]:
            assert row["dense_fired"] is False
            assert "dense" not in row["tiers_fired"]

    def test_by_type_breakdown(self):
        """by_type aggregate correctly separates within vs cross queries."""
        fp_with_dense = _fingerprint_response(
            gene_ids=["g1"],
            tier_maps=[{"fts5": 6.0, "dense": 0.77}],
        )
        fp_no_dense = _fingerprint_response(
            gene_ids=["g1"],
            tier_maps=[{"fts5": 6.0}],
        )
        pkt = _packet_response(found=True)

        # 2 within (dense), 2 cross (no dense) — transport alternates
        queries = [
            {"id": "w01", "type": "within", "query": "within q 1"},
            {"id": "w02", "type": "within", "query": "within q 2"},
            {"id": "c01", "type": "cross",  "query": "cross q 1"},
            {"id": "c02", "type": "cross",  "query": "cross q 2"},
        ]
        transport = _AltTransport(
            [fp_with_dense, fp_with_dense, fp_no_dense, fp_no_dense],
            pkt,
        )
        result = _patched_run(transport, "http://localhost:11437", queries, label="test_by_type")

        agg = result["aggregate"]
        assert agg["by_type"]["within"]["dense_fired"] == 2
        assert agg["by_type"]["cross"]["dense_fired"] == 0
        assert agg["by_type"]["within"]["pct_dense"] == 100.0
        assert agg["by_type"]["cross"]["pct_dense"] == 0.0

    def test_output_schema_shape(self):
        """run_diagnostic output has all required top-level and per-row keys."""
        fp = _fingerprint_response(gene_ids=["g1"], tier_maps=[{"fts5": 5.0}])
        pkt = _packet_response(found=True)

        result = _patched_run(
            _MockTransport(fp, pkt),
            "http://localhost:11437",
            DEFAULT_QUERIES[:1],
            label="schema",
        )

        # Top-level keys
        for key in ("label", "helix_url", "timestamp", "queries", "aggregate"):
            assert key in result, f"missing top-level key: {key}"

        # Aggregate keys
        agg = result["aggregate"]
        for key in ("n", "n_dense_fired", "pct_dense_fired", "by_type", "tier_frequency", "errors"):
            assert key in agg, f"missing aggregate key: {key}"

        # Per-row keys
        row = result["queries"][0]
        for key in (
            "id", "type", "query", "tiers_fired", "dense_fired",
            "lexical_dense_agree", "know_found", "confidence",
            "top_sources", "fp_count",
        ):
            assert key in row, f"missing row key: {key}"

    def test_fp_error_does_not_crash(self):
        """HTTP errors on /fingerprint are recorded as fp_error, run continues."""
        pkt = _packet_response(found=True)

        result = _patched_run(
            _ErrorTransport(pkt),
            "http://localhost:11437",
            DEFAULT_QUERIES[:2],
            label="error_test",
        )

        for row in result["queries"]:
            assert row["fp_error"] is not None
            assert row["dense_fired"] is False
        assert len(result["aggregate"]["errors"]) >= 2


# ===========================================================================
# Default query set sanity checks
# ===========================================================================

class TestDefaultQuerySet:

    def test_count(self):
        assert len(DEFAULT_QUERIES) == 16

    def test_unique_ids(self):
        ids = [q["id"] for q in DEFAULT_QUERIES]
        assert len(ids) == len(set(ids)), "duplicate IDs in DEFAULT_QUERIES"

    def test_types_valid(self):
        for q in DEFAULT_QUERIES:
            assert q["type"] in ("within", "cross"), f"bad type: {q}"

    def test_within_cross_ratio(self):
        within = sum(1 for q in DEFAULT_QUERIES if q["type"] == "within")
        cross  = sum(1 for q in DEFAULT_QUERIES if q["type"] == "cross")
        assert within == 10, f"expected 10 within, got {within}"
        assert cross  == 6,  f"expected 6 cross, got {cross}"

    def test_queries_are_non_trivial(self):
        """Each query should be at least 40 chars (no stub entries)."""
        for q in DEFAULT_QUERIES:
            assert len(q["query"]) >= 40, f"query too short: {q['id']}"

    def test_dense_tiers_constant(self):
        """_DENSE_TIERS must include 'dense' — the BGE-M3 canonical key."""
        assert "dense" in _DENSE_TIERS
        assert _BGE_DENSE_TIER == "dense"
