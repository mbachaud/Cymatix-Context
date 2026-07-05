# SIKE Bedsweep (#221): Issue → Resolution Log + Bench-Validity Plan

> 2026-07-05. Companion detail doc to `docs/ROADMAP.md` (sequencing layer).
> Covers: the two anomalies found in the 2026-07-03_1556 bedsweep results, their
> root causes and fixes (PR `fix/bench-body-metric-and-upstream-timeout`), the
> adversarial review of those fixes (21-agent workflow, 11 confirmed defects,
> 28 gaps), and the next-3-runs experiment sequence.
>
> Valid data: `benchmarks/results/sike_bedsweep_{xl,enterprise_rag_10k,enterprise_rag_50k}_2026-07-03_1556.json`
> — the ONLY valid run of this suite. The 07-02_1252 and 07-03_0104 attempts are
> poisoned (stub corpus / Sonnet 401; `n_gold_blocks=0` on all 300 rows) and must
> never be quoted.

## 1. Headline numbers from the valid run (what we can and cannot trust)

| Bed | Genes | gold_delivered_rate | body_has_answer_rate | claude:sonnet correct/answered | best-local |
| --- | --- | --- | --- | --- | --- |
| xl | 46,517 | **0.64** (32/50) | 0.0 (dead metric) | 32/35 (91.4%) | gemma4:26b-a4b 25/38 |
| enterprise_rag_10k | 15,889 | **0.80** | 0.0 (dead metric) | 30/32 (93.8%) | gemma4:26b 25/33 |
| enterprise_rag_50k | 80,363 | **0.82** | 0.0 (dead metric) | 29/31 (93.5%) | gemma4:26b 24/36 |

Trustworthy: `gold_delivered_rate` per-needle booleans (citation-based), consumer
answer scoring on rows with `status=ok`, Sonnet cost (~$1.79/bed).
**Not trustworthy:**

- `body_has_answer_rate` — flat 0.0 across all 150 needle-checks; the parser was
  dead (Anomaly 1). Historically 0.0–0.02 in every prior run: the metric has
  never produced signal.
- The three big-gemma rungs (26b-a4b / 31b / 26b) — 11–16 needles per bed
  recorded as `http_error` at ~181–182 s (Anomaly 2): ~25–30 % of their samples
  are censored, so their coverage (0.66–0.78) and correctness stats
  (n≈33–39, survivorship-biased toward fast queries) understate/misstate them.
- The headline rates themselves mismeasure retrieval in both directions
  (§4, A1): 6/4/3 false-negative needles per bed (marked miss, yet ≥5/10
  consumers answered correctly) and 7/14/15 false-positive needles (marked
  delivered, yet 0/10 consumers correct).

## 2. Anomaly 1 — `body_has_answer_rate = 0.0` (dead metric)

**Root cause A (this run):** the bench servers were launched with
`docs/benchmarks/helix_probe_lexical.toml` (`s2_sike_bed_sweep.ps1` 2026-07-03
GPU-contention fix), which sets `legibility_enabled = false`. Without
`[gene=...]` headers, `benchmarks/_citations.py::extract_block_bodies` paired
every citation with `body=""` — a word-boundary match on 150 empty strings.

**Root cause B (all runs, latent):** live `/context` content is wrapped as
`<expressed_context>\n[gene=...` (`context_manager.py:2722`); the parser split
on `\n---\n` and required each block to START with a header, so the wrapper
glued to block 1 → first body (the top-ranked document) silently dropped and
every citation→body pairing shifted by one. The existing test fixture omitted
the wrapper, which is why tests stayed green while production was broken.

**Resolution (this PR):** `extract_block_bodies` rewritten — strip the wrapper;
join blocks to citations by the `gene_id[:12]` hex header prefix; positional
fallback for headerless configs **count-guarded** (pair only when unmatched
citations == candidate blocks, else fail closed to empty bodies); elision
stubs pair by id but always yield `body=""`; non-hex bracketed text (doc
examples) treated as body, not header. 14 new regression tests using the real
wrapped shape, including an end-to-end `check_gold_delivery` case.

## 3. Anomaly 2 — big-gemma `http_error` clusters (censored rungs)

**Root cause:** proxy default `upstream_timeout = 180.0`
(`helix_context/config.py:197`) < generation time of CPU-offloaded gemma4
26b-class models (observed 67 % CPU / 33 % GPU). Server-side
`httpx.ReadTimeout` at 180 s → 5xx → bench rows at ~181 s. Same failure mode
as the documented 2026-05-02 bump (120→180 for gemma4:e4b); the ladder outgrew
180 s. This also explains the "28/153 HTTP-500 ollama ReadTimeouts" chain
defect flagged in ROADMAP (bench-gate section).

