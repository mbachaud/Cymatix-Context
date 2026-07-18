# SPLADE scale curve on paraphrase-style gold queries (#204)

**Headline: with query-time SPLADE verified firing (receipts in every
artifact), SPLADE-on adds no robust recall over SPLADE-off on paraphrase
gold queries at 15.6K or 80K genes — zero recall@10 delta in 3 of 4 cells,
+2/55 queries in the fourth (50k extracted; not shape-consistent, not
significant at n=55) — against a flat +33% disk cost (13.9KB/gene) and a
uniformly positive p95 latency cost. Recommendation: auto-OFF in the
measured 10K–80K band; do not set the auto-enable rescue arm; the <10K and
100K+ bands remain unmeasured (100K+ SPLADE-on rebuild is GPU-hours,
deferred).**

## Background

Issue #204 asks for a SPLADE-on / SPLADE-off recall comparison across the
corpus-size range so the two opt-in `[ingestion]` thresholds
(`splade_auto_enable_below_genes`, `splade_auto_disable_above_genes`, both
default `0` = disabled) can be set from data. The 2026-07-11 overnight rig
(`docs/research/2026-07-11-overnight-bench-results.md` §P9) unblocked the
mechanical half — copy a bed, `DROP TABLE splade_terms`, `VACUUM` = a valid
SPLADE-off twin in ~1 min — but flagged that its 30-query smoke used
auto-synthesized queries (random 8-token slices of the gold document's own
content), lexically guaranteed to overlap gold: SPLADE's worst case, so the
zero delta was explicitly not the #204 verdict.

This pass supplies the missing gold paraphrase query set and runs the
curve. In doing so it found — and fixed — a harness defect that
retroactively reclassifies every previous zero-delta from this script.

## Finding 0 (harness defect): the on-arm never ran SPLADE at query time

`sweep_splade_scale_curve.py` constructed
`Genome(path=..., dense_embedding_enabled=False)` without threading
`splade_enabled`. The `KnowledgeStore` constructor default is
`splade_enabled=False` (`knowledge_store.py:500`) — the #256-family
layer-default disagreement (config default `True`, constructor default
`False`) biting a bench harness. Tier 3.5 is gated on
`self._splade_enabled` (`knowledge_store.py:2528`), so the "on" arm was
byte-identical to the "off" arm at query time:

- **Run-1 of this pass** (55 paraphrase queries, raw shape) produced
  recall@10 and MRR identical to full float precision on BOTH beds —
  which is equally consistent with "SPLADE adds nothing" and "SPLADE never
  ran". A counting-wrapper diagnostic confirmed the latter:
  `splade_backend.encode` / `query_splade` were called **zero** times.
  Run-1 is therefore an **A/A receipt** (artifacts kept, renamed
  `2026-07-13-splade-curve-aa-inert-*.json`): it proves the strip-twin
  construction (copy + DROP + VACUUM) changes nothing outside the SPLADE
  tier — identical recall AND MRR on twins — but says nothing about SPLADE.
- **The 2026-07-11 P9 smoke has the same defect**: its zero-delta was
  doubly worst-case (lexically-biased auto-queries AND an inert SPLADE
  tier). Its disk numbers stand; its recall rows are A/A receipts too.

Fix (this branch): the on-arm constructs `Genome(splade_enabled=True)`,
the off-arm `False`, and each arm's metrics now embed a **firing receipt**
(`splade_fire`: encode / query_splade call counts + total hits) so the
artifact self-certifies. DID-IT-FIRE verification on the 10k on-twin:
encode fired (114 sparse terms from a raw paraphrase question; 98 from its
extracted keyword bag), `query_splade` returned 80 hits, 1 SPLADE hit
survived into the returned pool on the extracted shape; with
`splade_enabled=False` all counters are 0.

## Query set

`benchmarks/_splade_curve_queries.json` — 55 queries (45 ERB `semantic`
type = paraphrastic by construction, + 10 `basic` for contrast), sampled
deterministically (`random.Random(204)`) from the 470 ERB questions whose
gold documents resolve into both `enterprise_rag_10k_batched.db` and
`enterprise_rag_50k_batched.db`. Provenance + selection rule in the sidecar
`benchmarks/_splade_curve_queries.meta.json` (kept outside the consumed
file because the sweep's loader is a bare `json.load()` over a flat list —
a `_meta` key inside would be iterated as a query and crash).

