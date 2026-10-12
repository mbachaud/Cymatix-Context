"""Large-pool post-tier cost (perf/fusion-pool).

At 947k the candidate pool reaching Tier 5 is ~164k median. The work after the
timed retrieval tiers (authority boosts, access-rate tiebreaker, final sort)
scaled per candidate with pydantic JSON parses and full sorts. These tests pin:

* the cost driver -- no per-candidate pydantic parse for candidates that can't
  receive a boost, and no full sort when only the top-K is emitted;
* rank identity -- scores and ranked ids are exactly what the straightforward
  full-parse / full-sort reference produces, ties included;
* attribution -- the previously untimed regions publish their own
  ``last_signal_timings`` entries.
"""
from __future__ import annotations

import random
import time

import pytest

import cymatix_context.knowledge_store as ks_mod
from cymatix_context.genome import Genome
from cymatix_context.retrieval import rerank_combinators as rc
from cymatix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)

POOL = 400


def _gene(i: int) -> Gene:
    now = time.time()
    epi = EpigeneticMarkers()
    if i % 50 == 1:
        epi.created_at = now - 3600          # recent -> +0.5 authority
    elif i % 50 == 2:
        epi.created_at = now - 90 * 86400    # old
    if i % 40 == 3:
        epi.recent_accesses = [now - 10.0, now - 20.0]  # access-rate bonus
    return Gene(
        gene_id=f"g-{i:05d}",
        content=f"alpha beta doc {i}",
        complement="", codons=[],
        source_id=f"/data/{'topic' if i % 9 == 0 else 'x'}/f{i}.md",
        promoter=PromoterTags(
            domains=(["shared", "topic"] if i % 11 == 0 else ["shared", "other"]),
            entities=["ent"], summary="s",
        ),
        epigenetics=epi,
        chromatin=ChromatinState.OPEN, is_fragment=False,
    )


@pytest.fixture
def store():
    g = Genome(path=":memory:")
    for i in range(POOL):
        g.upsert_doc(_gene(i), apply_gate=False)
    yield g
    g.close()


def _run(store):
    docs = store.query_docs(["shared", "topic"], ["ent"], max_genes=12)
    return [d.gene_id for d in docs]


def _oracle_boosts(store, terms):
    """Full-parse reference for the authority and access-rate bonuses."""
    lower = [t.lower() for t in terms]
    now = time.time()
    auth, acc = {}, {}
    rows = store.read_conn.execute(
        "SELECT gene_id, source_id, promoter, epigenetics FROM genes").fetchall()
    for r in rows:
        boost = 0.0
        src = (r["source_id"] or "").lower()
        if any(t in src for t in lower):
            boost += 2.0
        prom = ks_mod.parse_promoter(r["promoter"])
        if prom.domains and any(
                t in {d.lower() for d in prom.domains[:3]} for t in lower):
            boost += 1.5
        epi = ks_mod.parse_epigenetics(r["epigenetics"])
        if epi.created_at > 0 and 0 < now - epi.created_at < 48 * 3600:
            boost += 0.5
        if boost > 0:
            auth[r["gene_id"]] = boost
        rate = epi.access_rate(3600.0)
        if rate > 0:
            acc[r["gene_id"]] = 0.05 * min(rate * 3600.0, 5.0)
    return auth, acc


def test_boosts_match_full_parse_reference(store):
    _run(store)
    auth, acc = _oracle_boosts(store, ["shared", "topic", "ent"])
    got_auth = {g: c["authority"] for g, c in store.last_tier_contributions.items()
                if "authority" in c}
    got_acc = {g: c["access_rate"] for g, c in store.last_tier_contributions.items()
               if "access_rate" in c}
    assert auth and acc, "fixture must exercise both bonuses"
    assert got_auth == auth
    assert got_acc == acc


