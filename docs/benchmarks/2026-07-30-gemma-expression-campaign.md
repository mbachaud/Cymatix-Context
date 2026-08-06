# Gemma expression campaign — the unknowns, measured (2026-07-30)

> Overnight session 1 of 2 (no OTel; session 2 installs the telemetry stack and
> measures its overhead). Bed: clean dogfood `13f8bbe3d3a37912…`, 18 needles.
> Models: `gemma4:e2b` (fits the 3080 Ti, 100% GPU) and `gemma4:e4b`
> (12 GB, spills 59% to CPU — every role it plays costs ~10-20s/call).
> Receipts: `expression_arms.json`, `ollama_concurrency.json`,
> `latency_scaling_1to2.json` (working files); every claim below was
> re-derived by 4 independent verifier agents + a completeness critic from
> the per-needle rows, and the refinements they forced are folded in.

## The one-line result

**Nothing tested tonight moved expressed gold delivery.** All 10 arms —
decoder mode, max_genes=18, splice 0.6/0.9, query-expansion with either
gemma — expressed the identical 9-needle gold set (gold_expressed = 0.5,
zero flips anywhere). The campaign's rank metric moves; the delivery metric
does not. The binding stage is expression selection, not retrieval.

## Where expression actually stops

- Per-arm expressed-genes distribution is `{0: 1, 3: 9, 5: 8}` — **never
  above 5**, which is the hard-coded factual-class cap
  (`retrieval/query_classifier.py:193`, `assembly_max_genes_cap=5`; code
  constants pending #205). Max tokens observed: 4,243 = 61% of the 7,000
  budget. **The budget never binds; the cap and a sub-cap limiter do.**
- `maxg18` (config + call override) changed nothing — byte-identical
  per-needle output to defaults, as did `broad` and both splice arms
  (72 rows × 7 fields, 0 diffs; latencies differ, so these were real
  independent runs). Data claim confirmed exact; causal reading ("knob has
  no effect" vs "override never reaches the pipeline") still needs a
  plumbing trace with effective-config logging.
- **Selection-stage defect, present in all 10 arms:**
  `scorerift_import_hygiene_confidence` ranks **4** with 5 genes expressed —
  and gold is not among them (same shape: `cymatix_proxy_port`, rank 7,
  3 expressed). Expression is not taking retrieval's top-k; something
  (freshness gate, tie-break, tier logic) evicts a rank-4 gold doc. This
  also means rank-based recall@12 (0.667–0.833 tonight) **overstates**
  end-to-end delivery.
- `driftwatch_default_secret` ranks 2 and expresses 0 genes / 72 tokens in
  every arm — the secret-suppression canary working as designed. Note the
  consumer rollups count its correct abstention as a miss, so consumer
  accuracies below are slightly *under*-reported.

## Splice / compression: a no-op without a live backend

Mean compression ratio 0.918 (per-needle 0.878–1.0) in every arm;
`splice_aggressiveness` 0.3 → 0.6 → 0.9 produced byte-identical output.
With the ribosome disabled there is no compressor to apply the
aggressiveness to. The earlier seat-math estimate (2× ratio → recall@18
0.944 at fixed budget) is **not reachable by config today**: it requires a
live splice backend (DeBERTa hybrid or LLM) *and* lifting the classifier
cap. Separately measurable, both currently gated.

## Query expansion: the cheap rank lever, with caveats

One expansion call per novel query (`_expand_query_intent`), wired via the
shipped `OllamaBackend`. **Config caveat for any adoption decision:**
`[ribosome] backend="ollama"` has been ignored since the explicit-opt-in
change (context_manager.py:1006-1086 honors only litellm/deberta); the
harness wired the backend directly. LiteLLM would add a 300 ms/call
min-interval throttle.

| arm | r@12 by rank | Δ vs 0.667 | median build latency |
| --- | ---: | ---: | ---: |
| defaults | 0.667 | — | 949 ms |
| qexp e2b | **0.833** | +16.7pp | 1,380 ms |
| qexp e4b | 0.778 | +11.1pp | 11,902 ms |

Verifier attribution: e2b's lift is exactly 3 window-crossings
(`fusion_default` 33→7, `max_genes` 14→11, `splice_aggressiveness` 13→12) —
the third is boundary-exact at rank 12 *and* coincides with the arm's only
accept-literal loss, so a third of the lift is fragile. e2b also regresses
`genome_path` 18→21. e4b crosses only 2, regresses `know_emit_floor` 15→19,
but uniquely rescues `confidence_gate`'s anchor text (survival 0.889 vs
0.778) — a content win, not just rank. The smaller model beats the bigger
one on rank because only e2b rescues `fusion_default`.

**And none of it converts:** gold_expressed stays 0.5 in both qexp arms.
Rank gains die at the expression cap. Query expansion is worth adopting
*after* the selection stage is fixed, not before.

## Consumer accuracy: the "80% if gold is present" claim, measured

gemma-as-consumer over the expressed context (grading = accept-literal in
answer):

| | e2b | e4b | e4b broad |
| --- | ---: | ---: | ---: |
| overall | 0.529 | 0.556 | 0.556 |
| when **gold gene** expressed | **8/9** | **9/9** | **9/9** |
| when only a value-string present (non-gold doc) | 1/5 | 1/5 | 1/5 |
| when value absent | 0.0 | 0.0 | 0.0 |
| median answer latency | 3.1 s | 19.6 s | 17.4 s |

The interpretation survives — **when the actual gold gene is expressed,
conversion is 89–100%.** But `accept_in_context` (0.778) is an inflated
proxy: value-strings arriving via non-gold docs convert at 1/5 and
otherwise produce *plausible-wrong* answers. The value-absent 0.0 is **not**
uniformly safe abstention: roughly half are confabulations
(`genomes/main.db` for the genome path in all three arms; `"5"` for
emit_floor from e2b). Two additional failure modes the taxonomy missed:
**stale-context poisoning** (`fusion_default` → "additive" in every arm —
the delivered context genuinely contains the legacy value) and **answer
transposition** (e2b returns the guardrails answer for `source_apps`).
`cons_e4b_broad` was byte-identical to `cons_e4b` end-to-end (18/18
answers): decoder_override="broad" bought zero information, and consumer
decoding is deterministic.

Harness accounting note: one e2b consumer ReadTimeout is invisible in the
rollup `errors` field (it counts expression-path errors only) and shrinks
that arm's denominators; e2b conditionals are /14 and /3, not /14 and /4.

## Concurrency: two workers are benign

- Retrieval path (`latency_scaling_1to2.json`): median 807→1,195 ms
  (1.48×), throughput 1.35×, identical recall, fair scheduling.
- LLM leg in isolation (`ollama_concurrency.json`, expansion-shaped calls):
  e2b c=2 throughput +16% with median *below* c=1 (continuous batching +
  shared prefix; honest cost is p95 1.9→3.4 s). e4b c=2 throughput +91%
  (0.11→0.21 calls/s) at +5% median.

Nothing here argues against 2 concurrent agents on one box. (15 would be a
different campaign.)

## Asymmetry worth its own investigation

7 of 8 `cymatix_*` needles miss gold expression while the other three repos
go 8/9 — and 4 of those 7 still show accept=true via non-gold docs. The
store fails specifically on its own project's config facts, consistent with
residual stale/near-duplicate cymatix literature outranking gold — the
893f19a contamination class, incompletely purged.

## Tagger A/B — RESULTS LANDED: see [2026-07-31-tagger-ab-tag-flood.md](2026-07-31-tagger-ab-tag-flood.md)

The e2b bed finished (17.7 h) and the verified outcome supersedes this
section's projections: file-level recall@12 0.667 → **1.000** (all six
cymatix misses recovered), mechanism = **CPU-tagger tag flood** (~⅔
reproducible by tag-thinning alone, no LLM), but 4 of 6 recoveries are
answer-hollow — the LLM ingest stubs 45.8% of gene content and destroyed
two of the answers it recovered. Cheapest next lever: deterministic
tag-IDF discipline (#327). Full receipts + ablation table in the linked
doc. Original section kept below for the projections' provenance.

## Tagger A/B — control bed validated, e4b bed in flight (as-projected, superseded)

`taggerdogfood.db` (gemma4:e4b as ingest tagger via `ribosome.encode`) vs
`taggerdogfood_cpu` (CpuTagger control) — **same-day snapshot, single
variable**. Note `[ingestion] backend="ollama"` is likewise unreachable
from config (the coherence guard flips it to cpu); `build_tagger_bed.py`
wires it.

The control half is done and valid: 6,276 genes, bed sha `d2a2177c…`,
recall@12 **0.667** with the same six cymatix needles missing — reproduces
the clean-bed baseline on today's snapshot. Decisively for comparability:
the control's **gold gene_id sha is byte-identical to the clean bed's**
(`c41739ff…`, 421 genes; per-repo shas match for driftwatch / scorerift /
MaxExpressKit) — the day's repo drift touched no gold file, so tagger-bed
deltas will be attributable to the tagger alone. Control baseline:
`baseline_tagger_cpu.json` (rank fields authoritative; latency fields
polluted by the concurrent e4b ingest, deliberately not comparable).

Cost datum already banked: the CPU-tagged build took **2.6 h** (dense
encode bound, ~21–28 files/min). The e4b build's measured throughput after
48 min is **2.2 genes/min → projected ~47 h** for the 6,276-gene corpus —
an **~18× ingest-cost multiplier** over the CPU tagger, before any
retrieval benefit is measured. That number is the first hard datum in the
tagger-cost column and it is brutal on this hardware; the driver is the
same e4b CPU-spill that made every e4b role ~10× slower tonight.

**Operator call (2026-07-30 ~05:45):** the e4b pass was stopped at ~150
genes and the first ingestion pass restarted with `gemma4:e2b` (100% GPU).
Measured over its first 204 genes: **6.4 genes/min → ~16 h projected**
(≈3× e4b, ≈6× slower than the CPU tagger; the per-call 10× advantage is
diluted by the chunk/dense-encode share of ingest). The target was wiped
by the build script, so the bed is pure-e2b — no mixed-tagger corpus. An
e4b pass remains on the table for a later window (DDR4 spillover
explicitly accepted; the box is dedicated to cymatix testing), with the
47 h projection as its cost estimate. Note for session 2: while any
tagger ingest runs it occupies CPU+GPU, so the OTel overhead A/B must not
be measured concurrently.

## What session 2 should do differently

1. Trace the expression-selection stage on `import_hygiene_confidence`
   (rank 4 → not expressed): freshness gate vs tie-break vs tier logic.
2. Plumbing check on `maxg18`/`broad`/splice overrides (log effective
   config inside build_context) before recording them as INERT in the
   feature ledger.
3. Install OTel, re-run the defaults + qexp_e2b arms, diff latency —
   the telemetry-overhead A/B this session deliberately excluded.
4. Consumer grading: score the canary's abstention as correct; separate
   abstain / confabulate / transpose in the grader, not post-hoc.
