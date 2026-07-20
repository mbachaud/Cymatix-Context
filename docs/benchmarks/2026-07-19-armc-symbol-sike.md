# Arm C (WS2/WS3 symbol graph) — SIKE xl measurement, 2026-07-19

> Clears gate items 2–3 from PR #230's bench-gate comment (A3 fail-fast +
> "the real arm-C number"). Companion to
> `docs/benchmarks/2026-07-05-sike-bedsweep-issue-resolutions.md` (§5 A3, §6).
> Raw rows: `benchmarks/results/sike_symbol_arm_xl.json` (+
> `sike_symbol_arm_xl_run2pin.json`, the attribution cell).

## Setup

- **Build:** `feat/ws3-symbol-pagerank` @ post-rebase on master `ce34a4b`
  (WS2 #230 + WS3 #231, stacked). Served from the branch worktree; scoring
  harness + needle set from the main checkout (identical to Run-1/2).
- **Bed:** `genomes/bench/sike_beds/xl_symbol.db` — byte-copy of the Run-2
  decontaminated `xl.db` (41,898 genes) + symbol graph backfilled with the
  branch's own extractor (`scripts/bench_chain/s4_symbol_backfill.py`,
  mirrors `_emit_symbol_graph` per source file): **48,541 symbol_defs rows,
  2,089 SYMBOL_REF edges**. No re-ingest → gene content/IDs byte-identical
  to Run-2's bed.
- **Probe:** `docs/benchmarks/helix_probe_symbol.toml` **fixed per gap A3**
  (now = lexical probe + `[ingestion] symbol_graph = true` +
  `[retrieval] symbol_expansion_cap = 8`; previously its keys silently
  no-opped on master). Runner `scripts/bench_chain/s4_symbol_arm.py` carries
  the **fail-fast capability asserts** the gate required: (a) served-tree
  config loader round-trips both symbol keys, (b) bed has >0 symbol_defs and
  >0 SYMBOL_REF edges, (c) server health. All three passed on every cell.
- **Cells:** `symbol_expansion_cap ∈ {0, 8}` × `fusion ∈ {rrf, additive}`,
  depth 48, all 50 needles, retrieval-only (`/context`, decoder off,
  `ignore_delivered`). cap=0 = symbol expansion off = lexical-equivalent
  serve of the same build+bed, so the cap contrast is the pure arm-C effect.

## Results

| cell | gold_delivered | content_has_answer | lat mean |
|---|---|---|---|
| cap=0 rrf | **0.64** | 0.82 | 5.7 s |
| cap=8 rrf | **0.62** | 0.80 | 4.9 s |
| cap=0 additive | **0.56** | 0.76 | 5.2 s |
| cap=8 additive | **0.56** | 0.78 | 5.0 s |

**Arm-C effect (cap=8 − cap=0): rrf −0.02, additive ±0.00.** Per-needle:
zero needles gained; rrf lost `helix_port` (gold) and
`helix_max_genes_per_turn` (content) — expansion candidates displaced golds
from the delivered top-K. Additive: no gold flips.

**Gate verdict (ROADMAP decision rule 2): C < +1pp on both fusions →
no merge-default-on.** Not a config no-op this time — the asserts prove the
knobs were live and the graph populated; the mechanism simply fired rarely
(2,089 edges over a docs-heavy bed) and, when it fired, cost delivery slots.

## Drift note (cross-run comparability)

cap=0 baselines sit below the Run-2 references (rrf 0.64 vs 0.74; additive
0.56 vs 0.62). Attribution so far:

- **Not** the graduated serving defaults: a pin cell (blend_mode=legacy +
  empty `rerank_combinator_by_class`, i.e. Run-2-era knobs) scored **0.56**
  — worse than current defaults' 0.64, consistent with the #282/#295
  graduation receipts (the new defaults help or are flat here).
- **Not** symbol-table contamination of the baseline: the only unfiltered
  query-time reader of `gene_relations` (`tie_break._fetch_nli`) ignores
  relation values other than NLI codes, and the WS2 reader is cap-gated.
  (Latent nit: `_fetch_nli` should filter `relation IN (1,2)` — a real NLI
  edge sharing an endpoint pair with a SYMBOL_REF edge would be clobbered in
  its dict. Not material on this bed, which has no NLI edges.)
- Residual ≈ −0.10 (rrf) traces to two weeks of master evolution between
  Run-2 (~`f2b...`, 2026-07-07) and `ce34a4b` (merges #247–#304, including
  the ranking-adjacent bugfix wave #299/#304). The arm-C verdict is
  **within-run** (same bed, same build, knob-only contrast) and is not
  affected; a fresh Run-1-style baseline revalidation on current master is
  the open follow-up before quoting absolute xl numbers anywhere.

## Disposition

Per the decision rule the branches do **not** merge default-on. Options
recorded for the maintainer: (a) merge dark (ws3 already ships
`symbol_graph = false` default; fix the two review findings from the
recorded #230 review first — symbol writes outside the #300 write lock;
no orphan cleanup), or (b) hold for the originally-planned external
code-retrieval arm (ContextBench/RepoBench per
`docs/benchmarks/2026-07-01-external-bench-run-plan.md` §2) — the SIKE bed
is docs-heavy and structurally a weak test of a code-symbol mechanism; a
code-heavy bed is where arm C could still earn default-on.
