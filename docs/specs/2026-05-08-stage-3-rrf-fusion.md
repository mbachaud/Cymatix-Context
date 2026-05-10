# Stage 3 — Reciprocal Rank Fusion (RRF)

Plan: helix-context retrieval-fix, Stage 3 of 6 (council 2026-05-08). Depends on Stage 2 (dense as a tier) for the dense participation in RRF.

## 1. Goals + non-goals

**Goals.** Replace the additive `gene_scores[gid] += tier_score` accumulator in `query_genes()` with rank-level Reciprocal Rank Fusion (Cormack 2009). Per-tier scores are non-commensurate (FTS5 negative-bm25, BGE cosine ∈ [-1,1], promoter exact ∈ {0, 3.0}, harmonic ∈ {0..3}, filename_anchor 4.0/match, etc.); their sum is mathematically meaningless and currently lets one over-scaled tier dominate. RRF is invariant to scale. Stack with Stages 1+2 to hit ≥75% located_n1000 retrieval@1.

**Non-goals.** No threshold recalibration of TIGHT/FOCUSED/BROAD floors (Stage 4). No per-classifier floors (Stage 4). No new tiers. No LLM in the fusion path. No change to candidate-generation order or BM25 pre/post-filter logic. No change to `query_genes_ann()` outer wrapper.

## 2. Surface area

| File | Lines | Change |
|---|---|---|
| `helix_context/genome.py` | 1815–1816 | Initialize `Fuser` alongside `gene_scores` dict |
| `helix_context/genome.py` | 1851–2380 | Each tier writes `fuser.add_tier(...)` and continues to populate `gene_scores`+`tier_contrib` for telemetry/back-compat |
| `helix_context/genome.py` | 2384–2386 | When `fusion_mode=="rrf"`, replace `last_query_scores` with fused scores |
| `helix_context/genome.py` | 2387–2407 | Telemetry block stays; reads raw `tier_contrib` (unchanged) |
| `helix_context/genome.py` | 2453–2454 | `ranked_ids = fuser.top_k(limit)` when `fusion_mode=="rrf"` |
| `helix_context/fusion.py` | new | `Fuser` class |
| `helix.toml` | new key in `[retrieval]` | `fusion_mode = "additive"` |
| `helix_context/config.py` | TOML loader | Parse `fusion_mode`, default `"additive"` |
| `tests/test_fusion_rrf.py` | new | Unit + regression tests |

## 3. Tier inventory (exhaustive)

Every site that writes `gene_scores[gid] +=` or `gene_scores[gid] =` in `query_genes()`:

| Tier name | Source lines | Current score range | Weight knob | RRF | Post-multiplier |
|---|---|---|---|---|---|
| `pki` (path-key compound) | 1851–1942 | 0.05 .. 12.0 (PKI_BASE/card, capped) | `PKI_BASE=10.0`, `PKI_FLOOR=2.0` literals | yes | `pki_weight = 1.0` (new) |
| `filename_anchor` | 1944–1962 | weight × match_count (4.0/match) | `filename_anchor_weight = 4.0` | yes | `filename_anchor_weight = 4.0` (reuse) |
| `tag_exact` | 1964–1991 | match_count × 3.0 | hardcoded 3.0 | yes | `tag_exact_weight = 3.0` (new, defaults to 3.0) |
| `tag_prefix` | 1993–2025 | match_count × 1.5 | hardcoded 1.5 | yes | `tag_prefix_weight = 1.5` (new) |
| `fts5` | 2027–2079 | min(-rank, 6.0) | hardcoded cap 6.0 | yes | `fts5_weight = 3.0` (new) |
| `splade` | 2081–2110 | min(score, 20)·3.5/20 | hardcoded 3.5 | yes | `splade_weight = 3.5` (new) |
| `sema_boost` | 2122–2152 | sim·2.0·boost_scale (≈0..2) | hardcoded 2.0 | **no** (gate-only re-rank, not a recall tier) | n/a — keep additive, applied AFTER RRF as tiebreaker |
| `sema_cold` | 2161–2198 | sim·3.0 (only when pool undersized) | hardcoded 3.0 | yes (when it fires) | `sema_cold_weight = 3.0` (new) |
| `lex_anchor` (IDF) | 2205–2239 | min(idf·1.5, 3.0), summed across terms | hardcoded 1.5/3.0 | yes | `lex_anchor_weight = 1.5` (new) |
| `authority_*` (source/domain/recency) | 1657–1726 (called 2242) | 0.5–4.0 stacked | hardcoded | **no** — flat boost on existing pool, applied AFTER RRF |
| `harmonic` | 2244–2277 | min(links·1.0, 3.0) | hardcoded 1.0/cap 3.0 | yes | `harmonic_weight = 1.0` (new), cap stays |
| `sr` (Successor Repr.) | 2279–2301 | sr_boost output (≤ sr_cap=3.0) | `sr_weight = 1.5`, `sr_cap = 3.0` | yes | `sr_weight = 1.5` (reuse) |
| `entity_graph` | 2303–2328 | 0.5/match, cap 2.0 | hardcoded 1.0·0.5 | yes | `entity_graph_weight = 0.5` (new) |
| `party_attr` | 2330–2344 | flat +0.5 | hardcoded | **no** — applied AFTER RRF as flat additive |
| `access_rate` | 2346–2370 | 0.05 .. 0.25 | hardcoded | **no** — explicit tiebreaker, applied AFTER RRF |
| `dense` (Stage 2) | new (Stage 2 spec) | cosine ∈ [-1,1], top-500 | `dense_weight = 1.0` (new) | yes | `dense_weight = 1.0` |

