# Spec: ContextBench Step-0 Helix Offline Scorer (hardened)

**Date:** 2026-06-04 · **For:** Raude (impl) · **Status:** ready · **Scope:** retrieval-only, intentionally small — NOT an agent/patch/generation bench. Supersedes the draft scorer plan; corrections below are verified against the live ContextBench data + package source + the Helix worktree routes.

## 0. Verified corrections to the draft (load-bearing — build against THESE)
1. **Dataset id + split (draft was wrong).** `load_dataset("Contextbench/ContextBench", "contextbench_verified")` = 500 verified; `"default"` = 1,136. Both single split **`train`**. Python filter: column **`language` == "python"`** (lowercase; 8 langs = py/java/js/ts/go/rust/c/c++ — **no C#**). NOT `EuniAI/ContextBench` (that's the GitHub org), NOT `--split verified500`.
2. **Gold schema (draft assumed extra fields).** Column **`gold_context`** is a **JSON-encoded string** → `json.loads` → list of objects with **EXACTLY** `{"file","start_line","end_line","content"}` (inclusive lines). **No `block_type`, no `symbol`.** Task fields: `instance_id` (+ `original_inst_id`), `repo`, `repo_url`, `base_commit`, `problem_statement` (issue text), `patch`/`test_patch`/`f2p`/`p2p` (the fix+tests — **do NOT index these**). The paper's 23,116 blocks / 4,548 files / 522,115 lines are **derived at score-time via tree-sitter**, not columns.
3. **REUSE the official evaluator — do NOT reimplement.** `python -m contextbench.evaluate --gold <parquet> --pred <helix.json> --cache ./repos` accepts a **standalone pred file with no agent** (a retriever's ranked set = a one-step trajectory). It reuses the official tree-sitter "structural coordinate" alignment and reports **Coverage (= recall) + Precision** micro-averaged at **file / symbol(def) / span(byte) / line**. ⇒ the build is a **~50-LOC pred-emitter + a thin efficiency wrapper**, not a 100-LOC scorer. **F1 is not emitted — compute `F1 = 2·P·C/(P+C)` yourself.**
4. **Repo checkout is required for line/block/symbol** (tree-sitter reads file bytes); `evaluate_instance()` git-clones + checks out each `base_commit` (sparse-checkout supported on gold files). **File-level coverage/precision is the ONLY zero-clone granularity.** The clone/checkout is the real Step-0 cost — cache by `(repo_url, base_commit)`.
5. **Helix endpoints (both exist).** `/fingerprint` = raw ranked retrieval (`score_floor=0` ⇒ `eval_budget=max_results`, so pass a high `max_results` e.g. 200 to see ranks past 10). `/context/packet` = the delivered evidence packet (`build_context_packet`). Use **both**: `/fingerprint` for the recall ceiling, `/context/packet` for the delivered injected-tokens/gold-density.
6. **License care.** Package/harness = Apache-2.0. The **HF dataset card has NO license tag**, and the **66 source-repo licenses are not redistributed** — fine for internal A/B; for any **public** claim, clone from source and check each upstream license.

## 1. Pred schema to emit (verbatim — `contextbench/parsers/custom_parser.py`)
One JSON list, one object per task, per arm:
```json
{
  "instance_id": "<matches gold instance_id>",
  "traj_data": {
    "pred_steps": [],
    "pred_files": ["src/foo.py", "src/bar.py"],
    "pred_spans": {"src/foo.py": [{"start": 10, "end": 40}]}
  },
  "model_patch": ""
}
```
**Note the keys are `start`/`end`** (not start_line/end_line). `pred_steps: []` ⇒ final-set scoring. `instance_id` MUST match gold.

## 2. Arms (keep small)
- **B — BM25 dump (required foil).** BM25/Pyserini over the repo@base_commit; retrieve chunks until a token budget; emit `pred_files`/`pred_spans`. **Sweep ≥2 budgets (e.g. 8K and 27K tokens)** so the dump's recall-vs-tokens *curve* is visible, not one point. Cross-check against HF `princeton-nlp/SWE-bench_bm25_27K` where repos overlap.
- **D — Helix packet (the deliverable).** Two sub-measurements through the same evaluator:
  - **D-rank** = `/fingerprint` (LLM-free, `score_floor=0`, `max_results≈200`) → ranked spans → pred = the **recall ceiling**.
  - **D-packet** = `/context/packet` (the delivered, post-clamp product) → pred = the **delivered** recall + the injected-tokens/gold-density.
  - The **D-rank − D-packet gap** is a free internal diagnostic = the delivery-clamp cost on code (mirrors the prose conversion-gap finding).
- **E — Helix + semantic store (diagnostic ablation only).** Dense on/off; report honestly as "does dense add over lexical+structural on code?" — predicted ~0 lift; never the hero arm.
- **A — no-retrieval floor.** Optional for Step 0; don't block on it.

## 3. Scoring + the efficiency layer
- **Recall/precision/F1 @ file/line(/symbol/block):** run the **official evaluate** per arm's pred file → Coverage(=recall)+Precision; compute F1.
- **Efficiency layer (Helix-thesis metrics — WE compute, not the package), per arm:**
  - `injected_tokens_est` (median + p90) — for D-packet use the actual delivered packet tokens; for B the dump tokens.
  - `gold_density = overlapped_gold_lines / retrieved_lines`
  - `recall_per_1k_tokens = line_recall / (injected_tokens_est / 1000)`
- **Report table — one row per arm:** `arm · n · file_recall · line_recall · line_precision · line_F1 · median_injected_tokens · p90 · gold_density · recall_per_1k_tokens · median_latency_ms`.
- **The plot (the whole story):** scatter, x = `injected_tokens_est`, y = `line_recall`, one point per arm (B at each budget). Top-left wins; Helix-D should sit **above/left** of the BM25 curve.

## 4. CLI
```bash
# smoke (100 Python)
python benchmarks/contextbench_step0.py --config contextbench_verified --language python --limit 100 \
  --arms bm25:8k,bm25:27k,helix_fingerprint,helix_packet \
  --helix-url http://127.0.0.1:11439 --repo-cache F:/tmp/contextbench_repos \
  --out F:/tmp/cb_step0_py100.json