Cross-bed validity: gold_ids are byte-identical between
`benchmarks/results/erb_sweep_queries_erb10k.json` and `..._erb50k.json`
for all 470 common questions (gene_id is content/path-hash derived; the
50k bed is a superset of the 10k corpus), so one file serves both scale
points; re-verified per-gold_id against both beds' `genes` tables at
authoring time (0 missing).

## Query shape: `raw` vs `extracted` (and why absolute levels differ from other benches)

The harness's legacy behaviour (`--query-shape raw`) passes
`query_text.split()` — all stopwords — as `domains` terms. The serving
pipeline never produces that shape: stage 1 extracts keywords
(`accel.extract_query_signals`), and — decisive for this experiment — the
SPLADE tier encodes `" ".join(query_terms)` (the **extracted keyword
bag**, `knowledge_store.py:2537`), not the raw question. So `--query-shape
extracted` (added this branch) is the serving-faithful shape for both the
lexical tiers and the SPLADE query encoder; `raw` is kept as a sensitivity
arm — it is arguably SPLADE-*favorable*, since the encoder sees the full
paraphrase sentence with its context instead of a keyword bag.

**Absolute-level caveat (do not compare these recall numbers across
harnesses):** this sweep measures `recall@10` / `MRR@10` over the raw
`query_docs()` return of a `Genome` constructed with
`dense_embedding_enabled=False` — no BGE-M3 dense tier, no full pipeline
(no classifier, budget assembly, or delivery logic). `ab_semantic_probe`'s
`gold_delivered_id` ~0.25-0.38 (fused) / ~0.31 (lexical) on the same 10k
bed with overlapping ERB semantic questions is a **different metric
through a different stack** (full `build_context`, dense on for fused,
serving extraction internally). The run-1 raw-shape 0.0727 is additionally
depressed by the non-serving query shape. The numbers in this doc support
exactly one comparison: **SPLADE-on vs SPLADE-off, same bed, same shape,
same invocation.** Nothing else.

Note the dense-off choice is *conservative in SPLADE's favor*: without the
dense tier, SPLADE is the only semantic-matching signal in the stack and
has maximal headroom to show value on paraphrase queries. A null here is a
strong null for SPLADE's marginal retrieval value at these scales.

## Strip-twin mechanics (confirmed, ~1 min/scale point)

`benchmarks/build_striptwins.py` (new, scripts the P9 mechanics): copy the
canonical bed twice, `DROP TABLE splade_terms` + `VACUUM` one copy. Twins
live under `F:/tmp/splade204/` (scratch, deleted after the run); canonical
beds under `genomes/bench/matrix/` untouched (read-only, copies only).

| bed | genes | splade rows dropped | on bytes | off bytes | Δ bytes/gene | disk overhead | build time |
|---|---:|---:|---:|---:|---:|---:|---:|
| enterprise_rag_10k_batched | 15,598 | 1,896,100 | 868,667,392 | 652,079,104 | 13,885.6 | +33.2% | 6.8s |
| enterprise_rag_50k_batched | 80,072 | 9,697,586 | 4,451,106,816 | 3,334,438,912 | 13,945.8 | +33.5% | 39.4s |

