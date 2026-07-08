# Scale-N faithfulness — 24 diverse needles on Qwen3-4B, 23/24 causal-use (#239 §6, first half)

**Date:** 2026-07-08 (run executed 2026-07-07, `scaled_results_qwen3_4b.json` 14:52)
**Branch:** research/faithfulness-semantic-reach
**Status:** DONE for the **scale-N / needle-diversity half of §6** — the instrument's
verdict survives 4× the needle count, 5 semantic families, and a model swap.
Still open from §6: **(a)** confirmation on the helix *serving* model and
**(b)** the deferred **B1+B3 joint re-ship** (see "What §6 still leaves open").
**Parent:** [`2026-07-07-faithfulness-circuit-tracer.md`](2026-07-07-faithfulness-circuit-tracer.md)
"Next" item 5 (*scale N / non-arbitrary needles*).

---

## TL;DR

The §1–§2 pilot results rested on 6 hand-written needles and one small model
(gemma-2-2b) — open to the objection that the needles were arbitrary,
hand-tuned survivors. This run scales the ideal-context experiment to **24
needles across 5 semantic families × 5 sentence templates** on a stronger
instrument (**Qwen3-4B**, `mwhanna/qwen3-4b-transcoders`, self-hosted
circuit-tracer), with every answer **verified single-token in both the
gemma-2-2b and Qwen3-4B tokenizers** (re-verified at write-up time) so no
needle can fail for tokenizer reasons.

Result, one clean pass, no per-needle tuning (all numbers recomputed from
[`scaled_results_qwen3_4b.json`](../../benchmarks/results/faithfulness/scaled_results_qwen3_4b.json)):

| quantity | value |
|---|---|
| needles completed | 24/24 |
| pA (no-context hallucination) | **0.000 on all 24** |
| answer in-graph / answer-is-top-driver | **24/24 / 24/24** |
| **causal-use** (`in_graph ∧ lift ≥ 0.15 ∧ pB ≥ 0.30 ∧ top-driver`) | **23/24 = 0.958** |
| mean / median faith | **0.585** / 0.584 (range 0.493–0.697) |
| mean / median lift (= pB, since pA≡0) | **0.754** / 0.828 |

**Mechanistic faithfulness is 24/24** — the injected answer token is the answer
logit's #1 input driver on every single needle. The sole causal-use failure
(`spire`→plum, pB 0.252 < 0.30) is **behavioral prior-suppression, not a
mechanistic failure**: the model read the token (top driver, faith 0.5435) but
its prior held the emission probability under the bar. Same failure shape as
§1's sentinel/platinum.

## Method

Same instrument, metric code, and causal-use bar as the
[master doc](2026-07-07-faithfulness-circuit-tracer.md): per needle, condition
A (question only) vs condition B (one-sentence context + question), attribution
back-propagated from the **answer logit** (not the argmax), `faith` = fraction
of that logit's input attribution on the injected answer token.

This is the **ideal-context** configuration (§1 scaled up), not the real-helix
`expressed_context` path (§2) — it stresses the instrument and the needle set,
not helix delivery.

**Needle construction**
([`scaled_needles.py`](../../benchmarks/faithfulness/external/scaled_needles.py)):
one uniform template, `"Redwood Inference <doc>: the <subject> <verb-phrase>
<answer>."`, question = the same sentence truncated right before the answer, no
trailing space. Five families give five verb-phrases and answer pools:

| family | template tail | n | mean pB | mean faith | causal |
|---|---|---:|---:|---:|---|
| animals | "…is nicknamed the ___" | 4 | 0.762 | 0.614 | 4/4 |
| materials | "…is built on ___" | 6 | 0.813 | 0.606 | 6/6 |
| colors | "…accent is ___" | 6 | 0.919 | 0.557 | 6/6 |
| fruits | "…is codenamed ___" | 6 | **0.469** | 0.572 | **5/6** |
| elements | "…is powered by ___" | 2 | 0.926 | 0.584 | 2/2 |

