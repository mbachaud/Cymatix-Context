# 2026-07-06 — Review: J-Space Alignment + Gaussian-Splat roadmap × the "Global Workspace" paper

- **Reviews:** [`docs/design/2026-07-06-jspace-splat-roadmap.md`](2026-07-06-jspace-splat-roadmap.md)
- **New inputs:** Anthropic, *A Global Workspace in Language Models* (Transformer Circuits, Feb 2026, `transformer-circuits.pub/2026/workspace`); a Gemini Deep-Research literature sweep (*"Aligning Retrieval Geometry with the Transformer Residual Stream"*, provided 2026-07-06 — the share link was client-rendered and unfetchable, content pasted by the operator).
- **Grounding:** our own empirical work of 2026-07-06 — SIKE Run-2 (fts-depth × fusion), the `content_has_answer` metric fix, and the splice-truncation root-cause. See [`docs/benchmarks/2026-07-06-sike-run2-fts-depth-fusion.md`](../benchmarks/2026-07-06-sike-run2-fts-depth-fusion.md) and [`docs/benchmarks/2026-07-06-faithfulness-experiment-runbook.md`](../benchmarks/2026-07-06-faithfulness-experiment-runbook.md).
- **Status:** advisory — the shared evidence base for the roadmap council (see `docs/councils/2026-07-06-jspace-roadmap-council.md`).

---

## Bottom line

The Anthropic paper **retires the roadmap's biggest hedge** — that a "privileged broadcastable J-space" was unconfirmed. It now exists, is published, and is causal. That upgrades bet A from *"does this exist?"* to *"does it transfer to the GGUF models Helix fronts, and does it help retrieval/assembly?"* The Deep Research **strengthens bet B (splats + density know/go) more than bet A**, supplies a near-drop-in blueprint for it (LatentAudit-style Mahalanobis gating), and exposes one methodological error in Phase 0 that would quietly sink the J-space bet. Recommend: proceed, with a corrected Phase 0 and the subspace disambiguation below; **lead with bet B**; and don't let either bet block the cheap, bet-independent wins already in hand.

## The subspace triad — the central conceptual cleanup

Three distinct low-rank stories are currently braided under one name. Only one is the paper's workspace. **Each phase must declare which it targets and cite the right literature.**

