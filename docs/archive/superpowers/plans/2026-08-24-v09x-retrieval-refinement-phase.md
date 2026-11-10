# v0.9.x — Retrieval Refinement Phase

**Date:** 2026-08-24 · **Base:** master @ `5f5c677` (v0.9.0 = `55162c6`, tagged 2026-08-20)

---

## Context

v0.9.0 shipped and cleared the entire issue board — **zero open issues** — leaving the
project without a working plan document. `docs/ROADMAP.md` is stale (dated 2026-07-27,
opens "Open issues: 15", sequences three tracks that are all closed or deferred, and
predates both the encoder flip and the release). The real backlog lives in a **GitHub
comment** on #377, which is a single point of failure for a whole phase.

Meanwhile three performance questions are open, one rests on a receipt that no longer
describes reality, and — the reason this plan is shaped the way it is — **the code says
the central retrieval diagnosis of the last two months is probably wrong.**

**Intended outcome:** a truthful roadmap; three measured performance paths (ingest,
CLI/MCP, proxy injection); a measurable rise in ERB Document Recall and leaderboard
rank; and a *diagnosed* SlopCodeBench treatment rather than a third null campaign.

---

## The finding that shapes the phase

`cymatix_context/knowledge_store.py:3789-3831` runs the BM25 shortlist as a **hard
post-filter**. Every tier scores first; then any candidate outside the FTS5 top-50 is
deleted outright:

```python
shortlist_rows = cur.execute(
    "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH ? ORDER BY rank LIMIT ?",
    (bm25_match, self._bm25_shortlist_size),   # default 50
).fetchall()
...
gene_scores = {g: s for g, s in gene_scores.items() if g in shortlist}
```

Under RRF (the shipped default) `:3858` restricts the entire fused output to survivors —
`eligible_ids = set(gene_scores.keys())` — and the code comment states the intent
plainly: *"Documents dense-recall surfaced but bm25_shortlist dropped should not
resurface in the fused output."* Then `:3897` writes
`self.last_query_scores = dict(final_scores)`, the **post-filter** map.

`benchmarks/dogfood/erb/ablation_ladder.py:1137` reads exactly that map to compute
`gold_ranks`.

**Therefore:** a gold document that every tier scored correctly, but which FTS5's
`ORDER BY rank LIMIT 50` did not surface, is recorded as `gold_ranks == []` — byte-identical
to "never retrieved by any tier."

That is #340's static-miss signature. And it explains the result that made #340 look
like a deep architectural problem: the 99-needle set is **bit-identical across
`baseline / no_pki / no_tags / pki_w2 / pki_w3 / pki_w4`** — but every one of those is a
*tier-weight* arm, and shortlist membership is decided by bm25 rank over an OR-of-query-terms,
which is completely independent of tier weights. No weight arm could ever move it.

`bm25_shortlist_enabled` has **never been an arm.** It is not in `ARMS`
(`ablation_ladder.py:272-416`) nor in `DROPPED_ARMS` (`:293-330`). Its entire in-file
justification is **"+1/8 ans_full"** — n=8. And `bm25_match` is built from `query_terms`
(domains + entities *after* synonym expansion, `:2735-2738`), not the raw query.

The whole delivery path is a cascade of hard cuts, only one of which has ever been swept:

| stage | width | site | swept? |
|---|---|---|---|
| dense pool | 500 | `cymatix.toml:476` | partially (now inert) |
| **bm25 shortlist** | **50** | `knowledge_store.py:3789` | **never** (n=8) |
| **fused output `_fuse_limit`** | **12** (`= limit = max_genes`) | `knowledge_store.py:3850` | **never** |
| **classifier cap** | **2 arithmetic / 5 factual / 6 procedural / 8 multi_hop** | `query_classifier.py:181-218` (code constants) | **never** |
| abstain + tier ratios | — | additive-calibrated, never refit under RRF | never |

Arithmetic on the shipped receipt corroborates it: **r@12 = 0.6546 but
delivered_gold_rate = 0.5608** — a 0.094 gap ≈ **44 needles that ranked in the top 12
and were still not delivered.** Truncation, not ranking, owns that share. It is not a
hypothesis; it is subtraction on a committed receipt.

**Consequence for the plan:** attribution comes before intervention. A 4-cell shortlist
matrix costing ~2 hours can falsify or confirm the premise behind weeks of planned
admission-redesign work, and decides whether a ten-weight RRF sweep gets funded at all.

---

## Other findings that drive the plan

### 1. We are on the public ERB leaderboard at rank 18 of 21