**Resolution (this PR):** `HELIX_SERVER_UPSTREAM_TIMEOUT=600` set per-bed in
`s2_sike_bed_sweep.ps1` (env override already existed, `config.py:832`);
runner client timeouts raised above it (660 s) so the server side decides;
`http` status / `error` body now persisted in per-needle rows.

**Open (Run 3 / #205-adjacent):** consider promoting the timeout fix from bench
env to shipped config with a granular `httpx.Timeout(connect=10, read=...)` —
any user proxying big local models hits the same wall.

## 4. Adversarial review of the first-pass fix (what round 2 fixed)

The 21-agent review confirmed 11 defects; all are fixed in this PR:

| # | Defect (confirmed) | Fix |
| --- | --- | --- |
| 1 | Positional fallback mispaired bodies whenever headerless blocks ≠ citations (dropped citation; Markdown `\n---\n` inside a body — 2,318/46,777 xl genes contain one, 909 in the first 500 chars) | count-guard; fail closed to empty bodies |
| 2 | Elision-stub text kept as body: "delivered **16** queries ago" word-boundary-matches numeric accepts (`16` = helix_subpackages_count; `1` matches "1.2h") on default configs (session delivery + synthetic sessions ON) | stubs always yield `body=""`; `find_needle` and `_fetch_context` now send `ignore_delivered: true` |
| 3 | Timeout inversion regression: Pass-1 `httpx.Client(timeout=300)` < new 600 s server timeout; find_needle's unguarded Step-2 answer call raised → recall row dropped → denominator shrank silently | client 660 s; Step 2 wrapped in try/except so Step-1 retrieval fields survive |
| 4 | Pass-2 `coverage` divided by `len(recall_rows)` (could exceed 1.0 after Pass-1 drops) | denominator = full needle set |
| 5 | No circuit breaker: a wedged rung burns 50 × 600 s ≈ 8.3 h of identical errors (~225 h theoretical bound per sweep) | abort rung after 5 consecutive non-ok rows; `rung_aborted` recorded |
| 6 | Literal `[gene=...]` doc examples misparsed as headers | header id must be 8–16 hex chars |
| 7 | Claude-rung failures lost `rc`/`stderr`/`stdout_tail`; ollama `http_error` rows lost the response body; `_fetch_context` failure indistinguishable from empty retrieval | all persisted (`rc`, `stderr`, `stdout_tail`, `error`, `ctx_chars`, `ctx_fetch_failed`) |
| — | `HELIX_CONFIG` leaked into the caller's shell after the sweep | added to end-of-run `Remove-Item` |

## 5. Gap analysis (28 gaps; the ones that change conclusions)

### Tier A — the current numbers are wrong (fix before quoting #221)

- **A1 Metric mismeasurement** (§1 above). Post-fix, make
  answer-string-in-delivered-body the primary retrieval metric.
- **A2 Bed contamination — two independent corruptions of xl's 0.64:**
  (a) **260 echo genes** in xl.db (285 in erb50k): Stage-6 persisted
  query/response exchanges from prior runs that verbatim-match needle queries
  (`content LIKE 'User query:%' AND source_id IS NULL`); for
  `helix_headroom_port` the top-8 FTS5 hits are ALL echoes (bm25 −30.1…−28.0)
  and can never count as gold. (b) **worktree/clone duplicates**: 9 gold files
  have 43 competitor genes under `_worktrees/...`/sibling paths whose
  source_ids can never substring-match `gold_source` (helix.toml ×6,
  README.md ×5). Purge + rebuild beds; serve with Stage-6 persist disabled;
  do NOT loosen `src_matches`.
- **A3 Probe-config no-ops — the bench didn't run its intended config:**
  `[abstain] enabled = false` is a nonexistent key (real switch:
  `budget.abstain_enabled`, default True → abstain tier was ACTIVE);
  `[know] confidence_floor` → real key `emit_floor`; no `[synonyms]` section →
  synonym expansion was a no-op for all 50 needles;
  `helix_probe_symbol.toml` sets `symbol_graph` / `symbol_expansion_cap` which
  **do not exist on master** — if arm C is served from master it is
  byte-identical to the lexical arm and would falsely dark-ship WS2/WS3 under
  the ≥+1pp gate (ROADMAP decision rule 2). Fix TOMLs; harden the config
  loader to warn on unknown scalar keys; add a fail-fast capability assert to
  the arm-C script.
- **A4 FTS5 candidate-pool starvation:** depth hard-coupled to max_genes
  (`knowledge_store.py:2291`, LIMIT limit*2 = 48 at max_genes_per_turn=12).
  7/18 missed xl needles' golds never enter retrieval (gold FTS ranks:
  helix_cold_start_threshold 63, mek_version 78, helix_headroom_port 86,
  helix_calibration_staleness 118, helix_dense_encoder 158;
  helix_expression_budget / helix_pipeline_steps absent from top-200 —
  'helix' alone matches ~36 k genes). Add a `fts5_candidate_depth` knob (or
  wire up the set-but-unused `bm25_shortlist_size`).
