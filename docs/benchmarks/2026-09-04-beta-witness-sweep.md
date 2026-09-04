# Beta witness sweep — code and RAG receipts on the 0.9.2 integration head (2026-09-04)

**Question.** `beta` (`21606a0`, the standing integration branch that `release/v0.9.2`
will be cut from) is 104 commits over shipped v0.9.1 (`2644c3e`). The 2026-09-02 check
witnessed a bit-exact reproduce on the ERB 947k bed only. Does beta reproduce every other
frozen v0.9.1 receipt — the code beds and the three non-ERB RAG corpora — and what does it
score on the code benches that have never been run on a post-wave-1 tree?

**Receipts.** Runner `benchmarks/dogfood/sweeps/run_sweep_beta.py` (sequential queue, one
bed at a time, cold-bed gate ≥ 1 h, every receipt annotated with a `sweep` block); verdict
`benchmarks/dogfood/receipts/sweep_beta_witness_2026-09-04.json` (per-needle pairing of
every ladder receipt against its frozen reference). Per-corpus receipts are listed in each
section. Logs and the run manifest: `F:/tmp/sweep_beta_2026-09-04/`.

## 0. Setup and the config identity that makes pairing legal

| item | value |
|---|---|
| code | worktree at `21606a0` (= `origin/beta`), tracked tree clean, `PYTHONPATH` pinned to the worktree so the import cannot resolve elsewhere |
| config | the worktree's shipped `cymatix.toml` (sha256 `dd4ca744…`), passed explicitly to every ladder |
| config identity | `load_config` resolves it **field-identical (214 fields, 0 diffs)** to `w24_min_delivered_12.toml` (sha `7129e83c…`, the EnronQA reference arm). Against the reference commits of the MULoc / LoCoMo / FinanceBench receipts (`309a14e`, `3f9dd86`, `ad52cc6`, all carrying the v0.9.1 toml `22b1d397…`) the only textual diff is `[ingestion] entity_autolink_hub_cutoff 0 → 200`, an ingest-time knob that is inert on a prebuilt bed |
| resolved fields | rrf_k 20, fusion rrf, eps_band on all five classes, min_delivered_docs 12, expression_tokens 7000, dense/SPLADE/PKI/cover-walk off, ribosome `none`, fts5_candidate_depth auto |
| env | `PYTHONHASHSEED` pinned (0 for the witness arms, 1 for the sensitivity arms — every frozen reference left it unpinned), `PYTHONUTF8=1`, `CYMATIX_DISABLE_LEARN=1`, `CYMATIX_SQLITE_CACHE_SIZE=-12582912`, encoder URL and `CYMATIX_CONFIG` unset |
| interpreter | CPython 3.14.3, SQLite 3.50.4, Windows 11 |
| beds | MULoc 108,375 genes (tagger_v2, parallel c=6), dogfood 6.4k, RepoBench-R fresh per-run genomes, LoCoMo 6,276 (tagger_v2, sequential), FinanceBench 73,759 (tagger_v2, parallel), EnronQA v2 84,677 (tagger_v1, c=1), EnronQA padded 596,707 (tagger_v1, c=6). Bed identities are those of the reference rows; nothing was rebuilt |
| pairing | per-needle by needle name **within the same needle file** — legal here because new and reference runs read the identical committed `needles_resolved_*.json`; the cross-bank name join that the 2026-09-02 identity hazard forbids does not arise |

Latency is wall milliseconds on a shared workstation. The queue is sequential and each bed
was cold (the MULoc gap was 46.5 h), so the numbers are the cleanest this box produces, but
they are still not a clean-room measurement. Rank axes are the citable part.

## 1. Code lane

### 1.1 MULocBench — beta reproduces the reference wherever the hash seed cannot bite

Receipt `benchmarks/dogfood/muloc/receipts/ladder_muloc_beta_seed0_2026-09-04.json`
(36 min, n = 680 issues, file-level gold, k = 12) against the frozen cold-gated reference
`ladder_muloc_baseline_cold_2026-08-31.json` (`309a14e`):

| axis | beta seed 0 | reference (2026-08-31 cold) | delta |
|---|---:|---:|---:|
| delivered gold rate | **0.4426** (301) | 0.4485 (305) | −0.0059 (paired +2 / −6) |
| r@12 (score map) | 0.4838 | 0.4824 | +0.0015 |
| fr@12 (final order) | 0.4882 | 0.4868 | +0.0015 |
| median rank of first gold | 5.0 | 4.0 | — |
| exact-commit strict subset (n = 70) delivered / r@12 | 0.514 / 0.571 | 0.529 / 0.571 | −1 needle |
| p50 / p95 wall ms | 2,734 / 6,989 | 3,757 / 9,376 | not citable |

