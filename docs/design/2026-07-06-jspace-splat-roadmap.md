# 2026-07-06 — J-Space Alignment + Gaussian-Splat Genes: Phased Roadmap

Roadmap for taking Helix from token-level proxy retrieval (FTS5 + BGE-M3 + cymatics + RRF)
to (A) retrieval/compression targeted at the decoder's residual-stream subspace ("J-space")
and (B) genes represented as Gaussian splats (mean + covariance) with density-based
know/go and native level-of-detail rendering.

**Epistemic status of the two motivating claims.** A literature sweep (2026-07-06) found
adjacent work — residual-stream subspace steering, low-rank weight/gradient subspaces
(WeLore, GaLore-family) — but could not confirm the specific results as stated
("broadcastable cognition lives in a privileged J-space"; "universal ~16 dominant weight
directions, 19–100x reduction"). Treat both as **hypotheses this roadmap re-measures on
our own models**, not as established inputs. Phase 0 exists to kill the bet cheaply if
the first hypothesis is false for the models Helix actually fronts.

**The structural honesty problem, stated up front.** Helix injects *text into a prompt*.
It never touches the decoder's residual stream at inference time, and on the default
stack (Ollama / OpenAI-compatible upstream) it *cannot* — no hidden-state access, no
soft-prompt injection. So "injection-shape targeting" decomposes into two very different
claims:

1. **Weak form (testable now):** the decoder's residual geometry is a better *retrieval
   and compression metric* than BGE-M3 cosine — i.e., selecting/compressing text by where
   it lands in J-space picks better context at equal token budget. Requires model
   internals **offline only** (calibration), not at query time.
2. **Strong form (stack-gated):** rendering context directly as residual-stream vectors
   (soft prefix) beats any text injection. Requires an inference stack we control
   (HF transformers / vLLM / custom llama.cpp), which most local deployments won't have.

Phases 0–4 pursue the weak form; the strong form is quarantined in Phase 5 behind an
explicit go/no-go. The splat representation (B) is deliberately decoupled: it delivers
value in plain embedding space even if J-space fails.

## TL;DR

| Phase | Bet type | Deliverable | Kills the bet if... | Effort |
|---|---|---|---|---|
| 0. J-space retrieval probe | RESEARCH | Offline benchmark: J-subspace retrieval vs BGE-M3 | J-space ≤ BGE-M3 across layers/ranks | S–M |
| 1. Splat genes in embedding space | ENGINEERING + small RESEARCH | `splats` table, Mahalanobis know/go behind flag | density no better calibrated than logistic | M |
| 2. Query-side J-projection head | RESEARCH | CPU-cheap text→J-coords encoder | head can't predict J-coords OOD | M |
| 3. J-space splats as a fusion tier | ENGINEERING | `jspace` tier in Fuser, LOD render path | tier weight tunes to ~0 on eval | M |
| 4. Coverage-shaped assembly | RESEARCH + ENGINEERING | splice/assemble optimizing J-space coverage | no end-to-end QA gain at fixed budget | M–L |
| 5. Activation-level injection | RESEARCH (stack-gated) | soft-prefix renderer for HF/vLLM stacks | text injection already captures the gains | L–XL |

Ordering rationale: Phase 0 is the smallest experiment that can falsify the core premise
(weeks, zero schema changes). Phase 1 is independent insurance — it ships even on a
Phase 0 kill. Phase 2 de-risks the query-time internals problem before Phase 3 builds
production plumbing on it. Phase 4 is where the bet pays or doesn't. Phase 5 only makes
sense if Phase 4 shows gains that plateau below the offline ceiling.

## Phase 0 — The kill-shot: does decoder residual geometry beat the embedding proxy?

**Goal.** Establish, on one local model Helix actually fronts (e.g. a Qwen or Llama
variant), whether similarity in a low-rank residual-stream subspace retrieves better
context than BGE-M3 cosine. This is the falsifiable core of bet A: if the decoder's own
geometry isn't a better retrieval metric than the proxy, injection-shape targeting has
no foundation and Phases 2–5 die here.

**Research vs engineering.**
- RESEARCH: whether a privileged low-rank subspace exists and is retrieval-useful; which
  layer(s); what rank. All uncertain.
- ENGINEERING (prerequisite, do first): a labeled eval harness. Helix has no gold
  retrieval set today — `cwola_log` gives weak labels (requery-delta buckets), which is a
  start, but Phase 0 needs ~200–500 (query, relevant-gene) pairs. Build
  `benchmarks/eval_retrieval.py` + a curated eval set from the corpus. This harness is
  reused by every later phase; it is the single highest-leverage engineering item in the
  whole plan.

**Method.** Offline, under HF transformers with output_hidden_states (NOT Ollama — flag:
this requires fp16/bf16 weights and a GPU with room for the model; quantized GGUF via
llama.cpp does not expose hidden states without a fork):
1. For each gene's content and each eval query, capture mean-pooled residual activations
   at a sweep of layers (e.g. 25%, 50%, 75% depth).
2. SVD/PCA the gene-activation matrix per layer → candidate subspaces at ranks
   {4, 8, 16, 32, 64}. (This is also the local re-measurement of the "~16 directions"
   claim: look at the spectrum's knee.)
3. Score retrieval by cosine/Mahalanobis in each (layer, rank) subspace; compare
   Recall@k and nDCG@k against BGE-M3 cosine on `embedding_dense_v2`, and against the
   full RRF stack as the deployed baseline.
4. Bonus probe for the "universal" claim: repeat on a second model family; measure
   principal-angle overlap between the two subspaces after Procrustes alignment.

**Schema/pipeline changes.** None. Pure offline scripts under `benchmarks/`.

**Validation.** Pre-register the success bar: J-subspace beats BGE-M3 by ≥5 points
Recall@10 at some (layer, rank), or beats the full RRF stack at equal candidate depth.
"Ties BGE-M3" is a kill — the proxy is drastically cheaper.

**Risk / fallback.** Main risk: mean-pooling washes out the signal (residual geometry is
token-positional; a document doesn't have one location, it has a trajectory). Fallback
within the phase: try last-token pooling and per-chunk pooling before concluding. If all
pooling schemes lose: **kill Phases 2–5, proceed with Phase 1 only** (splats in embedding
space), and write the negative result up in docs/design/ — it's a real finding.

## Phase 1 — Gaussian-splat genes in the space we already have

**Goal.** Replace the point-estimate gene (one 1024-d BGE-M3 vector) with a soft
distribution: mean + low-rank-plus-diagonal covariance fitted from the gene's chunk/
fragment embeddings. Make know/go a density decision. This is bet B decoupled from bet A:
it stands on its own as a calibration and LOD upgrade, in plain embedding space.

**Research vs engineering.**
- ENGINEERING: fitting μ and Σ = FFᵀ + D (F: d×k factors, k≈8–16; D diagonal) from chunk
  embeddings at ingest; Woodbury identity makes Mahalanobis O(dk) — trivial at query
  time. Storage, backfill script, config plumbing: all known-how.
- RESEARCH (small but real): (a) covariance estimation from few chunks is degenerate —
  a 3-chunk gene gives a rank-2 scatter in 1024-d; needs shrinkage (Ledoit–Wolf or a
  corpus-level prior covariance blended per-gene). (b) whether the density signal is
  actually better *calibrated* than the current 5-feature logistic
  (`know_calibration.py`), not just different.

**Schema/pipeline changes.**
- New table (not columns on `genes` — a gene will eventually have one splat per space):
  `splats(gene_id, space_id, mean BLOB f32, cov_factors BLOB f32, cov_logdiag BLOB f32, rank INT, n_obs INT, updated_at REAL, PRIMARY KEY(gene_id, space_id))`
- New table `spaces(space_id PK, kind TEXT 'embedding'|'jspace', model_id TEXT, layer INT, dim INT, basis BLOB, center BLOB, calibrated_at REAL, calibrated_on_n INT)` — Phase 1 registers one row (`embedding`/BGE-M3/1024); Phase 3 adds J-space rows.
- Ingest: fit splat after chunk embedding (already computed); backfill via
  `scripts/backfill_splats.py` (pattern: `backfill_bgem3_v2.py`).
- Know path: add per-candidate log-density and mixture log-density over top-K as
  features. **Fallback-friendly integration:** first ship density as a 6th logistic
  feature (one more beta in `[know]`, recalibrate via
  `scripts/calibrate_know_confidence.py`); only replace the logistic with a pure
  density threshold if it wins head-to-head.
- Config: `[splat] enabled=false, rank=16, shrinkage="ledoit_wolf", min_chunks=2`.
- KnowBlock: optional fields `log_density`, `mahalanobis_top1`, `space_id` (back-compat:
  Optional, absent when flag off).

**Validation.** On CWoLa-derived labels + the Phase 0 eval set: risk–coverage curves
(selective prediction) and ECE for (i) current logistic, (ii) logistic+density feature,
(iii) pure density threshold. Success: density variant dominates the risk–coverage curve
— fewer wrong "know"s at equal coverage. Also check the failure mode the scalar logistic
can't express: queries landing *between* two gene modes (high top_score, high ambiguity)
should now abstain.

**Risk / fallback.** Risk: covariances collapse toward the shared prior (all genes get
the same Σ), making Mahalanobis ≈ scaled cosine — lots of machinery, no information.
Detect by measuring inter-gene covariance divergence; if low, fallback is to keep only
the density-as-logistic-feature integration (cheap, already useful) and drop the pure
density gate.

## Phase 2 — Query-side J-projection without model internals at query time

**Goal.** The hard practical problem Phase 0 exposes: gene J-coordinates can be computed
offline (genes are static; batch a forward pass over the corpus), but the *query* arrives
at request time on a stack (Ollama) that exposes no hidden states. Deliverable: a small
CPU-cheap projection head (linear probe or 2-layer MLP) mapping query text — or, cheaper,
the BGE-M3 query embedding Helix already computes — to predicted J-coordinates.

**Research vs engineering.**
- RESEARCH: whether J-coords are predictable from text/proxy-embedding with enough
  fidelity that retrieval rankings survive. If the J-space were a linear function of
  semantic content, a linear probe from BGE-M3 space would suffice — that would itself
  be evidence the "privileged subspace" is mostly re-expressed semantics (a partially
  deflationary but useful finding).
- ENGINEERING: training loop, ONNX/CPU export, latency budget (must fit Helix's
  no-model-at-query-time ethos; target <10 ms CPU).

**Schema/pipeline changes.** None in SQLite yet. Artifact: `models/jproj_{model_id}.onnx`
+ `[jspace] projection_head_path`. Training data: (text, true J-coords) pairs harvested
during the Phase 0 offline pass — keep that capture script.

**Validation.** Rank correlation (Kendall τ) between retrieval rankings under true
J-coords vs predicted J-coords on held-out queries; success bar τ ≥ 0.8 and ≤2 point
Recall@10 drop vs true coords. Test OOD: train on corpus-domain queries, eval on
novel-domain ones.

**Risk / fallback.** Risk: prediction is fine in-domain, collapses OOD. Fallbacks, in
order: (a) restrict J-tier to high-confidence predictions (predict with an ensemble,
gate on variance); (b) if a linear probe from BGE-M3 works, J-space becomes a learned
*re-metric* on the existing embedding — no new encoder at all, just a d×r matrix stored
in `spaces.basis`; (c) if nothing predicts J-coords, J-space is only usable on
hidden-state-exposing stacks — demote Phases 3–4 to an opt-in profile for HF/vLLM users.

## Phase 3 — J-space splats as a first-class retrieval tier + LOD rendering

**Goal.** Production integration: fit Phase-1 splats in the Phase-0 J-space (a second
`spaces` row per model profile), add a `jspace` tier to the RRF `Fuser`, and implement
level-of-detail rendering from the splat eigenstructure.

**Research vs engineering.** Mostly ENGINEERING — the uncertain parts were burned down
in Phases 0–2. Residual RESEARCH: per-model space management. If the universal-subspace
hypothesis holds, one canonical basis + a cheap per-model Procrustes rotation suffices
(store rotation in `spaces.basis`); if it fails (Phase 0 bonus probe answers this),
each model profile needs its own splat fit — storage cost ~(d·k + 2d) floats per gene
per space, so ~80 KB/gene at d=1024, k=16 fp32; consider fp16 and k=8 for the
per-model case.

**Schema/pipeline changes.**
- `splats` gains rows with `space_id` = jspace profile; `spaces` rows per (model, layer).
- `retrieval/fusion.py`: `fuser.add_tier("jspace", ranked, weight=cfg.retrieval.jspace_weight)` — zero-weight disables, consistent with existing knobs.
- Know decision (`scoring/know_decision.py`): density evaluated in the space matching
  the *upstream model actually being proxied* (`[server] upstream` → model_id → space).
- LOD render path in `encoding/`: eigendecompose Σ once at fit time, store eigenvalues
  with factors; a device budget maps to rank r — watch: mean only; laptop: r=4;
  workstation: r=16. Downstream consumer is Phase 4's assembler; also expose over
  `/fingerprint` (it's already the "navigation, not content" endpoint — natural fit).
- Config: `[jspace] enabled, model_profile, layer, rank; [retrieval] jspace_weight`.

**Validation.** Ablation on the eval harness: full RRF stack ± jspace tier, tuned
weights. Success: jspace tier earns nonzero weight under tuning and improves nDCG@10
≥3 points, or displaces a costlier tier (e.g. lets you disable SPLADE — see
2026-07-05 efficiency doc — at neutral quality). LOD: quality-vs-rank curve must be
monotone and saturate near the Phase-0 knee; if quality at r=4 ≈ r=16, the
16-direction budget story is confirmed in-store.

**Risk / fallback.** Risk: tier is additive-noise — RRF tuning drives its weight to ~0.
That's a clean negative result; keep the splat/LOD machinery (it's space-agnostic,
Phase 1 already justified it) and drop only the J-space rows.

## Phase 4 — Injection-shape targeting: coverage-shaped splice and assembly

**Goal.** The payoff phase for the weak form. Change *what gets spliced and assembled*:
instead of per-document relevance + token budget, treat the query as a target region in
J-space (predicted coords + uncertainty from Phase 2) and select fragments whose splat
mixture *covers* that region — max-coverage / facility-location objective, greedy
(submodular, so greedy is near-optimal and fast). Redundant fragments that pile onto the
same J-direction get elided even when individually high-scoring; fragments covering an
otherwise-empty direction of the query region get in despite mediocre lexical scores.

**Research vs engineering.**
- RESEARCH: does J-coverage selection improve *downstream answer quality* at fixed token
  budget? This is the first phase measuring end-to-end task performance, not retrieval
  metrics — and the first real test of whether text selection can shape what the decoder
  receives (the weak form's core assumption that the text→residual map is controllable
  enough to make selection-side targeting matter).
- ENGINEERING: greedy coverage selector in `pipeline/` (Stage 4/5 seam — `interference_trim`
  in `scoring/cymatics.py` is the existing per-fragment scoring hook to mirror);
  per-fragment splats (fragments are already chunk-level, so Phase 1 fitting covers
  them); budget interaction with `[budget] expression_tokens` and tier logic.

**Schema/pipeline changes.** Fragment-level splat rows (`is_fragment` genes) in `splats`;
`[splat] fragment_splats=true`; assembler flag `[budget] assembly_objective =
"relevance" | "jcoverage"`. Session working-set register interaction: already-delivered
documents should count as *already-covered* J-region — a nice unification with the
existing elision logic in Stage 5.

**Validation.** End-to-end: fixed 7,000-token budget (`[budget] expression_tokens`
default), same upstream model, QA exact-match/faithfulness on the eval set, relevance
assembly vs jcoverage assembly. Pre-register: ≥5% relative EM gain or equal EM at ≥25%
fewer injected tokens. Secondary: measure offline that jcoverage-selected context
actually moves the decoder's residual state closer to the target region than
relevance-selected context (this directly tests text→activation controllability and
tells you whether Phase 5 has headroom).

**Risk / fallback.** Risk: gains exist in retrieval metrics but vanish end-to-end —
decoders are robust to context composition, and shaping doesn't matter once the right
facts are present in any form. Fallback: keep coverage selection only for the
*redundancy-elision* half (it subsumes and improves the session working-set dedup),
drop the exotic half.

## Phase 5 — (Gated) Activation-level injection: rendering splats as soft prefix

**Goal.** Strong form: skip text entirely for part of the context — render selected
splats as continuous vectors injected as prefix embeddings / KV cache. Only enter this
phase if Phase 4 succeeded *and* its secondary measurement shows text injection
saturating well below the offline ceiling.

**Flagged constraints (read first).**
- Requires an inference stack accepting embedding-level input: HF transformers (yes),
  vLLM prompt-embeds (yes), llama.cpp (no without a fork), Ollama (no). This will never
  be the default Helix path; it's an opt-in backend profile.
- Soft prompts are model- and even finetune-specific; every upstream swap invalidates
  the renderer.
- Learned-prefix training needs labeled behavioral data and GPU training time — this is
  a prefix-tuning research program, not a retrieval feature.

**Research vs engineering.** Almost entirely RESEARCH. Deliverable: a
proof-of-concept renderer + a report, not a production feature.

**Schema/pipeline changes.** New backend in `backends/` implementing an
embedding-injection client; `[ribosome] backend = "hf_softprefix"` experimental value.
No storage changes — splats already carry everything.

**Validation.** Same end-to-end harness as Phase 4: soft-prefix vs jcoverage-text vs
relevance-text at matched *effective* budget (count prefix vectors at their KV cost).
Success: beats jcoverage-text at equal cost, on the stacks that support it.

**Risk / fallback.** Highest-risk phase; expected outcome is honestly "text was fine."
Fallback is free: Phases 0–4 don't depend on it, and a negative result caps the
architecture cleanly at the weak form.

## Cross-phase notes

**Model-internals access, summarized.** Offline calibration (Phases 0, 2, 3 fitting):
needs HF-format weights + GPU, once per model profile — acceptable. Query time: never
requires internals in Phases 0–4 (that is exactly what Phase 2 buys). Phase 5: requires
stack control at inference time. Any local model available only as GGUF-quantized with
no fp16 counterpart cannot get a J-space profile without a llama.cpp fork that dumps
hidden states — for those models Helix stays on the embedding-space splat path
automatically (`spaces` row absent → jspace tier silently off).

**What survives a total J-space kill.** The eval harness (Phase 0), splat genes +
density know/go (Phase 1), and coverage-based redundancy elision (Phase 4 fallback).
That's a defensible roadmap even if both motivating claims are false — worth stating
because it means the maximum downside of the bet is bounded and mostly prepaid.

**Doc/config honesty.** Per the 2026-07-05 audit pattern: every new knob ships with
code-default = toml-default, off by default, and a design-doc line item. Splat and
jspace paths must degrade to current behavior when flags are off — all KnowBlock
additions Optional.

## Open questions that would most change this plan

1. **Is J-space stable under quantization and fine-tuning?** If the subspace of a Q4
   GGUF (measured via its fp16 parent) drifts materially from what the quantized model
   actually computes, offline calibration is systematically wrong for the deployed
   model, and per-model profiles become per-*artifact* profiles. This single question
   determines whether Phase 3's profile management is a one-off cost or a treadmill.
   (Measurable in Phase 0 at small cost: compare fp16 vs quantized hidden states on one
   model via a llama.cpp debug build.)

2. **Are query J-coordinates predictable from text without a forward pass?** If no
   (Phase 2 fails OOD), J-space retrieval is only viable on HF/vLLM stacks and the
   default Ollama path is permanently excluded — the bet shrinks from "Helix's new
   architecture" to "an opt-in profile for heavyweight deployments." If yes via a
   *linear* probe from BGE-M3, the philosophically interesting version of the claim
   deflates (J-space ≈ relabeled semantics) but the engineering gets dramatically
   cheaper — one d×r matrix, no new encoder.

3. **Is the text→residual-stream map controllable enough that selection-side shaping
   matters?** Phase 4's secondary measurement answers this. If context composition
   barely moves where the decoder lands in J-space (robust re-encoding), then only
   activation-level injection (Phase 5, stack-gated) can realize bet A, and the weak
   form quietly reduces to a better redundancy metric. If it moves a lot, Phases 4
   gains are real and Phase 5 may be unnecessary.
