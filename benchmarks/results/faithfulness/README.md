# Faithfulness campaign raw data (#239)

Raw result artifacts from the 2026-07-07 circuit-tracer faithfulness campaign.
Imported 2026-07-08 from their original out-of-repo locations (the isolated
circuit-tracer venv at `F:/Projects/np-graph/` and two Claude session
scratchpads) so the campaign data survives those environments. File mtimes are
preserved from the original runs (all 2026-07-07).

Interpreting docs, in reading order:

1. [2026-07-07-faithfulness-circuit-tracer.md](../../../docs/research/2026-07-07-faithfulness-circuit-tracer.md) — the master doc (instrument, §1 pilot, §2 real-helix, §3 48-needle bed, §4 delivery-balanced bed)
2. [2026-07-07-answer-presence-spike-239.md](../../../docs/research/2026-07-07-answer-presence-spike-239.md) — the answer-presence spike (NO-GO as causal-use discriminator)
3. [2026-07-08-b1-operating-point-coupling.md](../../../docs/research/2026-07-08-b1-operating-point-coupling.md) — B1 operating-point sweep on these beds
4. [2026-07-08-scale-n-faithfulness-239.md](../../../docs/research/2026-07-08-scale-n-faithfulness-239.md) — §6 scale-N run (N=24, Qwen3-4B)

## Files

| file | rows | study | produced by |
|---|---:|---|---|
| `full_pilot_results.json` | 6 | §1 ideal-context pilot, local self-hosted rerun on gemma-2-2b (bare one-sentence facts; teal 0.057 / platinum 0.246 non-causal) | `external/faith_local.py` |
| `expressed_contexts.json` | 6 | §2 stage 1 — helix `build_context()` `expressed_context` dumps (all 6 `answer_survived`) | `external/dump_expressed.py` (helix env) |
| `realhelix_local_results.json` | 6 | §2 real-helix faithfulness on gemma-2-2b — 6/6 causal, mean lift 0.834 (teal 0.941, platinum 0.598) | `external/faith_local_realhelix.py` |
| `needles_239_stage1.json` | 48 | §3 graded-distractor bed — 5 know-features + confidence + `answer_survived` per needle | `external/build_bed_239.py` (helix env) |
| `needles_239_faith.json` | 48 | §3 causal-use labels — 20 stratified-hardest graph-measured on Qwen3-4B (20/20 causal), rest imputed | `external/faith_239.py` |
| `needles_239b_stage1.json` | 72 | §4 delivery-balanced 3-cell bed (30 answerable / 30 heldout / 12 competition), abstain ratio-gate OFF | `external/build_bed_239b.py` (helix env) |
| `needles_239b_stage1_abstainON.json` | 72 | §4 bed with the abstain ratio-gate ON (the 4/72 `<helix:no_match>` suppressions — §4 secondary finding) | `external/build_bed_239b.py --abstain` |
| `needles_239b_faith.json` | 72 | §4 causal-use labels — 24 graph-measured (7/7 answerable causal, 0/5 heldout, competition 5/12), rest imputed by cell | `external/faith_239b.py` |
| `spike_239b_results.json` | 72 | answer-presence spike — span-scorer scores/labels per cell (best C12 / G24 operating points) | `external/spike_239b_answerpresence.py` (helix env) |
| `scaled_results_qwen3_4b.json` | 24 | **§6 scale-N** — 24 needles on Qwen3-4B: 23/24 causal-use, mean faith 0.585, mean lift 0.754 | `external/faith_scaled.py` |
| `qwen_scaled.txt` | — | run log of the §6 scale-N run (per-needle timings ~65 s/graph, summary block) | `external/faith_scaled.py` (stdout) |

Schema note: per-needle rows carry `pA`/`pB` (P(answer) without/with injected
context), `lift = pB − pA`, `faith` (fraction of the answer logit's input
attribution on the injected answer token), `answer_is_top_driver`, `in_graph`,
and the composite `causal_use` (:= `in_graph AND lift ≥ 0.15 AND pB ≥ 0.30 AND
answer_is_top_driver`). Stage-1 files instead carry the five know-features,
the shipped `raw_confidence`, and delivery flags (`answer_survived`,
`competitor_survived`, `cell`).

These are frozen result artifacts — do not regenerate in place. Reruns should
land under a new dated filename. The runner scripts live in
[`benchmarks/faithfulness/external/`](../../faithfulness/external/README.md).
