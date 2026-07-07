# Faithfulness of injected context — a Circuit-Tracer instrument for the #239 know/miss contract

**Date:** 2026-07-07
**Status:** instrument validated; ideal-context calibration done; real-helix
retrieval-preservation measured (6/6); real-helix *faithfulness* measurement
pending (Neuronpedia anonymous quota — needs API key or reset).
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
- **Faithfulness** (the model's job): **pending** — all 6 attribution graphs
  returned HTTP 429 (anonymous Neuronpedia quota exhausted after ~30 calls this
  session). Method is proven end-to-end on the ideal-context run above; this is
  a quota wall, not a design gap. Re-runs with `NEURONPEDIA_API_KEY` set.

The legibility headers make the real-helix faithfulness question sharper than
the ideal-context one: injected context now carries `fired=…` metadata, so the
retargeted attribution will tell us whether the model reads the *answer token in
the GENE body* or is distracted by header noise.

## Interpretation for #239

`found=true` is a *delivery* claim. **Causal use is what it should predict.**
The instrument gives, for the first time, a per-turn mechanistic label
("did the model read the delivered answer?") to calibrate the KnowBlock logistic
against — a ground truth the confidence fit (PR #249, β1<0 anti-signal, ECE
0.74→0.04) never had.

The direct lever: pair helix's own `_compute_know_or_miss_block(…).confidence`
with the measured faithfulness per turn and ask **does high know-confidence
predict high causal use?** On the 6-fact bed confidence is saturated (retrieval
is trivial → no variance), so this needs a bigger, noisier corpus with a mix of
found/not-found turns — where it converges with the ERB semantic retrieval work.

## Limitations

- **gemma-2-2b** is the only model with an open attribution-graph API. It is
  small (strong priors compete with context) and not the helix serving model —
  faithfulness numbers are model-relative, not a universal constant.
- **Synthetic single-token needles, N=6.** Establishes the instrument, not a
  population estimate.
- **Anonymous rate limit** (~15–20 calls / rolling window) caps batch size;
  keyed runs remove this.
- Attribution-graph faithfulness is a *mechanistic proxy*, not a causal
  intervention (no activation patching). Directionally trustworthy; not a proof.

## Reproduce

```
benchmarks/faithfulness/faithfulness_circuit_tracer.py    # gen_graph / backward_influence /
                                                          # answer_logit_node / faithfulness
benchmarks/faithfulness/needle_faithfulness_experiment.py # ideal-context 6-needle run
benchmarks/faithfulness/real_helix_faithfulness.py        # real build_context() run
```

Egress: synthetic content only (public repo facts + fictional "Redwood
Inference" ERB facts). Anonymous graphs save to a public S3 bucket; set
`NEURONPEDIA_API_KEY` (env var, never in source) for private/higher-limit runs.

## Next

1. **Complete real-helix faithfulness** — re-run action 3 with the API key (or
   after quota reset). Report causal-use | survived.
2. **Scale N** and add non-arbitrary needles (facts the model *could* half-know)
   to map the faithfulness/prior boundary.
3. **Know-confidence correlation** — the #239 calibration payoff; needs the
   bigger ERB bed for confidence variance.
4. Once landed, this becomes the **yardstick** the retrieval work (complement /
   DNA-pair dense re-embedding, ANN threshold) is measured by: prove the model
   *causally uses* newly-retrieved content, not merely that helix delivered it.
