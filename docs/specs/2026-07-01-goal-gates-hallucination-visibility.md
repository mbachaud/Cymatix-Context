# Goal gates: 90/10 → 95/5 + hallucination visibility

**Date:** 2026-07-01 · **Companion:** `docs/research/2026-07-01-next-bench-wave-consensus.md`,
2026-06-10 tuning roadmap §3 · **Status:** telemetry wired (this PR); gates defined below.

## North-star goals

| Horizon | Accuracy / correctness | Hallucination | Condition |
|---|---|---|---|
| **Near (G2)** | ≥ 90% avg across functions | ≤ 10% avg, **visible in telemetry** | all helix-context delivery paths |
| **Long (G3)** | ≥ 95% code AND prose | ≤ 5% both tracks | + abstain triggers search with the abstain as reference doc |

## Definitions (so the numbers mean one thing)

- **Hallucination rate** = confidently-wrong / answered: responses judged incorrect
  (bench rubric / LLM judge) among those where helix emitted `know` (not
  miss/abstain). Abstains are NOT hallucinations — they are the mechanism that
  buys the budget. Measured by scored benches; **not** directly observable at
  runtime.
- **Runtime proxy (the "visible" part)** = the know/miss/abstain event stream.
  Every turn now lands on `helix_know_decision_total{outcome,reason}`; the
  calibration mapping confidence → P(correct) (`scoring/know_calibration.py`,
  `[know]` floors) converts the confidence histogram into an *expected*
  hallucination bound. Scored bench runs recalibrate that mapping; Grafana
  carries it between runs.
- **Accuracy source of truth** = scored benches per track: prose = ERB 500-question
  correctness (published baselines: BM25 68.8 / Vector 51.4 / Onyx+GPT-4 72.4);
  code = ContextBench packet recall + (post-merge) RepoBench-R/CodeRAG-Bench;
  navigation/agentic = SNOW-2 arms.

## The instrument set (roadmap §3, landed in two phases)

**#209 phase 1** (already on master before this spec): `helix_know_decision_total`
{outcome ∈ know|miss|abstain, reason (= "none" for know)} in the
`decide_know_or_miss` wrapper; `helix_dense_cosine` {arm ∈ hot|cold} at both
computation sites; `helix_shard_fanout` + `helix_shard_discrimination`
(routed/known ∈ [0,1]) in `shard_router.query_genes`;
`helix_session_tokens_saved_total` and `helix_splice_ratio`
{caller_model_class} in `_assemble`.

**#209 phase 2** (this change):

| Series | Labels | Emission site |
|---|---|---|
| `helix_know_confidence` | — | know branch of the `decide_know_or_miss` wrapper |
| `helix_abstain_total` | gate (floor_and_ratio\|ratio_only), fusion_mode | `pipeline/tier_logic.py` ABSTAIN gate |
| `helix_freshness_demotion_total` | status (stale\|missing\|unknown\|superseded) | `retrieval/freshness.py` |
| `helix_session_elided_total` | — | `_assemble` elision branch (event count beside tokens-saved) |
| `helix_pki_candidates` / `helix_pki_pairs_skipped_total` | — | `knowledge_store` PKI tier |
| `helix_fingerprint_filtered_total` | cause (floor\|cap) | `server/routes_context.py` /fingerprint |
| `helix_ingest_vram_bytes` | — | `backends/bgem3_codec.encode_batch` |

All lazy-getter pattern, no-op when OTel is off, `try/except` guarded — zero
hot-path risk. Tests: `tests/test_telemetry_wiring.py`.

## Gates and their PromQL

**G1 — visibility gate (precondition, closes now):** during any bench run or
live session with `HELIX_OTEL_ENABLED=1`, these series exist and move:

```promql
# Non-know share of turns (the observable slice of the 10% budget;
# outcome is know|miss|abstain — abstains count as coverage loss)
sum(rate(helix_know_decision_total{outcome=~"miss|abstain"}[15m]))
  / sum(rate(helix_know_decision_total[15m]))
# Expected-correctness floor from calibrated confidence
histogram_quantile(0.5, rate(helix_know_confidence_bucket[1h]))
# Stale-answer suppression activity
sum by (status) (rate(helix_freshness_demotion_total[1h]))
```

Alert sketch: miss-share > 0.10 sustained 1h → warn ("hallucination budget
spent on visible misses — retrieval, not honesty, is the bottleneck");
know-share with confidence < emit_floor+margin trending up → recalibrate.

**G2 — 90/10 gate:** per track, scored run ≥ 90% correctness AND judged
hallucination ≤ 10% AND G1 series captured during the run (the run must be
observable, not just scored). Prose vehicle: ERB scored 500-q @500K fixture.
Code vehicle: ContextBench packet + external benches per the council plan.

**G3 — 95/5 gate:** same vehicles, both tracks, plus the abstain→search loop
live (below). Do not tune abstain floors on the bench that grades the gate
(held-out discipline, same rule as the cap sweep).

## Abstain → search escalation (the G3 mechanism)

Already in the tree: `MissBlock.reason` + `refresh_targets` +
`escalate_to=_pick_escalation(query, reason)` — the packet already tells the
agent *what to do next*. The extension:

1. **Reference doc:** on miss/abstain, assemble a compact "abstain packet" —
   query, miss.reason, top-k below-floor candidates (titles + fired tiers +
   confidence), refresh_targets. This is the search *seed*, not delivered
   context.
2. **Escalation branch:** extend `_pick_escalation` with `web_search` /
   `external_rag` targets; `/context/packet` carries an `escalation` block:
   `{action: "search", seed: <abstain packet>, reason}`.
3. **MCP surface:** `helix_search_escalate` tool (or client-side: Claude/agent
   sees `miss{reason}` and calls its own search with the seed attached as the
   reference doc).
4. **Persistence loop:** search results get `/ingest`-ed with provenance →
   next turn the same query lands `know` — the miss literally repairs the
   corpus. `helix_know_decision_total{reason}` rates measure repair velocity.

Sequencing: build as SNOW-2 **arm E extension** (the 2026-06-10 roadmap §1
already specs miss-reason-driven escalation); wire external search only after
arm E's internal escalation baseline exists — otherwise the search lift is
unattributable.

## The loop (standing cadence)

1. **Status checks** — regression-log delta + branch/PR sweep + G1 dashboard
   glance. Per session start.
2. **Testing/tuning** — per merged change: targeted suites + the council
   plan's bench arms; sweeps only on held-out corpora; every run with OTel on
   so it doubles as calibration capture.
3. **Telemetry completion** — remaining roadmap §3 items as they become
   load-bearing: PKI counters, dense cold-arm, VRAM gauge, fingerprint
   floor/truncation counters; Grafana panels for today's nine series; the
   `genai_telemetry` docs-vs-code drift (OBSERVABILITY.md documents a module
   that doesn't exist — land or strike).

## Follow-ups / known gaps

- Grafana dashboard JSON has no panels for the new series yet (add to
  `deploy/otel/grafana/dashboards/helix-overview.json`; delete the dead
  pipeline-observatory dashboard per roadmap §3a).
- `helix_dense_cosine{arm="cold"}` not yet emitted (cold-tier peek site).
- OBSERVABILITY.md needs the nine new series documented (fold into the
  config-doc sync that gates the #228/#229 merge).
- Pre-existing: `scripts/calibrate_know_confidence.py --smoke` fails
  ("features must have length 5 per row") on this checkout — pre-dates this
  change; verify on the rig and fix before the next know-floor calibration.
- Session-elision savings assume estimate_tokens parity between spliced text
  and stub — good enough for the ~40% claim's direction, not for billing.
