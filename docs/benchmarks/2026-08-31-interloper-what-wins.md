# What wins when we're wrong — cross-bed interloper analysis (2026-08-31)

Every prior miss receipt said where gold *landed*; none said who *took its
seat*. This campaign replayed all 3,322 misses across seven beds read-only
(`benchmarks/dogfood/mine_interlopers.py`), captured the delivered slice and
score-map head per miss (docs, tags, snippets, keyword coverage), and ran a
two-wave taxonomy + synthesis over the dumps.

**Receipts:** `interloper_mine_<bed>_2026-08-31.json` per bed (LoCoMo pair
untracked — 15MB/13MB, regenerable in <1 min),
`interloper_taxonomy_wave{1,2}_2026-08-31.json` (agent taxonomies + cross-bed
synthesis). Replay fidelity: rank_match 100% on six beds; muloc 342/375 (the
known access-trail jitter, drifted rows self-flagged).

## Beds and misses mined

| bed | n | delivered | misses | replay fidelity |
|---|---|---|---|---|
| EnronQA v2 85k | 500 | .856 | 72 | 72/72 |
| EnronQA padded 829k | 500 | .792 | 104 | 104/104 |
| ERB 947k | 470 | .668 | 156 | 156/156 |
| LoCoMo turns | 2,378 | .435 | 1,344 | 1344/1344 |
| LoCoMo +obs | 2,378 | .519 | 1,144 | 1144/1144 |
| MULocBench 108k | 680 | .449 | 375 | 342/375 |
| FinanceBench pages | 150 | .153 | 127 | 127/127 |

## The one-line answer

**What wins is almost never random junk — it is a same-family sibling that
speaks the QUERY's vocabulary while gold speaks the ANSWER's.** Question-echo
turns beat answer turns (LoCoMo), filled-template boilerplate beats the fixed
source file (muloc), third-person observations beat raw turns (obs), MD&A/
comp-plan prose beats number-dense statement tables (FinanceBench), max-length
topic siblings beat the chunk holding the answer phrase (ERB), the named
person's other mail beats the reply that answers (Enron).

## Bed headlines

- **ERB 947k** — *no hub effect*: 1,645 unique winners fill 1,674 seats
  (repeaters 3.2%). Winners are same-genre sibling decoys (other incident
  tickets/rollout PRs; genre lift 5.7×), 85% of winner seats at the 4,000-char
  chunk cap vs gold 43% (length is a distinct-term coverage subsidy). New
  mechanism measured: **chunk-split dilution** — in 65/156 misses (41.7%) the
  gold *document's* union coverage ties/beats the top winner but its best
  single chunk loses. Delivery machinery exonerated: 0 gold pass-overs;
  delivered = map_top prefix as a set on all 156.
- **LoCoMo** — **addressee-name tag asymmetry**: the queried person's name is
  tagged on turns *addressed to* them, never on their own answer turn, so
  winners collect a tag lane gold can never enter (gold ahead 0/311 on tags).
  Question-echo turns sit in the winners' top-6 for 68% of weak-anchor misses.
  121 misses are pure 6-seat capacity losses. Cognitive family: winners'
  similarity to gold (0.010 Jaccard) is *below* the two-random-cues baseline
  (0.015) — retrieval is anti-correlated with gold on paraphrase.
- **LoCoMo +obs** — observations take 78.5% of delivered seats and 94.4% of
  rank-1 wins on non-cognitive misses by being SHORTER (median 134 vs 213
  chars, 2× keyword density) — a bm25-normalization + tag-count subsidy, not
  richness. 92% are about the right conversation, wrong episode. Displacement
  operates at all three stages (candidate fetch, RRF rank, 6-seat trim).
- **EnronQA v2 (residual)** — correspondent/person hubs 35%, paraphrase
  anchor-loss coverage outguns 29%, near-duplicate other-mailbox copies 22%
  (partly a grading artifact: the answer text WAS delivered under a sibling
  gene_id), 6-seat trim 14%.
- **EnronQA padded** — **padding takes ZERO of 1,122 delivered seats** (three
  independent classifiers agree). The .856→.792 decay is candidate-pool depth
  crowd-out (pool-absent 22→49) plus small internal rank slides — scale hurts
  through the fixed-depth candidate fetch, never through cross-domain seat
  leakage. Includes one abstain false-negative (enron_173_r: zero docs shipped
  with gold at pool rank 2).
