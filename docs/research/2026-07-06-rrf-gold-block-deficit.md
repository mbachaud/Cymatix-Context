# Why RRF delivers fewer gold blocks on xl_clean (tier-breadth bias)

**Question.** SIKE Run-2
(`docs/benchmarks/2026-07-06-rrf-default-rebaseline.md`) found RRF wins
answerability on xl_clean (+6–8pp `content_has_answer`, +12–14pp
`body_has_answer`) but delivers **fewer gold blocks** than additive
(`gold_delivered` 0.46–0.48 vs 0.50–0.54). Why does the fusion that
surfaces more *answers* surface fewer *gold source blocks*?

**Answer: RRF rewards tier breadth; additive rewards single-tier
dominance.** On the needles RRF loses, the answer-bearing gold chunk is
a narrow-but-strong signal — rank-1 in the literal-match tiers (`fts5`,
`tag_exact`) because it contains the rare query term and the answer
literal — but it fires in *fewer tiers* than the broad project documents
around it. RRF's score is `Σ_tiers 1/(k + rank_in_tier)`, which
accumulates more mass for a document present in 6–7 tiers than for one
that is #1 in 5 tiers. So the gold chunk is demoted below documents that
are mediocre-but-present across more tiers and don't contain the answer.

## Mechanism, measured (in-process, lexical probe, `biged_ram_ceiling`)

Query: *"What RAM ceiling percentage does BigEd target…"*, answer `97%`,
answer-bearing gold chunk = `Education/fleet/fleet.toml`.

| fusion | gold chunk rank | pool | delivered? | what outranks the gold |
| --- | --- | --- | --- | --- |
| **additive** | **1** (score 42.5) | 3124 | **yes** | — (gold is #1) |
| **rrf** | **20** | 50 | **no** (top-5 only) | 6 docs firing in 6–7 tiers |

Under additive the gold chunk fires in 5 tiers
(`access_rate, fts5, lex_anchor, tag_exact, tag_prefix`) and its raw
accumulated score (dominated by the strong `fts5` + `tag_exact` literal
match) is the pool maximum. Under RRF the top-6 are all broad project
docs (`SYNTHESIS.md`, `BENCHMARKS.md`, `COMPARISON_*.md`) firing in 6–7
tiers each — they add `authority` and `harmonic` (co-activation) tier
mass the gold chunk lacks. The gold's rank-1 literal dominance in two
tiers cannot outweigh the displacers' presence in two *extra* tiers.

The same pattern holds on the other two genuine-loss needles
(`biged_smoke_tests`: gold rank 1→19; `cosmictasha_postgres_version`:
rank 2→7). Reproduce: `benchmarks/diag_rrf_tier_breadth.py`.

## Why this is the flip side of RRF's win, not a bug

RRF's tier-breadth reward is exactly why it wins net answerability: on
needles where the answer needs *consensus* across tiers to surface above
noise (the majority), democratizing tier ranks floats the right document.
The loss cases are the minority where the answer is a rare literal in one
chunk with a dominant single-tier signal — additive's score-sum preserves
those, RRF's rank-democracy buries them. On xl_clean the mix nets to
−3 gold blocks but +6–8pp answerability (the churn also means RRF often
delivers the answer from a *different, non-gold* block —
`scorerift_confidence_floor` flips `content_has_answer` False→True under
RRF despite losing the gold block). Reverting RRF would trade the
majority win for the minority.

## The disagreement between metrics, quantified

Of the 7 xl_clean depth-200 needles where RRF drops the gold block vs
additive, only **3** are genuine answerability losses
(`biged_ram_ceiling`, `biged_smoke_tests`, `cosmictasha_postgres_version`
— `content_has_answer` True→False); the other 4 are neutral (the answer
was not in that block anyway) or RRF still surfaces the answer elsewhere.
Against those, 4 needles gain gold under RRF that additive dropped. So
`content_has_answer` (which scans the whole assembled window) is the
honest arbiter, and it favors RRF.

## Actionable follow-ups (not shipped here — this is a diagnosis)

1. **Literal-match tier boost under RRF.** Raise the RRF post-multiplier
   weights on `fts5` / `tag_exact` (`[retrieval] fts5_weight`,
   `tag_exact_weight` — already bind as RRF post-multipliers per #202) so
   a rank-1 literal hit carries more fused mass. Measure on the SIKE set.
2. **Any-tier rank-1 rescue.** Float a document that is #1 in a strong
   lexical tier past the delivery cut even if its fused rank is lower —
   a targeted fix for the single-tier-dominant answer case. Ties into the
   #205 per-class retrieval profiles.
3. Both are measurable against the committed Run-2 harness; neither
   should revert the RRF default (net answerability positive).

## Caveats

- Measured on xl_clean, lexical probe (dense/splade off — matching the
  Run-2 sweep that surfaced the deficit). The tier-breadth mechanism is
  fusion-level and bed-independent, but the exact loss/gain mix is
  corpus-specific.
- `last_query_scores` exposes the full accumulated pool under additive
  (~3k) vs the fused top-K under RRF (~50); the rank comparison is on
  delivery-order position, which is what determines the top-5 cut either
  way.