**Rule of thumb.** Recall/discovery tiers participate in RRF. Re-rank/tiebreaker/policy boosts (sema_boost gate, authority, party, access_rate) stay additive on top of the fused score so the user's "is this gene authoritative?" semantics survive unchanged.

## 4. RRF formula and parameters

For each gene `d` and each participating tier `t`:
```
score(d) = Σ_{t ∈ tiers}  weight_t · 1 / (k + rank_t(d))
```
where `rank_t(d)` is `d`'s 1-based rank in tier `t`'s descending-score-ordered list (genes not in tier `t` contribute 0). Constants:

- `k = 60` (Cormack default; configurable as `[retrieval] rrf_k`).
- `weight_t` from helix.toml (post-multipliers above).
- **Ties.** Stable rank-by-(score desc, gene_id asc). Genes with bitwise-equal float scores get adjacent ranks (NOT shared rank — keeps RRF strictly monotone in input order).
- **Float tiers** (FTS5 BM25, dense cosine, SEMA cosine): rank by raw score descending.
- **Count tiers** (tag_exact, tag_prefix, filename_anchor): rank by match_count descending; ties broken by gene_id.

## 5. Implementation shape

New file `helix_context/fusion.py`:

```python
class Fuser:
    def __init__(self, k: int = 60):
        self._k = k
        self._scores: dict[str, float] = {}

    def add_tier(self, name: str, ranked_ids: list[str], weight: float) -> None:
        """ranked_ids must already be in descending tier-score order."""
        if weight == 0.0 or not ranked_ids:
            return
        for rank, gid in enumerate(ranked_ids, start=1):
            self._scores[gid] = self._scores.get(gid, 0.0) + weight / (self._k + rank)

    def top_k(self, n: int) -> list[tuple[str, float]]:
        return sorted(self._scores.items(), key=lambda x: (-x[1], x[0]))[:n]

    def scores(self) -> dict[str, float]:
        return dict(self._scores)
```

In `query_genes()`, each tier now collects `(gid, raw_score)` pairs locally, sorts them descending, and calls `fuser.add_tier(name, [gid for gid, _ in pairs], weight=cfg.<tier>_weight)`. The existing `gene_scores[gid] += raw_score` and `tier_contrib[gid][name] = raw_score` writes stay (they feed telemetry and the additive fallback). At the sort site (line 2454):

```python
if self._fusion_mode == "rrf":
    fused = fuser.top_k(limit)
    ranked_ids = [gid for gid, _ in fused]
    self.last_query_scores = dict(fused)
else:
    ranked_ids = sorted(gene_scores, key=gene_scores.get, reverse=True)[:limit]
```

Authority/party/access_rate post-additives (re-rank class) apply to `last_query_scores` AFTER RRF when `fusion_mode=="rrf"`, preserving their original purpose.

## 6. Per-tier telemetry preservation

`tier_contribution_histogram()` and `tier_fired_counter()` must keep observing **raw pre-RRF** scores so panels remain interpretable. The existing emit block at lines 2387–2407 reads `tier_contrib[gid][tier_name]` (raw scores) and is unchanged. Each tier still writes its raw score to `tier_contrib` *before* calling `fuser.add_tier(...)`. **Single emit point** stays at line 2392; do not move it. Add a parallel `rrf_fused_score_histogram` (new, gated on `fusion_mode=="rrf"`) emitting the post-fusion score per gene for the new "RRF distribution" panel.

## 7. Config flag

```toml
[retrieval]
fusion_mode = "additive"   # "additive" | "rrf"
rrf_k = 60
# Post-multipliers (mostly defaulted to current implicit weights):
fts5_weight = 3.0
splade_weight = 3.5
tag_exact_weight = 3.0
tag_prefix_weight = 1.5
sema_cold_weight = 3.0
lex_anchor_weight = 1.5
harmonic_weight = 1.0
entity_graph_weight = 0.5
dense_weight = 1.0
pki_weight = 1.0
# filename_anchor_weight, sr_weight already exist — reused as-is.
```