| Subspace | Dim | Space | Defined by | Right use | Wrong use |
|---|---|---|---|---|---|
| **Universal Weight Subspace** (UWSH, arXiv:2512.05117 — *unverified*) | ~16–32 | *parameters* | SVD of weight matrices | LoRA / adapters / model merging | ❌ not a retrieval metric, not the workspace |
| **J-space / workspace** (Anthropic, *verified read*) | ~25 J-lens vectors | *activations, mid layers ("Workspace Band")* | coordinate-corrected **Jacobian** of output logit w.r.t. hidden state | output-shaping (Phase 4/5) | ❌ not found by activation PCA |
| **Activation-variance subspace** | any k | *activations* | PCA/SVD of activations | a retrieval *re-metric* (honest name: "residual-rep subspace") | ❌ not the J-space (it is the ≥90% that ISN'T) |

The roadmap's "universal ~16 directions" motivation is the **weight** subspace — real (if UWSH holds) but about parameter-efficient adaptation, **not retrieval and not the workspace**. Phase 0's proposed PCA finds the **activation-variance** subspace, which the paper says is ≥90% *disjoint* from the J-space (J-space is ≤10% of activation variance). So Phase 0 as written tests the wrong hypothesis for the output-shaping bet.

## What the Anthropic paper establishes (verified read)

- **J-space is real, sparse, causal, and a broadcast format.** J-lens = `E[∂h_final/∂h_ℓ]` — "what the model is poised to verbalize." Its vectors compose with downstream weights *more broadly* than ordinary activations (broadcast). Occupancy is near-zero in early layers, plateaus at a **median ~25 active vectors** in the mid-layer "Workspace Band," collapses at the output. It is **≤10% of activation variance** yet mediates *all* flexible reasoning — a limited-capacity bottleneck.
- **The model actively manages the workspace.** *Directed modulation* ("concentrate on citrus" makes `orange`/`lemon` causally active though unspoken); *multi-hop mediation* (intermediate concepts are causally load-bearing — swap spider→ant flips 8→6 legs); "information not usually in the workspace can be **pulled in** when the task requires it."
- **Injecting a J-lens vector enters the workspace** — inject `lightning` on early tokens → the model introspectively reports detecting it (positive signal for Phase 5 soft-prefix on stacks that allow it).

## What the Gemini Deep Research adds — and the epistemic caution

**Adds (directionally):**
- **Anisotropy → Mahalanobis beats cosine.** The residual stream is dominated by high-variance task-agnostic directions; cosine is the wrong metric. **LatentAudit** (claimed arXiv:2604.05358) is essentially Phase 1 + our faithfulness probe fused: mid-to-late residual pooling, Mahalanobis know-vs-go gating, **Ledoit–Wolf shrinkage**, threshold τ*, 0.942 AUROC on Llama-3-8B. This is a near-drop-in blueprint for bet B.
- **Concepts are depth-extended (CAZ).** Concepts "emerge across a contiguous region and settle after assembly," so single-layer / 25-50-75% pooling is suboptimal — use **Delta-PCA / Windowed-PCA over a layer window**. Refines Phase 0's method regardless of subspace.
- **Phase 5 has concrete methods** — PrefixMemory-Tuning (relocates the prefix *outside* the attention head to dodge attention collapse), SoftSkill/LatentRevise (Frank-Wolfe projection onto the vocab convex hull to fight drift), vLLM `prompt_embeds` + Automatic Prefix Caching.
- **UWSH** (claimed arXiv:2512.05117): k=16–32 captures ≥90% weight-spectral variance across 1,100+ models — the citation for the roadmap's "~16 directions," but in *weight* space (see triad).

**Epistemic caution (load-bearing).** The roadmap author was scrupulous: "could not confirm… treat as hypotheses we re-measure." The Deep Research now *confirms* those exact claims with a wall of citations — which is suspicious in the way confirmation always is. Several anchors are shaky: an off-topic pedestrian-crossing paper cited for Mahalanobis selective prediction, a cluster of future-dated 2605/2606 arXiv IDs, and systems (LatentAudit, PMT, SoftSkill, Polar Transformer, uGMM-NN) none independently verified here. **Treat the Deep Research as hypothesis-generating scaffolding, not verified truth.** The two load-bearing anchors are the Anthropic paper (read) and UWSH (verify before it's load-bearing). Do not downgrade the roadmap's "re-measure on our models" stance to "cited, therefore true."

## Bet-by-bet assessment

**Bet A — J-space as a retrieval/compression target (Phases 0, 2, 3, 4; strong form Phase 5).**
- *Existence:* now published (their models). *Transfer to our GGUF/Ollama stack:* open — this is now the central risk (roadmap Open Q#1, quantization drift).
- *Method error:* Phase 0 PCA ≠ J-lens (fix below).
- *Structural ceiling:* the J-lens is **vocabulary-bounded** — it cannot represent multi-token/non-contiguous spans. Our factual needles are exactly multi-token (`claude-haiku-4-5-20251001`, `PostgreSQL 16`, `51/52`), so J-space output-targeting is weakest precisely where our current gaps are. Bet A likely pays most for *conceptual* retrieval, least for *identifier lookup*.

**Bet B — Gaussian-splat genes + density know/go (Phase 1, decoupled).**
- Best-supported part: a published Mahalanobis-gating blueprint exists; anisotropy is well-established; Ledoit–Wolf is the standard fix the roadmap already named.
- Needs **no model internals at query time** in the embedding-space version, and directly upgrades **#239 (know/miss recalibration)** — live work.
- **Cosine→Mahalanobis whitening of BGE-M3 similarity is a bet-A-independent win available now.**

## Phase-by-phase notes

- **Phase 0 — correction (highest value).** Split into two honestly-labeled probes: (i) *residual-rep* retrieval re-metric (activation geometry vs BGE-M3) with **windowed pooling over a CAZ**, explicitly *not* called "J-space"; (ii) a small *actual Jacobian* J-lens computation on one fp16 model for the output-shaping question. Pre-register bars separately. The UWSH "bonus probe" (principal-angle overlap across models) is already answered for *weights* — don't reuse the number for activations.
- **Phase 1 — strongest; ship early.** Mirror the LatentAudit recipe. Guard the covariance-singularity failure (calibration samples < hidden dim → singular Σ) with Ledoit–Wolf + a corpus prior. First ship density as a **6th logistic feature** in `[know]` before any pure density gate.
- **Phase 3/4 — coverage in J-directions, not PCA directions.** The paper's "same J-lens vector serves many downstream computations" is a facility-location/coverage structure; if Phase 4 shapes coverage, it must be over the *Jacobian* directions the query needs to broadcast.
- **Phase 5 — a legibility conflict, not just a stack gate.** Soft prompts occupy a **non-natural-language, un-decodable manifold** (prompt waywardness). That forfeits the *legible, attributable, know/miss-contracted* context that is Helix's product thesis. Recommend capping at the weak form unless someone defends shipping an illegible-context mode.

## New risks for the roadmap's register

1. **The density gate is blind to corpus poisoning.** Mahalanobis only measures adherence to the *retrieved* context; a faithful generation of *poisoned/false* context scores low anomaly and passes — exactly our SIKE echo-contamination failure (fixed via `HELIX_DISABLE_LEARN`). **Density know/go must be paired with provenance/anti-poisoning** (CWoLa, provenance, claims — Helix already has the scaffolding). Neither alone suffices.
2. **J-lens vocabulary ceiling** — bet A weak on multi-token identifier answers (above).
3. **Depth-extended concepts** — single-layer pooling under-measures; use windowed extraction.
4. **Soft-prefix vs legibility** — a values conflict, above.

## Where our 2026-07-06 empirical work plugs in

- **The eval harness — Phase 0's self-described "single highest-leverage engineering item" — is half-built.** The SIKE 50-needle set + `bench_needle` scorer + the just-fixed `content_has_answer` metric + the `s3_fts_depth_sweep.py` server-per-config driver are the skeleton of `benchmarks/eval_retrieval.py`. The roadmap assumes "Helix has no gold retrieval set"; we are further along.
- **The faithfulness experiment IS the cheap version of Open Q#3 / Phase 4's secondary measurement** — with/without-context → "does the answer concept enter J-space?" Run it before committing to Phase 0.
- **The splice-truncation bug is Phase 4's problem, broken at the floor.** Helix's *current* output shape discards answers via a 1000-char query-agnostic prefix cut (`context_manager.py:1707` → `headroom_bridge.py:227`; `headroom_ai` absent), and the answer often lives only in `complement`, which is never emitted. Fix this mundane output-shape bug (query-aware splice / budget-proportional cap / emit complement) **before** building J-space coverage-shaped assembly on top of a last stage that throws the answer away.
- **RRF is the current baseline to beat** (Run-2: RRF `content_has_answer` 0.72 vs additive 0.58 on xl).

## Recommendations

1. **Reorder: lead with bet B.** Best-supported, needs no query-time internals, upgrades #239. First experiment: **cosine→Mahalanobis embedding-space whitening** (bet-A-independent).
2. **Re-scope Phase 0** around the subspace triad; windowed pooling; separate retrieval-metric vs Jacobian-output probes with separate pre-registered bars.
3. **Add the four risks** above to the register; elevate quantization drift (Open Q#1) to primary — our default stack is GGUF/Ollama.
4. **Cap at the weak form** unless the illegible-context tradeoff is explicitly defended.
5. **Keep the prepaid work moving now:** splice fix, `eval_retrieval.py`, faithfulness probe — all pay off on a total J-space kill.

## Decision points for the council

1. Accept the **three-subspace split** and re-scope Phase 0 accordingly?
2. **Lead with bet B + Mahalanobis whitening** rather than the J-space probe?
3. Is **Phase 5 off the table on legibility grounds**, or kept as a research escape hatch?
4. Who **verifies UWSH + LatentAudit** before either becomes load-bearing?
5. Which single **fp16 model** do we calibrate on (must have a real fp16 parent, not only GGUF)?