ERB = **EnterpriseRAG-Bench** (Onyx, MIT, arXiv 2605.05253).
[Leaderboard](https://huggingface.co/spaces/onyx-dot-app/EnterpriseRAG-Bench-Leaderboard) —
21 systems, updated 2026-08-20, **all judged with GPT-5.4** so entries are
protocol-comparable. Onyx excludes itself.

| rank | system | Overall | Correctness | Completeness | **Doc Recall** | **Invalid Extra Docs** |
|---|---|---|---|---|---|---|
| 1 | metor.com | 80.34 | 82 | 86.22 | 85.53 | 4.96 |
| 2 | Troml | 76.79 | 83.8 | 81.84 | 86.55 | 12.65 |
| 4 | OpenClaw | 68.22 | 81.6 | 72.86 | 79.02 | 0.47 |
| 8 | RAGFlow | 50.24 | 56 | 58.74 | 63.05 | 4.61 |
| 11 | hRAG | 44.74 | 52.6 | 54.38 | 69.65 | 9.01 |
| 15 | NVIDIA AI Blueprints | 37.73 | 59.6 | 45.2 | 72.61 | 7.72 |
| 16 | AnythingLLM | 35.58 | 47.8 | 44.59 | 40.5 | 3.31 |
| 17 | Weaviate Verba | 34.48 | 41.4 | 44.9 | 51.98 | 1.81 |
| **18** | **Cymatix-Context** | **33.93** | **42.2** | **42.74** | **50.7** | **12.99** |
| 19 | LlamaIndex (default) | 27.2 | 32.4 | 37.76 | 30.56 | 1.49 |
| 20 | LangChain (default) | 24.98 | 31 | 35.65 | 36.39 | 3.15 |

`Overall = mean(correctness × completeness)`; incorrect answers score 0 regardless.
**Invalid Extra Docs is a reported diagnostic, not a score term.**

Three readings:

1. **Document Recall 50.7 is the binding constraint** — the top four sit at 79–86, and
   everything above rank 15 except AnythingLLM is ≥ 61. Our internal
   `delivered_gold_rate` of 0.5650 tracks it closely, which means the internal instrument
   is measuring the right thing and the retrieval lanes are aimed correctly.
2. **Our recall→correctness conversion is normal; the recall is not.** Cymatix: 50.7 →
   42.2 (0.83). Weaviate Verba: 51.98 → 41.4 (0.80). We are not wasting retrieved gold;
   we are not retrieving enough. With #340's ceiling at ~0.789 delivered-gold,
   **recall 50.7 → ~70 puts correctness near 58 and Overall near 46 — rank 10–11.**
   That is this phase's realistic target.
3. **Invalid Extra Docs 12.99 is second-worst on the board, but it does not gate rank.**
   Rank-2 Troml sits at 12.65 with recall 86.55 — it floods too, and still places second.
   The differentiator is recall, not flooding. Worth fixing because it contradicts the
   product thesis (`DESIGN_TARGET.md` names the right metric as *"fraction of delivered
   tokens that are load-bearing"*, and records that irrelevant context corrupts
   downstream reasoning) — but **do not prioritise it over recall.** With 13.71 docs
   submitted and 12.99 judged invalid, only ~0.7 documents per question are landing.

**Where the recoverable points actually are** (official judged, per type; `combined` =
correctness × completeness, the score term):

| type | n | correctness | completeness | **combined** | recoverable points |
|---|---|---|---|---|---|
| basic | 175 | 46.29 | 44.01 | 40.39 | **10,432** |
| **semantic** | **125** | **17.60** | **21.73** | **14.76** | **10,655** |
| project_related | 40 | 25.00 | 47.44 | 18.49 | 3,260 |
| intra_doc_reasoning | 40 | 60.00 | 60.42 | 52.50 | 1,900 |
| info_not_found | 20 | 85.00 | 10.00 | 10.00 | 1,800 |
| constrained | 30 | 50.00 | 73.89 | 46.21 | 1,614 |
| completeness | 20 | 25.00 | 38.84 | 22.50 | 1,550 |
| high_level | 10 | 30.00 | 47.33 | 30.00 | 700 |
| conflicting_info | 20 | 75.00 | 78.08 | 65.14 | 697 |
| miscellaneous | 20 | 80.00 | 72.08 | 69.58 | 608 |

**`semantic` and `basic` hold ~63% of every recoverable point.** Semantic is 25% of the
benchmark scoring 17.6% correctness. Concretely:

- semantic 14.76 → 40.39 (merely matching `basic`) = **+6.4 overall → 39.9, ~rank 14**
- semantic → 60 = **+11.3 overall → 44.9, ~rank 11**
- both semantic → 60 and basic → 60 = **~51.7, ~rank 7–8**

**This converges with the shortlist finding.** The semantic class is paraphrastic — query
terms do *not* lexically match the document. A top-50 FTS5 `OR`-of-query-terms post-filter
is exactly the gate that should fail hardest there, and cc-exchange's cell-5 receipt
already measured that **62 of 125 semantic questions miss gold in all six arms, while
plain FTS at depth 1000 recovers 45 of those 62 (73%)**. Stratify the delivery-loss
ledger by question type and this is directly testable.

Two classes need a *different* fix, worth noting so they don't get mis-assigned to
retrieval: **`project_related`** retrieves gold 100% of the time internally yet scores
25.0 correctness — that is an assembly/generation loss, not a retrieval one. And
**`info_not_found`** scores 85.0 correctness with 10.0 completeness, which is a scoring
interaction with abstention rather than a retrieval gap.

**Provenance — recovered from the submitted exports.** No submission artifact exists in
the repo, but the exports you supplied settle it:

- `erb500k_blob_official_judged_2026-07-14.json` — the official judged run:
  correctness **41.6**, completeness **42.8**, combined **33.57** (recall and
  invalid-extra-docs are 0.0 in this file because it was judged answers-only).
- `erb500k_blob_document_ids_2026-07-15_seen64k.jsonl` — 500 rows, **6,853 document IDs
  total, mean 13.71 / median 14 / max 16 per question, zero empty**. This is what
  produced the leaderboard's Document Recall 50.7 and Invalid Extra Docs 12.99.

**The submission dates to 2026-07-14/15 — a full month before the encoder flip
(2026-08-15..19) and before v0.9.0.** So **the shipped v0.9.0 defaults have never been
submitted**, and internal receipts show post-flip delivered-gold *rose* (0.504 → 0.610
at 100k).

**Fidelity problem — state this plainly before any re-submission.** The reproduction doc
flags it as "the single most important fidelity flag": the run was executed under a
**64K-character context cap** (~16 evidence items, ~56K chars), **not cymatix's
production ~7,000-token expression budget**. The doc's own words: it measures *"the
quality of the retrieval pool, not what cymatix would actually inject in production."*
The 64K change **is not on master**. The 13.71 docs/question in the export corroborates
it — the shipped `max_genes_per_turn` is 12.

So the public 33.93 corresponds to **no shippable configuration of cymatix** and cannot
currently be reproduced from master. Any re-submission needs an explicit decision about
which config it represents, and the answer should probably be "shipped defaults", even
if the first honest number is lower.

**A cross-check worth running on Day 0.** The repro doc records **median gold rank ≈ 384
on the semantic arm** — "gold is in the pool but ranks far below the delivery budget."
That is *arithmetically impossible* with an active top-50 BM25 shortlist, which caps
attainable ranks at 50. Either the shortlist soft-failed, or the run was swap-mediated —
and the ledger records that `/admin/swap-db` silently omitted `bm25_shortlist_enabled`
and `filename_anchor_enabled` (ctor default `False`, shipped `True`) before `299e9e2`.
If the submitted run had the shortlist **off**, then the leaderboard number is from a
third distinct config, *and* it is independent evidence about what the shortlist does.
Cheap to settle; high information either way.

### 2. The 829k latency number is stale — and the retest is not a bug gate

`postflip_default_confirm_829k.json` reports **p50 25,716 ms / p95 51,001 ms** with
`bed_bytes = 34,392,408,064`. The beds were compacted **2026-08-20, after that run**;
`erb_829k_nosplade.db` is now **17.4 GB** (`erb_blob.db` 37.1 GB, `erb_100k.db` 6.4 GB).
Box: 47.9 GB RAM / 22.8 GB free / 16 logical CPUs — the old bed exceeded free RAM, the
new one fits. Precedent exists: the 2026-08-03 → 08-04 ladder recorded 829k going
**111,673 → 26,081 ms (4.3×)** when a ">RAM cliff" was removed.

**Answering the question directly: yes, a current ERB bed is already under 25 GB
(17.4 GB) — no fresh carve needed.**

**Correction to an assumption I made earlier in this session:** I first framed the retest
as "delivered-gold must be unchanged, any delta is a bug, since #346 is signal-neutral by
design." That is wrong, and the shortlist finding is why. The compaction did an **FTS5
external-content rebuild**, which recomputes bm25 statistics — and the shortlist is
`ORDER BY rank LIMIT 50` over exactly those statistics. Changed bm25 stats can legitimately
change shortlist membership, hence `eligible_ids`, hence the ranking. The PKI drop is a
second content mutation in the same step. So the retest must be declared **a new arm on a
new bed** with a two-way delta whose interpretation is "cannot attribute" — not a bug gate.

The `ingest_c` house rule does not cover this: compaction changed bed *content after
ingest*, not ingest concurrency, so the rule would wrongly green-light the comparison.
We need a **bed identity digest** (schema version + `genes` rowcount + `genes_fts`
rowcount + fts5 integrity status + a stable hash of gene_ids) in the receipt. That
instrument is also literally one of the owed cc-exchange 0017-s8 items.

### 3. Proxy injection — the 5 qps memory is right, and formally not citable

Dogfood bed, `POST /context`, 2026-08-17:

| c | 1 | 2 | 4 | 8 | 16 |
|---|---|---|---|---|---|
| median ms | 123.5 | 184.1 | 557.7 | 1051.2 | 2004.3 |
| qps | 1.24 | 2.86 | **4.21** | 4.66 | 4.99 |
| valid | ✅ | ❌ overlap | ✅ | ❌ recall | ❌ recall |

Pre-flip the wall was flat **1.46 qps from c=2 to c=16**; the ceiling moved 3.4×. But
c=8/16 fail the recall-drift gate, so the **highest citable cell is c=4 → 4.21 qps**.
The instrument that resolves it — the rank-hash gate (`CYMATIX_BENCH_RANK_HASH`, #339) —
merged as PR #405 on 2026-08-23 and **has never been run**. `w16` = 16 client
subprocesses, not executor workers; the executor stayed at 2, and sweeps showed qps
plateaus ~1.7 at every executor size (the wall is the shared encoder/GIL layer).

### 4. Ingest is faster than assumed — the LLM is not in the path

`[ingestion] backend = "cpu"` (spaCy + regex). **Ollama is never reached at shipped
defaults.** Best v0.9.0-defaults number (`sema_ingest_ab_dogfood.json`, 1,181 files /
21.2 MB → 6,788 genes): **761.5 s wall, 1.551 files/s, 8.91 genes/s, 27.8 KB/s.**
CpuTagger is ~7 ms/chunk vs ~6 s/chunk for the LLM tagger; a full-bed comparison
measured **CPU 2.6 h vs LLM 17.7 h**.

**Learn-loop feasibility verdict: viable, with a transport caveat.** A compacted session
learning of 5–20 KB ingests in **~0.2–0.7 s** in-process. But `POST /ingest` measured
**0.409 genes/s under read load — 5.6× slower than a direct in-process writer**
(2.281 genes/s), because it serializes on the server's `_write_lock`. That receipt is
2026-08-03 with encoders on and needs re-running, but the ordering is structural: an
auto-ingest loop should use the in-process path or accept batching. `[mem_sync]` already
exists as an out-of-band poller (`scripts/run_mem_sync.py`, default off, `watch_dirs=[]`,
no config dataclass, never exercised).

### 5. SlopCodeBench: both campaigns ran, both null — and the design may be unable to detect anything

| campaign | date | model | verdict | 95% CI on median paired delta |
|---|---|---|---|---|
| Luna | 2026-08-08 | gpt-5.6-luna | `no_detectable_difference` | [−0.0352, +0.0205] |
| Terra | 2026-08-11 | gpt-5.6-terra | `no_detectable_difference` | [−0.0130, 0.0] |

There is no un-run campaign waiting. Treatment shape: a packet rendered at 3,715–3,840
tokens against a 4,000 ceiling, **+8.4% of input tokens**, elapsed ratio 1.028,
head-to-head 24/23/17 — a coin flip. The structural problem: **the control agent already
has full filesystem access to the snapshot repo**, so on a small single-repo task the
packet competes against the agent simply reading the files. The measured ceiling for a
retrieval treatment under those conditions is near zero.

Standing risk independent of the science: the whole harness
(`cymatix_context/integrations/scbench/`, `benchmarks/scbench/`) and every pilot artifact
live **only** on unmerged branch `origin/codex/scbench-cymatix-ab`, and
`docs/benchmarks/BASELINES.md` cites paths that do not resolve from master.

### 6. AssetOpsBench — what DP2 can and cannot deliver

The 2026-07-13 verdict is blunt: AOB's suite is n=152 but **scenarios requiring document
retrieval ≈ 0**. The FMSR "knowledge base" is a CouchDB store of failure-mode *name
lists*; the only prose is one-line `description` fields in three CSVs with one data row
each. Their `semantic_similarity` scorer is an unimplemented `NotImplementedError`.

So DP2 / Reshape A (council: GO_WITH_CHANGES, operative) is a **judge-transfer trial**,
not an AOB score. It answers *"is their judge usable to grade document-grounded agent
work?"* It will not produce "cymatix scores X on AOB", and the council's two-claim
discipline forbids publishing one. Note it shares the "control has filesystem access"
design flaw with SCBench.

### 7. cc-exchange board state

Authoritative reading (claim files + BOARD.md; note BOARD.md is itself stale in three rows):

| card | what | state |
|---|---|---|
| **B1** static-miss crosscheck | our 99/469 vs Joe's 62/125 set | **LANDED** — Jaccard 0.916667, ruling *"no split. One root cause, one class, one lever (entry-path coverage)"* |
| **B4** entry-path coverage | fts5 depth on the fixed 62-set | **RETIRED** (premise thrice-refuted; superseded by D1) |
| **D1** `fts5_candidate_depth` sweep | #340/#260 reframed | **CLAIMED by Joe** 2026-08-22 |
| **D2** per-stage **query** latency map at blob scale | #377 re-measure | **OPEN, unclaimed** |
| **D3** per-stage **ingest** wall + EXP-A batching prize | #373/#372/#371 | **OPEN, unclaimed** |

Joe has 2× DGX Spark (GB10); **we do not** — plan assumes this box only.

**Time-sensitive:** Joe's D1 sweeps `fts5_candidate_depth` (the FTS content-tier *fetch*
depth). The shortlist is a top-50 post-filter that runs **downstream** of it. Widening
fetch depth cannot help if a top-50 bm25 cut follows — so **D1 could return a false
"depth inert" verdict for a reason Joe cannot see from his side.** He must be told before
he runs it.

**SIKE:** per your decision, the `sike-run1` ladder is **stood down**, not unblocked. That
retires the 64-file gold-pack and the xl decontamination ruling. The five 0017-s8
artifacts are a separate debt and stay owed — and one of them (the bed identity digest)
is an instrument this phase needs anyway (§2).

---

## Plan

### Day 0 — unblock, instrument, and stop a bad measurement (≈1 day, little compute)

| # | item | effort |
|---|---|---|
| 0.1 | **Message Joe**: stand SIKE down formally; warn that the top-50 bm25 shortlist post-filter sits downstream of `fts5_candidate_depth`, so D1 may read "depth inert" spuriously; send the L1.1 matrix design so depth and shortlist-size aren't swept incoherently on two beds; claim D2 + D3 | ≤1h |
| 0.2 | **Commit the submission provenance receipt** — the four exports (judged JSON, answers, document_ids, repro doc) plus the recovered facts: 2026-07-14/15, pre-flip stack, 64K-char cap (not the 7K shipped budget), 64K change not on master, 13.71 docs/question. Add a BASELINES row so the public number stops being unattributable | ≤1h |
| 0.2b | **Settle the median-gold-rank-384 contradiction** — impossible under an active top-50 shortlist. Determine whether the submitted run was swap-mediated (shortlist silently off pre-`299e9e2`). Either answer is high-information and it is a read-only check | ≤1h |
| 0.3 | **Start the backend on 11437**, confirm MCP connects. This is a config/process issue, not an MCP bug — `mcp/mcp_server.py:126` is a thin proxy to `CYMATIX_MCP_URL` | ≤30m |
| 0.4 | **Promote the #377 deferral ledger to a tracked file** and open one 0.9.x phase tracker issue (mirrors the #377 pattern) | ≤30m |
| 0.5 | **Bed identity digest** (0017-s8 item) added to the ERB receipt schema — prerequisite for every post-compaction comparison | half-day |

### Day 1 — attribution before intervention

| # | item | effort |
|---|---|---|
| 1.0a | Thread **per-stage gold booleans** through the existing #405 rank-hash seam (`context_manager.py:2160-2166`, already captures `_candidate_set_sha256` + `pool_size` at "the admission choke point"): `gold_in_tier_scores` → `gold_in_shortlist` → `gold_in_fused[:12]` → `gold_in_candidates_at_hash` → `gold_in_delivered`. Also add a `bm25_shortlist_dropped` / `shortlist_size` receipt observable — without it a `no_bm25_shortlist` arm is reported `unfalsifiable` by `validate_arm` (`ablation_ladder.py:672-712`) | half-day |
| 1.0b | **Delivery-loss ledger** @100k n=141: of 141 needles, how many lose gold at each of {tier scoring, shortlist, fuse-12 cut, classifier cap, moe/frontier cap, abstain}. **Stratify by ERB question type** — the prediction is that shortlist loss concentrates in `semantic`, and that is the single highest-value cell in the whole phase. This is the 0.9.x ledger's #344 | ≤2h run |

**This table gates everything downstream.** Every subsequent arm is conditioned on it.
Running any sweep before it is guessing.

### Day 2 — the de-risk

**L1.1 — shortlist matrix**, @100k n=141, single pass (see sequencing rule 4):

| cell | `bm25_shortlist_enabled` | `bm25_shortlist_size` |
|---|---|---|
| A (control) | true | 50 |
| B | true | 200 |
| C | true | 500 |
| D | false | — |

Report delivered_gold_rate, r@12, **static-miss set membership against the known
25-needle 100k set, and a per-question-type split.** If B/C/D rescue static-miss needles,
#340's "pool-entry failure upstream of tier weighting" verdict is falsified in one
afternoon and the admission workstream collapses to tuning one integer. If they rescue
zero, the admission-redesign hypothesis is *earned* with a real receipt. The type split
is what connects a delivered-gold delta to a leaderboard delta — a needle rescued in
`semantic` is worth far more than one rescued in `miscellaneous`.

The size ladder matters as much as the boolean: if `false` wins but costs latency,
`size=500` may capture most of the gain at bounded cost, and it is a shipped knob.

**Then, in the same box-day:** 829k confirm of the winning cell — **run it even on a 100k
null**, because the shortlist is a *fixed* 50 against a growing corpus, so its harm is
scale-dependent and 100k will understate it. Fold in the **Lane 0 compacted-bed retest**
(declared as a new arm, two-way delta, no bug gate) and a bed-size inventory via the
existing `benchmarks/dogfood/erb/storage_audit.py`.

### Days 3–4 — the rest of the ranking lane, conditioned on 1.0b

| # | arm | note |
|---|---|---|
| L1.2 | **Classifier caps {5, 8, 12}** | Only if 1.0b shows the cap binding on ≥10 needles. **Must be swept jointly with a `max_genes`/`_fuse_limit` widen** — raising the cap above 12 is a no-op because `_fuse_limit = limit = max_genes = 12`. Also watch the `small_moe`/`frontier` caps at `context_manager.py:2183-2190`, which apply *on top*. Requires lifting three code constants into config, default-inert |
| L1.3 | **`rrf_k` {10, 20, 60, 120}** | The one weight-family arm with a real mechanism: `k` changes the *shape* of fusion (head sharpness), not one tier's amplitude. Cheap |
| L1.4 | **Abstain + tier-ratio refit** | `scripts/calibrate_thresholds.py` exists; `cymatix.toml:699-728` has the per-class blocks ready. A refit, not a search. **Must follow L1.1/L1.2** — refitting against a distribution those are about to shift is work done twice. Known prize: at 829k, 16/469 abstains are all `score_below_floor` and **13 of the 16 hold gold in the final top-12** |
| L1.5 | **Synonym re-verify, then prune** | **Do not apply the prune first.** `query_terms` is synonym-expanded and feeds `bm25_match`, so pruning changes shortlist membership. The +0.033 no-synonyms result was measured under shortlist=ON, where extra terms dilute a 50-doc funnel; with the shortlist off the verdict could flip sign |
| L1.6 | **The ten RRF multipliers** | **Fund only if 1.0b shows a meaningful population reaching the fused stage at rank 13–50.** RRF is deliberately insensitive to score magnitude — that is its design property, and why `pki_w2/w3/w4` regressed monotonically. If the population is small, write the null up from 1.0b rather than measuring it. If funded, sweep `filename_anchor_weight` and `fts5_weight` **jointly** (they compete for the same head positions), not ten one-at-a-time arms |

### Parallel — CLI/MCP harness (yours; half-day to box-day)

Three-layer decomposition reusing `run_server_scaling.py`'s machinery:
in-process (ERB ladder) / HTTP `/context` / MCP stdio→HTTP.

- **CLI:** subprocess cold-start decomposed into interpreter start / import stack /
  `load_config()` / SQLite open / first query — this also tests the unverified
  "sub-100ms `cymatix --help`" claim asserted at `cli/dispatcher.py:8`.
- **MCP:** per-tool round-trip for the default-exposed core 5 (`cymatix_context`,
  `cymatix_context_packet`, `cymatix_ingest`, `cymatix_health`, `cymatix_sessions_list`),
  separating transport from server time using the server-side stage ring.
- **Tokens returned per call** — the real agent cost.
- **Boot path only, never `/admin/swap-db`.** The ledger records that swap silently
  omitted `filename_anchor_enabled` and `bm25_shortlist_enabled` (ctor default `False`,
  shipped `True`) before `299e9e2` — a harness booting via swap would measure a different
  retrieval config than the ERB ladder. Assert `tests/test_swap_db_knob_sync.py` in CI.
- Re-validate c=2/8/16 with the #405 rank-hash gate before citing any qps number.
- Deliberately **not** `bench_claude_matrix.py` (~$19/arm, whole-agent turns, cannot
  isolate tool latency).

### Parallel — Joe's box

D2 (per-stage query latency map) and D3 (per-stage ingest attribution). **The learn-loop
feasibility gate rides on D3's receipt** — no separate measurement needed. Our only
obligations: matching `ingest_c` on any comparison, and not blocking his C1 half-2.

### External benchmarks

**SCBench — merge first, then diagnose offline. No new campaign.**
1. **Merge `benchmarks/scbench/` + `integrations/scbench/` to master** as a pure code
   move with the campaign artifacts (half-day, unconditional). One stale-branch prune
   currently loses the entire harness. Fix the dangling BASELINES paths and the terra row
   that still reads "in flight".
2. **Was the packet ever used?** (≤2h, pure grep over committed `agent/rollout-*.jsonl`)
   — does any file/symbol named *only* in the packet appear in a later tool call or edit?
   Near-zero usage ⇒ the failure is agent ingestion (placement/framing/format), not
   retrieval.
3. **Was the packet even right?** (≤2h) — for each checkpoint, do the files in the gold
   diff (`diff.json`, `evaluation.json`, both committed) appear in the packet? This
   yields a real retrieval precision/recall number on the benchmark, from data we already
   have. Low ⇒ it's a retrieval bug and the ranking lane fixes it.
4. **Redundancy audit** (≤2h) — for checkpoints the *control* solved, how many did it
   solve by reading files the packet also contained? This quantifies whether the benchmark
   can detect the treatment at all.
5. **Only then redesign the condition.** Not a bigger packet — 3,840 tokens is already at
   the ceiling and +8.4% input bought nothing. In cost order: (a) measure **tool-calls /
   tokens-to-solve** instead of solve rate, since efficiency is retrieval's actual claim;
   (b) constrain the control (no filesystem grep); (c) move to a repo large enough that
   reading everything is infeasible.

**AssetOpsBench DP2 slice.** Vendor their Apache-2.0 judge into
`benchmarks/assetops_judge/` with attribution + commit pin; author ≤15 utterances against
our small bed; three arms (cymatix MCP / filesystem baseline / closed-book control);
non-Claude judge; pre-registered kill criteria; internal-only memo. **Run the SCBench
redundancy audit (step 4) before finalising the filesystem-baseline arm**, or DP2 will
reproduce the same null for the same structural reason.

**ERB re-submission.** Deferred per your call until after Day 2. The decision then is
informed by 0.2's provenance answer: if the 33.93 was pre-flip, a re-submission on
current defaults may be worth doing immediately and separately from any retrieval win.

### Close-out

- Rewrite `docs/ROADMAP.md` with this phase's receipts in it (the ledger promotion in 0.4
  covers the interim).
