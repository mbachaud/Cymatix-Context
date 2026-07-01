# PRD: Deterministic recall@10 lift via end-to-end rank-fusion (un-bury retrieved gold)

- **Date:** 2026-05-30
- **Author:** Max (w/ Claude)
- **Status:** DRAFT — awaiting sign-off
- **Touches:** retrieval ranking (per-shard fusion + cross-shard merge), config defaults, feature flags → PRD-first, do not implement before sign-off.
- **Constraint:** deterministic, LLM-free. The ribosome/cross-encoder stays disabled (design pillar). No embedding change.

---

## 1. Problem

The recall@200 diagnostic (300q, sharded 100-shard Onyx) showed gold is **retrieved but buried**:

| type | recall@10 (users get) | recall@200 (in the pool) | recoverable by re-ranking |
|---|--:|--:|--:|
| basic | 24.6% | 60.6% | **+36.0 pp** |
| semantic | 2.4% | 21.6% | **+19.2 pp** |

The candidate pool is right; the **ordering** is wrong. Goal: a deterministic re-rank that recovers the buried gold, measured as recall@10 on the EnterpriseRAG bench.

## 2. Verified root cause (2 adversarial verification rounds, on the real code)

The bench ran **sharded**, so the relevant path is:
`/fingerprint` → `_retrieve` → `ShardedGenomeAdapter.query_docs` → `ShardRouter.query_genes` (`shard_router.py:356`) → per-shard `query_docs` + a cross-shard merge. (`query_docs_ann` is **bypassed** — the adapter hard-codes `_dense_embedding_enabled=False`, `sharding.py:212`.)

**The dominant burial locus = the cross-shard merge sort key.** `shard_router.py:565-572` (re-applied at `:798-804` after co-activation):

```python
sorted(corrected, key=lambda gid: (-corrected.get(gid, 0.0), -rrf_all.get(gid, 0.0), gid))
```

- **primary** = `corrected[gid]` = per-shard **IDF-corrected ADDITIVE scalar** (`raw = shard.last_query_scores[gid]`, a single flattened number summing sharp tier hits + soft topical mass on one scale)
- **secondary** = `rrf_all` (RRF) — used **only as a tiebreaker**
- per-shard **tier evidence** (`last_tier_contributions`) is carried forward for introspection only (`:507-510`) — **never read by the sort**