All associations are arbitrary (a base model cannot know that the "Spire graph
engine" is codenamed plum), so pA must be ≈0 — and measured pA **is exactly 0.0
on all 24**. Answer candidates were pre-filtered to single-token-with-leading-
space continuations by
[`tok_probe.py`](../../benchmarks/faithfulness/external/tok_probe.py); at
write-up time all 24 were re-checked single-token in **both** tokenizers
(gemma-2-2b and Qwen3-4B), so the set runs cross-model unchanged.

**Run** ([`faith_scaled.py`](../../benchmarks/faithfulness/external/faith_scaled.py),
np-graph venv, RTX 3080 Ti 12 GB):

```bash
python -X utf8 faith_scaled.py --model Qwen/Qwen3-4B \
    --transcoders mwhanna/qwen3-4b-transcoders --needles scaled --mfn 1024 --bs 24
```

bf16 + `lazy_encoder` + `offload="cpu"`, ~65 s/graph, 24 needles in ~26 min,
single pass, zero errors
([run log](../../benchmarks/results/faithfulness/qwen_scaled.txt)).

## Per-needle results

pA = 0.000 everywhere, so lift = pB. Top-driver = "the injected answer token is
the answer logit's #1 input driver."

| needle | answer | pB | faith | top-driver | causal |
|---|---|---:|---:|---|---|
| beacon | tiger | 0.758 | 0.619 | ✓ | ✓ |
| cascade | eagle | 0.938 | 0.614 | ✓ | ✓ |
| atlas | shark | 0.836 | 0.595 | ✓ | ✓ |
| sentinel | wolf | 0.516 | 0.627 | ✓ | ✓ |
| vault | quartz | 0.910 | 0.697 | ✓ | ✓ |
| relay | granite | 0.738 | 0.622 | ✓ | ✓ |
| forge | copper | 0.934 | 0.596 | ✓ | ✓ |
| nova | marble | 0.820 | 0.570 | ✓ | ✓ |
| delta | bronze | 0.641 | 0.583 | ✓ | ✓ |
| echo | slate | 0.836 | 0.566 | ✓ | ✓ |
| pillar | violet | 0.910 | 0.544 | ✓ | ✓ |
| mesh | turquoise | 0.969 | 0.551 | ✓ | ✓ |
| pulse | amber | 0.965 | 0.602 | ✓ | ✓ |
| grove | crimson | 0.949 | 0.493 | ✓ | ✓ |
| ember | olive | 0.973 | 0.616 | ✓ | ✓ |
| fern | azure | 0.746 | 0.534 | ✓ | ✓ |
| prism | mango | 0.613 | 0.604 | ✓ | ✓ |
| orbit | peach | 0.404 | 0.557 | ✓ | ✓ |
| halo | cherry | 0.352 | 0.585 | ✓ | ✓ |
| comet | lemon | 0.590 | 0.567 | ✓ | ✓ |
| ridge | grape | 0.602 | 0.578 | ✓ | ✓ |
| **spire** | **plum** | **0.252** | 0.543 | ✓ | **✗** |
| zephyr | neon | 0.902 | 0.596 | ✓ | ✓ |
| onyx | helium | 0.949 | 0.572 | ✓ | ✓ |

### The spire→plum failure — behavioral, not mechanistic

`spire` fails only the `pB ≥ 0.30` behavioral bar (0.252) and the argmax check;
mechanistically it is indistinguishable from the passes: the answer is
in-graph, it is the answer logit's **#1 input driver**, and its faith (0.5435)
sits inside the pack's range (0.493–0.697). The whole *fruits/codenamed* family
is behaviorally hardest (mean pB 0.469 vs 0.76–0.93 elsewhere; halo/cherry
0.352 and orbit/peach 0.404 pass the bar narrowly) — "codenamed ⟨fruit⟩"
apparently invites the strongest prior competition, and plum drew the deepest
suppression. This is the same shape as §1's sentinel/platinum, and §2 already
showed the remedy on gemma: injecting helix's richer *real* delivery format
(the `<GENE>` wrapper + legibility headers) flipped both §1 prior-suppressed
needles to strongly causal. Untested here, but there is no reason to expect
spire to behave differently — the mechanistic read is already there.

Note the bar-sensitivity honestly: at `pB ≥ 0.25` this run reads 24/24; at the
shipped 0.30 bar it reads 23/24. We keep the master doc's bar unchanged.

## Interpretation for #239

1. **The "arbitrary needles" objection is answered.** The pilot verdicts do not
   depend on 6 hand-tuned needles: 4× the count, 5 distinct semantic families
   and templates, tokenizer-verified answers, zero per-needle adjustment, one
   pass — and the mechanistic read is **24/24**, causal-use 0.958.
2. **The instrument strengthens with model scale.** On gemma-2-2b, bare
   one-sentence context lost 2/6 needles to prior competition (§1) and mean
   ideal-context faith was 0.335. On Qwen3-4B the same configuration reads mean
   faith **0.585** (attribution concentrates ~1.7× harder on the injected
   token) and loses only 1/24 behaviorally. Prior competition shrinks — it does
   not vanish — as the model grows.
3. **Family matters, mechanism doesn't budge.** Behavioral suppression is
   family-structured (fruits-as-codenames ≪ colors/materials), but faith is
   flat across families (0.56–0.61). The mechanistic metric is measuring
   something the behavioral one can't see — exactly why the know-gate work
   (B1/B3) keys on causal-use rather than emission probability.

## What §6 still leaves open

- **(a) Confirm on the helix serving model.** Qwen3-4B is the *instrument* (it
  has released transcoders), not the deployment target. The faithfulness
  numbers are model-relative; the serving-model confirmation remains to be run.
- **(b) The deferred B1+B3 joint re-ship.** Per
  [`2026-07-08-b1-operating-point-coupling.md`](2026-07-08-b1-operating-point-coupling.md)
  and the [answer-presence spike](2026-07-07-answer-presence-spike-239.md), the
  monotone-constrained **B1 beta/floor re-fit** and the **B3 answer-absence
  gate** couple (every operating point that restores recall also fires on
  ≥40% of answer-absent queries) and must ship **together**, fit on a
  **scale-N *delivery-balanced* bench**. This run does *not* supply that bench:
  it scales the **answerable, ideal-context** cell only. The heldout
  (answer-absent) and competition cells still need the same 4×+ scaling before
  the joint re-fit is licensed.

## Limitations

- **Ideal context, not helix delivery.** One-sentence injected facts (§1
  configuration); no retrieval, splice, or legibility headers in the loop. The
  real-helix path was measured separately (§2, 6/6 on gemma-2-2b) and is what
  (a) should re-measure on the serving model.
- **Still synthetic single-token needles** — diverse, but one fictional
  universe ("Redwood Inference"), uniform syntax per family, and a
  single-token answer constraint the instrument requires.
- **Single model per run.** These numbers are Qwen3-4B-relative. Cross-model
  invariance is suggested by gemma-vs-Qwen agreement on the mechanistic read,
  not established.
- Attribution-graph faithfulness remains a **mechanistic proxy**, not an
  intervention (no activation patching).
- All 24 rows are graph-measured (no imputation), but N=24 still gives a wide
  CI on the causal-use rate (0.958 ± ~0.08 at 95%).

## Reproduce

```
benchmarks/faithfulness/external/scaled_needles.py   # the 24-needle set (data)
benchmarks/faithfulness/external/faith_scaled.py     # model-parametrized runner (np-graph venv)
benchmarks/faithfulness/external/tok_probe.py        # single-token answer-pool verification
benchmarks/results/faithfulness/scaled_results_qwen3_4b.json   # frozen results
benchmarks/results/faithfulness/qwen_scaled.txt                # run log
```

Environment split and hardcoded-path caveats:
[`benchmarks/faithfulness/external/README.md`](../../benchmarks/faithfulness/external/README.md).
All campaign raw data:
[`benchmarks/results/faithfulness/README.md`](../../benchmarks/results/faithfulness/README.md).