- **Retire dead code**, each a delete with no measurement needed:
  `retrieval/lexical_rescue.py`, `retrieval/relevance_window.py`, `scoring/ray_trace.py`'s
  unused public API (`ray_trace_boost` / `ray_trace_info`), `integrations/scorerift.py`,
  `identity/claims_graph.py`, the self-declared-inert `CYMATIX_SHARD_GLOBAL_IDF`, and
  `splade_auto_enable_below_genes` / `_disable_above_genes` (orphaned by the SPLADE remove
  verdict). Consider a real console script for MCP — `pyproject.toml` has no
  `[project.scripts]` entry, so the documented invocation goes through a 15-line
  `sys.modules` shim. **Leave `write_queue.py` alone** — #353 says explicitly do not delete it.

---

## Sequencing rules

1. **Attribution precedes intervention.** 1.0b's table decides whether L1.2–L1.6 and the
   admission workstream get funded.
2. **The compacted-bed retest is a new arm, not a bug gate.** FTS5 rebuild changes bm25
   statistics, which legitimately changes shortlist membership under `ORDER BY rank`.
3. **Screen at 100k (n=141), confirm at 829k (n=469).** Exception: confirm the shortlist
   arm at 829k even on a 100k null — a fixed 50 against a growing corpus is
   scale-dependent. **Never difference across needle sets** — `needles_resolved_blob.json[:30]`
   and `needles_resolved_blob_probe30.json` are different sets and once produced a
   spurious 0.767→0.667 "regression".
