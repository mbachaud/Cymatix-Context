# Council: J-Space Alignment + Gaussian-Splat Genes — Phased Roadmap

- **Date:** 2026-07-06
- **Format:** 19-member council (6 specialties × pro/con/neutral — mechanistic interpretability, retrieval/IR, density modeling, ML systems/serving, product/architecture, epistemics/adversary) + chair synthesis. Panelists grounded in the code; the chair *verified* every load-bearing premise empirically before ruling.
- **Reviews:** [`docs/design/2026-07-06-jspace-splat-roadmap.md`](../design/2026-07-06-jspace-splat-roadmap.md), [`docs/design/2026-07-06-jspace-roadmap-review.md`](../design/2026-07-06-jspace-roadmap-review.md)
- **Status:** advisory — the decision record for whether/how to fund the J-space + splat program.

---

## Chair's verdict

**Fund a re-scoped, re-ordered version — lead with bet B, cap at the weak form, and do two one-day fixes that sit outside all six phases first.** The roadmap is unusually disciplined (falsifiable phases, bounded downside) and its "re-measure on our own models, don't trust the citation" epistemics are the best thing in this bundle — better than the review's, which quietly upgrades unverifiable citations to actionable blueprints. But three things are true in the code that reorder everything: (1) the shipped default is `fusion_mode="additive"`, under which the `Fuser` **is built but never queried** (`knowledge_store.py:1993`), so Phase 3's whole "add a jspace tier" plan is inert until a separate one-line flip lands; (2) the last assembly stage truncates to a **query-agnostic 1000 chars** (`context_manager.py:1706` → `headroom_bridge.py:227`), so both Phase 0's Recall bar and Phase 4's EM bar are being measured against a pipeline that throws the answer away; and (3) the incumbent know-logistic **has never been calibrated** (`helix.toml` betas are byte-identical to `DEFAULT_BETAS`, and the calibration-data generator `benchmarks/located_n1000.py` does not exist), so grading density against it books a false win.

**The single most important correction:** the review's empirical spine is not on this branch. The metric it leans on (`content_has_answer`), the two "grounding" benchmark docs, and the `s3_fts_depth_sweep.py` driver are **absent from the tree** (HEAD = `docs/jspace-roadmap-council @ de9c92c`). The council must not reprioritize on numbers that aren't reproducible from repo state. The roadmap's instinct — *build* the harness and pre-register bars — is vindicated; the review's "we're further along, the harness is half-built" is over-claimed.

---

## Decision point 1 — Accept the three-subspace split and re-scope Phase 0?

- **PRO (mech-interp pro):** The subspace triad is the review's best contribution. The paper's low-dimensional (~25-vector, ≤10%-variance) workspace is precisely the *enabling condition* for Phase 4 — in 1024-d BGE-M3 space, points are near-orthogonal and max-coverage collapses to max-sum-relevance, so the submodular objective only pays when the target region is genuinely low-dimensional.
- **CON (mech-interp con + neutral):** Phase 0's stated instrument cannot compute the target. The J-lens is a **backward-pass Jacobian** `E[∂h_final/∂h_ℓ]`; Phase 0's Method captures `output_hidden_states` — a **forward pass**, mean-pooled. PCA over those finds the activation-variance subspace, which the paper says is ≥90% *disjoint* from the workspace. The review's "add a small Jacobian bonus probe" patch leaves the wrong instrument and the S–M effort tag intact.
- **NEUTRAL (mech-interp neutral):** Even after the triad, three numbers stay conflated — "~25 active vectors" (occupancy), "~16" (weight-spectral rank, UWSH), and Phase-1 splat "k≈8–16" (covariance rank). Phase 3's LOD claim ("if quality at r=4 ≈ r=16, the 16-direction story is confirmed") is a **category error**: covariance-truncation rank says nothing about workspace occupancy.

**CHAIR VERDICT: Accept the triad as mandatory labeling discipline; reject the review's implication that a "bonus probe" repairs Phase 0.** *Verified:* `output_hidden_states`, `inputs_embeds`, `AutoModelForCausalLM` appear in **zero** code files — every torch model in `helix_context/` is an encoder (BGE-M3, SPLADE, DeBERTa, NLI). Helix has no decoder-in-fp16 capture path at all. So the honest re-scope is: **Phase 0 = the activation-variance retrieval re-metric probe only** (buildable, bet-B-adjacent, use windowed/CAZ pooling), explicitly *not* named "J-space"; the true Jacobian/J-lens computation is a **separate, unbudgeted research line** that must be costed as such, not smuggled in. And the mech-interp neutral's ban is adopted: no phase may claim a covariance rank "confirms" the workspace occupancy number.

