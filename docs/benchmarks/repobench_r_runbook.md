# RepoBench-R Step-1 Runbook

**Dataset:** `tianyang/repobench-r` (CC-BY-4.0, HuggingFace)
**Metric:** acc@k — gold snippet in top-k of the ranked candidate pool
**Settings:** XF-F (`python_cff`, `java_cff`) and XF-R (`python_cfr`, `java_cfr`)
**Arms:** random floor / Jaccard-overlap / BM25Okapi (foils) + Helix per-example + Helix global
**LLM-free, GPU-free, no server required for foils; live Helix (in-process) for Helix arms**

---

## 0. Background

RepoBench-R is a closed candidate-pool retrieval benchmark: for each example the
model is given the in-file code context and must rank a small set of cross-file
candidate snippets so that the gold snippet (the one actually imported/used) is
ranked highest.  Acc@k = fraction of examples where gold is in the top-k.

Strategy-doc section 3 specifies:
- **easy** split: acc@1, acc@3
- **hard** split: acc@1, acc@3, acc@5
- **XF-F** (`cff`): cross-file, next-line (file context from imported files)
- **XF-R** (`cfr`): cross-file, random snippet (harder; random same-repo dependency)

The Helix arms use the in-process `HelixContextManager` with a lexical-only config
(dense/splade/ribosome OFF) — the "code workhorse" profile.

---

## 1. Prerequisites

```powershell
# cb-step0 venv (foils only):
pip install huggingface_hub rank-bm25

# helix063 venv (Helix arms):
# - helix_context must be importable (pip install -e . from repo root)
# - dense/splade/ribosome OFF in helix_probe_lexical.toml
```

The lexical-probe config template lives at:
`docs/benchmarks/helix_probe_lexical.toml`

Key settings that must be OFF:
```toml
[ribosome]
backend = "none"          # no LLM splicing

[ingestion]
splade_enabled = false    # no SPLADE sparse expansion

[retrieval]
dense_embedding_enabled = false   # no BGE-M3 dense recall
```

---

## 2. Step 1 — Run foils (random / overlap / BM25)

This writes per-example JSON dumps to `benchmarks/results/` that the Helix arms
then read.  Must be run before the Helix arms.

```powershell
# Python XF-F (200 examples/level, both easy and hard)
python benchmarks/repobench_r.py --config python_cff --n 200

# Python XF-R
python benchmarks/repobench_r.py --config python_cfr --n 200

# Java XF-F
python benchmarks/repobench_r.py --config java_cff --n 200

# Full dataset (all examples, both levels)
python benchmarks/repobench_r.py --config python_cff --n 0

# Hard only, with explicit output path
python benchmarks/repobench_r.py --config python_cff --n 200 --levels hard \
  --out benchmarks/results/rb_foils_hard_200.json
```

Outputs:
- `benchmarks/results/repobench_r_{config}_{level}_n{n}.json` — per-example dump
- `benchmarks/results/repobench_r_{config}_foils_{timestamp}.json` — summary

---

## 3. Step 2 — Run Helix per-example arm

Reads the per-example dumps from Step 1.  Run DIRECTLY with the helix063 python,
NOT via `uv` (ProcessPoolExecutor trampoline deadlock).

```powershell
# Set the lexical-probe config (or pass --helix-config)
$env:HELIX_CONFIG = "F:/tmp/cb_helix_probe/helix_probe.toml"

# Serial (safe, recommended for first run)
F:/Projects/_venvs/helix063/Scripts/python.exe -u benchmarks/repobench_r_helix.py `
  --config python_cff --levels easy,hard

# With worker parallelism (only when running the python.exe directly)
F:/Projects/_venvs/helix063/Scripts/python.exe -u benchmarks/repobench_r_helix.py `
  --config python_cff --workers 4

# Cap to 50 examples/level for a quick smoke run
F:/Projects/_venvs/helix063/Scripts/python.exe -u benchmarks/repobench_r_helix.py `
  --config python_cff --limit 50
```

Output: `benchmarks/results/repobench_r_{config}_helix_{timestamp}.json`

---

## 4. Step 3 — Run Helix global-genome arm

One shared genome over the deduped union of ALL candidate snippets.  Scores both
B-mode (pool-rank, comparable to foils) and C-mode (global-rank, realistic agent
scenario).  Also runs a matched global-BM25 foil automatically.

```powershell
$env:HELIX_CONFIG = "F:/tmp/cb_helix_probe/helix_probe.toml"

