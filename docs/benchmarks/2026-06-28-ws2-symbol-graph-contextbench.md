# WS2 symbol graph — ContextBench Step-0 validation

**Date:** 2026-06-28
**Build:** cAST byte-exact + WS2 (`symbol_graph` on) vs the same build with WS2 off (`cast_fix`).
**Gold:** `gold_smoke_4repo.parquet` (26 tasks). Official evaluator, lexical + SEMA-off, micro-avg over the common 26.

## Result

| arm | file_R | line_R | line_P | sym_R |
|---|---|---|---|---|
| BM25:27k | 0.881 | 0.484 | 0.020 | 0.585 |
| WS2-off fp@8k | 0.571 | 0.222 | 0.027 | 0.352 |
| WS2-off fp@27k | 0.833 | 0.679 | 0.026 | 0.746 |
| WS2-off packet | 0.881 | 0.804 | 0.023 | 0.808 |
| WS2-on fp@8k | 0.595 | 0.212 | 0.026 | 0.352 |
| WS2-on fp@27k | 0.786 | **0.538** | 0.020 | 0.648 |
| **WS2-on packet** | 0.881 | **0.825** | 0.018 | 0.829 |

## Verdict — net win on the packet, regression on unranked budget-fill

- **Packet (the production delivery path): WS2 lifts line recall +2.1pp (0.804 → 0.825) and symbol recall +2.1pp (0.808 → 0.829)**, file coverage tied at 0.881, with a small precision dip (0.023 → 0.018) expected from adding genes. Symbol expansion pulls referenced definitions that are gold into the delivered set. This is the WS2 win, on the arm that matters.
- **Fingerprint@27k: WS2 regresses line recall −14pp (0.679 → 0.538).** The fingerprint fills a 27k budget greedily; **unranked, unbounded** symbol expansion dumps referenced definitions into the budget and displaces gold lines. The packet avoids this because it caps genes (max 32), so its expansion is naturally curated.

## Interpretation → motivates WS3

The symbol graph demonstrably surfaces real gold definitions (packet recall up), but raw expansion is too aggressive for a large unranked budget. This is precisely the gap **WS3 (personalized PageRank + budget-ordered trim)** closes: rank the expanded definitions by structural centrality and let the budget keep the central ones, dropping noise. Expected to recover the fingerprint regression while preserving the packet gain.

## Recommendation
- Keep WS2 (edges + the packet win are real; the packet is the production path).
- Before WS3 lands, bound the expansion (cap referenced definitions added per query) to avoid the fingerprint-style dilution on budget-fill paths.
- WS3 is the proper fix (rank-then-trim); prioritize it. WS3 slice 1 (the PageRank scoring module) is built (PR pending).