SPLADE's disk cost is flat per gene across this range (13,885.6 vs
13,945.8 bytes/gene, 0.4% spread) — the +33% is corpus-content-dependent
(the #164 850K fixture measured 21.1% of total), but the per-gene overhead
is stable, and the 10k receipt reproduces P9's 13,886 bytes/gene exactly.

## Results — SPLADE-on vs SPLADE-off, firing-verified

All four cells run serially on the same box (one invocation per bed×shape;
on/off arms adjacent within each invocation), 55 queries, topk=10.

### Primary: extracted shape (serving-faithful)

| bed | arm | recall@10 | MRR | mean_s | p95_s | splade_fire (encode / query_splade / hits) |
|---|---|---:|---:|---:|---:|---|
| erag10k | on | 0.0909 (5/55) | 0.0764 | 2.98 | 6.10 | 55 / 55 / 4,400 |
| erag10k | off | 0.0909 (5/55) | 0.0523 | 2.76 | 5.67 | 0 / 0 / 0 |
| erag50k | on | 0.0909 (5/55) | 0.0571 | 13.79 | 32.49 | 55 / 55 / 4,400 |
| erag50k | off | 0.0545 (3/55) | 0.0299 | 11.00 | 28.87 | 0 / 0 / 0 |

### Sensitivity: raw shape (SPLADE-favorable full-sentence encode)

| bed | arm | recall@10 | MRR | mean_s | p95_s | splade_fire (encode / query_splade / hits) |
|---|---|---:|---:|---:|---:|---|
| erag10k | on | 0.0727 (4/55) | 0.0727 | 22.32 | 32.48 | 55 / 55 / 4,400 |
| erag10k | off | 0.0727 (4/55) | 0.0636 | 21.29 | 30.94 | 0 / 0 / 0 |
| erag50k | on | 0.0727 (4/55) | 0.0370 | 5.71 | 7.20 | 55 / 55 / 4,400 |
| erag50k | off | 0.0727 (4/55) | 0.0394 | 3.69 | 4.53 | 0 / 0 / 0 |

### Deltas (on − off)

| bed | shape | recall@10 Δ | MRR Δ | p95_s Δ | disk Δ bytes/gene |
|---|---|---:|---:|---:|---:|
| erag10k | extracted | 0.0 | +0.0241 | +0.43 | +13,885.6 |
| erag50k | extracted | +0.0364 (+2/55) | +0.0272 | +3.62 | +13,945.8 |
| erag10k | raw | 0.0 | +0.0091 | +1.54 | +13,885.6 |
| erag50k | raw | 0.0 | −0.0024 | +2.67 | +13,945.8 |

Every on-arm carries a non-zero firing receipt (55 encode calls, 55
`query_splade` calls, 4,400 tier hits = 55 × 80-hit pool); every off-arm
is 0/0/0. These are real SPLADE ablations, unlike the aa-inert run-1.

**Latency caveats:** (1) run-1's 10k cells overlapped another agent's
bench cells on this box — its 10k p95 (47.6s) vs 50k p95 (7.7s) inversion
is box-load contamination, not a scale trend; run-1 latencies are
superseded by the tables above. (2) The re-run chain itself
(2026-07-13 15:44–17:01, daytime, other agents active — blend-receipt
cells overlapped) shows the same contamination signature: wall-clock per
cell was 10k-ext 5.4 min, 50k-ext 22.4 min, 10k-raw 40.1 min, 50k-raw
9.1 min — the 5×-larger bed ran its raw cell 4× *faster* than the 10k
bed did. Absolute latencies and cross-bed / cross-shape comparisons in
these tables are therefore box-load-contaminated; the only defensible
latency signal is the within-invocation on-vs-off delta (adjacent arms,
same conditions), which is positive in all four cells (+0.4 to +3.6s
p95). **No latency scale-trend claims are made from this data.** (3) The
absolute per-query latencies are inflated by the harness's direct
`query_docs` shape (see caveat above) — serving latency is not measured
here.

## Interpretation

With SPLADE demonstrably firing, its marginal retrieval value on
paraphrase gold queries at 15.6K and 80K genes is at most **+2 of 55
queries in one of four cells** (50k, extracted shape) and exactly zero in
the other three:

- **Recall@10**: zero delta at 10k on both shapes and at 50k raw. The one
  positive cell (50k extracted, 5/55 vs 3/55) is not shape-consistent
  (the same bed shows zero delta on the raw shape, which gives the SPLADE
  encoder *more* signal — the full paraphrase sentence) and is not
  statistically distinguishable from noise at n=55 (a paired sign test on
  a net +2 discordant queries cannot reject chance; p ≥ 0.5). It is a
  weak directional hint at 50k, not a measured effect.
- **MRR**: small positive movement in 3 of 4 cells (+0.009 to +0.027),
  small negative in the fourth (50k raw, −0.002). Same n=55 caveat; no
  cell's MRR movement corresponds to a gold document entering the top-10
  except the two 50k-extracted queries above — the rest is rank shuffling
  among already-recalled golds.
- **Costs are certain, benefits are not**: +33% disk (13.9KB/gene, flat
  across the 5× scale range) and a positive p95 latency delta in all four
  within-invocation comparisons.

This was SPLADE's best case by construction: dense tier off (SPLADE the
only semantic signal in the stack), 45/55 queries paraphrastic by ERB
type, and a sensitivity arm that feeds the encoder the full question. A
null under those conditions is a strong null for SPLADE's marginal
retrieval value at these scales — consistent with, and now firing-verified
unlike, the #164 850K observation (0pp recall@10) and the (retroactively
A/A) P9 smoke.

