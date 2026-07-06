# Roadmap lineage — 2026-06-07 → 2026-07-06 (council comparison index)

Supporting material for the J-Space roadmap council. One month of forward-planning
docs, oldest → newest. The throughline is a rising ladder of ambition:
**as-shipped reference → tune the retrieval profiles → cut cost → align retrieval
geometry to the decoder's *internal* geometry.** The newest roadmap (J-Space) is
the first to bet on model-internal structure, and the first whose core premise was
just externally validated by a paper (Anthropic, *A Global Workspace in Language
Models*). Compare each against its successor to see where the strategy turned.

| Date | Doc | What it proposed | Relation to the next |
|---|---|---|---|
| 2026-06-07 | [`architecture/…pipeline-and-switchboard`](../architecture/2026-06-07-pipeline-and-switchboard.md) | As-shipped reference: the retrieval pipeline + switchboard. The baseline every later roadmap builds on. | Establishes the surface the profiles spec then re-parametrizes. |
| 2026-06-09 | [`specs/…retrieval-profiles`](../specs/2026-06-09-retrieval-profiles.md) | 3-layer profiles/modes (#205): one static pipeline can't serve all workloads; ~6 knobs are corpus-sensitive; per-corpus calibration + classifier arm + code/prose/small profiles. | The tuning roadmap operationalizes it. |
| 2026-06-10 | [`audits/…test-tuning-roadmap`](../audits/2026-06-10-test-tuning-roadmap.md) | Test / tuning / balancing plan on v0.7.1 against the profiles spec. | Feeds the value-consensus council. |
| 2026-06-12 | [`audits/…pipeline-value-consensus`](../audits/2026-06-12-pipeline-value-consensus.md) | 4-persona consensus council on Helix's next architectural investment. | Precedent for *this* council's format; sets up the July cadence. |
| 2026-07-03 | [`ROADMAP.md`](../ROADMAP.md) | Canonical forward-planning doc from a full repo/issue/bench triage. | The live sequencing spine the efficiency memo and J-space roadmap slot into. |
| 2026-07-05 | [`design/…efficiency-cost-reduction`](../design/2026-07-05-efficiency-cost-reduction.md) | The three-question memo: binary-vs-JSON disk, algorithm-vs-model, per-prompt token cost. Cost-reduction program. | Frees budget/attention for a bigger bet. |
| **2026-07-06** | [`design/…jspace-splat-roadmap`](../design/2026-07-06-jspace-splat-roadmap.md) **(newest)** | **Bet A** (retrieval/compression targeted at the decoder's residual-stream "J-space") + **Bet B** (Gaussian-splat genes, density know/go, LOD). Phases 0–5, weak-form vs stack-gated strong form. | Reviewed in [`design/…jspace-roadmap-review`](../design/2026-07-06-jspace-roadmap-review.md); adjudicated in this council. |

**Reading guide for the council.** The earlier docs optimize *within* the text-retrieval
paradigm (profiles, calibration, cost). The J-Space roadmap is the first to propose
leaving it — targeting the model's internal geometry rather than a text proxy. So the
comparison the council should draw is not "is J-Space a better knob than RRF," but
"is the paradigm jump justified now, given (a) the paper validates the mechanism and
(b) the review shows the cheap in-paradigm wins are not yet exhausted (splice bug,
Mahalanobis whitening, eval harness)."