A gold doc whose strength was a *sharp* tier signal is normalized into the same additive currency as topical neighbors with broad soft-tier mass, and the merge has no tier-aware re-rank to recover it. **Structurally identical to the `query_docs_ann` flattening — just at the merge.** (`fusion.py`'s own docstring argues per-tier scores are non-commensurate and that rank-fusion, being scale-invariant, is the fix — but the merge uses RRF only as a tiebreaker.)

Ruled out by verification: per-shard additive order (a) — depth is over-fetched 4× (`per_shard_fetch = 2·max_genes` × per-shard `limit = 2·max_genes`), so within-shard order doesn't bury top-200 gold; IDF/doc-boost (c) — uniform per shard, can't reorder within a shard; RRF tiebreak — only engages on exact-equal scores.

**Separately:** the single-store path (`query_docs_ann`, `knowledge_store.py:2744`) has the *same* pathology — it sorts the lex⊕dense union by raw dense cosine, pinning lex-only docs at `threshold−0.01` below all dense hits and discarding `gene_scores`. Confirmed 2/2 refuters. This does **not** affect the sharded bench but hurts single-store / small-corpus users.

## 3. Proposed fix — rank-fusion as the ranking currency

The pathology is the same everywhere: **non-commensurate per-tier/per-shard scores summed into one scalar, flattening sharp evidence.** RRF (rank-fusion) is scale-invariant and is already implemented and wired. Make it the ranking currency end-to-end, each piece flag-gated to default-current-behavior.

| # | Change | Site | Targets | Effort | Risk |
|---|---|---|---|---|---|
| **P1** | **Cross-shard merge: RRF as PRIMARY** key, additive as tiebreak: `(-rrf_all, -corrected, gid)` | `shard_router.py:565-572` & `:798-804` | the bench's dominant locus (b) | trivial | low |
| **P2** | **Flip `fusion_mode` default `additive`→`rrf`** (per-shard) | `config.py:319` | per-shard order feeding P1; also single-store | trivial | medium |
| **P3** | **`query_docs_ann` union sort-key blend** (gene_scores ⊕ cosine, not cosine alone) | `knowledge_store.py:2744` | single-store / small-corpus only (NOT the bench) | small | low |

**P1 + P2 are the coherent bench fix.** P2 makes each shard publish rank-fused order; P1 fuses those ranks across shards. Either alone is partial: P1-only fuses per-shard *additive* ranks (inherits intra-shard additive flattening); P2-only still hits the merge's additive-scalar primary key (a scale seam). Together = end-to-end rank fusion.

All three behind config flags; defaults reproduce today's behavior exactly until we flip after measuring.

## 4. Measurement (gates every flip)

Harness: `benchmarks/bench_enterprise_rag_recall.py` (`/fingerprint`, `max_results=k`, recall@k = gold rank < k; 200-cap at `routes_context.py:694`). Reuse the recall@200 diagnostic infra (`F:/tmp/recall200_diagnostic.py`).

1. **Fast loop:** Onyx **10K** fixture (`genomes/bench/matrix/enterprise_rag_10k_batched.db`), `--max-questions 100 --k 10` — detects a 2-3 pp shift in minutes.
2. **Confirm:** 100-shard Onyx, the calibrated 300q (semantic 125 + basic 175), recall@{1,10,50,200}.
3. **A/B matrix:** baseline (additive, today) → P1 → P1+P2 → **P1+P2 + SPLADE-on** (see §4a). Each 100q. Report per-type recall@10 delta + the recall@200 ceiling (should be ~unchanged for P1/P2 — same pool, re-ordered; SPLADE-on is the one variant that *does* change the pool, since it adds a recall tier).

Success = recall@10 moves materially toward the @200 ceiling (basic 24.6→ target, semantic 2.4→ target) with the @200 pool unchanged for the re-ordering variants.

### 4a. SPLADE-on follow-up (re-enable the disabled sparse tier)

All bench runs to date are **variant A (SPLADE off)** — disabled originally because the SPLADE-on daemon hung at boot on the v2/100-shard fixture. Verified prerequisites (2026-05-30):
- **SPLADE data exists** in the fixture: `splade_terms` ≈ 1.27M rows/shard (ingested corpus-wide), so re-enabling at query time queries real data, no re-ingest needed.
- **Not the per-shard model storm A1 fixed:** the SPLADE encoder (`naver/splade-cocondenser-ensembledistil`) is a *process singleton* (`splade_backend._ensure_loaded`, module globals), not loaded per shard.
- **The boot hang was almost certainly HF-429** (encoder fetched from HF Hub on first load under the rate limit) — fixed by the v0.6.2 `HF_HUB_OFFLINE=1` launcher guard + the model already being cached from ingestion. **The first SPLADE-on boot confirms the hang is gone.**

Run SPLADE-on **on top of P1+P2 (rank-fusion), not against today's additive baseline** — as tier 3.5 in an additive sum it would just pile more soft mass onto the buried-gold problem; under rank-fusion it becomes a properly rank-weighted recall signal. This variant is the only one that changes the *candidate pool* (the others only re-order), so its recall@200 may rise too.

Watch-items: (1) the query is SPLADE-encoded once **per shard** in the fan-out (~100× the same query through the singleton model) — a latency cost, mitigated by the singleton + concurrent fan-out (#172); worth a p95 check. (2) ANN calibration (`margin_over_random`) is for the **dense** threshold only, so SPLADE-on needs no dense re-calibration — but it shifts the tier balance, which rank-fusion absorbs by construction.

## 5. Risks

| risk | mitigation |
|---|---|
| **RRF weights untuned** — default tier weights (fts5 3.0, dense 1.0, …) were calibrated as *additive magnitudes*; reused as RRF post-multipliers they may underperform (dense_weight 1.0 vs fts5 3.0 still favors lexical 3:1, may blunt the semantic lift) | measure first; if lift is weak, a per-tier RRF-weight sweep is a fast follow-on (esp. raising `dense_weight` for the semantic bucket) |
| **Scale seam (P2 without P1)** — merge ranks RRF-scale per-shard numbers on its additive-scale key | ship P1 with P2; P1 makes the merge rank-based so the per-shard scale no longer matters |
| **Downstream gates read `last_query_scores`** — RRF produces a flatter/different distribution; ratio gates + dynamic expression-budget allocator key on absolute magnitude | re-validate those gates on the RRF distribution before flipping the default; flag stays off until clean |
| **Confidence/abstention heuristics** keyed on absolute score | RRF compresses gaps — re-check; relevant to Joe's abstention work (he's separately finding the system confabulates rather than abstains) |
| default flip surprises users | flag-gated, byte-identical default; patch-bump + CHANGELOG; flip only after the A/B proves lift |

## 6. Sequencing & recommendation

1. **P1 first** (trivial sort-key swap, flag-gated) → measure on 10K then 100-shard. It directly hits the verified dominant locus; cheapest possible test of the rank-fusion thesis.
2. **Add P2**, measure P1+P2 (the coherent end-to-end rank fusion). Re-validate the downstream gates (§5).
3. If lift is real but sub-ceiling, **RRF-weight sweep** (fast follow-on, esp. `dense_weight`).
4. **P3 in parallel** (independent single-store win; doesn't touch the bench) — lower priority.
5. Flip defaults only after the A/B; ship as a patch release with a CHANGELOG provenance note (post-#172 generation, per Joe's flag).

## 7. THE sign-off decisions

1. **Scope to start:** P1-only first (measure the cheapest hit on the verified locus), or P1+P2 together (the coherent fix), or all three incl. the single-store P3? — *Recommend: P1 → measure → P1+P2 → measure.*
2. **Keep it strictly deterministic / ribosome-off?** — *Recommend yes (design pillar); this whole plan is LLM-free.*
3. **OK to spend a daemon cycle on the bench A/B** (boots 100-shard daemons, ~20-40 min each) once implemented? The 10K fast loop is cheap; the 100-shard confirm is the slow part.

→ Confirm scope (1) + the A/B budget (3), and I'll implement P1 TDD-first off origin/master, measure on the 10K fixture, and bring you the recall@10 delta before going further.
