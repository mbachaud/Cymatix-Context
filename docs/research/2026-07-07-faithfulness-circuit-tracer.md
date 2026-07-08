# Faithfulness of injected context — a Circuit-Tracer instrument for the #239 know/miss contract

**Date:** 2026-07-07 (real-helix faithfulness completed via self-hosted graph-gen)
**Status:** COMPLETE. Instrument validated; ideal-context calibration done;
real-helix retrieval-preservation 6/6 AND real-helix *faithfulness* 6/6
causal-use — measured locally (self-hosted circuit-tracer, no rate limit, no
egress) after the hosted Neuronpedia quota blocked the batch.
**Feeds:** #239 (KnowBlock confidence logistic / know-miss agent contract).

## Motivation

The Stage-6 know/miss contract emits `know { found, confidence, gene_id_match }`
per turn. Everything it reports is a property of *retrieval* — did we find a
high-scoring document, do the tiers agree, is the coordinate grain covered. None
of it verifies the thing the contract implicitly promises: that the downstream
model **causally uses** the context helix delivered. `found=true` can be right
about delivery and still say nothing about whether the answer the model produced
was *read from* the injected span or pattern-matched from its own weights.

Faithfulness is that missing mechanistic ground truth. This note builds an
instrument that measures it directly and reports first results.

## Instrument

Anthropic/Neuronpedia shipped an open **Circuit Tracer / Attribution Graph** API
(`POST neuronpedia.org/api/graph/generate {modelId:"gemma-2-2b", prompt, slug}`
→ graph JSON: nodes with `is_target_logit`/`ctx_idx`/`influence`/`clerp`, ~54k
weighted `links`). We back-propagate influence from a logit node over the link
graph (abs weight, normalized per target, 6 relaxation passes) to get a
per-input-token attribution, localized by `ctx_idx`.

Two conditions per needle:

- **A** — question only. Does the model hallucinate?
- **B** — injected context + question. Does the answer trace to the context?

**Metric suite (per needle):**

| metric | meaning |
|---|---|
| `pA`, `pB` | P(answer token) in A vs B |
| `lift` = pB−pA | behavioral: did context raise the answer? |
| `faith` | fraction of the **answer logit's** input attribution localized to the answer token *in the injected context* |
| `answer_is_top_driver` | is that context token the answer logit's #1 input driver? (mechanistic copy) |

**Causal use** := `in_graph AND lift ≥ 0.15 AND pB ≥ 0.30 AND answer_is_top_driver`
— context both *raised* the answer to a real probability and the answer logit
*mechanically read* the injected token.

### Design decisions that matter

- **Retarget to the answer logit.** gemma-2-2b is a 2B model; its prior often
  outranks the injected answer (predicts "red" though context says "teal"). But
  the answer logit is still in the top-k graph *with* incoming edges (verified:
  non-argmax logits carry 700–1400 incoming links). So we attribute *from the
  answer logit* even when it isn't argmax — decoupling the faithfulness
  measurement from the small model's prior-competition weakness.
- **No trailing space.** End the question at the word before the answer with no
  trailing space, so the leading-space answer token is the natural continuation.
  A lone trailing space makes gemma emit markup (`<strong>`/`<em>`) instead of
  the answer — an artifact that silently zeroes faithfulness.
- **Single-token, prior-free answers.** Multi-token answers ("walrus" → `wal`+`rus`)
  never enter the graph; strong-prior answers (colors, tiers) get suppressed
  behaviorally even when mechanically faithful. Distinctive single tokens
  (otter, mango, cobalt, raven) behave cleanly.
- **Structural exclusion.** `<bos>` and spaces dominate raw attribution; excluded.

## Results

### 1. Ideal-context calibration (hand-written context, 6 synthetic needles)

Synthetic "Redwood Inference" facts, arbitrary associations a base model cannot
know (so A must hallucinate, B must read). `scratchpad/needle_faithfulness_experiment.py`.

