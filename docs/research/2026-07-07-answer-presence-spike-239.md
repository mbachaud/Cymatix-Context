# Answer-presence scorer vs. causal-use — offline discrimination spike (#239)

**Date:** 2026-07-07
**Branch:** research/faithfulness-semantic-reach
**Status:** decided — **NO-GO** for the competition (causal-use) discriminator; **sub-threshold PARTIAL** for the answer-absence / abstain half.
**Parent:** [`2026-07-07-faithfulness-circuit-tracer.md`](2026-07-07-faithfulness-circuit-tracer.md) — this is that note's *Next #4* ("prototype an answer-presence feature… re-run the §4 bench and see if this feature clears chance where coord/top_score do not").

---

## TL;DR

The circuit-tracer study proved the shipped 5 retrieval-strength features carry **no causal-use signal** on ambiguous deliveries, and proposed a **new answer-presence feature** (answerability / NLI on the delivered span vs. the query) as the fix. This spike tests that proposal offline, before any integration.

**It does not work as a competition discriminator.** On the decisive competition cell (C12, both gold *and* competitor delivered), **no shippable scorer beats chance**:

- MS-MARCO cross-encoder (the purpose-built, hot-path candidate): **C12 AUC = 0.486** — at chance.
- **Every** scorer's C12 95% CI lower bound is **< 0.5** → the pre-registered GO condition is failed by all four.
- Mechanism (dispositive): MS-MARCO mean score on `competition/causal = 6.757` vs `competition/non-causal = 6.782` — **indistinguishable** (fractionally *reversed*). A (query, assembled-span) answerability score is the same whether or not the model causally read the gold fact, exactly as pre-registered.

A **weak answer-*absence* signal does exist** (MS-MARCO G24 = 0.708, p = 0.044; held-out/answer-absent rows score low) but it **misses the pre-registered PARTIAL bar** (needs G24 ≥ 0.75 with heldout ≥4/5 in the bottom quartile; got 0.708 and 3/5). At **n = 12** with **±0.35** CI half-width, it is under-powered to gate a code change on its own.

**Recommendation:** do **not** open the B2 six-feature change-set. The abstain-gate half (B1/B3) is *plausible but not pre-registered-sufficient*; before committing it, take the parent note's §6 path — **scale N and confirm on the helix serving model** — and, if the competition discriminator is still wanted, run the pre-registered **A′ per-fact-margin** probe (gold-span vs competitor-span), because the assembled-span route is now empirically dead.

---

## Method

Offline, read-only. No helix code/config changed, no graphs run, nothing ingested. One script:
`scratchpad/spike_239b_answerpresence.py` (graph venv `F:/Projects/np-graph/venv`).

**Data** (join on `id`, from the §4 delivery-balanced bench):
- `np-graph/needles_239b_stage1.json` → `q`, `expressed_context` (the delivered span), 5 features, `cell`. Gold `ans`/`competitor_ans` used for **labeling audit only**, never as a scorer input.
- `np-graph/needles_239b_faith.json` → `causal_use` (graph-measured), `pB`, `cell`.

**Sets** (`pB is not None`; **not** filtered on the stricter `in_graph`):
- **G24** — 24 graph-measured rows: 7 answerable (all causal), 5 heldout (all non-causal), 12 competition (5 causal / 7 non-causal).
- **C12** — the 12 competition rows: the decisive, non-circular test (retrieval-strength features are provably at chance here — coord AUC 0.529).

**Scorer input** per row: `query = q`, `span = expressed_context`. Score = "does this delivered span answer this query." The gold token is never fed.

