# Tagger A/B: the win is real, the mechanism is tag flood, and half the trophy is hollow (2026-07-31)

> Paired same-snapshot beds: `taggerdogfood_cpu` (CpuTagger control, 6,276
> genes, identity `d2a2177c…`) vs `taggerdogfood` (gemma4:e2b LLM ingest,
> 5,693 genes, identity `60d88fb3…`), 18 needles, k=12. Every claim below
> survived a 5-agent adversarial verification (independent recompute,
> ablation arms, counterfactuals, completeness critic); the refinements and
> refutations they produced are folded in. Receipts under
> `docs/benchmarks/data/2026-07-31-*`.
>
> Model identity: the build was launched with `--model gemma4:e2b` and
> `ollama ps` showed gemma4:e2b resident (100% GPU) across the 17.7 h
> ingest; the wired-model log line was lost to an output filter (fixed for
> future runs), so identity rests on process evidence, not the log.

## The honest headline (critic's wording, adopted)

Single build, single run, n=18: a gemma4:e2b-tagged ingest lifts
**file-level recall@12 from 12/18 to 18/18**, recovering all six
cymatix-config misses — a ranking effect driven mostly by **escaping the
CPU tagger's tag flood** (about two-thirds reproducible by thinning CPU
tags with no LLM at all). It is **not yet an answer-level result**: 4 of
the 6 recovered needles deliver gold chunks that no longer contain the
answer text, `know_emit_floor`'s value appears nowhere in its delivered
top-12, and the LLM ingest itself stubbed out the defining text of two
answers it is credited with recovering.

## The numbers

| | control (CPU tagger) | e2b LLM ingest |
| --- | ---: | ---: |
| recall@12 | 0.667 | **1.000** |
| recall@5 | 0.556 | 0.944 |
| MRR | 0.434 | 0.676 |
| genes / gold genes | 6,276 / 421 | 5,693 / 357 |
| ingest wall time | 2.6 h | **17.7 h** |
| files failed at ingest | 0 | **23** |

Per-needle: 6 recovered (all cymatix-config: `cymatics_bins` MISS→1,
`max_genes` MISS→1, `splice_aggressiveness` MISS→1, `know_emit_floor`
MISS→2, `genome_path` MISS→2, `fusion_default` MISS→5), 3 improved, 6
flat, and **3 regressed** (`confidence_gate` 1→2, `import_hygiene` 7→10,
`ollama_model` 1→3 — identical gold both sides, so genuine rank losses the
headline must carry).

## The confound that died three ways

The e2b bed is 583 genes smaller, so bed-slimming had to be excluded:

1. **Crowder existence:** the control's top-12 crowders survive in the e2b
   bed (12/12 on five flipped needles, 10/12 on `genome_path`) and every
   survivor ranks strictly below gold there. Demoted, not deleted.
2. **Counterfactual deletion:** removing every e2b-absent gene from the
   control's ranked pool and re-ranking flips **zero** needles — best case
   moves gold r18→r14, still outside the window.
3. **Arm E (verifier's ablation):** the 592 missing genes are worth
   exactly 0.0000 recall / 0.0000 MRR.

The comparability audit cuts the same way: all 64 lost gold genes come
from just two files, gold shrinkage makes e2b's targets *harder* (e.g.
`fusion_default` 118→61 gold genes — its recovery at r5 happened against
roughly 0.6× the hit surface), and only 2 of the control's 216 top-12
slots hold a gene absent from e2b.

## Mechanism: tag flood, not semantics

The complement hypothesis (LLM writes query-shaped summaries → FTS
paraphrase layer) was **falsified by ablation**, despite its descriptive
half being true (e2b complements are 381-char abstractive summaries vs
1,510-char raw excerpts, and FTS indexes the column —
`storage/ddl.py:394`). The verifier's within-bed arms, gold held fixed:

| arm | intervention | recall@12 | reading |
| --- | --- | ---: | --- |
| A | blank complements out of e2b FTS | recoveries hold at r1, MRR **up** | complement not the cause |
| B | transplant e2b complements onto cpu bed | +0.0033 MRR | noise |
| C | transplant e2b **tags** onto cpu bed | 0.667 → **0.944** | tags are the cause |
| D | thin cpu's own tags to e2b density (same vocabulary) | 0.667 → **0.833** | ~⅔ is pure density |
| E | delete e2b-absent genes from cpu pool | 0.667 → 0.667 | slimming is nothing |

