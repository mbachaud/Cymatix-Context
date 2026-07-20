# Arm C (WS2/WS3 symbol graph) — ContextBench held-out sweep, 2026-07-20

> The "broader held-out sweep" owed since the 2026-06-28 council dark-ship
> decision (`docs/benchmarks/code-track-regression-log.md`), executed per
> `docs/benchmarks/2026-07-01-external-bench-run-plan.md` §2 arm C.
> Companion to the SIKE measurement
> (`docs/benchmarks/2026-07-19-armc-symbol-sike.md`), which was
> flat-to-negative on a docs-heavy bed.
> Raw artifacts: `benchmarks/contextbench/results/helix_arm{B,C}_*_{pred,meta,eval}.*`
> (per-chunk files kept), tasks `F:/tmp/cb_tasks_verified.json` (266, zero
> checkout errors after repo-local `core.longpaths=true` on
> ansible/transformers/langchain).

## Setup

- **Gold:** `gold_verified_python.parquet` — 266 held-out tasks, 19 repos
  (django 80, transformers 34, sympy 29, ansible 26, …). None used for
  WS2/WS3 tuning (tuning was the 26-task smoke; sympy was the only prior
  held-out probe).
- **Builds:** both arms served the `feat/ws3-symbol-pagerank` tree
  (post-rebase on `ce34a4b`) via `PYTHONPATH` isolation (regression-log
  method). Arms differ ONLY in config:
  arm B = `symbol_graph=false`; arm C = `symbol_graph=true` +
  `symbol_expansion_cap=8`. Both: lexical profile, sema off, additive
  fusion, workers 3.
- **Runner:** `cb_helix_pred.py`, 6 task-chunks per arm (crash isolation),
  fresh genome per task, whole-file `content_type="code"` ingest. 265/266
  tasks produced preds in both arms. ~20 h/arm (ingest-dominated:
  ~14 min single-worker per big-repo task; retrieval seconds).
- **Scoring:** official `contextbench.evaluate` (cb-step0 venv), micro-avg
  over the common 265-instance intersection.

## Results (common 265)

| mode | arm | file_R | line_R | line_P | sym_R |
|---|---|---|---|---|---|
| fingerprint@8k | B | 0.406 | 0.250 | 0.046 | 0.291 |
| fingerprint@8k | C | 0.402 | 0.246 | 0.045 | 0.285 |
| fingerprint@27k | B | 0.621 | 0.491 | 0.028 | 0.521 |
| fingerprint@27k | C | 0.606 | 0.484 | 0.027 | 0.522 |
| **packet** | B | 0.662 | 0.531 | 0.022 | 0.564 |
| **packet** | C | 0.662 | **0.559 (+2.8pp)** | 0.020 | **0.601 (+3.8pp)** |

**Packet (the production delivery path): +2.8pp line recall, +3.8pp symbol
recall, file coverage tied, −0.2pp precision.** Paired per-task on packet
line recall: **44 improved / 0 regressed / 221 tied** (one-sided sign test
p ≈ 6e-14). Winners span 12/19 repos (django 12, ansible 7, transformers 5,
sympy 4, matplotlib 4, …) — not single-repo-driven.

**Fingerprint (budget-fill diagnostic): the WS2-era regression is gone.**
fp@27k paired: 27 up / 27 down / 211 tied (dead even; micro-delta −0.7pp
line, +0.1pp sym). WS3's cap + centrality ordering did exactly what it was
built for — the smoke-era −14pp unbounded-expansion catastrophe does not
reproduce with the production cap.

## Gate verdict

**ROADMAP decision rule 2 is satisfied: arm C ≥ +1pp on an external bench**
(packet +2.8pp line / +3.8pp sym, held-out, n=265, zero per-task
regressions). Per the rule this unlocks: cap sweep {4, 8, 16} (on a corpus
not used for tuning), revisit default-on, and the scope-gap work.

Contrast with SIKE (2026-07-19, flat-to-negative): the mechanism's value is
corpus-dependent exactly as the council suspected — inert-to-mildly-harmful
on docs-heavy prose beds, real and consistent on code retrieval. Any
default-on proposal should be **code-gated** (the classifier's code-query
gating already exists per the regression-log knob map) rather than global.

## Caveats

- Python-only refs: SYMBOL_REF edges exist only for python (defs-only for
  other languages) — this python split is the mechanism's best case among
  languages; JS/TS/Go corpora would show cAST-only behavior.
- Blob-mode only: sharded stores get no symbol graph (adapter no-ops).
- The two review findings from the recorded #230 review (symbol writes
  outside the #300 write lock; no symbol_defs orphan cleanup) predate any
  merge, dark or not.
- RepoBench-R (second external bench) not yet run — the decision rule needs
  "either", already met; RepoBench remains available as corroboration.
