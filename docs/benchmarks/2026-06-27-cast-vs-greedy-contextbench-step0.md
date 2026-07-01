# cAST recursive chunking — ContextBench Step-0 (Phase 1 acceptance)

**Date:** 2026-06-27
**Build:** master + #224 + #227 (SEMA-off) + cAST recursive split-then-merge (PR #228, byte-exact)
**Gold:** `benchmarks/contextbench/gold_smoke_4repo.parquet` (26 tasks; django / scikit-learn / requests)
**Scorer:** official ContextBench evaluator, micro-average over the common 26-task set.
**Config:** lexical (dense / SPLADE / rerank / cymatics off), `sema_embed_on_ingest = false`. LLM-free, in-process.

## Result

| arm | file_R | **line_R** | sym_R |
|---|---|---|---|
| BM25:8k | 0.690 | 0.314 | 0.404 |
| BM25:27k | 0.881 | 0.484 | 0.585 |
| greedy-AST fp@8k | 0.500 | 0.134 | 0.228 |
| greedy-AST fp@27k | 0.667 | 0.626 | 0.591 |
| greedy-AST packet | 0.714 | 0.662 | 0.622 |
| cAST fp@8k | 0.571 | 0.222 | 0.352 |
| cAST fp@27k | 0.833 | 0.679 | 0.746 |
| **cAST packet** | **0.881** | **0.804** | **0.808** |

## Verdict — Phase 1 acceptance: PASS

- **cAST beats BM25 decisively.** Packet line recall **0.804 vs BM25's best 0.484 (+32pp)**, at lower injected tokens; matches BM25's file coverage (0.881) while far exceeding it on line and symbol recall.
- **cAST beats greedy-AST.** Packet **+14.2pp line** (0.804 vs 0.662), **+18.6pp symbol** (0.808 vs 0.622), **+16.7pp file** (0.881 vs 0.714). fp@27k **+5.3pp line** (0.679 vs 0.626). The recursive split-then-merge — keeping whole methods instead of char-cutting oversized classes — is the lever, as cAST (arXiv 2506.15655) predicted.
- **The one soft spot improved too.** At the tight 8k budget cAST fp lifts line recall 0.134 → 0.222 (+8.8pp); BM25:8k (0.314) still edges it, but the gap roughly halved.

## Methodology note — a bug the measurement caught

The first cAST draft reassembled decoded piece text and **dropped whitespace-only interstitial gaps**, so a merged chunk was no longer a verbatim substring of the source. ContextBench maps retrieved genes back to line ranges by exact-substring match (`recover_lines`), so those chunks failed to line-map and were dropped — collapsing cAST packet to a spurious **0.141** even though retrieval (`gold_in_rank`) was unchanged. Per-task gene survival was the tell: packet kept g8/5k tokens vs the baseline's g47/37k. The fix emits each chunk as an exact `code_bytes[start:end]` slice (verified 0/1189 non-verbatim over the helix corpus); gene survival recovered to g50/37k and the scores above followed. This is also strictly better for real use (citations / line-mapping rely on verbatim chunks).

## Lineage of baselines on this gold
- BM25:27k line_R 0.484 (reproduces the frozen `step0_summary` baseline exactly).
- regex chunking (no tree-sitter), packet 0.655 (prior `m224` run).
- greedy-AST packet 0.662.
- **cAST packet 0.804.**