**Deprecation timeline.** v(N): ship `fusion_mode = "additive"` default. v(N+1): flip default to `"rrf"`. v(N+2): remove additive code path. Document in this spec doc.

## 8. Tied-tier semantics

When two tiers both rank gene G at rank 1, RRF awards `weight_a/(60+1) + weight_b/(60+1)`. **Independent contribution per tier.** No max, no min, no per-gene cap. This is the math working as intended: agreement across tiers is the strongest signal RRF can express. Tied-tier covered by unit test `test_rrf_tier_independence`.

## 9. Score-ratio compatibility (TIGHT/FOCUSED/BROAD)

`context_manager.py` lines 976–984 compare `top_score` against `TIGHT_SCORE_FLOOR=5.0` and `FOCUSED_SCORE_FLOOR=2.5`. Under RRF the score scale collapses to roughly `Σ_tier weight/(k+1) ≈ (3+3.5+3+1.5+...)/61 ≈ 0.3` max. **Keeping these floors hardcoded would force every query to BROAD.**

**Decision: defer retune to Stage 4.** Stage 3 ships with a transitional bypass: when `fusion_mode == "rrf"`, `context_manager` reads `top_score / mean_score` ratio only (which IS scale-invariant) and skips the absolute-floor gates. Add `if self.genome._fusion_mode == "rrf": skip_absolute_floors = True` at `context_manager.py:976`. Stage 4 will introduce empirically-fitted RRF floors (target table: TIGHT≥0.10, FOCUSED≥0.05) after a 100-query distribution sweep.

Hand-off note: "Stage 4 owns absolute floor recalibration. Until then, RRF mode operates on ratio gates only."

## 10. Test plan

In `tests/test_fusion_rrf.py`:

- `test_rrf_pool_dominates_when_dense_alone_misses_lexical_match` — dense ranks gene A at 1, FTS ranks B at 1, A at 2; assert B > A only if FTS weight > dense weight; assert tie-breakable by k.
- `test_rrf_with_zero_weights_disables_tier` — set `fts5_weight=0`; assert FTS-only candidates absent from output.
- `test_rrf_preserves_filename_anchor_winners` — replay 2026-04-22 Dewey axis-2 corpus; assert filename-anchored gene at rank 1 (regression for +12pp result).
- `test_fusion_mode_additive_unchanged` — back-compat: bench 50 known queries under `fusion_mode="additive"`; assert ranked_ids identical to pre-Stage-3 baseline (snapshot test).
- `test_rrf_tier_independence` — two tiers both rank G at rank 1; assert score = `2·w/(k+1)`.
- `test_rrf_telemetry_emits_raw_pre_rrf` — record observations to a fake meter; assert raw bm25/cosine values surface, not RRF fractions.

## 11. Migration validation

A/B sweep on `genome-bench-N1000.db` (located_n1000 set, ≈200 queries):
- Run A: `fusion_mode="additive"` with current weights (baseline).
- Run B: `fusion_mode="rrf"` with all weights preserved as post-multipliers.
- Metric: retrieval@1, retrieval@5, retrieval@20.
- **Pass:** retrieval@1 lift ≥ +15pp.
- **Soft-fail (investigate, do not merge):** lift < +5pp — likely a weight-mapping bug; suspect filename_anchor or PKI mass mis-weighted under RRF rather than abandon the approach.
- Bench harness: `benchmarks/bench_skill_activation.py --fusion-mode rrf` (add CLI flag).

## 12. Acceptance criteria

- located_n1000 retrieval@1 ≥ 75% with Stages 1+2+3 stacked (current baseline 13.8%).
- Per-query RRF overhead ≤ 2ms (one `Fuser` instance, ≤ 12 `add_tier` calls of ≤ 500 ids each, single sort at end — well within budget).
- All unit tests in §10 pass.
- Telemetry panels for raw per-tier scores unchanged (visual diff).
- `fusion_mode="additive"` is byte-identical to pre-Stage-3 ranking on snapshot queries.

## 13. Out of scope

- Threshold recalibration for TIGHT/FOCUSED/BROAD absolute floors (Stage 4).
- Per-classifier (`caller_model_class`) floor tables (Stage 5).
- Learned weight fitting (would require `cwola_validated` labels — Stage 6+).
- Replacing the BM25 shortlist post-filter with an RRF cutoff (separate spec).
- Migrating `query_genes_ann()` thresholding to RRF space (Stage 4 with the floors).