def test_no_per_candidate_parse_for_unboosted_pool(store, monkeypatch):
    """Cost driver: pydantic parses must not scale with the pool."""
    calls = {"epi": 0, "prom": 0}
    real_e, real_p = ks_mod.parse_epigenetics, ks_mod.parse_promoter

    def epi(*a, **k):
        calls["epi"] += 1
        return real_e(*a, **k)

    def prom(*a, **k):
        calls["prom"] += 1
        return real_p(*a, **k)

    monkeypatch.setattr(ks_mod, "parse_epigenetics", epi)
    monkeypatch.setattr(ks_mod, "parse_promoter", prom)
    _run(store)
    assert store.last_pool_total >= POOL
    # Body fetch of the delivered docs parses a handful; the pool must not.
    assert calls["epi"] < POOL // 4, calls
    assert calls["prom"] < POOL // 4, calls


def test_untimed_regions_publish_signal_timings(store):
    _run(store)
    for name in ("authority", "access_rate", "fusion_prep", "rerank_combine"):
        assert name in store.last_signal_timings, store.last_signal_timings


def _ref_sort(scores, limit):
    ranked = sorted(scores, key=lambda g: (-scores[g], g))
    return ranked if limit <= 0 else ranked[:limit]


def _ref_eps_band(fused, rerank, delta, limit, coverage=None):
    cov = coverage or {}
    base = sorted(fused, key=lambda g: (-fused[g], g))
    ordered, i, n = [], 0, len(base)
    while i < n:
        leader = base[i]
        ls = fused[leader]
        if ls <= 0.0:
            ordered.append(leader)
            i += 1
            continue
        thr = ls * (1.0 - delta)
        j = i
        while j < n and fused[base[j]] >= thr:
            j += 1
        band = base[i:j]
        band.sort(key=lambda g: (-cov.get(g, 0.0), -rerank.get(g, 0.0),
                                 -fused[g], g))
        ordered.extend(band)
        i = j
    return ordered if limit <= 0 else ordered[:limit]


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("limit", [0, 1, 7, 12, 50, 5000])
def test_combinators_rank_identical_with_ties(seed, limit):
    rng = random.Random(seed)
    n = 3000
    # Few distinct score levels => heavy ties (id tie-break), zeros included.
    levels = [0.0, 0.0, 0.01, 0.0161, 0.0162, 0.02, 0.05, 0.05001]
    fused = {f"g{rng.randrange(10**6):06d}-{i}": rng.choice(levels)
             for i in range(n)}
    rerank = {g: rng.choice([0.0, 0.5, 1.5, 2.0, 3.5]) for g in fused}
    cov = {g: float(rng.randrange(4)) for g in fused}
    for comb in ("additive", "off"):
        final, ranked = rc.combine_rerank(
            comb, fused, rerank, {}, 20, 1.0, 0.05, limit)
        assert ranked == _ref_sort(final, limit)
    for delta in (0.0, 0.05, 0.5):
        final, ranked = rc.combine_rerank(
            "eps_band", fused, rerank, {}, 20, 1.0, delta, limit)
        assert final == fused
        assert ranked == _ref_eps_band(fused, rerank, delta, limit)
        _f, ranked_c = rc.combine_rerank(
            "eps_band_coverage", fused, rerank, {}, 20, 1.0, delta, limit,
            coverage=cov)
        assert ranked_c == _ref_eps_band(fused, rerank, delta, limit, cov)


def test_sort_does_not_fully_sort_when_limit_small(monkeypatch):
    """Cost driver: top-K must not materialise a full n-element sort."""
    import builtins
    sizes = []
    real_sorted = builtins.sorted

    def spy(it, *a, **k):
        out = real_sorted(it, *a, **k)
        sizes.append(len(out))
        return out

    monkeypatch.setattr(rc, "sorted", spy, raising=False)
    fused = {f"g{i:06d}": (i % 17) * 0.001 for i in range(5000)}
    rc.combine_rerank("additive", fused, {}, {}, 20, 1.0, 0.05, 12)
    rc.combine_rerank("eps_band", fused, {}, {}, 20, 1.0, 0.05, 12)
    assert max(sizes or [0]) < 5000, sizes