141 of 680 needles moved on at least one rank axis. That is the same size as the movement
between the two *frozen* reference runs themselves (warm vs cold on 2026-08-31: 138
needles, +8 / −5 delivered), which the campaign doc had logged as "~71/680 unexplained
jitter". This sweep attributes it:

- **139 of the 141 moved needles carry more than 64 distinct Stage-1 query terms**
  (dedup count over `accel.extract_query_signals`; the two exceptions sit at 62 and 64, and
  the intent router appends implicit entities that this approximate count omits). The
  frozen pair shows the identical pattern: 137 of 138.
- `filename_anchor.py:164` builds the stem probe set with `list({...})` and truncates it to
  64 at `:170`, so **`PYTHONHASHSEED` decides which stems the highest-weight lane (4.0)
  queries** for every needle with more than 64 terms — 520 of MULoc's 680 issue bodies.
- **On the 147 needles with at most 60 terms, beta and the reference agree on every one of
  the twelve per-needle fields** (delivered, both recall bases, all three rank bases,
  delivered count, gold ranks, gate and floor diagnostics, splice count): zero diffs.
  All eight delivered flips (+2 / −6) are long-query needles (97 to 288 terms).

So the MULoc delta is not beta. It is the seed. The `PYTHONHASHSEED=1` arm in §3 prices the
same hazard on beta alone; the one-word fix (`sorted(...)`, the same repair #131 made at
`knowledge_store.py:2186`) moves ranking and needs its own paired receipt, not this sweep.