# first real report (500 verified)
python benchmarks/contextbench_step0.py --config contextbench_verified \
  --arms bm25:27k,helix_fingerprint,helix_packet --helix-url http://127.0.0.1:11439 \
  --repo-cache F:/tmp/contextbench_repos --out benchmarks/results/cb_step0_v500_<ts>.json
```
(Target the **bench lane :11439**, not the dev :11437.) Each arm → emit pred JSON → `python -m contextbench.evaluate --gold <parquet> --pred <arm>.json --cache F:/tmp/contextbench_repos --out <arm>.jsonl` → aggregate + efficiency layer + plot.

## 5. Implementation notes (keep it ~1 file)
Do: load tasks → for each `(repo_url, base_commit)` clone/sparse-checkout **once** (cache; dominant cost) → ingest into a Helix genome keyed by `(repo, commit)` hash (reuse across that commit's tasks) → run B + D arms → emit pred JSONs → call official evaluate → aggregate + efficiency + plot. **Deps:** `tree-sitter` + `tree-sitter-language-pack` into the bench venv (`uv pip`); `contextbench` package; BM25 (Pyserini or rank-bm25). **Do NOT:** run an LLM, generate/validate patches, run an agent loop, mutate the repo, or ingest post-fix `patch`/`test_patch`/`gold_context`.

## 6. Leak guards
Checkout at **`base_commit`** (pre-fix → the gold edit locations exist but the fix doesn't). Never ingest `patch`/`test_patch`/`gold_context`/`f2p`/`p2p` into the indexed tree; keep all dataset/bench metadata outside the Helix-indexed path. Stamp every result row with `repo`, `base_commit`, `helix_commit`, `genome_id`, `config_hash`, `timestamp`.

## 7. Acceptance gates
A valid 100-task smoke + a valid 500-task `contextbench_verified` run; the B-vs-D table; the recall-vs-injected-tokens plot; **zero LLM calls in retrieval**; reproducible (repo commit + helix commit + config hash + timestamp in every row).

## 8. Decision rule
- **Helix line/block recall ≈ or > BM25 at materially fewer injected tokens → proceed to RepoBench-R** (the next rung).
- **Helix lower recall but much higher gold density → diagnose the miss:** use the **D-rank vs D-packet gap** + per-granularity coverage to localize it as **file-routing** (file_recall low), **block-span** (file ok, span low), or **line-truncation/clamp** (D-rank ok, D-packet low).
- **Helix loses both recall and gold density → fix code retrieval before any SWE-bench e2e.**
- **Do not let Step 0 become SWE-bench.** ContextBench gives the intermediate gold-context signal SWE-bench lacks — use it before spending on generation/patch validation.