---

## Decision point 2 — Lead with bet B + Mahalanobis whitening, or the J-space probe?

- **PRO (near-unanimous — IR pro, product pro, epistemics pro, ML-systems pro):** Bet B needs no decoder internals and its seams are prepaid. *Verified:* `fusion.py:104` — a zero-weight tier is a silent no-op; `know_calibration.py:64` — `DEFAULT_BETAS` is a 6-tuple (intercept+5) and the Stage-7 freshness feature is the exact 4→5→6 pattern a density feature copies; `splats`/`spaces` are absent from `ddl.py` but follow the existing side-table pattern (`cwola_log`, `harmonic_links`, `genome_calibration`).
- **CON (density con + neutral, ML-systems con):** Three sharp corrections. (a) The "whitening available now" is **~50 lines net-new** — *verified:* no `mahalanobis|whiten|ledoit|covariance` code exists anywhere. (b) BGE-M3 is contrastively **L2-normalized** so cosine is trained-correct; whitening it may amplify noise — a 1-hour ablation, not a pre-registered certain win. (c) Per-gene covariance is **sample-starved by construction**: chunk-count `n_obs ≪ d=1024`, so Ledoit–Wolf shrinks to the shared prior and Mahalanobis ≈ scaled cosine for the *majority* of genes — the roadmap's named kill-switch is the default case, not a tail. Plus chunk vectors are discarded at ingest, hiding a re-embedding backfill.
- **NEUTRAL (density neutral):** Split Phase 1. The review conflates two statistically *opposite* Mahalanobis objects: a single corpus-level Σ (N≫d, well-conditioned — the real prepaid win) vs per-gene Σ (N≪d, ill-conditioned). And the repo has **hard anisotropy evidence**: `knowledge_store.py:383-404` documents a calibrated dense threshold of 0.779 sitting *above* the corpus max cosine of 0.713 → 0/5000 chunks cleared, 70% never-surfaced golds. Global whitening is a *measured need*, not a hypothesis.

**CHAIR VERDICT: Yes, lead with bet B — but with the statisticians' split, not the review's framing.**
- **Phase 1a** = *one* global Ledoit–Wolf-shrunk whitening transform over the existing `embedding_dense_v2` column, applied to query and gene, stored in a single `spaces` row. Well-conditioned, cheap, and the repo already documents the pathology it fixes. Measure it — don't assume it (BGE-M3's normalization is a real caveat).
- **Phase 1b** = per-gene splats + density feature — gated on a *measured* `chunk_count` distribution and inter-gene covariance divergence; use adaptive rank `k_eff = min(rank, n_obs−1)`; invert the roadmap's detect-then-fallback into **detect-then-upgrade** (global-Σ + per-gene-μ is the well-conditioned default). Density enters **only** as a 6th logistic feature — never a replacing gate (legibility, DP3) — and is **AND'd** with provenance for poison, never OR'd.
- **Prerequisite for both:** build the labeled set + ECE/risk-coverage harness and **actually fit the incumbent 5-feature logistic first**. *Verified uncalibrated:* `helix.toml:449` betas == `DEFAULT_BETAS`, no `calibrated_at`, `located_n1000.py` absent. This fit *is* the cheap #239 win density is being pre-credited for.

---

## Decision point 3 — Is Phase 5 soft-prefix off the table on legibility grounds?

- **KILL (product con + pro, epistemics con):** Phase 5 forfeits the know/miss legibility contract that is Helix's moat and contradicts the "AI is the first-class user" thesis — an agent cannot audit or cite an un-decodable residual vector. Strike it, don't merely gate it.
- **CON on serving grounds (ML-systems con + neutral):** Independently fatal. Phase 5 is a **proxy→server category inversion** — Helix forwards the decoder over HTTP and never loads its weights (`config.py:187` upstream = Ollama). A prefix tuned on an fp16 parent is OOD for the served Q4 GGUF; Ollama/llama.cpp can't ingest embeddings at all; and `prompt_embeds` bypass vLLM Automatic Prefix Caching, so you *lose* prefix-cache reuse and serving TCO ends up worse than the text it replaces.
- **PRO (weak — ML-systems pro):** Keep as a research escape hatch; APC could amortize a stable prefix — but concedes this abandons the proxy thesis.

