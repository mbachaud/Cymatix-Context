# EnterpriseRAG-Bench 829K blob run — external reproduction guide

**Date:** 2026-07-10 · **Issue:** #93 · **Corpus:** `erb_blob.db` = 829,131 genes / 499,997 ERB source docs · **Run commit:** `290cc35` (a worktree off master) · **Doc branch:** `docs/erb-blob-repro`

This document explains, for an external reader, exactly what helix-context ran against Onyx's public
[EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench) (ERB), what it measured, the
hardware it ran on, and how to reproduce it. It is written to be honest about scope: the numbers below
are a **retrieval-pool** measurement graded by a **non-official** judge protocol, on **one consumer
desktop**. Read the "Caveats" and "Reproduction fidelity" sections before quoting anything.

---

## 1. What EnterpriseRAG-Bench is

ERB (Onyx, MIT-licensed code, dataset on HuggingFace `onyx-dot-app/EnterpriseRAG-Bench`, arXiv
[2605.05253](https://arxiv.org/abs/2605.05253)) is a RAG benchmark built on a **synthetic company's
internal documents**: "slightly over 500,000 documents and 500 questions" across nine source types
(Slack, Gmail, Linear, Google Drive, Hubspot, Fireflies, GitHub, Jira, Confluence) — verified from
`F:\Projects\EnterpriseRAG-Bench-main\README.md`.

The 500 questions are split into ten types (README table):

| Type | Count | Character |
|---|---|---|
| Basic | 175 | single ground-truth doc |
| Semantic | 125 | roundabout / low keyword overlap ("challenging, loose match") |
| Intra-Document Reasoning | 40 | combine distant sections of one long doc |
| Project Related | 40 | aggregate across a project's docs |
| Constrained | 30 | qualifiers disqualify all-but-one superficially-relevant doc |
| Conflicting Info | 20 | documents contradict; must reconcile |
| Completeness | 20 | must fetch **all** relevant docs (≤10) |
| Miscellaneous | 20 | informal / off-topic / oddly-filed docs |
| High Level | 10 | no single gold doc (synthesize from org context) |
| Info Not Found | 20 | unanswerable; correct action is to abstain |

**How ERB's own baselines / grading work (framing — verified, not overclaimed).**
The README does **not** claim that leaderboard contenders all use LLM-directed retrieval, so this doc does
not either. What the repo actually documents:

- `methodology.md` §Evaluation ships **three reference retrievers**: a **BM25** keyword retriever
  (`bm25_retrieval.py`), a **vector-search** retriever (`vector_retrieval.py`), and an **LLM-agent**
  retriever (`agent_retrieval.py`, "an LLM equipped with bash commands to traverse the documents").
  So ERB's own toolbox spans both **LLM-free** retrieval (BM25, Vector) and **LLM-directed** retrieval
  (the agent). helix sits in the LLM-free-retrieval camp.
- The **official grading protocol** is `answer_evaluation/`'s `metrics_based_eval` (single-system) and
  `comparative_eval` (head-to-head). It scores four metrics — **Correctness** (holistic LLM judgment),
  **Completeness** (fraction of `answer_facts` supported), **Document Recall**, **Invalid Extra
  Documents** — and runs a **three-judge consensus** document-correction flow that can update the gold set
  (`methodology.md` §Correction Utilities, §Metrics Based Evaluation). Submissions require an
  `answers.jsonl` of `{question_id, answer, document_ids}`.
- The leaderboard lives on HF Spaces; **Onyx excludes itself** from it ("to avoid conflict of interest").

**Published reference baselines used for context in this doc: BM25 68.8 / Vector 51.4 / Onyx+GPT-4 72.4.**
These are cited from helix's `docs/specs/2026-07-01-goal-gates-hallucination-visibility.md` ("Accuracy
source of truth"), which labels them published ERB-500 correctness baselines. **Onyx+GPT-4 is a
paper/reference figure, not a leaderboard entry** (Onyx excludes itself). We did not re-derive these three
numbers from the arXiv paper; see the TODO in §11.

---

## 2. What helix's run measured, and the distinctive claim

**The distinctive claim: helix's retrieval stage makes zero LLM calls.** Candidate selection and ranking
are done by a rule-based query classifier, heuristic keyword extraction, and algorithmic tiers (FTS5
lexical, tag/synonym lookup, co-activation graph, 256-bin cymatics spectrum), plus **local encoder models
only** — BGE-M3 dense query/passage encoding and SPLADE sparse expansion. **No API call, no frontier
model, is made during retrieval.** The store is a persistent SQLite file.

In this run the LLM (Claude Sonnet) appears **only as the answerer and the judge** — the same stage where
every RAG system spends model budget, and far less than an agentic/Onyx-style entry spends inside its
retrieval loop.

**Honest scope of what was measured.** This run fed the answer model helix's **full retrieved pool**
(~16 evidence items, ~56K characters) under a **64K-character context cap** — not helix's compressed
production expression. Helix's production **expression budget is ~7,000 tokens** (`helix.toml` →
`[budget] expression_tokens = 7000`). So the run measures **the quality of the retrieval pool**, not what
helix would actually inject in production. A 64K cap is defensible in this comparison because baseline RAG
systems also hand their LLM the retrieved surface — but under helix's real ~7K budget, gold that ranks
mid-pool would be truncated, which is a genuine **ranking** weakness on the hard arm (see §9, §10).

---

## 3. Headline results (500 real ERB questions)

Source of every number below: `benchmarks/results/erb_blob93_verdict.md` and its scored summary
`erb500k_blob_additive_scored_summary_2026-07-09_2001.json` (both re-read for this doc; figures
cross-checked and consistent).

Run config: **BLOB mode, additive fusion, dense on, SPLADE on, ribosome (compressor) off**, judge = Sonnet
trinary with ~10% audit, 0 judge-error rows.

| Metric | Value | Fraction |
|---|---|---|
| gold_delivered | **55%** | 276 / 500 |
| coverage (answered / total) | **71.4%** | 357 / 500 |
| correctness among answered | **66%** | 236 / 357 |
| hallucination among answered | 34% | 121 / 357 |
| **end-to-end graded correct** | **47.2%** | 236 / 500 |
| abstain / total | 28.6% | 143 / 500 |
| hallucination / total | 24.2% | 121 / 500 |
| **correct when gold IS delivered** | **79%** | 218 / 276 |
| know_emitted | **0** (all-miss; see below) | 0 / 500 |
| audit agreement (Opus/Sonnet vs judge) | 0.96 | 48 / 50 |

**Read:** when retrieval delivers the gold doc, the answer model is correct **79%** of the time — the
ceiling here is **retrieval**, not answering. When gold is not delivered, ~37% of those questions
hallucinate from a plausible neighbor doc (the rest abstain); that is the source of most of the 24.2%
total hallucination.

### Per-question-type (the semantic-arm story)

| Type | n | gold% | correct% | halluc% |
|---|---|---|---|---|
| project_related | 40 | **100%** | 70% | 30% |
| conflicting_info | 20 | 90% | **85%** | 10% |
| miscellaneous | 20 | 90% | 85% | 10% |
| completeness | 20 | 85% | 75% | 25% |
| constrained | 30 | 80% | 53% | 33% |
| intra_document_reasoning | 40 | 78% | 62% | 15% |
| basic | 175 | 59% | 47% | 25% |
| **semantic** | **125** | **20%** | **20%** | 30% |
| high_level | 10 | 0%\* | 50% | 30% |
| info_not_found | 20 | 0%\* | 25% | 0% |

\* `high_level` and `info_not_found` have **no gold documents by design** (README: correct action for
`info_not_found` is to abstain). The **semantic arm (125 questions, 20% gold) is the single biggest drag**;
structured/entity types retrieve gold at 78–100%.

### The `know=0` calibration finding

Helix's know/miss agent contract emitted **`know` on 0 / 500 questions** — the abstain gate flagged every
question `miss{reason: sparse}`. This is **regardless of fusion mode** (a 40-question RRF probe also emitted
know 0%). It makes `know_vs_judged_agreement` (0.528, ≈ chance) degenerate. This is a **calibration
finding**, not a run error; it does not affect gold_delivered or correctness. Root cause: the absolute
abstain floors are additive-era calibrated and the ERB corpus at 829K trips "sparse" on everything. Tracked
under #239 (know calibration).

---

## 4. Comparison to published baselines — with the protocol disclaimer

| System | ERB-500 correctness | Grading |
|---|---|---|
| Onyx + GPT-4 (reference) | 72.4 | ERB official-style |
| BM25 (reference) | 68.8 | ERB official-style |
| **helix blob 829K (this run)** | **47.2** | **trinary Sonnet judge — NOT ERB's official protocol** |
| Vector (reference) | 51.4 | ERB official-style |

> ⚠️ **This run does NOT use ERB's official grading protocol.** ERB's `metrics_based_eval` grades
> Correctness/Completeness/Document-Recall/Invalid-Extra-Docs with a **three-judge consensus** and a
> gold-set **correction flow** that can revise the ground truth. This run used a **single trinary Sonnet
> judge** (`CORRECT` / `INCORRECT` / `ABSTAINED`, reference-guided) with a ~10% second-opinion audit, no
> three-judge consensus, no completeness/doc-recall metric, and no gold-set correction. **The 47.2% is
> therefore not directly comparable to the published baselines** — treat the baselines as reference context
> for magnitude only. helix's 47.2% sits nearest the Vector baseline; the verdict attributes the gap to the
> retrieval ceiling at 829K, not to answering.

---

## 5. Hardware & runtime (one consumer desktop)

Live-verified on the run box (internally "gandalf") on 2026-07-10, cross-checked against
`docs/investigations/2026-06-02-hardware-optimization-investigation.md` §2:

| Component | Spec |
|---|---|
| OS | Windows 11 Pro, build 10.0.26200 |
| CPU | AMD Ryzen 7 5800X — 8 cores / 16 threads (Zen 3, AVX2, no AVX-512) |
| RAM | 48 GB DDR4-3200 (reported 47.9 GB; physically 2×16 + 2×8, mismatched) |
| GPU | NVIDIA GeForce RTX 3080 Ti, 12 GB GDDR6X (12,288 MiB) |
| Disk | Samsung 980 Pro 1 TB NVMe, carrying **both** C: and F: (corpus + blob live on F:) |

**Runtime for the scored run:**
- **~106 s / question** end-to-end wall (dominated by retrieval: a single-threaded, CPU-bound brute-force
  dense scan over all 829K vectors — the hardware investigation measured ~95–100 s/query at this scale;
  there is **no ANN/HNSW index**). The Sonnet answer + Sonnet judge (+ occasional Opus audit) add the rest.
- **~20–24 h wall** for the full 500-question scored run.
- **Bed size: ~47 GB** single SQLite file (`erb_blob.db`, measured 47,069 MB).

Notably, **retrieval ran on CPU** (`device=cpu`; the BGE-M3 dense codec is CPU-pinned per the hardware
investigation), so the 3080 Ti sat **idle** during the run. The "one consumer desktop, no cluster, no
vector-DB service" claim is if anything conservative — the GPU was not even the workhorse.

---

## 6. Reproduction

> Every command below was read from the actual script in this repo. Where a value the historical run used
> is **not** reconstructable from committed code, it is flagged **⚠ REPRO-FIDELITY** and collected in §11.

### Prerequisites
- Python env with `torch` **CUDA (cu121) build** so BGE-M3 dense + SPLADE encoders load; a `torch+cpu`
  interpreter silently degrades retrieval to lexical-only.
- The `claude` CLI authenticated (`claude login` or a valid `ANTHROPIC_API_KEY`) — the scored run shells
  out to `claude -p`.
- ~50 GB free disk for the blob, plus the sharded intermediate.

### Step 1 — Get ERB

Clone the repo and pull the dataset:

```bash
git clone https://github.com/onyx-dot-app/EnterpriseRAG-Bench
# Dataset from HuggingFace: onyx-dot-app/EnterpriseRAG-Bench
#   all_documents.zip  (or per-source <source_type>_slice_<n>.zip, ≤5000 docs each)
#   questions.jsonl
```

The scored harness (`scripts/bench_chain/erb500k_scored.py` → `load_needles`) consumes this **exact
layout** under `--erb-root` (default `F:\Projects\EnterpriseRAG-Bench-main`), verified present on the box:

```
<erb-root>/questions.jsonl                       # 500 questions (747 KB)
<erb-root>/generated_data/uuid_index.json        # dsid → relative source path (57.9 MB)
<erb-root>/generated_data/sources/<rel>          # the gold documents themselves
```

`load_needles` reads `q["question"]`, `q.get("gold_answer")`, `q.get("expected_doc_ids")`,
`q.get("question_type")`, `q["question_id"]`, and resolves each `expected_doc_id` (a `dsid_…` uuid) to a
gold path via `uuid_index.json`. Delivered-path→gold matching is prefix-tolerant on the substring after
`/sources/` (`make_gold_index` / `match_delivered`).

⚠ **REPRO-FIDELITY:** `generated_data/uuid_index.json` + `generated_data/sources/` is the *resolved* layout
the harness expects. The docstrings reference `benchmarks/bench_enterprise_rag.py` as the origin of this
schema, but **that file is absent from the current tree** — `erb500k_scored.py` carries its own
self-contained `load_needles`. If your ERB checkout ships only `all_documents.zip` / slice zips, you must
produce the `generated_data/{uuid_index.json, sources/}` tree yourself (TODO: the exact generator is not in
this repo).

### Step 2 — Build the corpus (sharded 500K fixture)

`scripts/build_fixture_matrix.py` has a dedicated `enterprise_rag_500k` profile (9 roots, one per source
type) reading from `F:\tmp\enterprise_rag_500k\sources\<source_type>`. Build it **sharded** (the direct
input to Step 3):

```powershell
python scripts/build_fixture_matrix.py --profile enterprise_rag_500k --mode sharded
# writes: genomes/bench/matrix-sharded/enterprise_rag_500k/main.genome.db  (+ per-shard *.genome.db)
```

Notes read from the script:
- Files are filtered to `INGEST_EXTS`, size `50 B .. 200 KB`; oversized roots auto-decompose along
  top-level subdirs (`--auto-subshard-threshold-{bytes,files}`, defaults ~5 GB / 100K files; set both to a
  very large number to disable).
- `embedding_dense_v2` is backfilled **after** each shard is built (BGE-M3, via `_backfill_dense` →
  `scripts/backfill_bgem3_v2.py`), unless `HELIX_BFM_DENSE_BACKFILL=0`. SPLADE at build time obeys
  `HELIX_BFM_SPLADE`.
- Parallelism: `--shard-workers N`, `--shard-file-workers N` (0 = auto). Resumable by default; `--rebuild`
  to nuke and start fresh.

⚠ **REPRO-FIDELITY:** the source-tree path `F:\tmp\enterprise_rag_500k\sources\...` is hard-coded in the
profile's `roots`. Point it at your extracted ERB sources (edit the profile or mirror the layout). Expected
output: ~829K genes over ~500K docs (matches the blob). Wall time for the dense backfill at this scale is
substantial (hours); the auto-subshard pass exists specifically to keep the backfill rate from collapsing
past the OS file-cache budget (issue #147).

### Step 3 — Merge shards into one blob

`scripts/shard_to_blob.py` merges the sharded tree into a single flat `genome.db`, copying
`embedding_dense_v2` **verbatim (no re-embed)** and rebuilding the FTS5 index:

```powershell
python scripts/shard_to_blob.py `
    --sharded-root genomes/bench/matrix-sharded/enterprise_rag_500k `
    --out F:\tmp\erb_blob.db
```

It de-dupes byte-identical (content-hashed) genes with `INSERT OR IGNORE`, then verifies
`blob_genes == sum(shard_genes) − dupes` and that non-null dense count is preserved. Output on the run box:
**`F:\tmp\erb_blob.db` ≈ 47 GB, 829,131 genes, dense_v2 ~97.6% populated.**

### Step 4 — Serve the blob

The scored run serves the blob in **plain (non-sharded) BLOB mode** so every retrieval tier is live
(the sharded path skips several blob-only tiers). From `s4_erb500k_scored.ps1`, the committed server launch:

```powershell
Remove-Item Env:\HELIX_USE_SHARDS -ErrorAction SilentlyContinue
$env:HELIX_GENOME_PATH = 'F:\tmp\erb_blob.db'
python -m uvicorn helix_context._asgi:app --host 127.0.0.1 --port 11437
```

Config the historical run used (verify **explicitly** on current master):
- **`fusion_mode = "additive"`** — the run forced additive to match additive-era comparison data.
  ⚠ **REPRO-FIDELITY:** current master ships `[retrieval] fusion_mode = "rrf"` (default flipped 2026-07-06);
  the committed `.ps1` does **not** set fusion, so on current master you must set
  `[retrieval] fusion_mode = "additive"` in `helix.toml` to match. Fusion is ~neutral here (a paired RRF
  probe was 75% vs additive 72% gold on the first 40 questions), so an RRF repro is defensible — just
  **state which you used**.
- **dense on, SPLADE on** (defaults), **ribosome/compressor off** — `helix.toml` ships
  `[ribosome] backend = "none"`, confirmed.
- **`HELIX_DISABLE_LEARN=1`** for read-only serving (no echo/learn writes into the bed). ⚠
  **REPRO-FIDELITY:** the committed `s4_erb500k_scored.ps1` does **not** set this (other bench scripts do,
  e.g. `s3_fts_depth_sweep.py:130`, `s2_sike_bed_sweep.ps1:91`). Set it explicitly for a clean read-only serve.
- **64K context cap** — see Step 5 and §11; this is the single most important fidelity flag.

### Step 5 — Score

`scripts/bench_chain/erb500k_scored.py`, wrapped by `s4_erb500k_scored.ps1`. Per question it: (a) POSTs
`/context/packet` and records the `know`/`miss` block verbatim, (b) answers with Claude Sonnet, (c) grades
with a Sonnet trinary judge, (d) audits ~10% with Opus. **Resumable** (append-only JSONL keyed by question
id; re-run to continue). The committed invocation:

```powershell
python scripts\bench_chain\erb500k_scored.py `
    --erb-root F:\Projects\EnterpriseRAG-Bench-main `
    --helix-url http://127.0.0.1:11437 `
    --out    benchmarks\results\erb500k_blob_scored.jsonl `
    --summary-out benchmarks\results\erb500k_blob_scored_summary_<ts>.json `
    --answer-model sonnet `
    --audit-model  opus `
    --answer-max-usd 0.20 --judge-max-usd 0.10 --audit-max-usd 0.40 `
    --audit-fraction 0.10 `
    --max-genes 8
```

**API-cost expectation (honest):** 500 questions × (1 answer + 1 judge Sonnet call) + ~50 Opus audits.
Per-call caps are `--answer-max-usd 0.20 / --judge-max-usd 0.10 / --audit-max-usd 0.40`, so the worst-case
ceiling is roughly `500×(0.20+0.10) + 50×0.40 ≈ $170`; typical spend is well under that (short answers/
verdicts). The `.ps1` also runs a **claude-auth preflight** — the first S4 attempt burned 74 minutes
producing 500 `judge_error` rows because every `claude -p` hit 401.

⚠ **REPRO-FIDELITY — the 64K cap is NOT in committed code.** `erb500k_scored.py` caps the injected context
at **`[:12000]` characters** (lines 228 and 241). The historical #93 run raised this to **64K** — that was
the fix for the *first aborted run*, where the 12K cap truncated gold and produced all-abstain (verdict
caveat #2). The 64K change lived on the `290cc35` worktree and **was not committed to master**. A repro on
current master will hit the original 12K abort condition. To reproduce the reported numbers, patch both
`[:12000]` sites to `[:64000]` (or your chosen cap). The committed `--max-genes 8` combined with the
packet's multi-list evidence (verified / stale_risk / contradictions) yields the "~16 items / ~56K chars"
the verdict describes — which only flows through under a ≥56K cap.

### Step 6 — The $0 retrieval-only path (no LLM, no API cost)

To reproduce the **retrieval** signal alone (gold_delivered + gold rank, no answerer, no judge), use the
in-process probe `benchmarks/ab_semantic_probe.py` (issue #260, merged 2026-07-10 as #272). It runs
`build_context(read_only=True, ignore_delivered=True)` per question and reports, per arm and per question
type, whether gold was delivered, its rank in the scored pool, and whether it was in the pool at all
(recall-miss vs rank-miss).

First build the sweep-queries file (resolves ERB gold uuids → gene_ids **in the target bed**):

```powershell
python scripts\bench_chain\erb_to_sweep_queries.py `
    --genome F:\tmp\erb_blob.db `
    --erb-root F:\Projects\EnterpriseRAG-Bench-main `
    --out benchmarks\results\erb_sweep_queries_blob.json
```

Then run the probe (arms: `lexical` = FTS/tag only, `dense` = BGE-M3-dominated, `fused` = full stack; all
forced onto RRF):

```powershell
python benchmarks\ab_semantic_probe.py `
    --bed-db F:\tmp\erb_blob.db `
    --questions benchmarks\results\erb_sweep_queries_blob.json `
    --types semantic --arms lexical,dense,fused `
    --json-out benchmarks\results\semantic_probe_blob.json
```

Type labels + gold-answer text are joined in from the #93 scored jsonl
(`--types-jsonl`, default `benchmarks/results/erb500k_blob_additive_scored.jsonl`) by normalized question
text. This is the LLM-free, ~$0 way to iterate on the retrieval ceiling.

---

## 7. Artifacts (committed to the gitignored results dir on the run box)

Under `benchmarks/results/` (not tracked in git; present on the box):
- `erb_blob93_verdict.md` — the #93 verdict (source of the tables here).
- `erb500k_blob_additive_scored.jsonl` — per-question scored rows (500 lines).
- `erb500k_blob_additive_scored_summary_2026-07-09_2001.json` — the summary metrics.
- `erb_blob_rrf_retrieval_probe.json` — the 40-question RRF probe (gold 75%, know 0%).
- `erb_sweep_queries_blob.json` — resolved sweep queries for the blob bed (Step 6 input).
- Sharded retrieval baselines: `sike_bedsweep_enterprise_rag_{10k,50k}_2026-07-03_1556.json`,
  `diag_blob_vs_shard_medium_20260616T185224Z.json`.

---

## 8. Caveats (read before quoting)

1. **64K cap = generous.** The answer model saw helix's full ~16-item / ~56K-char retrieved pool, **not**
   its compressed ~7K-token production expression. Correctness/coverage therefore **overstate** what helix
   would deliver in production, where mid-pool gold (e.g. rank 9 @ char 33.5K) would be truncated — a real
   ranking weakness on the hard arm.
2. **The first run aborted** on the original 12K cap (gold truncated → all-abstain); it was raised to 64K.
   That 64K change is **not on master** (see §6 Step 5 / §11).
3. **Mixed audit model.** 50 audits = 14 Opus + 36 Sonnet (switched mid-run ~row 132 for quota). For the
   Sonnet audits the auditor ≈ the judge model, so `audit_agreement = 0.96` is **partly by construction**,
   not a fully independent cross-model check.
4. **Not ERB's official protocol.** Single trinary Sonnet judge, no three-judge consensus, no gold-set
   correction, no completeness/doc-recall metric (§4). The 47.2% is not apples-to-apples with the published
   baselines.
5. **No own sharded 500-q *scored* baseline exists** (only a quarantined 401-storm failure), so end-to-end
   accuracy is compared to **published** baselines, not to helix's own sharded scored run.
6. **`know = 0` on ERB** — the abstain gate is non-functional on this corpus regardless of fusion
   (calibration finding, #239); it does not affect gold_delivered / correctness.
7. **Scale/question-set confound** on the 10k/50k→829k comparison: the 80–82% figures use 50 sike-bed
   needles; the 55% uses 500 real ERB questions — a genuine scale effect but not a clean controlled delta.

---

## 9. What the run actually resolved (#93)

Serving mode (blob vs sharded) is **not** the lever: blob is retrieval-neutral-to-slightly-better than
sharded (it rescues gold that sharding buries; no bed regressed). What moves the number at 829K is
(a) **scale** (gold 82%→55%) and (b) **question type** (semantic 20% vs structured 78–100%). Fusion mode is
~neutral. Verdict: "close blob-vs-sharded as blob ≥ sharded, ship blob; redirect effort to the semantic-arm
retrieval ceiling and the know/miss calibration."

## 10. Known ceilings / roadmap

- **Semantic-arm ranking (#260).** The 125 semantic questions are the dominant drag (20% gold). A fresh
  in-process #260 probe on a dense-backfilled bed indicates **`pool_present ≈ 1.00` with median gold rank
  ≈ 384** on the semantic arm — i.e. gold is *in the pool* but ranks far below the delivery budget. That
  reframes the semantic ceiling as a **ranking** problem (fusion/re-rank on paraphrastic queries), not a
  recall/reach problem. (Preliminary probe signal; `ab_semantic_probe.py` is the reproducible instrument.)
- **know-calibration (#239).** `know = 0` on ERB: the additive-era absolute abstain floors mis-fire at
  829K. Re-fitting the know logistic on this corpus is the open calibration work.

## 11. Reproduction-fidelity notes / TODOs

The committed chain scripts do **not**, by themselves, reproduce the reported run. Three run-specific
settings were applied on top of committed code and must be re-applied:

| # | Setting | Committed code | Historical run | Action to reproduce |
|---|---|---|---|---|
| 1 | Injected-context cap | `erb500k_scored.py` `[:12000]` (×2) | **64K** | Patch both sites to `[:64000]`. **Not committed; exact value ("64K") is from the verdict — no `64000` constant exists in the tree (TODO: confirm exact value).** |
| 2 | Fusion mode | `helix.toml` default `rrf` | **additive** | Set `[retrieval] fusion_mode = "additive"`, or run RRF and state it (≈neutral: 75 vs 72 on first-40). |
| 3 | Read-only serve | `s4` ps1 does not set it | `HELIX_DISABLE_LEARN=1` | Export `HELIX_DISABLE_LEARN=1` before serving. |

Other TODOs:
- **Baseline provenance.** BM25 68.8 / Vector 51.4 / Onyx+GPT-4 72.4 are cited from helix's goal-gates spec
  as "published." Not independently re-derived from arXiv 2605.05253 / the HF leaderboard for this doc.
- **`generated_data/` origin.** `uuid_index.json` + `sources/` is the layout `load_needles` consumes;
  `bench_enterprise_rag.py` (the referenced generator) is absent from the tree, so producing that layout
  from a raw HF release is not covered by a committed script here.
- **`--max-genes`.** Committed `.ps1` passes `--max-genes 8`; the verdict's "~16 items" is the packet's
  multi-list evidence total, not a `max_genes` of 16. If your packet returns fewer items, the ~56K-char
  figure will differ.

---

*Verified against: `benchmarks/results/erb_blob93_verdict.md`,
`erb500k_blob_additive_scored_summary_2026-07-09_2001.json`, `scripts/build_fixture_matrix.py`,
`scripts/shard_to_blob.py`, `scripts/bench_chain/{erb500k_scored.py, s4_erb500k_scored.ps1,
erb_to_sweep_queries.py}`, `benchmarks/ab_semantic_probe.py`, `helix.toml`,
`docs/specs/2026-07-01-goal-gates-hallucination-visibility.md`,
`docs/investigations/2026-06-02-hardware-optimization-investigation.md`,
`F:\Projects\EnterpriseRAG-Bench-main\{README.md, methodology.md, answer_evaluation/README.md}`.*