Two side facts the receipt carries. **Seat regime:** 227 of 680 needles deliver 6 seats and
447 deliver 12 — the FOCUSED budget-tier cut (#430) is not floored by `min_delivered_docs`
on this bed either. **Bed mutation:** beta's #424 DDL created `idx_filename_gene` on open,
so `muloc.db` grew 1,715,494,912 → 1,719,836,672 bytes (+4.34 MB) with the gene count
unchanged at 108,375 and the WAL checkpointed. Index-only; the reference beds are otherwise
untouched.

### 1.2 Dogfood four-repo bed — standalone beta receipt

Receipt `benchmarks/dogfood/code/receipts/dogfood_baseline_beta_2026-09-04.json`
(`run_baseline.py`, gene_id set membership, 18 value-pinning needles over cymatix-context /
driftwatch / scorerift / MaxExpressKit, bed `genomes/dogfood/genome.db` 271 MB):

| axis | beta |
|---|---:|
| recall@1 / @3 / @5 / @12 | 0.556 / 0.722 / 0.889 / **0.889** |
| MRR | 0.671 |
| median rank when delivered | 1.0 |
| misses | `cymatix_proxy_port`, `cymatix_fusion_default` (both cymatix-context needles) |
| median latency | 49.5 ms |

Not paired: the July receipts were k = 50 runs on pre-wave-1 defaults (rrf_k 60, two-class
combinator map) and one of them on a different tagger bed. The two misses are the
cymatix-context pattern the 2026-07-28 baseline named — the bed is 88 % cymatix-context by
gene count and ranks worst on the repo it is made of.

### 1.3 RepoBench-R — beta reproduces June, and the retriever sits at the random floor on code-context queries

Receipts `benchmarks/dogfood/code/receipts/repobench_r_python_{cff,cfr}_global_{shipped,legacy_probe}_beta_2026-09-04.json`
(`benchmarks/repobench_r_cymatix_global.py`; the 2026-06-13 per-example dumps, 200
examples per level, copied from the main checkout; fresh in-process ingest of the deduped
candidate union per arm; `shipped` = beta `cymatix.toml`, `legacy_probe` = the additive-fusion
lexical probe template the June run used). **B** ranks only the example's own candidate pool
(directly comparable to the foils); **C** is the gold snippet's rank against the whole
corpus. The in-harness BM25 foil is a floored-IDF Okapi with the same identifier tokenizer.

**python_cff** — global corpus 3,310 unique snippets (June: 3,310)

| arm (easy, n=200, avg pool 6.7) | B acc@1 | B acc@3 | C rec@1 | C rec@3 | C rec@5 | C rec@10 |
|---|---:|---:|---:|---:|---:|---:|
| beta shipped cymatix.toml | 0.140 | 0.445 | 0.040 | 0.095 | 0.135 | 0.290 |
| beta legacy lexical probe | 0.160 | 0.430 | 0.030 | 0.085 | 0.155 | 0.275 |
| June 2026 helix 0.6.3 (probe) | 0.155 | 0.415 | 0.045 | 0.080 | 0.160 | 0.280 |
| matched BM25 foil (in-harness) | 0.140 | 0.490 | 0.100 | 0.305 | 0.415 | 0.525 |
| random floor (per-example pool) | 0.145 | 0.495 | — | — | — | — |

| arm (hard, n=200, avg pool 17.5) | B acc@1 | B acc@3 | B acc@5 | C rec@1 | C rec@3 | C rec@5 | C rec@10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| beta shipped cymatix.toml | 0.060 | 0.195 | 0.330 | 0.015 | 0.070 | 0.115 | 0.195 |
| beta legacy lexical probe | 0.065 | 0.180 | 0.345 | 0.030 | 0.080 | 0.115 | 0.205 |
| June 2026 helix 0.6.3 (probe) | 0.055 | 0.195 | 0.355 | 0.020 | 0.080 | 0.120 | 0.205 |
| matched BM25 foil (in-harness) | 0.100 | 0.255 | 0.390 | 0.075 | 0.190 | 0.280 | 0.400 |
| random floor (per-example pool) | 0.050 | 0.220 | 0.345 | — | — | — | — |

**python_cfr** — global corpus 3,052 unique snippets (June: 3,052)

| arm (easy, n=200, avg pool 6.8) | B acc@1 | B acc@3 | C rec@1 | C rec@3 | C rec@5 | C rec@10 |
|---|---:|---:|---:|---:|---:|---:|
| beta shipped cymatix.toml | 0.190 | 0.490 | 0.060 | 0.100 | 0.165 | 0.315 |
| beta legacy lexical probe | 0.200 | 0.525 | 0.065 | 0.120 | 0.195 | 0.285 |
| June 2026 helix 0.6.3 (probe) | 0.190 | 0.520 | 0.065 | 0.150 | 0.205 | 0.320 |
| matched BM25 foil (in-harness) | 0.200 | 0.560 | 0.140 | 0.315 | 0.430 | 0.530 |
| random floor (per-example pool) | 0.145 | 0.470 | — | — | — | — |

| arm (hard, n=200, avg pool 17.7) | B acc@1 | B acc@3 | B acc@5 | C rec@1 | C rec@3 | C rec@5 | C rec@10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| beta shipped cymatix.toml | 0.080 | 0.260 | 0.405 | 0.035 | 0.100 | 0.125 | 0.215 |
| beta legacy lexical probe | 0.080 | 0.270 | 0.425 | 0.040 | 0.120 | 0.165 | 0.230 |
| June 2026 helix 0.6.3 (probe) | 0.080 | 0.270 | 0.415 | 0.045 | 0.120 | 0.165 | 0.250 |
| matched BM25 foil (in-harness) | 0.135 | 0.320 | 0.465 | 0.105 | 0.240 | 0.315 | 0.435 |
| random floor (per-example pool) | 0.080 | 0.250 | 0.400 | — | — | — | — |

Three readings:

- **Beta reproduces June.** The legacy-probe arm on beta lands within one to three
  examples per cell of the June helix 0.6.3 run on every B and C cell; the shipped
  config (RRF, k = 20, eps-band, tagger v2 at ingest) is a wash against it — no cell moves
  by more than 0.035 in either direction. The in-harness BM25 foil reproduces June to the
  example, except one example in one hard cell per config (its score accumulation
  iterates a Python set, so it is itself seed-sensitive at float ties; June was unpinned).
- **On code-context queries the retriever is at the random floor on closed pools and at
  roughly half of BM25 at global rank.** Pool-rank acc@3 / acc@5 sits within ±0.03 of
  random in all four cells; global-rank rec@10 is 0.195–0.315 against BM25's 0.400–0.530.
  The query here is imports plus the last 30 lines of code (median 130–150 distinct
  Stage-1 terms), not natural language, and the arm reads the fused score map through
  `_retrieve` / `_apply_candidate_refiners` with harmonic, SR and cymatics off. This is
  the same picture June drew; nothing in the v0.9.x ranking work was aimed at it, and it
  is the honest number for "paste a file, find its dependency".
- **One query crashes the store.** RepoBench example 123 of cff-easy (`arienchen/pytibrv`,
  5,236 chars, **622 distinct terms**) raises `sqlite3.OperationalError: too many terms in
  compound SELECT`: `_tag_prefix_sql` emits one `UNION ALL` branch per query term and
  SQLite's `SQLITE_LIMIT_COMPOUND_SELECT` is 500. Reproduced through the production
  `build_context(read_only=True)` path — nothing catches it. The rewrite that introduced
  the compound form landed 2026-08-03 (`6c2aac6`), so v0.9.1 carries it too; June's tree
  predates it, which is why June had no error. The harness now records such a query as a
  miss instead of aborting (`cymatix_query_errors` in the receipt; 1/200 in cff-easy on
  both beta arms, 0 elsewhere). Short-query beds never reach it (MULoc max 288 terms). Fix
  is a ranking-inert chunking of the UNION ALL, tracked separately.

### 1.4 RepoBench-R Java — first non-Python retrieval receipt (second wave)

The owner approved additional code benches mid-sweep; the cheapest non-Python receipt is
RepoBench-R's `java_cff` / `java_cfr` configs, because every candidate snippet is one gene
and the chunker (whose tree-sitter path is wired for 5 of 10 detected languages, Java not
among them) is out of the picture — only retrieval is priced. Dumps and foils were produced
fresh (`benchmarks/repobench_r.py --config java_cff|java_cfr --n 200`; the raw `.gz` pulled
to `F:/Projects/bench_pulls/repobench-r/`), then the same two arms as §1.3. Receipts
`benchmarks/dogfood/code/receipts/repobench_r_java_{cff,cfr}_global_{shipped,legacy_probe}_beta_2026-09-04.json`.
No June reference exists for Java; no query errors on either config (Java contexts stay
under the 500-term compound limit).

**java_cff** — global corpus 3,832 unique snippets

| arm (easy, n=200, avg pool 6.5) | B acc@1 | B acc@3 | C rec@1 | C rec@3 | C rec@5 | C rec@10 |
|---|---:|---:|---:|---:|---:|---:|
| beta shipped cymatix.toml | 0.195 | 0.475 | 0.055 | 0.110 | 0.125 | 0.215 |
| beta legacy lexical probe | 0.200 | 0.470 | 0.095 | 0.175 | 0.230 | 0.320 |
| matched BM25 foil (in-harness) | 0.160 | 0.455 | 0.105 | 0.250 | 0.290 | 0.380 |
| random floor (per-example pool) | 0.165 | 0.525 | — | — | — | — |

| arm (hard, n=200, avg pool 18.0) | B acc@1 | B acc@3 | B acc@5 | C rec@1 | C rec@3 | C rec@5 | C rec@10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| beta shipped cymatix.toml | 0.075 | 0.185 | 0.340 | 0.030 | 0.060 | 0.085 | 0.175 |
| beta legacy lexical probe | 0.075 | 0.175 | 0.330 | 0.045 | 0.105 | 0.175 | 0.330 |
| matched BM25 foil (in-harness) | 0.065 | 0.190 | 0.355 | 0.045 | 0.130 | 0.225 | 0.355 |
| random floor (per-example pool) | 0.055 | 0.205 | 0.315 | — | — | — | — |

**java_cfr** — global corpus 3,808 unique snippets

| arm (easy, n=200, avg pool 6.6) | B acc@1 | B acc@3 | C rec@1 | C rec@3 | C rec@5 | C rec@10 |
|---|---:|---:|---:|---:|---:|---:|
| beta shipped cymatix.toml | 0.155 | 0.445 | 0.050 | 0.125 | 0.150 | 0.245 |
| beta legacy lexical probe | 0.185 | 0.465 | 0.065 | 0.215 | 0.270 | 0.375 |
| matched BM25 foil (in-harness) | 0.165 | 0.520 | 0.085 | 0.275 | 0.345 | 0.450 |
| random floor (per-example pool) | 0.175 | 0.435 | — | — | — | — |

| arm (hard, n=200, avg pool 20.1) | B acc@1 | B acc@3 | B acc@5 | C rec@1 | C rec@3 | C rec@5 | C rec@10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| beta shipped cymatix.toml | 0.110 | 0.235 | 0.355 | 0.060 | 0.090 | 0.160 | 0.260 |
| beta legacy lexical probe | 0.090 | 0.235 | 0.365 | 0.045 | 0.140 | 0.225 | 0.330 |
| matched BM25 foil (in-harness) | 0.135 | 0.280 | 0.405 | 0.105 | 0.205 | 0.275 | 0.385 |
| random floor (per-example pool) | 0.035 | 0.140 | 0.260 | — | — | — | — |

Java reads like Python on the closed pools (both arms within noise of random and of BM25),
with one difference Python did not show: **at global rank the legacy additive-fusion probe
beats the shipped RRF config by 0.07–0.16 at rec@10 in all four cells**, and BM25 beats
both. The shipped config's extra lanes (tag tiers, entity graph, cymatics) and RRF's
rank-plateau fusion cost recall here rather than adding it — the same direction §4's
tag-lane diagnostic measures directly on the new beds. Not a retrieval-language effect in
the tokenizer sense (the 2026-09-02 casing probe stands); it is the NL-tuned lanes meeting
code-context queries.

## 2. RAG lane

### 2.1 LoCoMo + LoCoMo-Plus — bit-exact reproduce

Receipt `benchmarks/dogfood/locomo/receipts/ladder_locomo_beta_seed0_2026-09-04.json`
(42 s, n = 2,378, k = 12, bed cold 3.5 days) against `ladder_locomo_baseline_2026-08-31.json`
(`3f9dd86`): delivered **0.4348** (1,034), r@12 0.4857, fr@12 0.4865, median rank 6.0 — all
identical — and **0 of 2,378 needles differ on any of the twelve per-needle fields**. Every
class reproduces to the needle: singlehop .581, temporal .544, adversarial .496, multihop
.427, commonsense .303, cognitive .007 (3/401). Seat regime 746 × 6 / 1,627 × 12. p50 15.8 ms
(reference 22.7 ms; not citable).

### 2.2 FinanceBench — bit-exact reproduce

Receipt `benchmarks/dogfood/financebench/receipts/ladder_financebench_beta_seed0_2026-09-04.json`
(92 s, n = 150, page-level gold, bed cold 3.6 days) against
`ladder_financebench_baseline_2026-08-31.json` (`ad52cc6`): delivered **0.1533** (23), r@12 =
fr@12 = 0.1533, median rank 6.0, all identical; **0 of 150 needles differ on any field**. By
type: novel-generated 17/50, domain-relevant 6/50, metrics-generated **0/50** — the
doc-routing blackout the baseline note described is unchanged on beta. Seat regime 20 × 6 /
130 × 12. p50 575 ms (reference 829 ms; not citable).

### 2.3 EnronQA v2 (85k) — bit-exact reproduce, paraphrase gap unchanged

Receipt `benchmarks/dogfood/enronqa/receipts/ladder_enronqa_v2_beta_seed0_2026-09-04.json`
(4.0 min, n = 500 = 250 original/rephrased pairs, bed cold 3.5 days) against the floor-arm
reference `ladder_enronqa_v2_v091_floor_2026-08-30.json` (integration `e6dba22`, config
`w24_min_delivered_12.toml`, which resolves field-identical to beta's shipped toml):
delivered **0.856** (428), r@12 0.876, fr@12 0.872, median rank 3.0, all identical; **0 of 500
needles differ on any field**. Paired halves reproduce too: original 0.880 / 0.904 (delivered /
r@12), rephrased 0.832 / 0.848 — the −4.8pp paraphrase gap the 2026-08-30 crater doc
decomposed is exactly where it was. Seat regime 154 × 6 / 346 × 12. p50 334 ms (reference
470 ms; not citable).

### 2.4 EnronQA padded (597k) — bit-exact reproduce at scale

Receipt `benchmarks/dogfood/enronqa_padded/receipts/ladder_enronqa_padded_beta_seed0_2026-09-04.json`
(38 min, n = 500, bed 596,707 genes cold 3.6 days) against
`ladder_enronqa_padded_v091_floor_2026-08-30.json` (same integration commit and config family
as §2.3): delivered **0.792** (396), r@12 0.808, fr@12 0.816, median rank 3.0, all identical;
**0 of 500 needles differ on any field**. Halves: original 0.832 / 0.844, rephrased 0.752 /
0.772 — the −8.0pp paraphrase gap at padding scale is unchanged. Seat regime 115 × 6 / 384 × 12
(one needle delivers 0). p50 2,680 ms (reference 2,275 ms; not citable — the p95 of 15.7 s says
this bed is I/O-bound on this box and the queue's other stages were writing to the same disk).

### 2.5 RAG lane summary

| bed | genes | n | beta delivered | reference | per-needle diffs (12 fields) | verdict |
|---|---:|---:|---:|---:|---:|---|
| LoCoMo + Plus | 6,276 | 2,378 | 0.4348 | 0.4348 | 0 | exact |
| FinanceBench | 73,759 | 150 | 0.1533 | 0.1533 | 0 | exact |
| EnronQA v2 | 84,677 | 500 | 0.856 | 0.856 | 0 | exact |
| EnronQA padded | 596,707 | 500 | 0.792 | 0.792 | 0 | exact |
| ERB 947k (2026-09-02 check) | 947,531 | 470 | 0.6681 | 0.6681 | 0 | exact |

Five corpora, 3,998 paired needles, zero differences on every rank, delivery and diagnostic
field. Beta's 104 commits over v0.9.1 leave the query path byte-for-byte where v0.9.1 left it;
the only bed-side effect of beta is the #424 index DDL, which every bed acquired on open
(LoCoMo +0.67 MB, FinanceBench +1.9 MB, EnronQA v2 +2.1 MB, padded +14.5 MB, MULoc +4.3 MB),
gene counts unchanged.

## 3. Hash-order sensitivity arms (PYTHONHASHSEED = 1 vs 0, beta only)

Same tree, same config, same bed, same needle order; only the interpreter's string hash
seed differs. Receipts `ladder_{muloc,locomo,financebench}_beta_seed1_2026-09-04.json`
beside their seed-0 twins.

| bed | queries > 64 distinct terms | seed 1 vs seed 0: needles differing on any field | of which > 64 terms | ≤ 60-term subset diffs | delivered seed 0 → seed 1 |
|---|---:|---:|---:|---:|---|
| MULoc (n = 680) | 520 | **165** | 163 | 0 / 147 | 0.4426 → 0.4485 (+7 / −3) |
| LoCoMo (n = 2,378) | 0 | 0 | — | — | 0.4348 → 0.4348 |
| FinanceBench (n = 150) | 0 | 0 | — | — | 0.1533 → 0.1533 |

This is the direct measurement the §1.1 attribution predicted. On MULoc the seed alone is worth
about ±4 delivered needles (0.6pp) and r@12 0.4838 → 0.4897; across the frozen reference and
both beta seeds, **204 of 680 needles vary**, every one a long-query needle. On the two beds
whose queries never exceed 64 terms the seed does nothing, to the needle. Consequences:

- Every MULoc receipt published before this one (2026-08-31 warm, cold, budget-14000 arms)
  carries an unreported ±0.6pp seed band; their 71-to-138-needle "jitter" was this.
- A MULoc arm-vs-arm comparison is only paired if both arms pin the same `PYTHONHASHSEED`;
  the runner now pins it and records it in every receipt.
- The fix (`sorted()` at `filename_anchor.py:164`) changes which 64 stems are probed for
  520/680 needles and therefore needs its own paired receipt; it is tracked as a separate
  task, not applied here.

## 4. Second wave — four new code corpora (user-approved 2026-09-04)

The owner approved additional code benches mid-sweep. Four were pulled, emitted as
one-file-per-document corpora, built into beds by `scripts/build_fixture_matrix.py`
(new profiles; `--parallel --workers 4`, tagger v2, `CYMATIX_BFM_HUB_CUTOFF=200` = beta's
shipped ingest default, `CYMATIX_BFM_DENSE_BACKFILL=0` for parity with every other bench
bed), gold-resolved by the new generic `scripts/resolve_bench_needles.py`, and measured with
the same ladder, config and env pins as §1–2. Builders: `scripts/build_coderag_corpus.py`,
`build_cosqa_corpus.py`, `build_swebench_corpus.py`. **NEW CORPORA — not comparable to any
other row**; each gets its own BASELINES row.

| bed | source (license) | files emitted | genes | needles (resolved / emitted) | gold unit | notes |
|---|---|---:|---:|---:|---|---|
| `coderag_solutions` | CodeRAG-Bench programming-solutions + humaneval + mbpp (CC-BY-SA-4.0) | 1,128 | 1,127 | 663 / 664 | one canonical solution per query | 1 mbpp doc dedups into another (identical gene content) |
| `coderag_docs` | CodeRAG-Bench library-documentation + ds1000 + odex (CC-BY-SA-4.0) | 34,003 (30,931 admitted, 3,032 pages < 50 bytes gated) | 31,201 | 709 / 714 (513 ds1000, 196 odex) | library page(s), 1–17 genes | 9,128 duplicate pages collapse; query = ds1000 prompt / odex NL intent; only rows with canonical docs |
| `cosqa` | CoIR CosQA corpus + test qrels | 20,604 | **6,271** | 500 / 500 | one snippet per query | the CoIR corpus is 70 % byte-duplicate snippets (14,337 collisions); 426 gold attributed through byte-identical twins — the deduped bed is the fairer haystack |
| `swebench` | SWE-bench Verified (12 repos) | 13,495 (10,464 admitted, 3,031 outside the 50 B–200 kB gate) | 33,309 | 489 / 500 (12 exact-commit) | files the gold patch touches (429 single-file); median 11 gold genes per needle | one snapshot per repo at the median-`created_at` instance's base_commit; 7 instances whose gold is absent from the snapshot + 4 whose only gold file exceeds 200 kB (matplotlib `_axes.py`, xarray `dataset.py`) dropped; query = problem_statement[:2000] |

Metrics reported per bed: the ladder's delivered / r@12 / fr@12 / median rank as everywhere
else, plus **document-level NDCG@10, recall@10 and MRR on the first gold gene** (the primary
metrics CodeRAG-Bench and CoIR publish; single-relevant form, a lower bound where a query has
several gold pages). Latency is warm-cache-free by construction: the runner's cold gate makes
each ladder wait ≥ 1 h after its own build.

### 4.1 Results — beta shipped config vs a haystack-matched BM25 foil

Ladder receipts `benchmarks/dogfood/<bed>/receipts/ladder_<bed>_beta_seed0_2026-09-04.json`;
foils `bm25_doc_foil_<bed>_2026-09-04.json` beside them (`benchmarks/dogfood/code/bm25_doc_foil.py`:
the CodeRAG harness's floored-IDF Okapi on an inverted index, over the same admitted and
deduplicated files the bed ingested, document level, first gold). Zero query errors on any bed.

| bed | n | delivered | r@12 | median rank | NDCG@10 | recall@1 | recall@10 | BM25 foil NDCG@10 | BM25 recall@12 | p50 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CodeRAG solutions (humaneval 164 / mbpp 499) | 663 | 0.971 | 0.982 | 2 | **0.624** (0.627 / 0.623) | 0.30 | 0.970 | **0.986** (0.998 / 0.982) | 1.000 | 20 |
| CoIR CosQA | 500 | 0.286 | 0.362 | 11 | **0.132** | 0.05 | 0.316 | **0.249** | 0.410 | 32 |
| CodeRAG docs (ds1000 513 / odex 196) | 709 | 0.212 | 0.227 | 10 | **0.098** (0.104 / 0.085) | 0.02 | 0.207 | **0.154** (0.168 / 0.118) | 0.265 | 684 |
| SWE-bench Verified file localization | 489 | 0.599 | 0.638 | 4 | **0.389** | 0.16 | 0.603 | **0.341** | 0.558 | 1,617 |

Three of four new beds sit **below** a plain BM25 over the same haystack, and the fourth
(SWE-bench, the only one whose queries are natural-language issue text) sits **above** it.
SWE-bench by difficulty: `<15 min fix` 0.661 delivered (n = 189), `15 min – 1 hour` 0.588
(255), `1–4 hours` 0.405 (42); exact-commit strict subset n = 12: 0.583. The seat regime is the
familiar one everywhere (CodeRAG solutions 172 × 6 / 487 × 12, CosQA 143 / 357, docs 175 / 534,
SWE-bench 118 / 371).

### 4.2 Diagnostic: the Tier-1/2 tag lanes under RRF

The CodeRAG-solutions gap is a top-1 gap, not a recall gap (recall@10 0.970 vs 1.000), which
pointed at rank plateaus in fusion. Two config-only arms, `benchmarks/dogfood/code/configs/tag_lanes_muted.toml`
(shipped toml with `tag_exact_weight = 0.0`, `tag_prefix_weight = 0.0`) and
`tag_and_anchor_muted.toml` (+ `filename_anchor_enabled = false`), receipts
`ladder_<bed>_beta_diag_tag_lanes_muted_2026-09-04.json` beside each baseline, same seed and
needle order, paired per needle:

| bed | delivered: shipped → muted (paired) | NDCG@10: shipped → muted | BM25 foil NDCG@10 | needles moved |
|---|---|---|---:|---:|
| CodeRAG solutions | 0.971 → **0.9985** (+18 / −0) | 0.624 → **0.872** | 0.986 | 428 |
| CoIR CosQA | 0.286 → **0.468** (+95 / −4) | 0.132 → **0.252** | 0.249 | 305 |
| CodeRAG docs | 0.212 → **0.236** (+25 / −8) | 0.098 → **0.121** | 0.154 | 253 |
| SWE-bench Verified | 0.599 → **0.656** (+41 / −13) | 0.389 → **0.441** | 0.341 | 385 |
| ERB 100k carve (prose, n = 141, counter-check) | 0.667 → **0.702** (+7 / −2) | 0.513 → **0.555** | — | 82 |
| MULoc 108k (long issue queries, counter-check) | 0.443 → **0.487** (+41 / −11) | 0.292 → **0.309** | — | 390 |

Muting the filename anchor on top of the tag lanes changed nothing on the solutions bed
(identical receipt). On CosQA the muted arm lands exactly on the BM25 foil. Mechanism, as
far as this sweep measures it: on a corpus of many small documents the tagger assigns the
same handful of tags to hundreds of files, so the tag tiers rank huge tied plateaus; under
RRF (rrf_k = 20) a document that is rank 1 in the FTS tier but unranked in the tag tiers
scores 1/21, while a document at FTS rank 5 that also sits at the top of both tag plateaus
scores 1/25 + 2/21 and wins. Neither counter-check rescues the lanes: the ERB 100k carve, a
prose bed where they were originally validated (under additive fusion, before the
2026-07-06 RRF default), is +7 / −2 muted, and MULoc, with 108k genes and 2,000-character
issue queries, is +41 / −11. Six beds, six paired wins, from +18 / −0 to +95 / −4; the
smallest effect is on the prose-like documentation bed. **No default moves from this**: the
947k ERB bed, EnronQA and the answer-quality lane are unpriced, and the right fix may be a
code-class gate or a plateau guard rather than a global weight — tracked as a separate task.

## 5. Caveats

- **Latency is not citable.** Shared box; sequential queue; cold beds. Cite rank axes.
- **The hash-seed attribution is a count over an approximate term bag.** The live bag is
  the router's expanded query, which can add implicit entities; the two "≤ 64" exceptions
  are consistent with that. The seed-1 arm is the direct measurement.
- **Harmonic Tier 5 is dark on every bed here.** The #432 skip warning fired on MULoc
  (52,948 candidates → 105,896 binds > 32,766) and on the 100k ERB carve during the smoke
  run. Every number in this doc is a Tier-5-dark measurement, as the 947k one was.
- **RepoBench-R and dogfood are not paired with anything.** RepoBench's June reference was
  a helix 0.6.3 tree with a different chunker, tagger and fusion; dogfood's July receipts
  ran a different k and defaults. Both are first beta receipts, not deltas.
- **The second-wave beds are first receipts with known haystack edits.** CosQA's bed holds
  6,271 unique snippets of the corpus's 20,604 ids (the rest are byte-duplicates), so a
  published CosQA number over the raw corpus is not comparable; the library-documentation bed
  drops 3,032 pages under 50 bytes and collapses 9,128 duplicate pages; SWE-bench uses one
  snapshot per repo, so 477 of 489 needles localize against a near-neighbour tree, not the
  instance's own commit, and four instances whose only gold file exceeds 200 kB are absent.
  The BM25 foils were rebuilt over the same admitted, deduplicated file sets (and the same
  extension allow-list) so they are haystack-matched to the beds, not to the papers.
- **The tag-lane arms are diagnostics, not a default proposal.** They zero two RRF tiers by
  config and were run warm, on the seed-0 needle order, with ranks read from the same score
  map as the baselines; the counter-check beds (ERB 100k carve, MULoc) are in §4. The 947k
  ERB bed, EnronQA and the answer-quality lane have not been priced, and any change is
  tracked as a separate task.
- **Wall times of the wave-2 and Java stages are load-inflated.** They ran concurrently
  with the seed arms and each other by design (rank axes are deterministic); no latency
  from those stages is cited.