**CHAIR VERDICT: Cap the architecture at the weak form.** The legibility argument and the serving-incoherence argument point the same way, which makes this the lowest-cost cap in the plan. Keep Phase 5 only as a clearly-labeled research escape hatch that (a) cannot enter until Phase 4 both succeeds *and* shows text saturating below the offline ceiling, and (b) is documented up front as abandoning the proxy + know/miss contract for that profile. *Verified:* zero `prompt_embeds`/`inputs_embeds`/`output_hidden_states` in the tree — the stack that accepts soft prefixes is not the stack Helix fronts.

---

## Decision point 4 — Who verifies UWSH + LatentAudit before load-bearing?

- **CON (epistemics con):** The review commits the exact confirmation-bias failure it warns against — it flags the Deep Research as shaky, then makes Phase 1 "mirror the LatentAudit recipe" and cites its 0.942 AUROC as a blueprint. The verify-gate (decision-point #4) sits *downstream* of the reliance.
- **NEUTRAL (epistemics neutral):** Asymmetric standard. The review tags the Anthropic paper "verified read" while it is equally unfetchable from here (same operator-pasted provenance). And the "future-dated 2605/2606 arXiv IDs" flag is arithmetically wrong — relative to the 2026-07-06 doc, 2605/2606 are May/June 2026, i.e. *past*; the real axis is fetchability, by which the Anthropic paper is in the same unverified bucket.

**CHAIR VERDICT: No claim from *either* the Deep Research or the Anthropic paper is load-bearing until independently fetched and verified by a named owner with web/library access — which this environment does not have.** Concretely: (1) nobody "mirrors LatentAudit" — Phase 1 is driven by Helix's own CWoLa/faithfulness labels and the textbook anisotropy→Mahalanobis result (which needs no citation), not by an unverifiable 0.942; (2) **I overrule the review's "verified read" tag** — add the Anthropic paper to the verify list alongside UWSH; (3) UWSH's "~16 directions" must not anchor any storage or rank default until verified, and that gate must clear before Phase 3, the first phase that makes J-space load-bearing. The roadmap author's original "treat both as hypotheses we re-measure" stance stands as the council's epistemic policy.

---

## Decision point 5 — Which fp16 model to calibrate on?

- **ML-systems pro:** The decoder whose geometry matters is the proxied *upstream*, which Helix never loads; the config compressor (`gemma4:e2b`) is not it. Phase 0 must pick a model Helix can *both* proxy for generation *and* load in fp16 — the same artifact.
- **Mech-interp con / ML-systems neutral:** Quantization noise (Q4_K_M, roughly uniform per-weight) lands *hardest* in the ≤10%-variance workspace — the worst signal-to-quantization-noise subspace. Ollama serves GGUF, so you must pin the exact tag + quant + build flags, not "a Qwen or Llama variant." Much of Ollama's catalog is community GGUF-only with **no fp16 parent** → the calibratable set is a strict subset of what users actually run.
- **ML-systems pro (cheap probe):** Final-layer drift is measurable now via Ollama `/api/embeddings` with no fork; the mid-layer band where the J-lens lives is fork-gated.

**CHAIR VERDICT: Defer the pick until Phase 3 needs it; when made, pin one artifact satisfying three constraints simultaneously** — has a real fp16/bf16 parent (rules out GGUF-only tags), is one Helix actually proxies for generation (not the gemma compressor), and its exact Ollama tag + Q4_K_M quant + build flags are reproduced so the drift probe measures the *deployed* artifact. A Qwen2.5 or Llama-3.1 instruct with both a published fp16 checkpoint and a standard Ollama tag fits. **First, run the near-free go/no-go:** compare fp16-parent vs served-Q4 *final-layer* embeddings via `/api/embeddings` — if even final-layer geometry doesn't track, the mid-layer J-lens certainly won't, and the decoder half of bet A is dead on the default stack. Note the ML-systems con's re-baselining trap: this whole decision only matters if the Phase 0 re-metric beats **whitened** BGE-M3 (DP2); if Phase 1a's whitening absorbs the anisotropy, the ≥5-point J-space bar is set against a baseline the plan is about to obsolete, and DP5 may never need answering.

---

## Cross-cutting kill-switches, ranked by which fires first

1. **The splice floor fires before Phase 0 even starts.** *Verified:* `context_manager.py:1706` `target = 1000` (query-agnostic) → `headroom_bridge.py:227` `content[:target_chars].strip()`. Raised by nearly every panelist. **Agree — top priority.** It decouples retrieval (Phase 0) from emission (Phase 4): a Recall win can't move end-to-end QA while assembly cuts position-0 blind to the query. Partly prepaid (`_compute_foveated_caps` at `:341` exists but is BROAD-only/off). **Correction to the review:** the mechanism is mis-located — `complement` and `headroom_ai` do **not** appear in `headroom_bridge.py` (verified). Pre-register the fix against the verified truncation path, not the "answer lives only in complement" claim, or the work stalls mid-implementation.

2. **Phase 3's jspace tier is inert on the shipped default.** *Verified:* `config.py:409` `fusion_mode="additive"`; `knowledge_store.py:1993` "the Fuser is built but never queried"; fused sort only under `rrf` (`:2532`, `:2908`). Raised sharpest by IR neutral. **Agree.** Flipping `fusion_mode` default → `rrf` is a zero-research one-line win that must precede the J-space program. The RRF>additive *direction* is plausible via the verified mis-scaling (dense cosine ×16.0 vs FTS bm25 capped at 6.0), even though the specific "0.72 vs 0.58" numbers are prose-only.

3. **Per-gene covariance collapse — bet B's own first kill-switch.** `n_obs ≪ d=1024` → shrinkage → shared prior → Mahalanobis ≈ scaled cosine for most genes. Raised, remarkably, by **all three** density panelists plus mech-interp con. **Agree** — this is why Phase 1 leads with one global Σ and treats per-gene splats as detect-then-upgrade. Compounded by the discarded-chunk-embedding re-embedding backfill the "already computed" framing hides.

4. **Grading density against an uncalibrated baseline.** *Verified:* incumbent logistic ships priors, never fit. Raised by density pro/con/neutral + epistemics pro. **Agree** — fit the incumbent first; that's the real #239 win.

5. **The eval harness is nearer not-started than half-built.** *Verified:* `benchmarks/eval_retrieval.py` absent; `content_has_answer` in zero code files (real scorer is `body_has_answer`/`found_in_context` in `bench_needle.py`); both 2026-07-06 grounding docs absent; `s3_fts_depth_sweep.py` source gone (only a `.pyc`). Raised by ML-systems con, epistemics all three, IR neutral. **Agree the review over-claims — but the roadmap's build-it instinct is right.** Counter (product pro): `bench_needle.py` + 50-needle set + `build_gold_from_genome.py` exist, so it's ~40–60% skeleton, not zero. Budget Phase 0's harness as real engineering.

6. **Bet A is validated on its adversarial worst case.** J-lens is single-next-token / vocabulary-bounded; Helix's needles are multi-token identifiers (`claude-haiku-4-5-…`, `PostgreSQL 16`, `51/52`). A Phase 4 null on identifier needles is *uninformative* about bet A's conceptual-retrieval value. Raised by mech-interp all three, IR con, product con. **Agree** — scope bet A to conceptual retrieval; leave identifier lookup to FTS5; a conceptual gold set does not yet exist.

7. **Phase 0's instrument can't compute the J-lens.** Forward `output_hidden_states` ≠ backward Jacobian; no decoder-fp16 path in the tree. Raised by mech-interp con + neutral. **Agree for the J-space half specifically** (see DP1).

---

## Verified-vs-unverified premises ledger

**Empirically confirmed by the chair (this tree, HEAD `de9c92c`):**
- Splice default = query-agnostic 1000-char cut; foveated override exists but is BROAD-only/off — `context_manager.py:1706`, `:341`; `headroom_bridge.py:227`.
- `fusion_mode` default = `additive`; Fuser built-but-never-queried under additive — `config.py:409`, `knowledge_store.py:1993/2532/2908`. Zero-weight tier = no-op — `fusion.py:104`.
- Additive mis-scaling: dense ×16.0 vs FTS cap 6.0 — `config.py:454`.
- Know-logistic is a 5-feature (6-beta) model shipped **uncalibrated** — `know_calibration.py:64/84`, `helix.toml:449`; calibration generator `located_n1000.py` **absent**.
- No `mahalanobis|whiten|ledoit|covariance` code anywhere — bet B is net-new but conflict-free.
- No decoder capture: `output_hidden_states`/`inputs_embeds`/`prompt_embeds`/`AutoModelForCausalLM` in **zero** code files; every torch model is an encoder.
- `splats`/`spaces` tables absent from `ddl.py`; side-table precedent (`cwola_log:634`, `harmonic_links:689`, `genome_calibration:712`) present.
- Phase-4 hooks exist: `interference_trim` (`cymatics.py:526`), `_compute_foveated_caps` (`context_manager.py:341`).
- `HELIX_DISABLE_LEARN` gates Stage-6 **write-back only** (`server/helpers.py:38-51/781/832`) — stops echo re-learning, **not** ingest-time poisoning.
- Current dense metric is cosine — `config.py:285` `distance_metric="cosine"`, `:447` `dense_additive_min_cosine=0.15`. Upstream = Ollama/GGUF — `:187`.
- Anisotropy evidence in-repo — `knowledge_store.py:383-404` (0.779 threshold > 0.713 max cosine, 0/5000, 70% never-surfaced).
- **Review's empirical spine absent:** `content_has_answer`, `eval_retrieval.py`, `s3_fts_depth_sweep.py` source, and both 2026-07-06 benchmark docs are not in the tree; real scorer measures `body_has_answer`. The review's `complement`/`headroom_ai` splice mechanism is mis-located (neither appears in `headroom_bridge.py`).

**Stays hypothesis (no local/web access — treat as zero-evidence for load-bearing decisions):**
- The Anthropic "Global Workspace" paper (J-lens = `E[∂h_final/∂h_ℓ]`, ~25-vector / ≤10%-variance workspace, broadcast, causal) — unfetchable; overrule the review's "verified read."
- UWSH (`2512.05117`), LatentAudit (`2604.05358`, 0.942 AUROC), PMT/SoftSkill — unfetchable.
- Whether decoder residual geometry beats BGE-M3 on Helix's models (Phase 0 premise); quantization-drift magnitude; linear-probe recoverability of J-coords; and the "RRF 0.72 vs additive 0.58" figures — all unmeasured/prose-only in-tree.

---

## Recommended sequencing (next 5 moves) and what survives a total J-space kill

1. **Flip `fusion_mode` default → `rrf` and re-baseline.** Zero-research; without it Phase 3's tier is dead weight. Publish RRF vs additive on the committed harness under the real metric (`body_has_answer`) to replace the prose-only numbers.
2. **Fix the splice floor.** Query-aware / budget-proportional cap; generalize the foveated path off BROAD-only; emit `complement` where it actually lives (packet/secondary branch — *not* via a `headroom_bridge` mechanism that doesn't exist). Re-measure. Hard prerequisite for any Phase 4 measurement.
3. **Build `eval_retrieval.py` + labeled set + ECE/risk-coverage harness, and fit the incumbent logistic** (the real #239 win). Commit the SIKE Run-2 + faithfulness runbooks so the baseline is a committed artifact.
4. **Phase 1a: one global Ledoit–Wolf whitening** over `embedding_dense_v2`, A/B'd (measured, not assumed) against cosine on the now-committed harness. Then density as a 6th logistic feature, AND'd with provenance.
5. **Only then, the re-scoped Phase 0 re-metric probe** (activation-variance, windowed/CAZ pooling, *not* called J-space), pre-registered against **whitened** BGE-M3 on a pinned fp16 model with a real parent — costed as research, with the true Jacobian/J-lens computation booked separately. Defer Phases 2/3 behind its result; Phase 5 stays a labeled escape hatch only.

**Survives a total J-space kill (verified prepaid at the schema/seam level):** the eval harness + fitted logistic (#239), global whitening (1a), splat genes + density-as-feature know/go where identifiable (1b), coverage-based redundancy elision subsuming session dedup (Phase 4 fallback), the RRF-default flip, and the splice fix. The roadmap author's "maximum downside is bounded and mostly prepaid" claim is **true** — which is exactly why leading with the bet-independent half and quarantining the strong form is the right call.

---

*Generated by a 19-member council workflow (6 specialties x pro/con/neutral + chair; 18 panelists), each grounded in the committed docs and the repo code. Evidence base: [`jspace-roadmap-review`](../design/2026-07-06-jspace-roadmap-review.md), [`jspace-splat-roadmap`](../design/2026-07-06-jspace-splat-roadmap.md), [`roadmap-lineage`](2026-07-06-roadmap-lineage.md).*
