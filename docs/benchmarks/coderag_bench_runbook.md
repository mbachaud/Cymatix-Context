# CodeRAG-Bench Step-2 Runbook

**Benchmark:** CodeRAG-Bench (arXiv 2406.14497, NAACL'25 Findings)
**Primary metric:** NDCG@10 — the paper's own primary; lower ranks penalised by 1/log2(rank+1)
**Supporting metrics:** Recall@{1,5,10}, Precision@{1,5,10}
**Efficiency layer:** median/p90 injected tokens from top-10 docs, median/p90 per-query latency
**Arms:** A (random floor) + B (BM25 foil) in `coderag_bench.py`; D (Helix /fingerprint) in `coderag_bench_helix.py`
**LLM-free, GPU-free** (foils + Helix lexical arm; no SPLADE, no BGE-M3, no ribosome)

---

## 0. Strategic context

CodeRAG-Bench is the **efficiency-argument vehicle** from the code-context benchmark strategy doc
(`docs/benchmarks/2026-06-04-code-context-benchmark-strategy.md`, section 2):

> *"SFR-Mistral gives better NDCG but ~5× index size and ~100× encode latency
> (3.7ms GIST-base → 316ms SFR-Mistral). [MEASURED]"*

Our job is to show Helix's cheap deterministic retriever is competitive with or beats BM25
(the canonical foil, arm B) at a fraction of the 7B-dense-embedder cost. The efficiency layer
(injected tokens + latency) makes that argument concrete.

The two datasets in scope:

| Dataset | # Queries | Gold type | Helix difficulty |
|---------|-----------|-----------|-----------------|
| HumanEval | 164 | Prompt shares function name with gold | LEXICALLY EASY (sanity floor) |
| MBPP | ~974 | NL problem statement -> code solution | MORE SEMANTIC (harder contrast) |

Corpus = `code-rag-bench/programming-solutions` (~1138 docs after dedup). One gold per query.

---

## 1. License and data access

**CC-BY-SA-4.0 (ShareAlike copyleft).**

- Commercial use is permitted.
- Any **redistributed processed data** (e.g. a published results file derived from the dataset)
  must carry the same CC-BY-SA-4.0 license.
- Per-source upstream licenses (StackOverflow CC-BY-SA, etc.) vary — confirm before
  any public dataset redistribution.
- Internal measurement and benchmark reporting: OK.
- Dataset access: HuggingFace `code-rag-bench/programming-solutions`, `code-rag-bench/humaneval`,
  `code-rag-bench/mbpp` — public, no auth required.
- GitHub: `github.com/code-rag-bench/code-rag-bench` (Apache-2.0 harness code).

---

## 2. Prerequisites

### Foils arm (random + BM25) — no Helix needed

```powershell
pip install datasets rank-bm25
```

### Helix arm — bench server required

1. Start the Helix bench server (dedicated bench lane, port 11439):
   ```powershell
   # In helix063 venv, from the repo root:
   $env:HELIX_CONFIG = "F:\Projects\helix-context\helix.toml"
   python -m uvicorn helix_context._asgi:app --host 127.0.0.1 --port 11439
   ```
   Use a **separate port from dev** (11437) so the bench genome is isolated.

2. The bench server needs the programming-solutions corpus ingested.
   See step 3b below — ingest after building the per-query dump.

### Lexical-probe config (dense/splade/ribosome OFF)

The Helix arm targets the lexical/structural retriever, not dense embedding.
Confirm `helix.toml` (or the bench config) has:

```toml
[ribosome]
backend = "none"

[ingestion]
splade_enabled = false

[retrieval]
dense_embedding_enabled = false
```

---

## 3. Step-by-step: foils first, then Helix

### Step 3a — Run foils (random + BM25), write per-query dump

```powershell
# Full run (HumanEval + MBPP, all queries)
python benchmarks/coderag_bench.py

# Smoke run (50 queries per dataset)
python benchmarks/coderag_bench.py --limit 50

# HumanEval only
python benchmarks/coderag_bench.py --datasets humaneval

# Custom output path
python benchmarks/coderag_bench.py --out benchmarks/results/coderag_foils_myrun.json
```

Outputs written to `benchmarks/results/`:

```
coderag_foils_{ts}.json     -- summary: BM25 + random NDCG@10, Recall, Precision, efficiency
coderag_queries_{ts}.json   -- per-query dump (query text, gold_idx, bm25_rank)
                               consumed by the Helix arm
```

### Step 3b — Ingest corpus into the bench Helix server

The Helix arm expects documents ingested with `path=doc_{idx}` metadata so it can map
fingerprint sources back to corpus indices.

```powershell
# Ingest via the /ingest endpoint (one doc at a time via HTTP, or use CLI):
# The per-query dump contains the query text only; you need the corpus.
# Option 1: use helix ingest CLI (if corpus JSON is available):
# python benchmarks/coderag_bench.py writes coderag_queries_*.json with
# query+gold_idx but NOT the corpus texts themselves.
# Re-run the corpus build step and POST each doc via /ingest:

python - << 'EOF'
import json, urllib.request
corpus = json.load(open("benchmarks/results/coderag_corpus.json"))  # if saved
for di, doc in enumerate(corpus):
    payload = json.dumps({
        "content": doc["text"],
        "content_type": "code",
        "metadata": {"path": "doc_{}".format(di)}
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11439/ingest",
        data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    urllib.request.urlopen(req, timeout=30)
    if di % 100 == 0:
        print("ingested {}/{}".format(di, len(corpus)))
print("done")
EOF
```

Alternatively, save the corpus during the foil run by adding `--corpus-out`:

```powershell
# (coderag_bench.py already writes corpus via the run() function;
#  to save it separately for ingest, pipe corpus through coderag_diag.py
#  or extend coderag_bench.py with --corpus-out if needed)
```

### Step 3c — Run the Helix arm

```powershell
# Auto-discovers the most recent coderag_queries_*.json
python benchmarks/coderag_bench_helix.py --helix-url http://127.0.0.1:11439

# Explicit queries file + side-by-side foils comparison table
python benchmarks/coderag_bench_helix.py \
    --queries benchmarks/results/coderag_queries_20260613T120000Z.json \
    --foils benchmarks/results/coderag_foils_20260613T120000Z.json \
    --helix-url http://127.0.0.1:11439

# Smoke run (50 queries)
python benchmarks/coderag_bench_helix.py \
    --queries benchmarks/results/coderag_queries_<ts>.json \
    --helix-url http://127.0.0.1:11439 \
    --limit 50

# Custom output
python benchmarks/coderag_bench_helix.py \
    --queries benchmarks/results/coderag_queries_<ts>.json \
    --helix-url http://127.0.0.1:11439 \
    --out benchmarks/results/coderag_helix_myrun.json
```

Output: `benchmarks/results/coderag_helix_{ts}.json`

---

## 4. Metrics reference

### Primary: NDCG@10

```
NDCG@10 = (1/N) * sum_i [ 1/log2(rank_i + 1) if rank_i <= 10 else 0 ]
```

IDCG = 1 for all queries (single gold). Range [0, 1]; higher is better.

### Recall@k

```
Recall@k = (1/N) * sum_i [ 1 if gold in top-k else 0 ]
```

Reported at k = 1, 5, 10. Monotonic: recall@1 <= recall@5 <= recall@10.

### Precision@k

For a single-gold query, Precision@k = Recall@k / k. Reported at k = 1, 5, 10.

### Efficiency layer

| Metric | Source | Purpose |
|--------|--------|---------|
| `median_injected_tokens` | word-count × 1.3, top-10 docs | Token budget impact |
| `p90_injected_tokens` | 90th percentile of above | Worst-case token budget |
| `median_latency_ms` | per-query /fingerprint wall time | Speed comparison |
| `p90_latency_ms` | 90th percentile | Tail latency |

The efficiency layer is the **core of the cheap-vs-7B-dense argument**: at comparable or
better NDCG, Helix uses <1ms query encoding vs 316ms SFR-Mistral (100× measured).

---

## 5. Result table format

The summary JSON (both foils and Helix arm) contains one entry per dataset:

```json
{
  "humaneval": {
    "n": 164,
    "corpus": 1138,
    "bm25_ndcg@10": 0.9123,
    "bm25_recall@1": 0.8780,
    "bm25_recall@5": 0.9573,
    "bm25_recall@10": 0.9695,
    "bm25_precision@1": 0.8780,
    "bm25_precision@5": 0.1915,
    "bm25_precision@10": 0.0970,
    "bm25_efficiency": {
      "median_injected_tokens": 4200,
      "p90_injected_tokens": 6100
    }
  }
}
```

Helix arm adds `helix_ndcg@10`, `helix_recall@{k}`, `helix_precision@{k}`, and `efficiency`
(which includes latency).

---

## 6. Diagnostic arm

When Helix underperforms BM25, use `coderag_diag.py` to identify whether the miss is:
- **(a) gating** — gold doc never reached the scored set (entity/domain gating excludes it)
- **(b) ranking** — gold is scored but buried (additive fusion / IDF not discriminating)

```powershell
# Run in helix063 venv DIRECTLY (not via uv):
F:\Projects\_venvs\helix063\Scripts\python.exe -u benchmarks/coderag_diag.py `
    --n 12 --ds humaneval `
    --queries-json benchmarks/results/coderag_queries_<ts>.json `
    --helix-config F:\Projects\helix-context\helix.toml

# MBPP (harder, semantic)
F:\Projects\_venvs\helix063\Scripts\python.exe -u benchmarks/coderag_diag.py `
    --n 20 --ds mbpp
```

Per-query output:
```
humaneval:42: scored=18 in_scored=True gold_rank=0 gold_score=0.812
  | q_tok=34 overlap=28 | bm25_rank=0 | top3=[(42, 0.812), (17, 0.543), (88, 0.421)]
```

Key fields:
- `in_scored=False` → gating miss (FTS/domain filter dropped the gold)
- `in_scored=True, gold_rank>10` → ranking miss (present but buried by noise)
- `overlap=0` → query and gold share no identifier tokens (MBPP pure-NL queries)

---

## 7. Leak guards

These are non-negotiable for result integrity:

1. **Ingest only at query time, not gold time.** The corpus is the canonical solutions; the
   queries are the prompts/problem statements. Never ingest the query text as a document.
2. **No query in the genome.** After ingestion, the genome contains only the programming
   solutions. Query text is sent via `/fingerprint` at score time.
3. **Deterministic random seed.** The random floor (arm A) uses `hash((gold_id, query_idx)) % N`
   — deterministic, no seeded RNG needed.
4. **Stamp every result.** Both output JSONs include `timestamp`, `datasets`, `limit`, and
   `license` fields. The Helix arm adds `helix_url` and `queries_file`.
5. **Report full distribution.** Do not filter out datasets where Helix loses. MBPP is expected
   to be harder than HumanEval; both should be reported.

---

## 8. Tests

```powershell
# Run the unit tests (no network, no server, no GPU):
python -m pytest tests/test_coderag_bench_harness.py -v --noconftest

# Specific test classes:
python -m pytest tests/test_coderag_bench_harness.py -v --noconftest -k "TestNdcgAt or TestBM25 or TestRunPipeline"
python -m pytest tests/test_coderag_bench_harness.py -v --noconftest -k "TestScoreQueriesMocked"
```

72 tests covering: NDCG@10/Recall@k/Precision@k correctness, BM25 IDF floor, tok()
tokenizer, token_estimate, _percentile, efficiency_stats, parse_doc_idx,
preview_token_estimate, run() full pipeline over an inline 10-doc fixture,
and score_queries() mocked with a fake /fingerprint to validate the Helix arm
accumulation logic without any server.

---

## 9. Expected ballpark results (reference, not a gate)

On the programming-solutions corpus, BM25 performs near-perfectly on HumanEval because
the gold function body repeats the function name and docstring tokens verbatim. MBPP is
harder (NL->code). Helix's lexical retriever should match or exceed BM25 on HumanEval
and be the diagnostic contrast on MBPP.

| Arm | HumanEval NDCG@10 | MBPP NDCG@10 | Notes |
|-----|-------------------|--------------|-------|
| Random (floor A) | ~0.001 | ~0.001 | 1/1138 chance |
| BM25 (foil B) | ~0.90–0.99 | ~0.40–0.60 | Lexically saturated vs NL |
| Helix (arm D) | TBD | TBD | Target: match/beat BM25 on HumanEval |

If Helix HumanEval NDCG@10 << BM25 (e.g. < 0.70), run coderag_diag.py to identify
whether it's a gating miss or a ranking miss.

---

## 10. Full command sequence (copy-paste)

```powershell
# Step 1: foils (downloads HF datasets on first run, cached thereafter)
python benchmarks/coderag_bench.py --datasets humaneval,mbpp

# Step 2: start bench server (separate terminal, helix063 venv)
$env:HELIX_CONFIG = "F:\Projects\helix-context\helix.toml"
python -m uvicorn helix_context._asgi:app --host 127.0.0.1 --port 11439

# Step 3: ingest corpus into bench server (see section 3b for script)
# [ingest loop here]

# Step 4: Helix arm
python benchmarks/coderag_bench_helix.py `
    --helix-url http://127.0.0.1:11439 `
    --foils (Get-ChildItem benchmarks/results/coderag_foils_*.json | Sort-Object -Last 1).FullName

# Step 5: diagnostic (if Helix misses)
F:\Projects\_venvs\helix063\Scripts\python.exe -u benchmarks/coderag_diag.py `
    --n 20 --ds humaneval `
    --helix-config F:\Projects\helix-context\helix.toml

# Step 6: run tests (always)
python -m pytest tests/test_coderag_bench_harness.py -v --noconftest
```
