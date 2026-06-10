# Spec: Retrieval Profiles / Modes (3-layer design)

Date: 2026-06-09 · Status: proposed · Evidence: 3-agent research loop over
issues, benches, and the config surface (v0.7.1).

## Product question

Does helix-context need per-workload profiles, or can one highly tuned
pipeline serve all retrieval?

## Verdict

**One static pipeline cannot serve all workloads — but only ~6 knobs are
genuinely corpus-sensitive.** Everything else either has one good value or
should adapt per-query. Profiles should therefore be SMALL, and most
"tuning" should be automatic.

Measured break points for a single pipeline:

| Knob | Evidence |
|---|---|
| `dense_additive_weight` | 4.0 evicts gold on EnterpriseRAG 10K prose (−19pp recall@10 vs dense-off, #138); the semantic arm needed 16.0 on the same corpus family; own-code needles insensitive (sub-noise). Optimum spans 0–16 by corpus + query shape. |
| `[ingestion] splade_enabled` | 0pp recall@10 at every size tested on ERB while costing 21.1% of disk (9.96 GB @ 850K, #164); hypothesized positive only <~50K genes — corpus-size regime knob by design. |
| `ann_similarity_threshold` | 0.58 measured on ONE own-code fixture's random-pair distribution (#139); the repo already had to recalibrate 0.35→0.58 and cold-tier 0.25→0.15 between its OWN fixtures. Absolute thresholds don't transfer. |
| `filename_anchor_*` | +24pp recall@1 where queries name files (Dewey spike); structurally meaningless on slack/gmail/fireflies shards. |
| `[synonyms]` | 100% corpus-specific. |
| abstain floors / know betas / PLR artifact | calibrated from one corpus's score distributions; dense tier totals alone ran 9–28 on ERB vs ~3 on own-code. |

Workload-INDEPENDENT (one value, keep static): `expression_tokens≈7000`
(8.4% utilization — cap never binds, #73), `per_gene_budget="dynamic"`
(4%→43% correctness), `dense_embedding_dim=1024`, cosine cymatics (w1
measured identical), sr/seeded_edges/ray_trace (measured 0 — default off),
per-query classifier caps/decoder modes, dense_additive_min_cosine 0.15.

## Design: three layers

### Layer 1 — corpus-time auto-calibration (preferred over presets)
Machinery exists; finish and default it:
- `ann_threshold_mode="margin_over_random"` default-on, calibration written
  into the genome (`genome_calibration` via `scripts/calibrate_thresholds.py`)
  at ingest-finish — calibration travels with the corpus.
- `[abstain] mode="per_classifier"` from the same script.
- `splade_auto_enable_below_genes` / `_disable_above_genes` set from the
  #164 scale curve once run (`benchmarks/sweep_splade_scale_curve.py`).
- Synonym-map bootstrap from corpus tag vocabulary (today a silent-failure
  hand-tuned knob).

### Layer 2 — query-time adaptation (classifier-owned, not profiles)
- Keep: decoder_mode, assembly caps, abstain floors per query class.
- **Promote the semantic arm into the classifier**: detect prose-shaped /
  low-identifier-density queries and swap dense weight 4.0↔16.0 per query;
  drop the `HELIX_SEMANTIC_ARM` env gate. This dissolves most of the #138
  conflict: identifier queries keep lexical dominance, prose queries get
  dense dominance.
- Candidate: `bm25_shortlist` per class (BM25 won 8/8 on config-value
  queries; −1/8 on helix_only) — needs the gap-4 bench first.

### Layer 3 — minimal static corpus profiles (ingest-time choices only)
Three profiles, chosen at genome create/ingest, stored in the genome:

| Knob | `code` (default) | `prose` (enterprise) | `small-mixed` (<~20K genes) |
|---|---|---|---|
| dense_additive_weight base | 4.0 (pending #138 sweep; likely 2–3) | 2.0 (+16 semantic via Layer 2) | 2–3 |
| filename_anchor_enabled | true (+24pp) | false (no filename semantics) | true |
| splade policy | auto (#164 thresholds) | off >100K (0pp, 21% disk) | on (hypothesized rescue regime — unproven) |
| synonyms | code-vocab seed | empty + bootstrap | bootstrap |
| shard routing | default | semantic_broaden_routing + prefilter (#159/#160) | n/a |

Precedents to reuse: `/fingerprint` already accepts `profile: fast|balanced|quality`
(API pattern), `HELIX_MEM_PROFILE` (naming pattern).

Also reconcile the silent divergence between code defaults and the shipped
helix.toml (expression_tokens 6000 vs 7000, max_genes 8 vs 12,
splice_aggressiveness 0.5 vs 0.3, decoder_mode full vs condensed,
sr_enabled false vs true) — today these are two undocumented products.

## Blocking measurements (ranked)
1. `sweep_dense_additive_weight.py` {0,1,2,3,4,6} on enterprise_rag_10k +
   _50k (ERB question set, recall@10 + gold_evicted) AND on medium/SIKE-50 —
   decides whether code/prose differ on the headline knob.
2. `sweep_splade_scale_curve.py` on/off twins at 1K/17K/50K/100K — sets the
   auto-toggle thresholds.
3. RRF gate run (located_n1000, ≥+15pp retrieval@1 to flip fusion default) —
   profiles can't own per-tier weights until RRF is default OR the additive
   path honors the weight knobs (see dead-knobs issue).
4. filename_anchor on/off on ERB 10K (confirm predicted no-op on prose).
5. bm25_shortlist × query-class grid.
6. Cross-corpus threshold transfer: run calibrate_thresholds.py on
   enterprise_rag_10k, diff vs own-code 0.58/5.0/2.5 — quantifies how wrong
   shipped absolutes are off-corpus.
