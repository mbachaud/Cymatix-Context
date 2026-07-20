# Code-track regression log + knob-impact map

Living record of ContextBench Step-0 results by build/config, and which knobs
move which arm/metric. All numbers: `gold_smoke_4repo.parquet` (26 tasks),
official evaluator, lexical config, SEMA-off, micro-avg over the common task set.
Headline metric = **packet line recall** (the production delivery path).

## Results history (packet line recall unless noted)

| build / config | packet line_R | fp@27k line_R | fp@8k line_R | notes |
|---|---|---|---|---|
| BM25:27k (foil) | 0.484 | — | 0.314@8k | reproduces frozen baseline exactly |
| regex chunking (`m224`) | 0.655 | 0.549 | — | no tree-sitter; pre-AST |
| greedy-AST | 0.662 | 0.626 | 0.134 | master tree_chunker (top-level greedy + char-cut) |
| **cAST byte-exact** (#228) | **0.804** | 0.679 | 0.222 | recursive split-then-merge; Phase-1 win |
| WS2 symbol_graph, **unbounded** (#230) | 0.825 | **0.538** | 0.212 | +packet, **−14pp fp@27k** (unranked expansion dumps into budget) |
| **WS3 cap=8 (PageRank-ranked)** | **0.825** | **0.605** | 0.212 | holds packet, recovers +6.7pp fp@27k; **best sym_R 0.834**; precision recovered |

Key transitions:
- regex → cAST: **+15pp packet** (the chunking lever).
- cAST → WS2-unbounded: **+2.1pp packet** but **−14pp fp@27k** (expansion helps curated assembly, hurts budget-fill).
- WS2-unbounded → **WS3 cap=8: packet recall unchanged (0.825), symbol recall best-in-class (0.834), precision recovered (0.018→0.020), and fp@27k recovered +6.7pp (0.538→0.605).** Net improvement over unbounded; the cap + centrality ranking works.

**Verdict (Phase 2a):** net win. The packet (production path) keeps its WS2 gain with better symbol recall + precision; the fingerprint budget-fill arm recovers most of its regression. Residual fp@27k gap to WS2-off (0.605 vs 0.679) is the budget-fill arm still paying a small expansion cost — addressable by Phase 2b (PageRank-ordered budget trim) **if the fingerprint arm matters**, but the production packet is fully won, so 2b stays deferred per the council. The cap (8) is a principled default; tune via a sweep on a **held-out** corpus, not this smoke set.

## Knob → impact map

| knob | default | primarily moves | neighboring impacts / cautions |
|---|---|---|---|
| `[ingestion] symbol_graph` | true | packet recall ↑; fp recall ↓ when unbounded | ingest time (edge emission), `symbol_defs`/`gene_relations` size |
| `[retrieval] symbol_expansion_cap` | 8 | **fp regression lever**: ↑cap → recall↑ but budget-fill precision↓ (0 disables, <0 unbounded) | interacts with token budget; ranked by PageRank centrality |
| `[ingestion] sema_embed_on_ingest` (#227) | true | — (off = no MiniLM load) | multi-worker bench OOM if true at scale; TCM uses text fallback when off |
| cAST `max_chars` | 4000 | chunk granularity → recall/precision | larger → fewer, bigger chunks; below candidate size = no chunking |
| PageRank `damping` (WS3) | 0.85 | centrality distribution | minor; rarely tuned |
| PageRank personalization (query 10×, session 50×) | Aider values | which defs survive the cap | **overfit risk** — do not tune on the smoke set; validate held-out |
| code-query gating (classifier) | on | confines symbol effects to code | prose non-regression depends on classifier accuracy |

## Method notes (reproducibility)
- Builds are isolated on one venv via `PYTHONPATH` override of the editable `.pth` (baseline) vs the WS branch (under test) — same deps, same config, only the code differs.
- Bench: `cb_helix_pred.py --tag <build> --config helix_probe_nosema.toml --workers 3`; scored by `cb-step0` venv over the common instance-id intersection.
- A worker OOM (`BrokenProcessPool`) yields a partial pred set; the common-id intersection keeps comparisons fair.
- **2026-07-01 environment note:** rig is multi-tasking today (baseline ~14% CPU / 59% RAM / 24% GPU from concurrent sessions). Per-query latency captured today is load-annotated, not clean-room — fine for recall/precision comparisons, but do **not** decide latency-sensitive knobs (Wall-2 A/B #206, PageRank query-budget checks) on today's numbers without an idle-rig re-run. OTel latency histograms (`helix_context_latency_seconds`, `helix_pipeline_stage_seconds`) captured today inherit the caveat.

## Council gating experiments (2026-06-28)

**Ablation — PageRank vs in-degree (smoke, common 26):** PageRank packet 0.825 / fp@27k 0.605 vs in-degree 0.809 / 0.580 vs WS2-off 0.804. PageRank beats in-degree (+1.6pp packet, +2.5pp fp); in-degree captures only ~¼ of the gain. → **PageRank earns its place** (the "in-degree suffices" hypothesis is refuted).

**Held-out — WS3-on vs WS2-off (sympy, unseen, common 20):** packet 0.598 vs 0.591 (**+0.7pp**, vs +2.1pp on the tuning smoke); fp@27k 0.461 vs 0.537 (**−7.6pp**). → **The symbol-graph gain is small and corpus-sensitive and does not robustly survive held-out**; it can regress the fingerprint on a new corpus.

**Decision:** **dark-ship WS2/WS3** — `[ingestion] symbol_graph` default flipped to **false**. The feature stays fully available (opt-in) but is not an always-on production default, because the gain is marginal + inconsistent across corpora. cAST (WS1) + #227 remain default-on (cAST's +15pp is robust and corpus-independent). Revisit default-on only after a broader held-out sweep.

> **Addendum (2026-07-20):** the broader held-out sweep happened and **reverses the held-out read above**. The arm-C ContextBench held-out re-run cleared the merge gate: **packet +2.8pp (line) / +3.8pp (sym)** — see [2026-07-20 armc-contextbench-heldout](2026-07-20-armc-contextbench-heldout.md). The 2026-06-28 "small / corpus-sensitive / does not robustly survive held-out" conclusion is superseded for the packet metric. **Dark-ship nonetheless remains the intentional decision** (deliberate deviation from decision rule 2's "merge default-on"): SIKE 2026-07-19 showed a prose-bed regression with the current code-query gating. Default-on is revisited after the `symbol_expansion_cap` sweep {4, 16} + code-gating validation (#231 lane); the flip belongs there, not to #230.

## Scope & caveats (council review, 2026-06-28)
- **Blob-mode only.** WS2/WS3 are not wired into the sharded router (`shard_router.py` walks only `harmonic_links`); `_emit_symbol_graph` no-ops on the sharded write adapter. All numbers here are single-genome/blob; sharded corpora get the cAST chunking gain but **no** symbol-graph gain.
- **Python-only symbol refs.** cAST chunking covers 8 languages, but reference extraction (`_PY_REF_LANGS`) is python-only — JS/TS/Go/Java/Rust/C++ get definitions indexed but **zero SYMBOL_REF edges**, so WS2/WS3 are inert there.
- **Existing genomes need re-ingest** to populate `symbol_defs` / `SYMBOL_REF` (`CREATE TABLE IF NOT EXISTS` adds the empty table; edges are written only on ingest).
- **PageRank ≈ in-degree in production.** At retrieval the personalization is `query_symbol_nodes=cand_ids` (uniform — the 10×/50× weights are not wired through the live path), so the measured fp recovery is plausibly the **cap**, not centrality ordering. A `cap+PageRank` vs `cap+in-degree` ablation is the deciding test for keeping the module.
- Validation is a single 26-task smoke (+2.1pp packet is sub-one-task); the held-out sweep is still owed before treating WS2/WS3 as load-bearing.

## Cautions
- The packet (max 32 genes) is the production path and the headline metric; the fingerprint arm is a diagnostic that stresses budget-fill behaviour (which is why it caught the unbounded-expansion regression).
- Personalization weights and the cap should be validated on a held-out corpus, not tuned to maximize the 26-task smoke.