4. **No order-reversal in the quality lane.** The ledger measured per-needle `gold_ranks`,
   `delivered_gold`, `final_rank_of_first_gold` as bit-identical forward vs reversed
   (141×6 at 100k, 30×3 at 829k, zero differences); only wall times move. Keep reversal
   only where wall time is the metric.
5. **No bed mutation during a campaign window.** Run migrations *between* campaigns and
   record new bed bytes + identity digest in the BASELINES row.
6. **Every shipped-default change requires a paired 829k confirm + a BASELINES row + a
   config hash in the receipt + a named revert commit.** The swap-db drift is why.
7. **The rank-hash gate governs HTTP concurrency claims only** — it is irrelevant to the
   in-process ERB ladder, and a hard prerequisite for the CLI/MCP lane.

---

## Verification

| Item | How it is verified |
|---|---|
| 1.0a instrumentation | Ships default-inert under the existing `CYMATIX_BENCH_RANK_HASH` gate, with a both-legs byte-identity proof (the pattern PR #405 already established) |
| 1.0b ledger | One committed table attributing per-stage gold loss across 141 needles; sums reconcile to the known `r@12 − delivered_gold_rate` gap |
| L1.1 shortlist | 4 cells @100k + 829k confirm of the winner; static-miss set membership reported per cell against the 25-needle 100k set; `bm25_shortlist_dropped` present so `validate_arm` returns a verdict rather than `unfalsifiable` |
| Compacted-bed retest | New BASELINES row with bed identity digest; two-way delta vs `sema_readgate_829k_n469.json` labelled "cannot attribute (FTS5 rebuild + PKI drop)" |
| Concurrency | `run_server_scaling.py` with `CYMATIX_BENCH_RANK_HASH`; c≥8 cells carry `valid=true` or a named cause |
| Ingest | First committed `benchmarks/dogfood/ingest_throughput.json` incl. the `no_embed_ingest` arm — the shipped-default world, never reported in any receipt, doc, or CHANGELOG line — with corpus identity hash and `ingest_c=1` |
| CLI/MCP | Committed receipt + BASELINES row; CLI cold-start decomposed into named stages; MCP per-tool p50/p95 for the core 5; boot path asserted, not swap |
| Each Lane-1 arm | Single pass, delivered basis, paired per-needle gain/loss ledger; no graduation without an 829k confirm |
| SCBench | Offline replay against the 16 committed pair receipts — **no new campaign spend** |
| AOB DP2 | Internal-only memo; pre-registered kill criteria honoured; no cymatix-alone number published on cymatix-authored gold |
| Suite | `python -m pytest tests/ -m "not live" -v` green before any merge |

---

## Open items needing your call later

- **ERB re-submission — which config does the entry represent?** The standing 33.93 was
  run at a 64K-char cap that is not on master and not shippable. A re-submission at
  **shipped defaults** is the honest entry and is what I would recommend, even if the
  first number comes in lower than 33.93; the alternative is continuing to publish a
  figure no released version can reproduce. Needs your call plus an `LLM_API_KEY` and a
  judging-spend decision. Best sequenced after Day 2 so the retrieval work is reflected.
- **L1.2** requires lifting three classifier constants into config. Assumed acceptable
  as a default-inert port, byte-identical for untouched configs — the pattern used for
  #351's knobs.

---

## Implementation log — 2026-08-24

### Shipped: bed provenance (plan item 0.5)

Beds carried **no build metadata whatsoever** before this. New:

- `cymatix_context/storage/provenance.py` — append-only `bed_provenance` table
  created by `init_db`, recording per event: UTC timestamp, event kind, cymatix
  version, git sha + **dirty flag**, `ingest_c`, the ingest-affecting config
  subset, gene/FTS counts, an optional `gene_id` identity digest, and corpus id.
  `config_drift()` diffs consecutive events and names the exact knob that
  changed — which is what makes an aging bed legible.
- `cymatix diag bed [--db PATH] [--identity] [--json]` — reads any bed on disk.
- `scripts/stamp_bed_provenance.py` — stamps a build, or retro-stamps an older
  bed *without* fabricating a build config (`--no-config`).
- `tests/test_bed_provenance.py` — 15 tests, all passing.

Verified against the live bed: `erb_829k_nosplade.db` reports 829,131 genes and
829,131 FTS rows (no index drift), matching the documented count exactly.

One bug found and fixed during the work: `COUNT(*)` on an external-content FTS5
table is a full scan of the backing view — it hung for minutes on the 17.4 GB
bed. `fts_count` now delegates to `ddl.fts5_index_row_count`, which reads the
`genes_fts_docsize` shadow (cheap, and the actual index-side number).

### Two traps in the bed builder — a naive "rebuild at v0.9.x defaults" is wrong

`scripts/build_fixture_matrix.py` does **not** honour `cymatix.toml` for the
knobs that matter most:

1. **`_env_flag(name, default="1")`** — `CYMATIX_BFM_SPLADE` and
   `CYMATIX_BFM_DENSE_BACKFILL` are **ON unless explicitly set to `0`**, while
   v0.9.0 ships `splade_enabled = false` and `dense_embed_on_ingest = false`
   (#371). Rebuilding without the pins yields a bed with SPLADE terms and
   BGE-M3 vectors — not the shipped world.
2. **`entity_graph=True` is hardcoded** in the `Genome` constructor
   (`build_fixture_matrix.py` ~line 955), not read from config.

Because of this, `provenance.record_event` takes an `overrides` dict: the stamp
records what *actually ran*, not what config claimed. `scripts/build_erb_blob_v09x.py`
pins both env flags and passes the effective settings through.

The wrapper also builds in **`--mode blob` (direct)**, skipping the
sharded → `shard_to_blob.py` merge that silently dropped 199 rows on the
2026-07-17 build.

### Ingest equivalence gate (`scripts/ingest_equivalence_gate.py`)

The BASELINES rule blocks `ingest_c > 1`, but it was adopted from cc-exchange
EXP-A, which ran the **LLM tagger** and classified its own result as
"LLM-batching drift, not a driver bug." v0.9.x ingests with
`backend = "cpu"` (spaCy + regex), so that mechanism cannot fire — equivalence
is plausible but unmeasured.

The gate measures it. Both arms drain through the same main-process writer; the
only difference is whether chunk+tag ran in an `mp.Pool`. Since the pool uses
`imap_unordered`, physical insertion order is *expected* to differ, so the gate
separates three questions: document set (`gene_id` digest), content (content +
sorted tags digest), and order-sensitive derived tables (`entity_graph`,
`harmonic_links`, `gene_relations`). `equivalent` requires all three.

Stakes: sequential rebuild is the safe default, parallel could cut the build
several-fold. `build_erb_blob_v09x.py --workers N` refuses to run without a
passing gate receipt. A pass would also retire the cc-exchange item that has
kept Joe's c=8/16/32 ceiling ladder blocked.

### ERB corpus, confirmed intact

`F:/tmp/enterprise_rag_500k/sources/` — 500,000 files, 2.65 GB, avg 5,690 B,
**all `.json`** (which does pass `INGEST_EXTS`), across 9 roots: slack 278,928 ·
gmail 118,553 · linear 34,483 · google_drive 24,521 · hubspot 14,667 ·
fireflies 9,936 · github 7,865 · jira 5,978 · confluence 5,069.

### Ingest equivalence: MEASURED, and a reproducibility bug fixed

Receipt: `benchmarks/dogfood/receipts/ingest_equivalence_erb.json`
(2,000-file ERB sample, seed 20260824, 6 workers, 0 errors both arms).

**Run 1 — parallel as shipped (`imap_unordered`):**

| tier | sequential | parallel | equal |
|---|---|---|---|
| documents (`gene_id` digest) | `b5b60b15…` | `b5b60b15…` | yes |
| content (content + sorted tags) | `44c0170c…` | `44c0170c…` | yes |
| `entity_graph` / `filename_index` | 43,832 / 3,828 | 43,832 / 3,828 | yes |
| **`gene_relations`** | **28,765** | **28,767** | **no (+2)** |

So EXP-A's drift mechanism genuinely cannot fire on the CPU tagger — documents
and content are byte-identical. But `gene_relations` is **retrieval-live**
(`okf/__init__.py` says so; it feeds `co_activation.py` neighbour lookups and
`retrieval/tie_break.py`), so a 0.007% edge delta is small but not nothing.

**Root cause and fix.** Chunking and tagging are pure at v0.9.x defaults, so
drain order was the *only* variable — and `_parallel_ingest_to_genome` used
`pool.imap_unordered`. Both drain paths in `scripts/build_fixture_matrix.py`
(blob and shard) now take `ordered: bool = True` and use `pool.imap`.

**Run 2 — ordered drain:**

| tier | sequential | parallel | equal |
|---|---|---|---|
| documents | `b5b60b15…` | `b5b60b15…` | yes |
| content | `44c0170c…` | `44c0170c…` | yes |
| **`gene_relations`** | **28,765** | **28,765** | **yes** |
| **verdict** | | | **`equivalent: true`** |

Throughput: **8.864 genes/s sequential vs 43.012 genes/s at 6 workers
(4.85×)**; ordering costs ~2% versus the unordered 5.03×.

**Consequences.**

1. The 829k-gene ERB rebuild drops from **~27.5 h to ~5.4 h**.
2. The BASELINES `ingest_c > 1` block now has the equivalence gate it was
   waiting for — for the CPU-tagger path only. The LLM-tagger finding is
   untouched and still stands; EXP-A remains correct about what it measured.
3. cc-exchange: this is the missing piece under which Joe's c=8/16/32 ceiling
   ladder was blocked, and the `imap_unordered` fix is worth shipping to him
   regardless of the ladder — any parallel bench build before this was
   silently nondeterministic in its co-activation edges.

Rate note: the honest sequential figure is **8.4–8.9 genes/s**, consistent with
the dogfood receipt's 8.91. A file-rate extrapolation taken from the first ~115
files gave 55.6 h and was wrong — it included model-load warmup. Quote the gene
rate, and discard warmup before extrapolating.

### Ingest throughput degrades superlinearly with bed size (new measurement)

Observed during the 2026-08-24 ERB rebuild (6 ordered workers, v0.9.x pins):

| bed size | genes/s |
|---|---|
| 3,829 (equivalence-gate sample) | 43.0 |
| ~90,000 | 22.2 |
| ~139,000 | 17.7 |

Rate falls roughly as `n^-0.5`. The 4.85x parallel speedup in the equivalence
receipt was measured on a near-empty bed and **must not be read as a
whole-build figure** — it is a small-bed number.

**Driver: secondary-index write amplification.** At 139,492 genes:

| table | rows | rows/gene | projected at 829k |
|---|---|---|---|
| `promoter_index` | 3,164,154 | **22.68** | ~18.8M |
| `entity_graph` | 1,774,451 | **12.72** | ~10.5M |
| `gene_relations` | 1,305,877 | 9.36 | ~7.8M |
| `filename_index` | 139,491 | 1.00 | ~829k |
| `genes_fts_docsize` | 139,499 | 1.00 | ~829k |

~35 secondary rows are written per gene, into B-trees that grow as the build
proceeds — which is the degradation mechanism.

**The uncomfortable part:** the two largest amplifiers are both unvalidated.
`promoter_index` is the 2.58 GB structure whose necessity cc-exchange card B2 /
#355 says is *still unmeasured* (the `no_lex_anchor` arm was never run).
`entity_graph` is 1.37 GB, is a REMOVE-CANDIDATE on the retrieval-layer ledger,
has never been ablated on the delivered basis, and its **read path is disabled
at shipped defaults** (`entity_graph_retrieval_enabled = false`) — so the build
spends a large share of its wall clock writing a structure retrieval does not
read by default.

**Not acting on it for this build.** `cymatix.toml` ships `entity_graph = true`,
so a bed claiming "v0.9.x shipped defaults" must include it; dropping it would
make the artifact something other than what it says it is. The observation
belongs to the #355/#356 tags-gate campaign and the ledger's entity_graph
ablation, not to a build-time shortcut.

**Future optimisation (content-safe, not applied here):** secondary indexes are
derived, so a bulk build could drop them, load, and rebuild at the end. That
does not change bed content and would remove most of the superlinearity. Worth
a receipt before adopting.

This measurement is what cc-exchange card **D3** (per-stage ingest attribution)
was carded to produce; the rebuild generated it as a side effect.

### REFUTED: the page-cache hypothesis for ingest write amplification

Receipt: `benchmarks/dogfood/receipts/sqlite_cache_ab.json` (2026-08-25).
Method: two copies of the same 6.4 GB partial ERB bed (249,004 genes), same
400 previously-unseen files, one knob.

| arm | cache resolved | write MB/gene | read MB/gene | genes/s |
|---|---|---|---|---|
| `profile_default` | −65536 (64 MB) | **2.056** | 3.239 | 4.67 |
| `cache_8gb` | −8388608 (8 GB) | **2.055** | 1.995 | 4.95 |

**A 128x larger writer page cache changed writes by 0.05%** — speedup 1.06x,
`write_amplification_ratio` 1.00. It cut *reads* 38%, which is real but was
never the constraint.

So the earlier claim in this document — that the 64 MB `cache_max_mib` cap was
causing the write amplification — **is wrong and is retracted.** What the cache
fixes is re-reading evicted pages, not the volume written.

Two further facts from the same receipt:

- **`mmap_size` resolved to 2,147,418,112 in both arms** despite a 16 GiB
  override. SQLite caps mmap just under 2 GB, so `CYMATIX_SQLITE_MMAP_SIZE`
  above that is inert. Any future tuning that assumes a larger mmap is
  assuming something SQLite will not do.
- **806 write ops per gene, 2.61 KB each, identical in both arms.** Writes that
  do not respond to cache size are not eviction-driven; they are driven by how
  often the transaction commits and how many pages each commit dirties. The
  ingest path has no transaction batching in the drain — commits are per
  document (#372, which chose that deliberately for the serving path).

**Standing measurement, cache-independent:** ~2.06 MB written per gene, i.e.
**~1.7 TB of writes for a full 829k-gene build**, and the writer — not the six
chunk/tag workers — is the throughput ceiling (the parallel build managed
7.4 g/s at 249k genes while a single-threaded arm managed 4.95 g/s on the same
bed, so six workers bought almost nothing).

Untested candidates, in order of expected value:
1. **Defer secondary indexes**: drop them, bulk load, rebuild at the end.
   Indexes are derived, so this is content-safe. Standard bulk-load practice
   and the only candidate that attacks the per-commit dirty-page count directly.
2. **Batch transactions** in `_drain_with_batched_splade` (N documents per
   commit) — bench-path only, must not change the serving path's #372 contract.
3. Reduce what is written at all: `promoter_index` (22.7 rows/gene) and
   `entity_graph` (12.7 rows/gene) are the two largest contributors and both
   are unvalidated — but both are shipped defaults, so this is a #355/#356
   question, not a build knob.

### REFUTED #2: deferring secondary indexes during ingest (21x WORSE)

Receipt: `benchmarks/dogfood/receipts/ingest_write_path_ab.json` (2026-08-25).
Three arms, same 6.4 GB partial bed copied per arm, same 400 unseen files.

| arm | wall | g/s | write MB/gene | read MB/gene |
|---|---|---|---|---|
| `profile_default` | 211.6 s | 4.92 | 2.058 | 3.24 |
| `cache_8gb` | 209.8 s | 4.97 | 2.057 | 2.00 |
| **`deferred_indexes`** (15 dropped) | **4463.6 s** | **0.23** | 7.548 | **1297.68** |

Dropping the 15 explicit indexes on the hot write-path tables made ingest
**21x slower**, with read volume up 400x. The mechanism is plain in the read
column: the ingest path performs **indexed lookups during insert** (upsert and
dedup checks against `promoter_index`, `entity_graph`, `gene_relations`), so
removing the indexes converted every check into a full scan of a
multi-million-row table. Index deferral is only valid for append-only bulk
loads; this is not one.

**Standing conclusion after two refutations.** ~2.06 MB written per gene is a
property of the write path, not of a mistunable knob:

- page cache: **no effect on writes** (0.05%); cuts reads 38%
- index deferral: **catastrophically negative**
- `mmap_size` above ~2 GB: inert, SQLite caps it

The one lever the evidence still points at is **commit frequency**. Writes that
do not respond to a 128x cache increase are flushed at commit boundaries, and
the drain commits per document (#372) — 806 write ops per gene, ~1,940 per
commit, 2.61 KB each. Batching N documents per transaction is untested. It is
also the only remaining candidate that does not change what the bed contains.

Anything further is a question about *what is written* — `promoter_index`
(22.7 rows/gene, 3 indexes) and `entity_graph` (12.7 rows/gene, 2 indexes) are
the dominant contributors and both are unvalidated — but both are shipped
defaults, so that belongs to #355/#356, not to a build-time shortcut.

**Refutation 3 (2026-08-26): commit batching — mechanism works, prize refuted.**
`_CommitBatcher` (one `deferred_commits()` scope per K genes, WAL + free-RAM
valves) landed in `build_fixture_matrix.py`; 3-arm A/B on copies of the fresh
947k v0.9.x blob, identical pre-chunked gene stream (4,488 genes), page cache
pinned equal at 4 GiB (receipt `ingest_commit_batch_ab.json`):

| batch | wall_s | g/s | commits | MB/gene | ops/gene |
|---|---|---|---|---|---|
| 0 (per-gene) | 277.7 | 16.16 | 4,488 | 1.234 | 497.3 |
| 500 | 252.4 | 17.78 | 9 | 1.073 | 492.6 |
| 5000 | 259.8 | 17.27 | 1 | 0.929 | 437.7 |

New-row digests byte-identical across arms (batching changes when pages are
written, not what is stored). Commits fell 4,488 -> 1 but bytes only −25%,
ops −12%, wall −7..-9%: **cadence does not own the amplification.** The write
volume is random-key leaf scatter — gene_id and FTS term keys are hash-uniform,
so at 947k-gene depth each insert dirties *distinct* leaf pages across ~15
B-trees; a batch commit coalesces only the few shared interior pages. Bigger
batches cannot fix key randomness.

**The reframe the numbers force:** the sequential drain ran **16–17 g/s at
947k depth** — vs the ~5 g/s tail of the 6-worker build. Two candidate causes,
now separable: the build ran at the profile's 64 MiB cache (this A/B pinned
4 GiB — read-side indexed lookups at 20+ GB depth may be the true wall), and
producer/writer disk contention. Cache-at-depth A/B running
(`ingest_commit_batch_ab_cache64.json`). Keep the batcher (free, correct,
default-inert 0); do not flip any default on this receipt.

**Cache-at-depth CONFIRMED (2026-08-26, `ingest_commit_batch_ab_cache64.json`).**
Same harness, cache dropped to the profile's 64 MiB: control fell 16.16 ->
**6.47 g/s** (batch=5000: 7.75) with write volume byte-identical to the 4 GiB
run (1.234 MB/gene). The 2.5x is therefore entirely the READ side — upsert-path
indexed lookups whose working set grows with the bed — and it reproduces the
real build's ~5-7 g/s tail in isolation. The 20h build is now attributed:
64 MiB profile cache at 20+ GiB depth (dominant, 2.5x), slack's 1.42 genes/file
composition (genes/s vs files/s), producer I/O contention (residual), commit
cadence (minor). The earlier "cache is neutral" verdict was write-side-true and
read-side-wrong at depth. Shipped: `bulk_load` mem profile in `hardware.py`
(cache from available RAM, cap 4 GiB, receipt-cited; NOT for the server) +
`build_erb_blob_v09x.py` defaults `--mem-profile bulk_load --commit-batch 5000`.
Pagefile question closed: not the layer — SQLite page cache sizing was.

**L1.1 shortlist matrix + classifier_off — RAN 2026-08-27 on the v0.9.x blob**
(235 stratified needles, k=12, receipts `ladder_v09x_*_2026-08-27.json`; all
runs contended by concurrent scale builds — rank metrics only, no latency
claims):

| arm | r@12 | delivered | med_rank | static misses | paired r@12 |
|---|---|---|---|---|---|
| baseline (sl=50) | 0.660 | 0.549 | 4 | 50 | — |
| shortlist_200 | 0.638 | 0.549 | 5 | 34 | +3/−8 |
| shortlist_500 | 0.604 | 0.523 | 6 | 28 | +2/−15 |
| shortlist_off | **0.353** | 0.323 | 26 | **7** | **+0/−72** |
| classifier_off | 0.536 | 0.472 | 7 | 50 | +7/−36 |

**Verdict: #340's "shortlist hides gold" is TRUE and its implied fix is
WRONG.** Width rescues entry (static misses 50→7 monotone) but the fusion
layer cannot rank the rescued gold into the top-12, and the widened field
dilutes previously-good ranks — monotone net loss, catastrophic at off
(+0/−72). The BM25 shortlist is load-bearing: it IS the effective ranker.
The lever is ranking-under-width (rrf_k shape, tier separation, or an
append-below-50 rescue lane), not width itself. Joe's D1 must expect the
same signature — depth alone will read inert-to-negative.

**classifier_off refutes the naive cap-lift too:** the 24 cap-bound needles
partially converted (+20 delivered, constrained 0.333→0.733 — the predicted
rescue is real) but −38 losses elsewhere and r@12 fell 0.660→0.536, so the
classifier is coupled into retrieval/ranking, not just assembly caps.
Mechanism untraced — REQUIRED before any L1.2 config lift: find where
[classifier] enabled changes the scored pool (suspect: per-class depth /
expansion coupling). A surgical per-class cap raise (constrained/multi_hop
only) may harvest the +6 without the ranking damage.

New-bed baseline per-type (delivered): basic .602, semantic .210 (r@12 .323 —
the crater, 42/62 never rank gold), intra_doc .850, project_related .950
(retrieval solved; public 25% correctness is downstream), constrained .333
(cap-bound, provably rescuable). Needles re-resolved from questions.jsonl +
uuid_index (100% of 722 gold files present in bed; 470/500 resolvable;
scripts/resolve_erb_needles.py; sets `*_v09x_*.json`).

---

## 2026-08-27 — Wave 1 launched: ranking-under-width, config-only

Design, code facts (subtractive-shortlist proof, #255 coupling mechanism,
#260/#341 dormant instruments), six arms with registered predictions, exact
commands and decision rules: **`2026-08-27-ranking-under-width-wave1.md`**
(user-approved 2026-08-27). Arms: w1_map_empty / w1_eps_band_all /
w1_gate50_off (headline) / w1_gate50_on / w1_rrf_k_20 / w1_rrf_k_240.
Receipts land as `benchmarks/dogfood/erb/receipts/ladder_v09x_w1_*_2026-08-27.json`,
paired-comparable with the L1.1 five-arm set.
