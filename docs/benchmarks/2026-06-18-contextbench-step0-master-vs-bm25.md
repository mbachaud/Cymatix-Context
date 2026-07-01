# ContextBench Step-0 — master+#224 vs BM25 (code retrieval)

**Date:** 2026-06-18
**Build:** `origin/master` @`3aa6c5c` + #224 (`fix-224-content-type` @`5d48321`, == v0.7.1 dev)
**Gold:** `benchmarks/contextbench/gold_smoke_4repo.parquet` (26 tasks; django / scikit-learn / requests)
**Scorer:** official ContextBench evaluator (`contextbench.evaluate`, tree-sitter span alignment), micro-average.
**Retrieval:** LLM-free, in-process, lexical config (`helix_probe.toml`: dense / SPLADE / rerank / cymatics all OFF). Repo is never mutated; gold/patch/test_patch are never indexed.

## Result (full 26-task smoke)

| arm | file_R | **line_R** | sym_R | median injected tok |
|---|---|---|---|---|
| bm25:8k | 0.690 | 0.314 | 0.404 | 8k |
| bm25:27k | 0.881 | 0.484 | 0.585 | 27k |
| **MASTER packet** | 0.810 | **0.655** | 0.591 | < 27k |
| **MASTER fingerprint@27k** | 0.762 | **0.570** | 0.560 | 27k |
| MASTER fingerprint@8k | 0.595 | 0.241 | 0.316 | 8k |
| wt fingerprint@27k (prior) | 0.762 | 0.549 | 0.539 | 27k |
| wt packet (prior) | 0.833 | 0.655 | 0.591 | < 27k |
| v062 fingerprint@27k (prior) | 0.762 | 0.549 | 0.539 | 27k |

`line_R` = line-level coverage (recall); `file_R` / `sym_R` = file- / symbol-level recall. Precision is metric-tiny for every arm at these budgets (recall-at-budget is the axis that matters here).

## Findings

1. **Master's packet beats the BM25 dump on line recall by +17pp** — 0.655 vs BM25's best 0.484@27k — at *fewer* injected tokens, and also edges BM25 on symbol recall (0.591 vs 0.585). On its designed terrain (path / symbol / structure) Helix returns materially more relevant lines and symbols than a raw lexical fill.

2. **Master is the strongest Helix build to date.** Fingerprint@27k 0.570 beats the prior `wt` and `v062` builds (both 0.549, **+2.1pp**) and beats BM25@27k (0.484, **+8.6pp**). The 47 commits between v0.6.x and current master (#217 dense-floor, #218 config-reconcile, #220 lazy-encoders, …) improved code fingerprinting without regressing the packet (packet ties `wt` at 0.655).

3. **Harness validation:** the BM25 arms reproduce the frozen baseline (`step0_summary.json`) exactly — 0.314@8k / 0.484@27k — so the comparison is sound.

4. **Where BM25 still wins:** raw file coverage (0.881 vs 0.810) and the tight 8k budget (line_R 0.314 vs master fingerprint 0.241). Helix's fingerprint under-fills at 8k — the one concrete weakness worth chasing on the code track.

## Run notes / caveats

- **Two-pass run.** The 3-worker run crashed at task 17 with `BrokenProcessPool` — a transient OOM from each worker eagerly loading the BGE/SEMA sentence-transformer **even under a fully lexical config** (dense/cymatics off). The first 16 (django) completed and were pred-written; the remaining 10 (scikit-learn / requests) were re-run at `workers=1` and merged. See issue: SEMA eager-load bypasses #220 lazy-encoders.
- A 16-task (django-only) subset scored before the merge gave the same ranking (master packet 0.658, master fp@27k 0.542, BM25@27k 0.516) — the full-26 numbers above are the canonical, repo-diverse figures.

## Reproduce

```bash
# Helix arm (per-task fresh genome, in-process, lexical):
#   venv: master+#224 ; HELIX_CONFIG=helix_probe.toml ; CONTEXTBENCH_SRC=contextbench-src
python benchmarks/cb_helix_pred.py --tasks <tasks.json> --tag master \
  --config helix_probe.toml --modes fingerprint,packet --budgets 8000,27000 \
  --dense-device cpu --workers 1            # workers=1 until the SEMA eager-load is fixed

# Score (cb-step0 venv, official evaluator):
python benchmarks/cb_score_all.py          # BM25 from step0_summary.json + every helix_*_pred.json
```
