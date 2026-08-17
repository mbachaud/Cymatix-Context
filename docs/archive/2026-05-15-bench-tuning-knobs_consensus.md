# Bench Tuning Knobs — Council Consensus

**Date:** 2026-05-15
**Question:** After PR #119 (sharded BM25 IDF normalization) stabilized the sharded cliff, which tuning knob(s) should be benched next to identify what moves `correct` / `wrong` / `abstain` / `retr_hit`?
**Context:** medium-sharded retr_hit = 2/10, medium-blob retr_hit = 3/10. Bug B follow-ups (#120 co-activation, #121 README/CLAUDE.md ranking) filed but not in scope. BGE backfill deferred. Haiku-driven correctness has ±3 needle variance run-to-run on identical code.

## Personas Consulted

| Persona | Lens |
|---|---|
| Pragmatic Engineer | Cheapest-experiment-most-signal |
| Statistician / Experiment Designer | Variance & multiple-comparisons rigor |
| Retrieval Specialist | Knob-leverage hierarchy from IR theory |
| Devil's Advocate | Challenges the metric itself |

## Independent Analysis

### Pragmatic Engineer
- **Position:** Bench synonym map expansion + classifier thresholds first. Both config-only, reversible.
- **Strongest argument:** Lowest cost-to-signal ratio. CLAUDE.md explicitly flags synonyms as load-bearing.
- **Key concern:** 10-needle N is tiny; without variance control we'd interpret noise as signal.
- **Scores:** Synonyms 9 · Classifier 7 · Abstain 6 · Splice 5 · Budget 5 · Cymatics 4 · PLR 3 · SR 4 · Cold-tier 5 · Decoder 6
- **Changes mind if:** Baseline variance already characterized.

### Statistician / Experiment Designer
- **Position:** DON'T touch a single knob yet. Replicate the current baseline 3-5× first.
- **Strongest argument:** We observed Haiku-correctness vary ±3 across runs of identical code. 10-needle binary metric has SE ~16%. Any change <2 needles is noise.
- **Key concern:** Knob-tuning conclusions on uncalibrated variance are worse than no tuning — they hard-code accidents.
- **Scores:** Baseline replication: 10 · Paired bootstrap ablation after: 8 · Anything before that: 3
- **Changes mind if:** Loose-signal mode accepted, directional trends only.

### Retrieval Specialist
- **Position:** Expansion > reranking > scoring. We tuned scoring (#119). Next is expansion: synonyms + cross-shard co-activation.
- **Strongest argument:** The 5 medium-sharded misses are recall failures, not scoring failures. Blob succeeds on these via `_expand_coactivated` pulling READMEs through harmonic edges.
- **Key concern:** Splice/budget/decoder can't move retr_hit. They affect what reaches Haiku, not what's retrieved.
- **Scores:** Synonyms 8 · Cross-shard co-activation 9 · PLR 7 · SR 7 · Cymatics 6 · Cold-tier 6 · Abstain 5 · Decoder 4 · Splice 3 · Budget 2
- **Changes mind if:** Empirical evidence shows splice/budget DO move retr_hit.

### Devil's Advocate
- **Position:** Metric is broken; fix it before tuning anything.
- **Strongest argument:** retr_hit is a strict single-label match against a partly-incorrect fixture (`helix_port`'s gold isn't retrievable even on blob). We agreed correctness was the better metric but kept using retr_hit anyway. Knob-tuning against a corrupted meter actively misleads.
- **Key concern:** Stopping to fix the bench feels like avoidance but is the highest-leverage move.
- **Scores:** Multi-valid-gold + variance baseline: 10 · Any knob tuning before: 2-3
- **Changes mind if:** Both meter-fix and variance baseline land first.

## Conflict Map

| Topic | Agrees | Disagrees | Confidence |
|---|---|---|---|
| Synonym map is cheapest first knob | Pragmatic, Retrieval | Statistician (no baseline), Devil's (broken meter) | Medium |
| Run baseline 3-5× before tuning | Statistician, Devil's | Pragmatic (slow), Retrieval (know enough) | Medium-High |
| Metric needs fixing | Devil's, Statistician (partial) | Pragmatic, Retrieval | Medium |
| Expansion > scoring > splice/budget | Retrieval, Pragmatic (implicit) | Devil's (irrelevant if broken) | Medium |
| Splice/budget/decoder don't move retr_hit | Retrieval | Pragmatic (correct/abstain might) | Medium |
| Cross-shard co-activation is biggest single lever | Retrieval, Pragmatic | — | High |

**Cross-validated risks:**
- Small-N + flawed-metric = false positives (Statistician + Devil's).
- Splice/budget/decoder are bench-noise for retr_hit (Retrieval explicit; others implicit).

**Blind spot:** Bench cost. Full-matrix runs are ~$1-2; aggressive ablation grids could hit $20-50.

## Consensus Recommendation

**Decision:** Two-stage approach. Stage 1 (rigor) is non-negotiable. Stage 2 (knob tuning) is informed by Stage 1's variance floor.

**Confidence:** Medium-High on staging; Medium on Stage 2 ordering.
**Unanimous:** No — Pragmatic dissents on Stage 1 delay; Retrieval is impatient to start expansion work.

### Stage 1 — Bench rigor foundation (~2 hours)

1. **Replicate baseline 3× on medium + medium-sharded.** Measure mean ± stddev for retr_hit, correct, wrong, abstain. Establishes the noise floor.
2. **Add multi-valid-gold mode.** `gold_source: str` → `gold_sources: list[str]`. Hand-curate the 10 existing needles by reviewing what blob currently delivers vs the labeled gold.
3. **Document headline metric.** Decide `correct - wrong` vs `retr_hit_v2` (multi-gold) vs both. Note in `docs/benchmarks/`.

### Stage 2 — Knob tuning (after Stage 1)

1. **Synonym map expansion** (config; cheap; reversible). Inspect the 5 failing medium-sharded queries; identify unmapped tokens. Add mappings. Bench.
2. **Cross-shard co-activation** (#120; engineering work; biggest expected lift per Retrieval).
3. **Paired reranker ablation:** PLR × cymatics distance_metric × harmonic_links. 2³ = 8 cells, medium-only.
4. **Skip:** splice, budget, decoder, cold-tier expansion. They don't move retr_hit (Retrieval consensus); indirect effect on correctness is below noise floor (Statistician).

### Mitigations

| Concern | Mitigation |
|---|---|
| Broken metric | Stage 1.2 multi-valid-gold before any tuning |
| No variance baseline | Stage 1.1 baseline × 3 |
| Stage 1 slow | Cap at ~2 hours wall time |
| Splice/budget might surprise | Run decoder_mode as placebo control during Stage 2 |
| Cost | Constrain Stage 2 ablations to medium fixture until a knob is identified as promising |

### Reversibility

- Synonyms: trivially reversible (toml)
- Co-activation: separate PR, fully revertable
- Reranker ablations: config toggles, all defaults preserved
- Multi-valid-gold: additive bench schema; legacy single-gold mode preserved

### Review triggers

- Stage 1 stddev ≥3 needles → every conclusion needs ≥5× replication. Re-plan.
- Multi-valid-gold lifts blob retr_hit from 3 → 7+ → gap to blob was always smaller; reduce knob-tuning ambition.
- Synonym map alone closes medium-sharded gap → #120/#121 deprioritized.

---

> This analysis simulates multiple specialist perspectives to surface risks and tradeoffs. It is not a substitute for input from actual domain experts on your team. The personas are heuristic models — real specialists may identify concerns not captured here. Use this as a structured starting point, not a final verdict.