- **A5 know-confidence is anti-signal:** misses score HIGHER than hits on
  every bed — xl hit-mean 0.327 vs miss-mean 0.421 (AUC 0.352), 10k 0.321 vs
  0.418 (0.383), 50k 0.328 vs 0.384 (0.442); biged_complex_model rc=0.74 with
  0/30 correct. Recalibrate (`scripts/calibrate_know_confidence.py`); track
  hit/miss AUC with a >0.7 gate before agents rely on the know contract.

### Tier B — diagnosis & transfer

- **B1 No baseline:** 1556 is the only valid run; add a write-time validity
  gate (`sum(n_gold_blocks)==0` → reject) and rerun for variance bands.
- **B2 In-pool rank loss:** 11/18 xl misses have gold at FTS rank 7–42,
  squeezed out of ~5 delivered blocks by echoes/near-dupes; FTS5 additive cap
  6.0 flattens the head while tag_exact is uncapped → A/B additive vs RRF
  post-decontamination.
- **B3 Dead needles / splice suspects:** all-bed dead: biged_complex_model,
  biged_max_workers, helix_expression_budget (0/30 each), biged_thermal_target
  (4/30) — 6/8 all-bed misses are biged_* (tagging/synonym hole). Separately,
  the delivered-but-unanswerable cluster (13 needles ≤2/30: e.g.
  bookkeeper_ocr_confidence, cosmictasha_auth_library,
  genome_compression_target 0/30) points at Stage-4 splice compressing out the
  value-bearing fragment — needs a body dump post-parser-fix.
- **B4 Stale gold paths:** 4/63 `gold_source` paths stale (mek_version,
  biged_audit_dimensions, helix_expression_budget, helix_subpackages_count) —
  ANY-of matching means no needle is fully starved (attribute those misses to
  rank, not missing gold); fix paths + add a min-resolved threshold to
  `sike_bed_ingest.py`.

### Tier C — backlog

xl-only regression set (7 needles: biged_ram_ceiling, biged_rust_binary_size,
bookkeeper_dashboard_port, helix_calibration_staleness,
helix_cold_start_threshold, helix_pipeline_steps, mek_version); answering
ceiling tracked separately from retrieval (sonnet 65/93 on the 31-needle
gold-delivered intersection; locals lose 45–65 %); needle staleness audit
(helix_pipeline_steps expects "6", docs now describe 7 stages); dense-weight
sweep winner w=6 sits on the swept boundary with mean_rank degrading
(2.10→2.71) — extend to {8,10,12}; `sema_embed_on_ingest=false` in probe
TOMLs; UTF-8 bench logs; flip `legibility_enabled=true` in the probes now
that the parser is robust either way.

## 6. Next-3-runs experiment sequence

**Run 1 — clean baseline (no ranking changes).** This PR's fixes + probe-TOML
corrections (A3) + bed decontamination (A2) + stale gold paths (B4) + validity
gate (B1). Expect: `body_has_answer_rate` 0.0 → nonzero ≈ tracking
gold_delivered; xl gold_delivered 0.64 → **≥0.75** (echo/dupe removal alone
should rescue helix_headroom_port, helix_dense_encoder, bookkeeper_monetary
and the biged_* false negatives); recall_rows n == 50 on every bed; big-gemma
errors ≈ 0 (600 s headroom, breaker as backstop).

**Run 2 — candidate depth + fusion A/B (decontaminated xl only).**
`fts5_candidate_depth` sweep 48 → 200 → 500 × `fusion_mode` additive vs RRF
(2×3 arms, retrieval-only scoring). Expect: the 7 deep-rank golds enter the
pool at 500; RRF outperforms additive on the 11 rank-7–42 squeeze-outs
(immune to the 6.0 cap); guardrail: mean_rank_of_gold on already-hit needles
degrades <0.5. The 7 xl-only needles are the regression set.

**Run 3 — calibration + production-profile arm.** Recalibrate `[know]` betas
against Run-1/2 per-needle data (gate: hit/miss AUC >0.7); one
production-profile arm (dense + SPLADE + cymatics + entity_graph ON) on the
decontaminated xl bed to quantify the neural contribution to the
distractor-density failure mode; extend the dense-weight sweep to {8,10,12}
on erb50k (report recall@10 + MRR side by side). Only after Run 3 do arm-C
symbol-graph numbers get recorded, behind the A3 fail-fast assert.

**Sequencing rationale:** Run 1 must precede everything — every tuning
conclusion from the 1556 files is polluted by dead parsers, contaminated beds,
and config no-ops. Run 2 targets the largest attributable retrieval loss
(pool starvation + rank squeeze = 18/18 diagnosed xl misses). Run 3 spends the
then-trustworthy data on calibration and the production-transfer question.