- **MULocBench** — the queries are filled GitHub issue forms, and the repo's
  own **issue templates/CONTRIBUTING/README win 37%** (the template contains
  the query's boilerplate half verbatim); vocabulary-aggregating code siblings
  (sysinfo/collect_env/entry points) 34%; 27 seat-starvation misses confirmed
  (gold at map rank 5–12, 6 or 3 seats shipped — note: the naive 15%-of-top
  check on the *fused* score does not reproduce the cut; the gate acts on
  tier scores). Silent seat tax: 22% of ALL delivered seats repeat an
  already-seated file (duplicate ingest paths, i18n copies). Plus 3 CJK
  queries where the keyword extractor emits n_kw=0 → nothing delivered.
- **FinanceBench** — wrong company 68/127 (alias "JPM"/"Ulta" and multiword
  identity-tag tokens `bestbuy`/`cocacola` match no query token), right
  company wrong YEAR 41/127 — and wrong-year winners skew *older* 254:65
  (the freshness gate can't help: it runs at assembly, gold is pool-absent
  before it). metrics-generated 0/50: number-dense statement tables covering
  ~1–5 of 28–42 query keywords vs verbose prose covering 27%+.

## Cross-bed mechanisms (synthesis, M1–M9)

1. **M1 Query-vocabulary echo beats answer-vocabulary gold** — all 7 beds;
   the dominant pool-absent driver (~1,900 misses touched cross-bed).
2. **M2 Seat-cap capacity loss** — 251 misses across six beds have gold at
   pool rank 7–12 with 6 (or 3, or 0) seats shipped; recoverable by
   definition at a true 12-seat delivery. Cleanest receipt-ready win.
3. **M3 Identity-anchor asymmetry** — tags reward *proximity to the entity*
   instead of *possession of the answer* (addressee tags, correspondent mail,
   alias/multiword token mismatches, wrong-year filings).
4. **M4 Chunk-split dilution** — gold doc out-covers the winner, best chunk
   loses (ERB 65/156; muloc union-ties-or-beats up to 196/372). **Not on the
   current ladder**; highest-leverage single fix per the ERB taxonomy
   (doc-level lane-evidence aggregation before RRF seating).
5. **M5 Length/shape subsidy, direction FLIPS by bed** — document beds favor
   max-length chunks; conversation beds favor short-dense docs. Any global
   length-norm tweak is a cross-bed trade.
6. **M6 Near-band losses are RRF lane-placement losses, not coverage
   losses** — ~866 misses sit at rank 13–100 where gold often out-covers and
   still loses; validates the W2.1/W2.2 kills; surviving levers must add NEW
   independent lanes (phrase, identity, KV), not re-weight existing ones.
7. **M7 Near-duplicate/copy seat burn** — muloc 22% repeat seats; Enron copy
   shadowing (partly grading false-negatives).
8. **M8 Candidate-pool depth crowd-out** — mass that never seats still evicts
   gold from the fixed-depth fetch (padded Enron present→absent 15 needles;
   `fts5_candidate_depth` is the shipped knob).
9. **M9 Hub concentration is bed-LOCAL** — hubby on muloc/locomo/finbench,
   provably absent on ERB/Enron (1,645 unique winners/1,674 seats). Hub-
   targeted levers don't generalize.

## Lever map deltas (vs the representation-campaign synthesis)

Confirmed ladder legs: phrase/proximity (M1-basic), title/doc-identity (M3),
claims/KV (M1 number-dense), tier_hard_floor_frac + classifier caps (M2),
fts5_candidate_depth (M8). **New proposals surfaced by this campaign:**
doc-level chunk-evidence aggregation before seating (M4), delivery-time
near-duplicate suppression (M7), query-side template/boilerplate stripping at
Stage-1 (muloc M1 variant), speaker-attribution tagging at ingest (LoCoMo M3
variant), abstraction→source linkage for obs docs, CJK-aware extraction, and
an abstain-gate audit under RRF (the enron_173_r zero-delivery case).
Known-killed levers stay killed: the mechanisms they targeted are real, but
the evidence shows they acted in the wrong lane (coverage re-weighting in a
band decided by lane placement; graph mass on a bed with no hubs).
