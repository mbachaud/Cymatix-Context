# Spec: abstain → search escalation (SNOW-2 arm E extension)

**Date:** 2026-07-01 · **Status:** DRAFT (research-grounded; build after arm-E internal baseline)
**Companions:** `docs/specs/2026-07-01-goal-gates-hallucination-visibility.md` (G2/G3 gates),
2026-06-10 tuning roadmap §1 (SNOW-2 arm E) · **Leads:** Max + Claude
**Research basis:** R2 web survey 2026-07-01 (citations inline; VERIFIED = read on page).

## 1. Design

On `miss{reason}` or `know.confidence < floor`, the packet already tells the agent
what to do next (`escalate_to`, `refresh_targets`). The extension makes the
escalation executable and persistent:

1. **Abstain packet (search seed).** query + `miss.reason` + below-floor
   candidate titles/fired-tiers/confidences + `refresh_targets`. Compact,
   provenance-tagged, never delivered as context — it is the *reference doc*
   for the search.
2. **Reason-mapped actions** (CRAG's Correct/Incorrect/Ambiguous taxonomy mapped
   onto our richer enum):
   - `stale` / `superseded` / `cold` → internal refresh: re-read
     `refresh_targets`, `/consolidate`, re-query. No external search.
   - `sparse` / `no_promoter_match` → tiered external escalation.
   - `abstain` (weak-and-flat) → query reformulation first (the packet's
     below-floor titles seed the rewrite), then external tier.
   - `denatured` → operator alert, no auto-search (corpus integrity issue).
3. **Tiered `escalate_to`** (FrugalGPT cascade pattern): local corpus →
   upstream RAG (`[headroom]` / configured upstream) → web search; each tier
   has its own acceptance threshold before escalating further.
4. **Round budget** (SIM-RAG): sufficiency check per escalation round, cap ≤3
   rounds; every round's decision is the same calibrated gate, not a new
   mechanism.