What this does NOT establish: SPLADE's value below 10K genes (no bed with
resolvable paraphrase golds exists), or in the 100K–850K band with a
genuine SPLADE-on fixture (building one is a multi-hour GPU ingest,
explicitly deferred), or under RRF-fused serving with dense on (where
SPLADE's contribution could differ in either direction).

## Threshold recommendation

For the `[ingestion]` auto-toggle knobs this sweep exists to set
(`splade_auto_enable_below_genes` / `splade_auto_disable_above_genes`,
both currently `0` = disabled):

1. **Do not set `splade_auto_enable_below_genes`** (leave `0`). The
   "likely useful below ~50K" prior in the config comment is not
   supported at either measured point (15.6K, 80K): there is no evidence
   of a sparse-corpus rescue band anywhere we measured, and the <10K band
   is unmeasured — no data justifies force-enabling.
2. **`splade_auto_disable_above_genes = 10_000` is the honest
   data-supported setting** for operators who opt in: every measured
   point at or above 10K genes (15.6K, 80K here; 850K in #164/#206) shows
   zero robust recall value against a certain +33% disk and positive p95
   cost. This forces SPLADE OFF for the entire measured range while
   leaving the unmeasured <10K band governed by the static
   `splade_enabled` value.
3. **Shipped defaults stay `0`/`0` in this PR** — flipping the shipped
   default (or the static `splade_enabled = true` ingest default) is a
   behavior change outside this bench branch's scope and should ride its
   own PR citing this doc.
4. **Untested case, explicitly deferred**: a 100K+ SPLADE-on rebuild
   (GPU-hours) to close the 80K→850K gap, and any <10K paraphrase bed.
   If someone later shows value in a band, the knobs are already wired to
   express it.

## Reproduce

```bash
# strip-twin build (copy -> DROP splade_terms -> VACUUM), ~1 min/point
python benchmarks/build_striptwins.py erag10k genomes/bench/matrix/enterprise_rag_10k_batched.db F:/tmp/splade204
python benchmarks/build_striptwins.py erag50k genomes/bench/matrix/enterprise_rag_50k_batched.db F:/tmp/splade204

# curve, per bed x shape (query file is bed-agnostic for these two beds)
for bed in erag10k erag50k; do
  for shape in extracted raw; do
    python benchmarks/sweep_splade_scale_curve.py \
        --on-genome F:/tmp/splade204/${bed}_on.db \
        --off-genome F:/tmp/splade204/${bed}_off.db \
        --label ${bed} --query-shape ${shape} \
        --queries benchmarks/_splade_curve_queries.json --topk 10 \
        --out docs/research/data/2026-07-13-splade-curve-${bed}-${shape}.json
  done
done
```

## Caveats

- Two scale points (15.6K, 80K genes). The 1K-band and the 100K-850K band
  are NOT covered: no <10K ERB bed exists with resolvable paraphrase
  golds, and no 100K+ SPLADE-on fixture exists locally — a genuine
  SPLADE-on ingest at that scale is a multi-hour GPU job, **explicitly
  deferred, not attempted here**. The 850K point has indirect evidence
  only (#164: 0pp recall@10 on its own query set; #206: SPLADE-off worth
  −94s p95 at 105-shard/850K).
- Zero-delta on recall@10/MRR@10 does not exclude sub-top-10 rank
  movement; the harness does not record full-pool gold ranks.
- Gold_ids were validated for existence in both beds, not re-validated
  for content drift since the beds were built.
- Single query file (n=55, 45 semantic / 10 basic); per-type splits not
  reported (n too small to slice).