F:/Projects/_venvs/helix063/Scripts/python.exe -u benchmarks/repobench_r_helix_global.py `
  --config python_cff

# Custom genome scratch dir (useful if TEMP is low-space)
F:/Projects/_venvs/helix063/Scripts/python.exe -u benchmarks/repobench_r_helix_global.py `
  --config python_cff --genome-dir F:/tmp/rb_global_genome
```

Output: `benchmarks/results/repobench_r_{config}_global_{timestamp}.json`

---

## 5. Expected metric ranges (Python XF-F, n=200)

Based on the RepoBench paper (arXiv 2306.03091) and general lexical retrieval
behaviour on this dataset.  Treat as orientation — exact numbers depend on the
Helix build and config.

| Arm | easy acc@1 | easy acc@3 | hard acc@1 | hard acc@3 | hard acc@5 |
|-----|-----------|-----------|-----------|-----------|-----------|
| Random floor | ~0.20 | ~0.55 | ~0.15 | ~0.40 | ~0.60 |
| Jaccard overlap | ~0.45 | ~0.70 | ~0.30 | ~0.55 | ~0.70 |
| BM25Okapi (per-pool) | ~0.40 | ~0.65 | ~0.28 | ~0.52 | ~0.68 |
| Helix per-example (B) | TBD | TBD | TBD | TBD | TBD |
| Helix global (B-mode) | TBD | TBD | TBD | TBD | TBD |

Notes:
- BM25Okapi on the per-pool setting can fall below overlap because its IDF goes
  negative on terms appearing in more than half the ~6-candidate pool.
- The global-arm BM25 (floored IDF) is the correct lexical foil for the global arm.
- Helix per-example is expected to underperform relative to global because the ~5-17
  snippet corpus is too small for Helix's FTS/IDF pipeline to exploit rarity signals.
- Helix global (B-mode) is the head-to-head comparison against the BM25 foil.

---

## 6. Reading the output JSON

Foils summary (`repobench_r_{config}_foils_{timestamp}.json`):
```json
{
  "config": "python_cff",
  "n_per_level": 200,
  "timestamp": "...",
  "levels": {
    "easy":  {"n": 198, "avg_cands": 6.2, "random_acc@1": 0.162, "overlap_acc@1": 0.449, ...},
    "hard":  {"n": 197, "avg_cands": 9.1, "random_acc@1": 0.142, "overlap_acc@5": 0.703, ...}
  }
}
```

Helix global summary (`repobench_r_{config}_global_{timestamp}.json`):
```json
{
  "corpus_size": 1847,
  "levels": {
    "easy": {
      "helix_B_acc@1": 0.412, "helix_B_acc@3": 0.681,
      "helix_C_recall@1": 0.031, "helix_C_recall@10": 0.298,
      "bm25_B_acc@1": 0.441, "bm25_B_acc@3": 0.703, ...
    }
  }
}
```

B-mode (`_B_acc@k`) is comparable to the foils table.
C-mode (`_C_recall@k`) measures how often gold lands in the global top-k — the
realistic multi-project agent scenario.

---

## 7. Leak guards and reproducibility

- The dataset provides a closed candidate pool per example.  No gold leakage is
  possible at the retrieval level: the harness only looks up `golden_snippet_index`
  after ranking, never before.
- Helix is seeded with `content_type="code"` and no metadata other than
  `path="cand_{i}"` / `path="snip_{sid}"`.  No repo name, file path, or gold label
  is passed to the Helix ingestion pipeline.
- Each result file is stamped with `timestamp` and `helix_config` path for
  reproducibility.  For a published result also record the helix_context commit hash:
  ```powershell
  git -C F:/Projects/helix-context rev-parse HEAD
  ```
- The per-example genome dirs are torn down after each example in the per-example arm.
  The global genome dir is deleted and recreated at the start of the global arm run.
- **Do not commit the genome DB files** (`*.db`, `*.db-shm`, `*.db-wal`) to the repo.

---

## 8. Running the unit tests (no network, no server)

```powershell
# From repo root, with helix063 venv active:
python -m pytest tests/test_repobench_r_harness.py -v --noconftest

# Or with the standard test suite (requires pydantic + full helix install):
python -m pytest tests/test_repobench_r_harness.py -v
```

The test file is at `tests/test_repobench_r_harness.py` and covers all pure-Python
logic: `acc_at`, `tok`, `make_query`, `rank_overlap`, `rank_bm25`, `rank_random`,
`_ks_for_level`, `BM25` (global floored-IDF), and a full end-to-end fixture.
Expected: 40 tests pass, 0 fail, ~0.3 s.
