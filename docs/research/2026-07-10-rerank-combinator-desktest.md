# Rerank-combinator desk test — no winner: the additives are load-bearing on literal beds

**Date:** 2026-07-10 · **Issue:** #255 · **Design:** `2026-07-09-scoring-combinator-exploration.md` (#259) · **Instrument:** PR #263 (`rerank_combinator` knob + `benchmarks/ab_rerank_combinator.py`) · **Verdict: no default change** — the pre-registered gate is failed by every candidate, in an instructive way.

## Method

Offline, in-process (`HelixContextManager`, `build_context(read_only=True, ignore_delivered=True)`), no server/GPU. Lexical probe profile (`docs/benchmarks/helix_probe_lexical.toml`: dense/SPLADE/cymatics/rerank-model off), `fusion_mode="rrf"`, topk 12, all 50 SIKE needles, both matrix beds.

Cells: `additive` (shipped: `final = fused + rerank_additive`) · `fused_tier@w=1.0` (rerank classes as RRF rank contributions) · `eps_band@δ∈{0.02,0.05,0.10}` (rerank breaks ties inside a leader-anchored relative fused band) · `off` (pure fused floor).

Metrics per cell: **exact inversions** (top-12 pairs where emitted order contradicts fused order, from the PR #263 debug hooks), **gold inversions** (non-gold above best gold on strictly lower fused), `gold_delivered` by assembled text (`check_gold_delivery`) and by gene-id, `content_has_answer`, mean best gold rank.

**Pre-registered gate** (plan + #259 §5): on both beds, exact inversions reduced ≥90% vs `additive` with gold inversions ≈ 0, AND `gold_delivered` ≥ the `additive` baseline.

## Results

`genomes/bench/matrix/xl.db` (n=50):

| cell | exactInv | goldInv | gd_text | gd_id | content_has_answer | mean best gold rank |
|---|---|---|---|---|---|---|
| additive | 677 | 57 | **0.44** | **0.62** | **0.84** | 5.6 |
| fused_tier@1.0 | 199 | 7 | 0.36 | 0.52 | 0.78 | 5.5 |
| eps_band@0.02 | **35** | **0** | 0.36 | 0.52 | 0.76 | 5.7 |
| eps_band@0.05 | 81 | 5 | 0.36 | 0.54 | 0.78 | 5.8 |
| eps_band@0.10 | 143 | 7 | 0.32 | 0.50 | 0.76 | 5.7 |
| off | 32 | 1 | 0.36 | 0.52 | 0.76 | 5.7 |

`genomes/bench/matrix/xl_clean.db` (n=50):

| cell | exactInv | goldInv | gd_text | gd_id | content_has_answer | mean best gold rank |
|---|---|---|---|---|---|---|
| additive | 611 | 139 | **0.42** | 0.46 | **0.74** | 9.0 |
| fused_tier@1.0 | 308 | 20 | 0.40 | 0.46 | 0.68 | 5.8 |
| eps_band@0.02 | **56** | **4** | 0.38 | 0.48 | 0.66 | 6.0 |
| eps_band@0.05 | 122 | 7 | 0.38 | 0.48 | 0.64 | 6.1 |
| eps_band@0.10 | 191 | 13 | 0.38 | 0.50 | 0.66 | 6.0 |
| off | 55 | 2 | **0.42** | **0.50** | 0.64 | **5.7** |

Artifact: `docs/research/data/2026-07-10-ab-rerank-combinator-xl-xlclean.json` (per-needle detail). Repro:

```bash
python benchmarks/ab_rerank_combinator.py \
  --bed-dbs genomes/bench/matrix/xl.db,genomes/bench/matrix/xl_clean.db \
  --combinators additive,fused_tier,eps_band,off --deltas 0.02,0.05,0.10 \
  --tier-weights 1.0 --topk 12 --json-out benchmarks/results/ab_rerank_combinator_full.json
```

## Reading

**1. The combinator works.** `eps_band@0.02` drives exact inversions to the achievable floor on both beds (35 vs `off`'s 32 on xl; 56 vs 55 on xl_clean; −95%/−91% vs additive) with gold inversions 0/4. DEFECT-1 is real, measurable at bed scale (611–677 inversions per 50 queries, 57–139 of them burying gold), and mechanically fixable.

**2. But every fix loses delivery — the gate fails.** On xl, `additive` beats every alternative on all three delivery metrics by wide margins (gd_id **0.62 vs 0.52**, answerability **0.84 vs 0.76–0.78**). On xl_clean the text/answerability story is the same (0.42/0.74 vs ≤0.40/≤0.68). The rerank additives — scale-broken as they are — are **net-positive carriers of delivery signal on literal-fact needles**: many SIKE golds are curated notes whose `source_id` earns the +2.0 source-path authority, so the bonus systematically rescues answer-bearing docs that pure fusion leaves mid-pool.

**3. The additive cell is simultaneously the best deliverer and the worst ranker.** On xl_clean, additive's mean best gold rank is **9.0** vs ~5.7–6.1 for everything else, and it carries 139 gold inversions — yet it wins answerability. The bonuses push some golds down (the inversions #255 documents) while lifting others into the assembly window; on these beds the rescue outweighs the burial. Both effects are real; the *net* favors keeping the additives here.

**4. Part of the alternatives' text-delivery gap is a trim artifact, not ranking.** Review finding F2 (PR #263): under `eps_band`/`off`, `last_query_scores` is pure-fused, so the assembly budget-trim keys on fused scores and can drop band-promoted docs. Signature in the data: on xl_clean, `off`/`eps_band` *gain* gold-by-id vs additive (0.48–0.50 vs 0.46) while *losing* gold-by-text (0.38–0.42 vs 0.42) — golds enter the expressed set at better ranks but their content survives the trim less often. The ranking layer and the trim layer disagree about what matters; fixing only the ranking layer surfaces the disagreement.

**5. The measurement floor is the blend layer (audit §4 item 5).** The `off` cell — no rerank additives at all — still shows 32/55 exact inversions: `_apply_candidate_refiners` (TCM hard-coded `use_tcm=True`) mutates `last_query_scores` post-fusion even under the lexical probe profile. Any future gate targeting "inversions = 0" needs the blend-layer absolutes retired first.

## Verdict

Per the council scoring-gate rule and the pre-registered criteria: **no combinator graduates; the default stays `rerank_combinator="additive"`** (which is what PR #263 shipped — nothing to revert). This is the same shape as the 2026-07-06 ANN-threshold live A/B: the desk test's job was to stop a plausible-but-wrong scoring change, and it did. #255 stays open as a documented scale defect whose naive fixes are measurably net-harmful on the 50-needle literal beds.

## Next hypotheses (ranked)

1. **Re-run on a semantic corpus** (#260's 125 paraphrastic ERB questions). The SIKE needles are literal-fact probes — the regime where lexical+authority signal is strongest and where #250 already showed dense recall is net-harmful. The combinator's expected win region is paraphrastic queries, where authority's correlation with gold should decay. Cheap: the driver takes any bed + needle set.
2. **Trim consistency before re-judging eps_band**: make the assembly trim rank-aware (or feed it combinator-consistent scores) and re-run — isolates the F2 artifact from the true ranking effect (worth ~2–4pp of gd_text on xl_clean by the id/text divergence).
3. **Authority as a literal-tier prior, not a global additive**: the +2.0 source-path bonus behaves like a "curated note" prior. Restricting authority to act *within* the literal-match tiers (the #250 any-tier-rank-1 rescue lead) could keep the rescue without the global inversions.
4. **Retire the blend-layer absolutes** (audit item 5) to clear the inversion floor — prerequisite for any future "inversions ≈ 0" gate; also repairs `[know]` input integrity independently.

Related: #255 (stays open), #259 (design), #263 (instrument), #264/#265 (sibling scale defects found by the #256 sweep), #260 (the semantic bed this should re-run on), #250 (the precedent verdict).