The sharpest datum: the tag `cymatix` sits on **1,596 genes** in the cpu
bed vs **255** in e2b, every cymatix needle's query contains the word, the
exact-tag tier scores a flat ×3.0 with no frequency discipline
(`knowledge_store.py:2441`), and tags are additionally baked into the FTS
content column itself (`storage/indexes.py:76-80`). Gold drowns in its own
project's tag. **The deterministic pipeline's blindness here is not
semantic — it is the absence of corpus-frequency discipline on the tag
tier (#327), and the LLM tagger fixes it mostly by accident, by being
sparse.**

Consequence, and the cheapest lever this campaign has found: **an IDF-style
tag-tier fix is deterministic, costs no model, and Arm D says it captures
roughly two-thirds of the 17.7-hour LLM ingest's win.** That fix should be
built and measured before any LLM-ingest adoption decision.

## The hollow half: answer containment

Scoring is gene_id set membership, and the critic audited what the
delivered genes actually *contain*: the LLM ingest path is a **bundled
intervention** — it tags, summarizes, and *compresses*: 45.8% of e2b genes
(2,610/5,693) store a 72-char `[COMPRESSED:euchromatin]` stub instead of
content (control: 16/6,276). Three of the six recoveries are scored on the
same stubbed cymatix.toml gene; `know_emit_floor`'s `0.45` appears nowhere
in the content+complement of its entire delivered top-12. Only
`fusion_default` and `splice_aggressiveness` deliver defining text in a
gold slot. This is the ingest-level version of the splice-fidelity hazard
flagged before the campaign: **a compressor that drops the pinned literal
converts delivery into nothing.**

End-to-end on the tagged bed (expression arms,
`2026-07-31-expression-arms-taggerbed.json`): rank recall 1.000 →
expressed gold 0.667 (up from 0.5) → consumer 0.647 overall / 0.846 when
the value was delivered. The 2×2 with query expansion: {cpu, raw} 0.667 ·
{cpu, +qexp} 0.833 · {e2b-tag, raw} 1.000 · {e2b-tag, +qexp} 1.000 —
**ingest-time tagging subsumes query-time expansion on this bed.**

## What this changes

1. **Build the deterministic tag-IDF fix (#327) and A/B it against both
   beds.** If it lands near Arm D's 0.833–0.944, the 6.8× ingest cost and
   the 23-file robustness tax buy little.
2. **Answer containment becomes a standard metric.** Set membership
   overstated this result; the harness gains accept-literal-in-delivered-
   content columns next to rank (this also implements Joe's reply-04
   top-k logging ask properly).
3. **Content stubbing needs a policy before LLM ingest ships**: preserve
   pinned literals (key_values already exist at ingest) or store content
   alongside the compressed representation.
4. **For the spark e4b run (cc-exchange 0013):** Joe's verifier should
   score answer containment from day one, expect the stubbing property,
   and treat gold-count deltas as first-class. His e4b arm now tests
   whether a stronger tagger moves the ⅓ that isn't density.

## Addendum, same day: the #327 lever, two iterations

**Iteration 1 — IDF scaling: NULL, mechanism-informative.**
`[retrieval] tag_idf_enabled` scales each matched tag's Tier-1
contribution by `log(N/df)/log(N)`. Measured on both beds: recall@12
0.667 flat, MRR ±0.001 (threaded, not inert-by-plumbing). Why it can't
work under RRF: flood-tied genes are scaled *uniformly*, so the tier's
ordering — the only thing RRF consumes — never changes. The flood is
tier **membership**, not score magnitude. Receipts:
`2026-07-31-tagidf-{cpubed,cleanbed}.json`.

**Iteration 2 — DF-cap membership relief: hits the Arm-D target.**
`[retrieval] tag_df_cap` excludes a tag from Tier-1 when its df exceeds
cap×N (tag-side drop; the query-side "scale, don't drop" verdict does
not cover index noise). The curve, identical on both beds:

| cap | recall@12 | cymatix recall | note |
| --- | ---: | ---: | --- |
| off / 0.25 | 0.667 | 0.250 | flood tags cover 25–39% — 0.25 misses them |
| 0.15 / 0.10 / 0.05 | 0.778 | 0.500 | two misses recovered |
| **0.02** | **0.833** | **0.625** | **= the tagger-ablation Arm-D ceiling** |

Per-needle at 0.02 (both beds): `cymatics_bins`, `max_genes`,
`splice_aggressiveness` recovered (splice at exactly r12 — boundary,
flagged); `proxy_port` 7→8 and `expression_tokens` ±1 jitter inside the
window; `fusion_default` deep either way (32→34). No needle leaves the
window; other-repo recall stays 1.000 in every arm.

So the deterministic query-time knob reaches the full density-relief
ceiling — **+16.7pp at zero model cost, zero re-ingest, replicated on
two independent bed builds** — and the residual gap to the LLM bed's
1.000 is confined to the two channels tier pruning cannot touch: tag
text baked into `fts_content` (next lever: exclude high-DF tags at
index time, needs an FTS rebuild to measure) and tag content quality
(what the spark e4b run tests). Both flags ship **default off** with
this receipt in the config comment; the default-flip is a separate
decision on more than n=18.

## Addendum 2, same day: the FTS channel, measured by per-arm index rebuild

Tags are baked into `genes_fts.content` at index time (per-doc
`storage/indexes.py:76-88`, bulk `storage/ddl.py:426-436`), a channel
tier pruning cannot reach. `benchmarks/dogfood/rebuild_fts_arm.py`
copies a bed (SQLite backup API — source beds are receipts, never
mutated) and rebuilds `genes_fts` with the arm's tag composition. The
2×3 factorial on the control bed, gold fixed:

| FTS composition \ tier cap | off | 0.02 |
| --- | ---: | ---: |
| stock (all tags) | 0.667 | 0.833 |
| tags df-capped @0.02 (81 tags out) | 0.722 | **0.889** |
| tags stripped entirely | 0.722 | **0.889** |

Findings:

1. **The FTS channel is real and worth +5.5pp on its own** (one needle),
   replicating **exactly** on the clean bed (0.722 both builds).
2. **Capping 81 flooded tags ≡ stripping all ~thousands** in every cell
   — the surgical fix captures the entire channel; rare tags in FTS are
   harmless and can stay.
3. **The channels are additive**: 0.667 + 5.5 (FTS) + 16.6 (tier) ≈
   0.889 combined. Independent mechanisms, compose freely.
4. **Replication scopes the claim**: the clean bed's combined cells top
   out at 0.833 — the 0.889 includes `know_emit_floor` crossing at
   exactly r12, which does not survive bed change. Claim as
   **0.833–0.889**, boundary disclosed. Clean-bed MRR also peaks at
   tier cap 0.05–0.10 (0.468) rather than 0.02 — the aggressive cap
   trades a little MRR for the extra crossing.
5. The excluded-tag list is face-valid index noise: gene-id fragments,
   bare years, `agent`/`agents`/`ablation`.
6. **What resists density relief entirely**: `fusion_default` (32→34 —
   slightly *worse* under relief) and `genome_path` (18, unmoved).
   These two are the tag-content-quality residual — the channel only a
   better tagger (or curated tags) reaches, and exactly what the spark
   e4b run isolates.

Deterministic scoreboard after both addenda: flat baseline 0.667 →
full deterministic stack **0.833–0.889**, vs LLM-ingest 1.000 (with its
45.8% stubbing, 23 failed files, and 6.8× ingest cost). Receipts:
`2026-07-31-ftsarm-{none,cap02,cap02-cleanbed}.json`. Productizing the
index-side cap (an `[ingestion]` knob + reindex migration for existing
beds) is a follow-up; the bench tool is the reference implementation.

## Caveats, stated not buried

n=18, one build per arm, no A/A control on ingest (the 23 failures
include load-dependent timeouts, so a rebuild changes the failure set and
gold denominators); `import_hygiene` sits at r10 of k=12 and
`fusion_default` at r5 on a halved gold set, so the exact 18/18 is
fragile; by rule-of-three, 18/18 is consistent with a true per-needle miss
rate up to ~15%. The rank *mechanism* (tag flood) is the robust finding —
it reproduced within-bed with gold fixed, in both directions.