| needle | answer | pA | pB | lift | faith | top-driver=answer | causal |
|---|---|---|---|---|---|---|---|
| beacon_mascot | otter | 0.00 | 0.66 | 0.66 | 0.33 | ✓ | ✓ |
| prism_codename | mango | 0.00 | 0.60 | 0.60 | 0.35 | ✓ | ✓ |
| harbor_zone | cobalt | 0.00 | 0.86 | 0.86 | 0.39 | ✓ | ✓ |
| sentinel_tier | platinum | 0.00 | 0.25 | 0.25 | 0.26 | ✓ | ✗¹ |
| cascade_codename | walrus | 0.00 | 0.00 | — | — | ✗ | ✗² |
| atlas_color | teal | — | — | — | — | — | rate-limited |

¹ mechanically faithful (top-driver=answer) but its tier prior held pB below the
0.30 behavioral bar. ² multi-token answer → never in-graph (design artifact,
fixed → `raven`).

**Headline:** of the 4 single-token / in-graph needles, **mechanistic
faithfulness is 4/4** — the injected context token is *always* the answer
logit's #1 input driver. Mean attribution-faithfulness 0.335, mean P(answer)
lift 0.59. The instrument reads out a clean, strong signal on ideal context.

### 2. Real helix output (`build_context().expressed_context`, shipped config)

`scratchpad/real_helix_faithfulness.py` — fresh 6-fact bed, ingest, then per
needle `build_context(read_only=True)` and inject helix's *actual* assembled
context (dense+SPLADE on, rrf, splice=query-aware trim). This separates two
failure modes the know/miss contract conflates:

- **Retrieval-preservation** (helix's job): did the answer survive
  retrieve→splice→assemble? **6/6.** For every needle the *correct* fact is
  retrieved **rank-1** with the strongest tier firing
  (`fired=fts5:6.0,lex_anchor:6.0,splade:3.5`), the answer token preserved
  verbatim in the `<GENE>` body, wrapped in real legibility headers
  (`[gene=… ◆ fired=… 73→126c]`). `expressed_context` ≈ 1.3 kB.
- **Faithfulness** (the model's job): **6/6 causal-use** — measured locally
  (self-hosted circuit-tracer; the hosted Neuronpedia quota 429'd the batch).
  Every needle: answer-token is the answer logit's **#1 causal driver** (top
  attribution) AND the injected context drives a large behavioral shift.

| needle | answer | pA | pB | lift | faith | top-driver=answer | causal |
|---|---|---|---|---|---|---|---|
| beacon | otter | 0.00 | 0.945 | 0.945 | 0.337 | ✓ | ✓ |
| harbor | cobalt | 0.00 | 0.918 | 0.918 | 0.341 | ✓ | ✓ |
| atlas | teal | 0.00 | 0.941 | 0.941 | 0.300 | ✓ | ✓ |
| prism | mango | 0.00 | 0.840 | 0.840 | 0.288 | ✓ | ✓ |
| cascade | raven | 0.00 | 0.762 | 0.762 | 0.286 | ✓ | ✓ |
| sentinel | platinum | 0.00 | 0.598 | 0.598 | 0.235 | ✓ | ✓ |

**mean lift 0.834 · mean faith 0.298 · causal 6/6.**

**Key finding — richer real context overrides prior competition.** In the
ideal-context pilot the two strong-prior needles were *non-causal*: the bare
one-sentence fact could not overcome gemma-2-2b's defaults (**atlas/teal pB
0.057**, **sentinel/platinum pB 0.246**). Injecting helix's *actual*
`expressed_context` — the retrieved document in its `<GENE>` wrapper with
legibility headers — flips both to strongly causal (**teal 0.057→0.941**,
**platinum 0.246→0.598**), and lifts the whole set (mean lift 0.53→0.83). So
helix's *delivery format*, not merely the raw fact, is what causally drives the
answer. The legibility headers do **not** distract the model: the answer token
in the `<GENE>` body remains the #1 driver in all 6. Attribution-faithfulness is
slightly lower (0.298 vs 0.335) only because the fraction is diluted across a
longer context — the answer is still rank-1.

### Self-hosted graph generation (unblocks batch + scale, no egress)

The hosted `/api/graph/generate` anonymous quota caps ~15–20 calls/window, which
429'd the real-helix batch. Neuronpedia is now open-source; its graph server is
built on the `circuit-tracer` library (safety-research/circuit-tracer), which
runs standalone. We generate graphs **locally** on a 12 GB RTX 3080 Ti:

```python
from circuit_tracer import ReplacementModel, attribute
from circuit_tracer.utils import create_graph_files
m = ReplacementModel.from_pretrained("google/gemma-2-2b", "gemma",
                                     dtype=torch.bfloat16, device="cuda",
                                     lazy_encoder=True)
g = attribute(prompt, m, offload="cpu", batch_size=48, max_feature_nodes=4096)
create_graph_files(g, slug, output_path)   # emits the SAME JSON schema
```

`create_graph_files` writes the identical `{metadata, nodes, links}` schema the
hosted API returns, so the faithfulness metric code is reused verbatim — the
local graphs reproduce the hosted pilot to ~1% (beacon pB 0.664→0.672, faith
0.333→0.339). Fit notes: bf16 + `lazy_encoder` + `offload='cpu'` keeps the model
in ~6.6 GB dedicated (Gemmascope transcoders, 7.4 GB, live in system RAM);
short-needle attribution peaks 8.4 GB, long ~350-tok `expressed_context` prompts
spill into shared memory (slow, ~6–16 min/graph, but complete). Windows note:
pin `pandas==2.2.3 numpy==2.1.3 pyarrow==21` (pandas 3.0 / numpy 2.5 heap-corrupt
on import), run with `-X utf8` (the `◆` in legibility headers breaks cp1252
JSON writes). Model choice: gemma-2-2b (Gemmascope) and Qwen3-4B
(`mwhanna/qwen3-4b-transcoders`) have released transcoders; gemma-3n/gemma-4 do
not, so they cannot be graphed.

### 3. Calibration gap on a causal-use-labeled bed (#239)

To get confidence *variance* (the 6-fact bed saturates), we built a **48-needle
graded-distractor bed**: fictional "Redwood Inference" facts, each with a unique
single-token answer (verified single-token in Qwen3-4B so a multi-token answer
can't become a false negative), plus per-family same-entity distractors graded
0–16 so retrieval quality varies. Every fact is written to a **real file** and
ingested with `source_id=abspath`, so `freshness_min=1.0` for all 48 (this kills
the earlier synthetic-`stale` haircut). Stage 1 dumps the five know-features +
the continuous confidence at budget `max_genes=2`; stage 2 graphs the **20
stratified-hardest survivors** (14 rank-2, 11 `lexical_dense_agree=False`,
confidence 0.128–0.44) on Qwen3-4B via the local circuit-tracer, and imputes
`causal_use=1` for the easier survivors (licensed below). *Every number here was
re-derived by an independent 6-agent adversarial pass, which corrected three
first-draft overclaims — the caveats box is not decorative.*

| quantity | value |
|---|---|
| golds delivered into `expressed_context` (`answer_survived`) | **45 / 48** |
| gold rank distribution | rank-1: 26, rank-2: 19, rank-3: 3 |
| graph-measured survivors **causally used** | **20 / 20** (0 exceptions) |
| — mean faithfulness / all answer-is-top-driver | 0.717 (0.58–0.81) / yes |
| KnowBlocks emitted @ shipped budget `max_genes=2` | **0 / 48** (max conf 0.4397 < floor 0.45) |
| KnowBlocks across budgets {1, 2, 3, 6} | 3, 0, 0, 0 (**≤ 3/48 at any budget**) |

**Core finding — the know-logistic has ≈0 recall against known-good deliveries.**
On a bed where 45/48 turns deliver the correct unique gold token and 20/20
graph-measured deliveries are *causally used* (the injected token is the answer
logit's top input driver, mean faith 0.72), the shipped logistic emits **zero**
KnowBlocks at the shipped operating point. This is not a surprising bug so much
as a *quantification of a documented design choice*: the `helix.toml [know]`
comment already notes calibrated confidence tops out ~0.465 and the floor (0.45)
was set so KnowBlocks "rarely fire." The measurement here is that "rarely" ≈
**never — even on facts the model provably retrieved and used.** The
precision-first operating point trades away essentially all recall.

**Mechanism.** β1(top_score) = **−1.1442** (perverse negative): stronger
retrieval *lowers* confidence. Flipping only that sign rescues a flawless-K=0
needle (`beacon`: 0.4397 → 0.8226, MISS → KNOW). β1 is the largest non-intercept
coefficient, though the intercept (−2.12) contributes more raw negative logit,
and `beacon` is a *marginal* miss (0.010 below floor) — so the low intercept +
the ~0.46 confidence ceiling matter alongside the β1 sign.

**Why the training was wrong.** PR #249 fit against the retrieval-top1 proxy
(`gold_rank==1`). On this bed that label marks only 26/48 positive and
**mislabels 19 rank-2 golds as negative** (14 graph-measured causally-used, 5
imputed; FN=19, FP=0) — the delivered-and-used facts the logistic should trust.

**Refit is direction-only.** Refitting against causal-use flips β1 to **+0.20**
and lifts the intercept to **+0.49** — the correct *direction*. But this bed is
94% positive (45/3) with **zero graph-measured non-causal deliveries**, so the
features cannot *discriminate* (in-sample AUC 0.504 = chance; LOOCV degenerate).
No AUC/ECE improvement may be claimed and these betas are **not a drop-in
production vector**; a production recalibration needs a harder, delivery-balanced
bench that actually contains delivered-but-unused turns.

> **Verified caveats (do not drop):**
> 1. **"0/48" is budget-specific.** Confidence is *not* pre-budget: `top_score`/
>    `score_gap` come from `query_docs(max_genes)` and `coordinate_confidence`/
>    `freshness_min` are computed over the post-budget expressed docs. The
>    budget-robust statement is "≤ 3/48 at any budget, 0 at the shipped one."
> 2. **Not "anti-calibrated."** AUC 0.407 rests on 3 negatives (bootstrap 95% CI
>    [0.15, 0.78]); the point-biserial is *positive* (+0.03). The robust framing
>    is **uncorrelated + systematically under-confident** — ECE 0.685 equals
>    base-rate 0.9375 − mean-confidence 0.2523 (pure under-confidence).
> 3. **`causal_use` ≡ `answer_survived`** on all 48 rows: the miscalibration
>    stands on *delivery* (measured retrieval success); the graphs upgrade 20
>    rows to "delivered **and** used" but add no non-causal negative.
> 4. Single template, single model (Qwen3-4B), pA≈0 assumed (not measured here).

### 4. The delivery-balanced bench — and why recalibration is not the fix (#239)

The §3 conclusion said a production refit needs a *delivery-balanced* bench with
graph-measured non-causal deliveries. We built one: 72 needles in three cells —
**answerable** (gold + distractors → causal=1), **held-out** (distractors only,
gold *not* ingested → causal=0), and **competition** (gold + a wrong-answer
sibling, both delivered → graph-decides which the model uses). To label what
retrieval *finds* we had to disable the abstain ratio-gate (below); 24 needles
were graph-measured on Qwen3-4B and the rest imputed by cell.

**What the graphs establish (robust):**

- The answerable⇒causal / held-out⇒not-causal proxy validated cleanly: **7/7**
  graphed answerable are causal, **0/5** graphed held-out are (imputation licensed).
- **The shipped logistic emits 0/72 KnowBlocks even on a balanced bed** — max
  confidence 0.419 < emit_floor 0.45. Zero recall is a property of the operating
  point, needs no labels, and reproduces §3.
- The **competition cell is the graph-measured "delivered-but-not-used" evidence
  §3 lacked**: the gold was delivered in all 12, yet the model used it in only
  **5/12**. Of the 7 misses, **4 used the competitor** (mis-attribution to the
  wrong delivered fact) and 3 used neither. So `found=true ≠ used` is now proven
  on genuinely ambiguous deliveries, not inferred.

**What does *not* survive scrutiny (an adversarial re-derivation caught it):**
A refit against the full labels *appears* to help — AUC 0.616→0.675 (LOOCV),
`coordinate_confidence` up-weighted β 0.30→**1.87**, ECE 0.220→0.087. **This is
a circularity artifact.** 48/72 labels are *imputed from cell identity*, and
coord separates the cells by construction (answerable mean 0.562 vs held-out
0.289 — answerable delivers a gold file whose path-grain matches the query;
held-out doesn't). The logistic simply re-learns the imputation rule:

| metric | on 48 imputed rows | on 24 graph-measured rows |
|---|---|---|
| coord single-feature AUC → causal | 0.809 | **0.462** (below chance) |
| refit coord β | +1.87 | **−0.06** |
| shipped-conf AUC → causal | — | 0.535 |
| competition-cell coord AUC (n=12, non-circular) | — | **0.529** (chance) |

On every genuinely-measured, non-constructed subset, **neither coord nor the
shipped confidence beats chance.** The ECE "improvement" is likewise a base-rate
artifact (a constant-0.5 predictor scores ECE 0.014 on this balanced bed).

**Conclusion for #239 (stronger than a refit would have been):** the shipped
five know-features (top_score, score_gap, agree, coordinate_confidence,
freshness) carry **no measurable causal-use signal on ambiguous deliveries**, so
**recalibrating them cannot fix the know/miss confidence.** The fix requires (a)
a genuine *answer-presence / answer-use* feature — an answerability or NLI check
on the delivered span, not a retrieval-strength proxy — and (b) the
operating-point repair (the confidence ceiling ~0.42 sits below floor 0.45, so
recall is structurally zero regardless of ranking).

**Secondary finding — the abstain ratio-gate.** Under RRF the abstain tier fires
on `top_score/mean < 1.8` and returns `<helix:no_match>`, suppressing *all*
context. On this clean bed it is rare (4/72 needles, 3 with the gold at rank-1)
but it is 100% of the observed delivery failures — a second, ratio-based path
(distinct from the logistic) by which under-separated retrieval withholds correct
rank-1 content. Worth folding into the same answer-presence rework.

## Interpretation for #239

`found=true` is a *delivery* claim; **causal use is what it should predict.** The
instrument supplies that per-turn mechanistic label, and two beds turn it into a
verdict on the shipped logistic: at its operating point it says "I don't know"
about facts it has both retrieved and demonstrably used (§3), and — the sharper
result — its features carry **no measurable causal-use signal on ambiguous
deliveries** (§4). The §3 direction (fix the β1 sign, clear the intercept/floor)
still holds as an *operating-point* repair, but §4 shows it is **not sufficient**:
a causal-use refit only *appears* to help through a cell-imputation circularity,
and on genuinely-measured deliveries no current feature beats chance.

So the actionable path for #239 is: (a) **repair the operating point** — the
confidence ceiling (~0.42) sits below emit_floor (0.45), so recall is
structurally zero; lower the floor / raise the intercept and un-invert β1 so the
gate *can* fire; and (b) **add an answer-presence signal** — an answerability or
NLI check on the delivered span — because the retrieval-strength features cannot
tell a causally-used delivery from an ignored one. Recalibrating the existing
five features is a dead end for the *discrimination* half of the problem. The
same answer-presence signal should also gate the abstain tier (§4), which today
suppresses correct rank-1 context on a bare ratio.

## Limitations

- **gemma-2-2b** is the only model with an open attribution-graph API. It is
  small (strong priors compete with context) and not the helix serving model —
  faithfulness numbers are model-relative, not a universal constant.
- **Synthetic single-token needles** (§1–2 N=6, §3 N=48, one template).
  Establishes the instrument + a calibration verdict, not a population estimate.
- **The §3 bed has no negatives to discriminate.** `causal_use` ≡
  `answer_survived` (delivered ⇒ used, 20/20); zero graph-measured
  delivered-but-unused turns, so it measures the *recall* arm only. §4 adds the
  negatives (the competition cell) but is itself **2/3 imputed** (48/72 labels by
  cell); every affirmative feature claim there collapses on the 24 graph-measured
  rows, and the competition AUC (n=12) is too small to be reliable — read §4 as a
  *negative* result (no causal-use signal found), not a measured feature ranking.
- ~~Anonymous rate limit caps batch size~~ — **resolved** by self-hosting
  circuit-tracer locally (unlimited, no egress). Long prompts are slow on 12 GB
  (shared-memory spill) but complete.
- Attribution-graph faithfulness is a *mechanistic proxy*, not a causal
  intervention (no activation patching). Directionally trustworthy; not a proof.

## Reproduce

```
benchmarks/faithfulness/faithfulness_circuit_tracer.py    # gen_graph / backward_influence /
                                                          # answer_logit_node / faithfulness
benchmarks/faithfulness/needle_faithfulness_experiment.py # ideal-context 6-needle run
benchmarks/faithfulness/real_helix_faithfulness.py        # real build_context() run
```

Local self-hosted graph-gen (the real-helix run): isolated venv on
`F:/Projects/np-graph/` — `faith_local.py` (local `gen_graph` monkeypatched into
the validated scoring), `faith_local_realhelix.py` (stage 2), and
`dump_expressed.py` (stage 1, helix env → `expressed_context` JSON). Two envs
because circuit-tracer and the helix model stack pin conflicting deps.

§3 #239 pipeline: `np-graph/needles_239.py` (48-needle graded-distractor bed
spec), `scratchpad/build_bed_239.py` (helix env → real-file ingest + stage-1
features/confidence JSON), `np-graph/faith_239.py` (Qwen3-4B causal-use graphs,
`--ids` subset + resume), `scratchpad/refit_239.py` (helix env → shipped-vs-refit
comparison). Data artifacts: `np-graph/needles_239_stage1.json`,
`needles_239_faith.json`.

§4 delivery-balanced pipeline: `np-graph/needles_239b.py` (72-needle 3-cell bed
spec), `scratchpad/build_bed_239b.py` (helix env; `--abstain` toggles the ratio-
gate), `np-graph/faith_239b.py` (competition-aware graphs — scores gold *and*
competitor), `scratchpad/refit_239b.py`. Data: `needles_239b_stage1.json`,
`needles_239b_faith.json`. The circularity check (imputed-vs-measured coord AUC)
is the load-bearing analysis — recompute it before trusting any refit number.

Egress: synthetic content only (public repo facts + fictional "Redwood
Inference" ERB facts). Self-hosting keeps everything local (no S3, no key).

## Next

1. ~~Complete real-helix faithfulness~~ — **DONE** (6/6 causal, local).
2. ~~Know-confidence vs causal-use~~ — **DONE** (§3: 48-needle bed, 20/20 causal,
   the ≈0-recall verdict + the β1-sign / intercept / training-label diagnosis).
3. ~~Delivery-balanced bench + production refit~~ — **DONE** (§4). Result: a
   recalibration is *not* the fix — the five features carry no causal-use signal
   on measured/ambiguous deliveries (the apparent refit win was a cell-imputation
   circularity). The competition cell supplies the delivered-but-unused negatives.
4. **Prototype an answer-presence feature** (the actual #239 lever): an
   answerability / NLI score on the delivered span vs the query, added as a sixth
   know-feature. Re-run the §4 bench (its competition cell + measured labels are
   the honest test set) and see if *this* feature clears chance where coord/
   top_score do not. Pair with the operating-point repair (floor/intercept/β1).
5. ~~Scale N / non-arbitrary needles~~ — **DONE 2026-07-08** (N=24, 5 semantic
   families, answers tokenizer-verified in both models, **23/24 causal-use** on
   Qwen3-4B, mean faith 0.585 / lift 0.754; the one miss is behavioral
   prior-suppression, not mechanistic — see
   [2026-07-08-scale-n-faithfulness-239.md](2026-07-08-scale-n-faithfulness-239.md)).
   Still open: **confirm on the helix serving model** (Qwen3-4B is the
   instrument, not the deployment target).
6. This stays the **yardstick** for the retrieval work (complement / DNA-pair
   dense re-embedding, ANN threshold): prove the model *causally uses*
   newly-retrieved content, not merely that helix delivered it.
