"""Tag-tier IDF discipline (#327) — flag-gated, default off.

The 2026-07-31 tagger A/B (docs/benchmarks/2026-07-31-tagger-ab-tag-flood.md)
verified that the dogfood bed's six cymatix-config misses were caused by
**tag flood**: the 'cymatix' tag sits on 1,596 of 6,276 genes, every cymatix
query contains the word, and Tier-1 scores a flat ``match_count ×
tag_exact_weight`` with no corpus-frequency discipline. The ablation arms
showed ~2/3 of the LLM tagger's recall win is reproducible by density relief
alone.

``[retrieval] tag_idf_enabled = true`` scales each matched tag's Tier-1
contribution by its selectivity, ``log(N/df)/log(N)`` (df=1 → 1.0, df=N →
0.0), instead of counting every tag as 1. Scale, don't drop — the
2026-07-29 clean-bed df-filter verdict.

Off (the default) must remain byte-identical to legacy scoring.
"""
from __future__ import annotations

from cymatix_context.config import RetrievalConfig
from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)

FLOOD_N = 30  # crowders carrying only common tags


def _gene(gid: str, domains: list[str]) -> Gene:
    return Gene(
        gene_id=gid,
        content=f"content for {gid} with terms {' '.join(domains)}",
        complement="",
        codons=[],
        promoter=PromoterTags(domains=domains, entities=[]),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
    )


def _flooded_store(**store_kwargs) -> Genome:
    """FLOOD_N crowders tagged [cymatix, config]; one gold gene tagged
    [cymatix, emitfloor]. A query matching {cymatix, config, emitfloor}
    gives every gene match_count=2 — a flat tie under legacy scoring,
    which is exactly the flood regime."""
    g = Genome(path=":memory:", **store_kwargs)
    for i in range(FLOOD_N):
        g.upsert_gene(_gene(f"crowd-{i:03d}", ["cymatix", "config"]),
                      apply_gate=False)
    g.upsert_gene(_gene("gold-rare-tag", ["cymatix", "emitfloor"]),
                  apply_gate=False)
    return g


QUERY = dict(domains=["cymatix", "config", "emitfloor"], entities=[],
             max_genes=12, read_only=True)


def test_config_default_is_off():
    assert RetrievalConfig().tag_idf_enabled is False


def test_legacy_scoring_unchanged_when_disabled():
    """Off = byte-identical legacy: every tag_exact score is exactly
    match_count × tag_exact_weight (a multiple of 3.0 here)."""
    g = _flooded_store()
    g.query_genes(**QUERY)
    g.close()
    scores = {gid: c["tag_exact"] for gid, c in
              g.last_tier_contributions.items() if "tag_exact" in c}
    assert scores, "tag_exact tier did not fire"
    assert scores["gold-rare-tag"] == 6.0  # 2 matches × 3.0
    assert all(abs(s / 3.0 - round(s / 3.0)) < 1e-9 for s in scores.values())
    # The flood regime: gold ties the crowders, nothing distinguishes it.
    assert scores["gold-rare-tag"] == scores["crowd-000"]


def test_idf_breaks_the_flood_tie_toward_the_rare_tag():
    """On: the gene matching the rare tag must strictly outscore every
    gene matching only common tags, at equal match_count."""
    g = _flooded_store(tag_idf_enabled=True)
    g.query_genes(**QUERY)
    g.close()
    scores = {gid: c["tag_exact"] for gid, c in
              g.last_tier_contributions.items() if "tag_exact" in c}
    assert scores, "tag_exact tier did not fire"
    gold = scores["gold-rare-tag"]
    crowd = [s for gid, s in scores.items() if gid.startswith("crowd-")]
    assert crowd, "crowders missing from tag tier"
    assert all(gold > s for s in crowd), (
        f"rare-tag gene must outscore all crowders: gold={gold} "
        f"crowd_max={max(crowd)}"
    )
    # Selectivity bounds: contribution per tag is within
    # (0, tag_exact_weight]; two matches can never exceed 2 × weight.
    assert 0.0 < gold <= 2 * 3.0


def test_df_cap_default_is_off():
    assert RetrievalConfig().tag_df_cap == 0.0


def test_df_cap_removes_flooded_tag_from_tier_membership():
    """#327 iteration 2: IDF scaling measured NULL on the beds because
    under RRF the flood is tier MEMBERSHIP — flood-tied genes stay tied
    when scaled uniformly. With a df cap, a tag covering more than
    cap×N genes stops matching at all: crowders whose only match was
    the flooded tag drop OUT of the tier; the gold gene stays in via
    its rare tag."""
    g = _flooded_store(tag_df_cap=0.25)  # 'cymatix' covers ~100% > 25%
    g.query_genes(**QUERY)
    g.close()
    scores = {gid: c["tag_exact"] for gid, c in
              g.last_tier_contributions.items() if "tag_exact" in c}
    assert "gold-rare-tag" in scores, "rare-tag gene must stay in the tier"
    # 'config' covers 30/31 genes (> cap) and 'cymatix' covers 31/31 —
    # both flooded, so every crowder must vanish from the tier.
    assert not any(gid.startswith("crowd-") for gid in scores), (
        f"flooded-tag crowders must drop out of the tier: {scores}"
    )
    # gold's remaining match is exactly the rare tag: 1 × weight.
    assert scores["gold-rare-tag"] == 3.0


def test_df_cap_legacy_identical_when_zero():
    g = _flooded_store(tag_df_cap=0.0)
    g.query_genes(**QUERY)
    g.close()
    scores = {gid: c["tag_exact"] for gid, c in
              g.last_tier_contributions.items() if "tag_exact" in c}
    assert scores["gold-rare-tag"] == 6.0
    assert scores["crowd-000"] == 6.0


def test_idf_scores_are_monotone_in_tag_rarity():
    """A gene matching two rare tags outscores one matching a rare + a
    common tag, which outscores common + common."""
    g = Genome(path=":memory:", tag_idf_enabled=True)
    for i in range(20):
        g.upsert_gene(_gene(f"bulk-{i:03d}", ["common1", "common2"]),
                      apply_gate=False)
    g.upsert_gene(_gene("two-rare", ["rare1", "rare2"]), apply_gate=False)
    g.upsert_gene(_gene("one-rare", ["rare3", "common1"]), apply_gate=False)
    g.query_genes(domains=["common1", "common2", "rare1", "rare2", "rare3"],
                  entities=[], max_genes=12, read_only=True)
    g.close()
    s = {gid: c["tag_exact"] for gid, c in
         g.last_tier_contributions.items() if "tag_exact" in c}
    assert s["two-rare"] > s["one-rare"] > s["bulk-000"]