5. **Write-back with provenance.** Accepted results are `/ingest`-ed with
   `source_kind=escalation`, provenance carrying the trigger (query, reason,
   tier) — so the next identical query is a `know` and the escalation cost is
   amortized across future queries (Evolve's persistence pattern). Web content
   enters **subject to the freshness gate and provenance tiers, never as
   authoritative** — CRAG treats web as complementary, so do we.
6. **Telemetry.** Rides the wiring landed today: `helix_know_decision_total`
   (repair velocity = miss-rate decay per reason), plus a new
   `helix_escalation_total{tier, reason, outcome}` when this ships.

## 2. Prior-art grounding (why this shape)

No published system combines all four pieces; each piece is separately
validated:

| Piece of our design | Closest precedent | Evidence |
|---|---|---|
| Calibrated-classifier abstain gate (no query-time LLM) | Kamath et al. 2020, selective QA calibrator | beats softmax: 56% vs 48% coverage @ 80% acc — [arXiv 2006.09462](https://arxiv.org/abs/2006.09462) VERIFIED |
| Post-retrieval evaluator → web-search escalation | CRAG | lightweight trained evaluator, {Correct, Incorrect, Ambiguous} → refine / discard+web / blend — [arXiv 2401.15884](https://arxiv.org/abs/2401.15884) VERIFIED |
| Cheap feature-based gate is competitive with LLM-uncertainty gates | LLM-Independent Adaptive RAG (27 external features, zero LLM calls at decision time) | matches LLM-based methods on 6 QA sets with large efficiency gains — [arXiv 2505.04253](https://arxiv.org/abs/2505.04253) VERIFIED; corroborated by the AdaRAGUE 35-method survey — [arXiv 2501.12835](https://arxiv.org/abs/2501.12835) VERIFIED |
| Persistent write-back / corpus self-repair | Evolve (teacher-compiled store, usage-driven refresh; 2B model 20–33%→60–84% while halving teacher calls) | [arXiv 2604.23424](https://arxiv.org/abs/2604.23424) UNCONFIRMED (snippet) |
| Auto-labeled trigger training (no annotation) | Adaptive-RAG (labels = which strategy sufficed) + SIM-RAG (self-practice rollouts labeled by final-answer correctness) | [arXiv 2403.14403](https://arxiv.org/abs/2403.14403), [arXiv 2505.02811](https://arxiv.org/abs/2505.02811) VERIFIED |

**Explicitly rejected** (with reasons): Self-RAG reflection tokens (bespoke
generator, breaks proxy transparency — [2310.11511](https://arxiv.org/abs/2310.11511));
FLARE/DRAGIN logit triggers (need mid-decode logits we don't have upstream —
[2305.06983](https://arxiv.org/abs/2305.06983)); SeaKR internal-state
uncertainty (white-box only — [2406.19215](https://arxiv.org/abs/2406.19215));
Rowen multi-sample consistency (2×+ LLM calls to detect what retrieval features
already signal — [2402.10612](https://arxiv.org/abs/2402.10612)); trusting
downstream self-abstention — AbstentionBench shows reasoning fine-tuning
*degrades* abstention by ~24%, so the gate must stay ours —
[2506.09038](https://arxiv.org/abs/2506.09038) VERIFIED.

## 3. Trigger training loop (auto-labeling)

Adopt Adaptive-RAG/SIM-RAG's trick against our own telemetry: each judged eval
run yields (features, outcome) pairs — know-that-was-wrong = calibrator false
positive; miss-that-search-repaired = correct escalation; miss-that-search
didn't-repair = corpus gap. Re-fit `[know]` betas from these via
`scripts/calibrate_know_confidence.py` (5-feature path fixed 2026-07-01). No
human labels. The `helix_know_confidence` histogram + judged outcomes are the
training stream.

## 4. Judge protocol for the G2/G3 gates (the measurement half)

- **Judge:** frontier-class model from a **different family than the answer
  generator**, **reference-guided** (question + gold + answer in prompt);
  cross-check a 10% subsample with a second judge family + human spot-audit,
  require ≥0.8 agreement. Judge-bias basis: position/verbosity/self-enhancement
  biases and mitigations — [arXiv 2306.05685](https://arxiv.org/abs/2306.05685) VERIFIED.
- **Rubric:** single-answer trinary — CORRECT / INCORRECT / ABSTAINED.
  Single-answer grading sidesteps pairwise position bias.
- **Scoring:** hallucination = INCORRECT / (CORRECT + INCORRECT); abstains go
  to **coverage** = answered/total. Report the (risk, coverage) pair and the
  risk-coverage curve (Kamath framing). **Gate = risk ≤10% AND correctness
  ≥90% among answered AND a coverage floor** — without the floor the gate is
  gameable by abstaining on everything.
- **Sample size** (two-proportion normal approx., computed): distinguishing
  true 15% from claimed 10% needs **n≈316 answered** (two-sided α=0.05, power
  0.8; n≈438 at power 0.9). At n=300, p̂=10% → 95% CI ±3.4pp. For the 5%-vs-10%
  long-goal gate: n≈264 (power 0.9). **Standing recommendation: ≥350 answered
  items per gate run (~500 queries at ~70% coverage), refreshed per release.**
- **Auxiliary monitor:** MiniCheck-class CPU fact-checker
  ([arXiv 2404.10774](https://arxiv.org/abs/2404.10774) UNCONFIRMED) or RAGAS
  faithfulness ([docs](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)
  VERIFIED) as always-on groundedness telemetry — but **never the gate**:
  groundedness ≠ correctness (a faithful answer to stale context still passes
  groundedness; that error class is exactly what our freshness gate exists for).

## 5. Sequencing

1. SNOW-2 arm E **internal** escalation baseline first (fingerprint triage →
   expand → gene_get) — otherwise external-search lift is unattributable.
2. Then external tier + write-back behind a flag (`[escalation]` section:
   enabled, max_rounds=3, tier order, acceptance thresholds).
3. Gate runs per §4 with OTel on; betas re-fit per §3 after each run.

> Research provenance: R2 agent survey 2026-07-01; VERIFIED/UNCONFIRMED tags
> preserved from the agent's page-level verification. Statistics computed via
> two-proportion normal approximation, not cited from literature.
