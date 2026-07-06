# Phase 0 re-metric probe — pre-registration (2026-07-06)

**Status:** pre-registered, NOT executed. Execution is gated behind the
Phase 1a whitening A/B result per council move #5 (*"Only then, the
re-scoped Phase 0 re-metric probe… pre-registered against whitened
BGE-M3 on a pinned fp16 model with a real parent — costed as research"*,
docs/councils/2026-07-06-jspace-roadmap-council.md).

## What this is — and is not

This probe measures whether an **activation-variance re-metric**
(windowed / CAZ pooling over a real decoder's fp16 activations) produces
document/query representations that retrieve better than the incumbent
BGE-M3 dense arm on the committed labeled harness. Per the council's
re-scoping it is **not called J-space**: the true Jacobian **J-lens
computation is a separate, unbudgeted line** — Helix has **no
decoder-fp16 capture path** today, and nothing in this probe pretends
otherwise. (The browser-driven Neuronpedia faithfulness runbook,
`docs/benchmarks/2026-07-06-faithfulness-experiment-runbook.md`, is a
different experiment feeding #239 and stays manual.)

## Baseline arm (fixed by Phase 1a's measured result)

Phase 1a (`benchmarks/ab_whitening_dense.py`, artifact
`ab_whitening_dense_xl_clean.json`) decides the incumbent:

- **RESOLVED (2026-07-06, n=1000):** whitening lost on every rank
  metric (retrieval@1 −0.7pp, @10 −3.3pp, MRR −0.016, AUC
  0.912 → 0.868) and won only μ+3σ threshold clearance — see
  `2026-07-06-phase1a-whitening-ab-results.md`. The control arm is
  **raw cosine** BGE-M3.

Either way the comparison is **measured on the same labels**, not
assumed from the Anthropic paper or any Deep-Research synthesis
(council: nothing from those sources is load-bearing until
independently verified).

## Pinned treatment arm

- **Model:** an open-weights decoder small enough for local fp16
  capture on the available 12 GB card (RTX 3080 Ti) — target class
  Qwen3-4B (fp16, ~8 GB); **pin the exact HF revision hash in the
  execution PR** before any measurement. "Real parent" means the
  activations come from an actual production-class decoder, not a
  proxy encoder.
- **Representation:** hidden-state activations from a pinned middle
  layer (select once on a 200-doc dev slice, freeze before the full
  run), pooled two ways as the treatment variants:
  1. **windowed pooling** — variance-weighted mean over fixed token
     windows (window=64, stride=32);
  2. **CAZ pooling** — activation-variance z-scored channels,
     aggregated per document.
- **Passages:** the same ≤2000-char strand cap the BGE-M3 ingest path
  uses (`bgem3_codec.PASSAGE_CHAR_CAP`) so document granularity is
  identical across arms.
- **Queries:** encoded through the same capture path (same layer, same
  pooling) — no mixed-space tricks.

## Labels + metrics (identical to Phase 1a)

- Labels: `located_n1000` JSONL on `genomes/bench/matrix/xl_clean.db`
  (planted_gene_id ground truth, 4-axis locator queries, seed 42).
- Metrics: retrieval@1/@5/@10, MRR, gold-pair vs random-pair AUC
  (200k seed-pinned random pairs), margin-over-random (μ+3σ) threshold
  clearance.

## Decision rules (pre-committed)

- **Advance** (Phases 2/3 unblock): treatment ≥ **+5pp retrieval@1**
  over the control arm AND gold-vs-random AUC not lower.
- **Kill** (defer Phases 2/3; Phase 5 remains a labeled escape hatch
  only): treatment < **+2pp retrieval@1**, or AUC regression > 0.02.
- Between +2pp and +5pp: report as inconclusive; a second bed
  (sike_beds/xl.db) replication decides.
- No post-hoc metric additions; anything else observed goes in an
  exploratory appendix, not the decision.

## Cost estimate (research line)

- Capture: 41,803 docs × 1 fp16 forward (≤512 tokens) ≈ 2–4 h on the
  3080 Ti; activations at one layer, pooled on the fly (no raw dump) —
  storage ~350 MB per pooling variant.
- Queries: 1,000 forwards ≈ minutes.
- One engineer-day of harness code (extend `ab_whitening_dense.py`'s
  rank/separation functions — they are representation-agnostic).

## Explicitly out of scope

- Any Jacobian / J-lens computation (no capture path; separate line).
- Any change to the shipped retrieval pipeline (research branch only).
- Phase 2/3 tier work (`jspace` retrieval tier) — dead weight until
  this probe's decision rule fires **Advance** (and note the tier is
  only live at all because the fusion default flipped to rrf, PR #247).