**Scorers** (decision priority):
1. **`ms_marco_ce`** — `cross-encoder/ms-marco-MiniLM-L-6-v2` via `sentence_transformers.CrossEncoder` (**not** routed through helix's ingestion rerank_model). Purpose-built (query, passage) relevance; cheapest hot-path candidate.
2. **`nli_span_q` / `nli_q_span`** — DeBERTa-v3-small NLI entailment prob, **both orders**. ⚠️ **The specified local model `training/models/nli` does not exist on this box** (only `training/models/rerank/` + base `microsoft/deberta-v3-small` in cache). Substituted the public `cross-encoder/nli-deberta-v3-small` (3-class; entailment index read from `config.id2label` = 1). **Scientific reference only — not allowed to drive the gate** (different model than would ship).
3. **`minilm_cos`** — raw `all-MiniLM-L6-v2` cosine. Naive baseline floor.

**Metric:** `sklearn.roc_auc_score(causal_use, score)` on C12 and G24; percentile bootstrap 95% CI on C12 (B = 5000, seed = 12345); one-sided Mann-Whitney (`alternative="greater"`); held-out rows' rank within G24 (bottom quartile = bottom 6 of 24). All on CPU (`device="cpu"`) for honest hot-path timing.

**Verification:** every AUC was independently re-derived via the U-statistic identity `AUC = U/(n₁n₂)` — exact match to sklearn for all 8 (scorer × set) cells. No leakage: scorers see only `(q, span)`; the span legitimately contains delivered facts (that *is* what answer-presence scores) — the forbidden inputs are the `ans`/`competitor_ans` gold-token fields, which are never passed.

---

## Results

| scorer | C12 AUC | G24 AUC | C12 95% CI | CI width | p(C12) | p(G24) | heldout mean %ile | heldout in bottom-6 |
|---|---:|---:|:--:|---:|---:|---:|---:|:--:|
| **`ms_marco_ce`** *(hot-path candidate)* | **0.486** | **0.708** | [0.11, 0.89] | 0.77 | 0.562 | **0.044** | 0.18 | 3/5 |
| `nli_span_q` *(substitute)* | 0.457 | 0.396 | [0.11, 0.81] | 0.70 | 0.622 | 0.815 | 0.63 | 0/5 |
| `nli_q_span` *(substitute)* | 0.743 | 0.438 | [0.39, 1.00] | 0.61 | 0.101 | 0.708 | 0.79 | 0/5 |
| `minilm_cos` *(baseline)* | 0.629 | 0.576 | [0.26, 0.94] | 0.69 | 0.265 | 0.272 | 0.38 | 1/5 |

**Per-cell mean score** (the mechanism):

| scorer | answerable | heldout | comp / causal | comp / **non-causal** |
|---|---:|---:|---:|---:|
| `ms_marco_ce` | 6.916 | 5.842 | 6.757 | **6.782** |
| `nli_span_q` | 0.324 | 0.493 | 0.346 | 0.395 |
| `nli_q_span` | 0.019 | 0.020 | 0.028 | 0.008 |
| `minilm_cos` | 0.537 | 0.524 | 0.589 | 0.548 |

Reading the MS-MARCO row: `answerable (6.92) > heldout (5.84)` is a real answer-*absence* contrast — that is the entire source of its G24 = 0.708. But `comp/causal (6.757) ≈ comp/non-causal (6.782)`: on the competition cell the score cannot tell a causally-used delivery from an ignored one, because both facts are present in the assembled span.

---

## Interpretation → pre-registered gate

**GO** (best scorer C12 ≥ 0.70 **and** G24 ≥ 0.70 **and** C12 CI-lo > 0.5): **failed.**
- No shippable scorer reaches C12 ≥ 0.70 (MS-MARCO 0.486, baseline 0.629).
- **Every** scorer — including the substitute NLI — has C12 CI-lo < 0.5.
- The one point estimate above 0.70 (`nli_q_span` = 0.743) is an **n = 12 artifact, not signal**: its per-cell means are all ≈ 0.02 and non-monotonic, its **G24 = 0.438 is anti-correlated**, its CI reaches down to 0.39, and p = 0.101 (not significant). Real answer-presence signal would help G24 too; this does the opposite.

**PARTIAL-GO** (G24 ≥ 0.75 from clean answerable-vs-heldout separation, heldout in bottom quartile, C12 ≈ 0.5): **narrowly missed.**
- C12 ≈ 0.5 ✓ (MS-MARCO 0.486).
- But G24 = 0.708 **< 0.75**, and heldout separation is **3/5** in the bottom quartile, not ≥4/5. The answer-absence signal is *real but sub-threshold*.

**NO-GO** (best scorer < 0.60 on both): not literally triggered (best G24 = 0.708 > 0.60), so the result lands in the **seam between sub-threshold-PARTIAL and NO-GO**.

**Verdict.** The GO thesis — "a real discriminator on genuinely ambiguous competition deliveries, the thing the 5 features cannot do" — is **cleanly refuted**, and refuted *for the pre-registered reason*: assembled-span answerability is blind to *which* delivered fact was read. Treat this as a **NO-GO for the B2 six-feature competition discriminator.** The answer-absence half earns only a **qualified, sub-threshold PARTIAL**: suggestive (p = 0.044, heldout low) but below the bar and under-powered at n = 12.

---

## Cost profile (hot-path candidate, if the abstain half is ever pursued)

Relevant only for a B1/B3 answer-absence gate; measured single-pair on CPU, post-warmup:

| scorer | params | CPU ms/pair (batch 1) |
|---|---:|---:|
| `ms_marco_ce` | **22.7 M** | **~35 ms** |
| `minilm_cos` | 22.7 M | ~41 ms |
| NLI (deberta-v3-small substitute) | 141.9 M | ~104 ms |

MS-MARCO MiniLM-L-6 is the cheapest and the only purpose-built option; ~35 ms/pair on CPU is compatible with the "compute only when a backend model is already resident" hot-path policy.

---

## Recommendation

1. **Do not open the B2 change-set** (6th `answer_presence` know-feature aimed at competition discrimination). It does not clear chance on the honest test set; shipping it as a causal-use discriminator is unsupported.
2. **Operating-point repair (B1) stands on its own** — it comes from §3, independent of this scorer, and remains the right fix for the ceiling-below-floor (0 KnowBlocks) problem.
3. **Abstain / answer-absence gate (B3): hold.** There is a *directional* case (MS-MARCO separates answer-present from answer-absent, p = 0.044) but it is below the pre-registered PARTIAL bar and rests on 5 heldout rows. Do not gate code on it yet.
4. **Take the parent note's §6 path first:** scale N and **confirm on the helix serving model** (Qwen3-4B is the instrument, not the deployment target); the current CIs (±0.35 on C12) cannot distinguish "weak real signal" from "noise."
5. **If the competition discriminator is still wanted, run the pre-registered A′ probe** — score gold-span vs competitor-span *separately* and use the margin, which needs the per-candidate `spliced_map[gene_id]` (a small helix-env re-dump, `context_manager.py:1700-1766`). The assembled-span approach tested here is empirically dead for that question, so A′ is the only remaining lever.
6. **Data-integrity flag — corrected 2026-07-08:** helix's fine-tuned NLI model (`training/models/nli`, referenced by `nli_backend.py:46`) is absent on this box **because it was never trained** — not lost. A 2026-04-22 audit already recorded it "Never trained — dir doesn't exist" while its sibling heads (`rerank`, `splice`) were trained and persist on disk; git has never touched `training/models/nli*` or `training/data/nli_pairs*`. It is also **dormant, not live-broken**: shipped config is `[ribosome] enabled=false / backend="none"`, so the NLI path is unreachable, and even under `backend="deberta"` a missing model soft-fails to an empty relation graph. The NLI numbers here used a public substitute and are reference-only. **Restore = train for the first time** (runbook + first genuine train executed 2026-07-08; see [`2026-07-08-b1-operating-point-coupling.md`](2026-07-08-b1-operating-point-coupling.md) and the NLI-restore note).

---

## Reproduce

```
scratchpad/spike_239b_answerpresence.py   # graph venv; python -X utf8
scratchpad/spike_239b_results.json        # per-row scores, labels, cells, metrics, cost
```

Inputs: `np-graph/needles_239b_stage1.json`, `np-graph/needles_239b_faith.json` (the §4 bed).
Models: `cross-encoder/ms-marco-MiniLM-L-6-v2`, `sentence-transformers/all-MiniLM-L6-v2` (public downloads); NLI substitute `cross-encoder/nli-deberta-v3-small` (helix `training/models/nli` absent). CPU, seed 12345.

## Limitations

- **n = 12 (C12)** — CI half-widths ≈ ±0.35; point estimates are weak evidence either way. This is why the verdict is NO-GO/scale-N, not a precise AUC ranking.
- **NLI is a public substitute**, not helix's fine-tuned 7-class model (which is not on disk). It is excluded from the gate; its 0.743 is shown only to demonstrate the n = 12 artifact.
- **Instrument model = Qwen3-4B**, not the helix serving model — causal-use labels are model-relative (inherited from the parent study).
- Spans are scored **as delivered** (legibility headers / `◆` glyphs included). Stripping headers was not tested; it is a defensible robustness variation for the scale-N follow-up, but cannot manufacture competition signal that the mechanism (both facts present) precludes.
