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

Egress: synthetic content only (public repo facts + fictional "Redwood
Inference" ERB facts). Self-hosting keeps everything local (no S3, no key).

## Next

1. ~~Complete real-helix faithfulness~~ — **DONE** (6/6 causal, local).
2. **Scale N** and add non-arbitrary needles (facts the model *could* half-know)
   to map the faithfulness/prior boundary — now unlimited via self-hosting.
3. **Know-confidence correlation** — the #239 calibration payoff; needs the
   bigger ERB bed for confidence variance.
4. Once landed, this becomes the **yardstick** the retrieval work (complement /
   DNA-pair dense re-embedding, ANN threshold) is measured by: prove the model
   *causally uses* newly-retrieved content, not merely that helix delivered it.
