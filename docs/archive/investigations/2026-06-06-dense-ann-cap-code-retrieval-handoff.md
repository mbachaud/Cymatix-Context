# Handoff → Raude: the dense ANN cap collapses code retrieval (it's `max_genes`, not the threshold *mode*)

**From:** Laude (code-context benchmark track) · **Date:** 2026-06-06 · **For:** Raude (dense/SPLADE A/B owner)
**TL;DR:** On the new ContextBench **code** benchmark, **shipped v0.6.3's default dense path collapses the candidate pool to ~5–12 and drops the gold** on a single (non-sharded) genome. Root cause = `[retrieval] ann_threshold_max_genes = 12` (+ `ann_similarity_threshold = 0.58`). **Your arm2/arm3 A/B toggles `ann_threshold_mode` (absolute↔margin_over_random) but both keep `max_genes = 12` — so they can't fix this. The cap is the lever, not the mode.**

## Why this matters / how it connects to your work
- **v0.6.3 ships dense ON by default** (`dense_embedding_enabled=True`, `dense_embed_on_ingest=True`; SPLADE off). So this is the **production default retrieval path**, not an opt-in — the collapse is shipping.
- It's almost certainly the **same wiring failure as the prose semantic-2.4%** (dense replaces/【caps】the lexical pool instead of augmenting it), now reproduced and root-caused on code where retrieval is LLM-free and cleanly scorable.
- Your `vibrant-easley` untracked arms (`helix.toml.arm2-dense-absolute`, `arm3-dense-margin`) change `ann_threshold_mode` only. Both still cap at `max_genes=12` → both will still collapse. Suggest adding an arm that **raises `max_genes` (→500) and/or drops `ann_similarity_threshold` (→0)**.

## Evidence (official ContextBench evaluator, frozen v0.6.3 wheel @ 1510f128)

Same genome, two managers (diag isolates query path from ingest — genome is healthy: 161 open / 8 eu / 6 hetero, gold present):
```
task 88e1ffd3 (requests/models.py is gold)
  dense-on manager  (cap 12):  _retrieve -> 5 candidates,  gold NOT in pool
  lexical manager   (same db): _retrieve -> 55 candidates, gold IN pool
```

Matched requests (N=2, @27k tokens):
| arm | file_R | line_R | sym_R | injected |
|-----|--------|--------|-------|----------|
| v063 lexical (dense off) | 0.750 | 0.466 | 0.500 | 27.8k |
| v063 SHIPPED dense (cap 12) | 0.750 | **0.000** | 0.000 | **16.4k (pool starved, can't fill budget)** |
| v063 densefix (max_genes=500, sim=0) | 1.000 | 0.466 | 0.600 | 27.8k |

django individual tasks: shipped cap12 `93721db4` → gold **0/1 (lost)**; densefix cap500 `1a760e52` → gold **1/1 (kept)**.

**Read:** dense-as-shipped is *worse than dense-off* on code (cap starves the pool). Lifting the cap **restores dense to lexical parity** — it does not (yet) beat lexical on this small sample, so the cap fix stops the bleeding but isn't a lift by itself. SPLADE (the +6pp-on-code hope) is still untested (dense GPU-ingest compute wall — see below).

## Root cause (where to look)
- `knowledge_store.apply_density_gate` is fine (genome healthy). The collapse is in the **dense ANN candidate path** reached on a single/non-sharded genome: `config.retrieval.ann_threshold_max_genes=12`, `ann_similarity_threshold=0.58`, `ann_threshold_mode=absolute`, `dense_pool_size=500` (wired into the retriever at `context_manager.py:~490-514`).
- `dense_pool_size=500` is *intended* as the recall breadth ("decouples breadth from the final cut" per your comment) but in practice the **`max_genes=12` cut dominates and dense appears to replace the lexical set** rather than additively augment it → net pool ≈ the 12-cap, minus anything below sim 0.58. On code, few chunks clear 0.58 cosine to an issue-text query → ~5 survive.
- Likely the real fix is two-fold: (a) **raise/disable the `max_genes` cut for the additive path** so dense augments rather than caps, and (b) confirm additive fusion **unions** lexical+dense (gold is found by lexical/tag here, and it's vanishing).

## Repro (LLM-free, no daemon; my harness)
- Frozen v0.6.3 venv: `F:/Projects/_venvs/helix063` (wheel + torch cu121 + transformers==4.49.0 + sentence-transformers + spacy/en_core_web_sm). **transformers 5.x BREAKS helix** — pin 4.49.0.
- Configs: `F:/tmp/cb_helix_probe/helix_v063_shipped.toml` (cap12) vs `helix_v063_densefix.toml` (cap500/sim0).
- Driver: `helix-context/benchmarks/cb_helix_pred.py --config <toml> --dense-device cuda --workers 1 --tasks F:/tmp/cb_tasks_requests.json` (in-process: ingest repo@base_commit whole-file → `_prepare_query_signals`→`_retrieve`→`_apply_candidate_refiners`; line recovery by verbatim content-match).
- Isolation diag: `F:/tmp/cb_v2_diag2.py` (the same-genome lexical-vs-dense candidate-count test above).
- **Compute caveat:** dense GPU ingest needs ~5–6 GB/worker → **only 1 worker fits the 12 GB 3080 Ti** (2 → BrokenProcessPool OOM; 3 → multi-hour VRAM thrash). 1-worker dense ingest ≈ 12 min/django. Use requests/sklearn for fast iteration; avoid django ×N.

## Suggested next experiment (yours to run)
Add a 4th arm to your A/B: **`ann_threshold_max_genes = 500`, `ann_similarity_threshold = 0.0`** (mode irrelevant) on the ContextBench code set — and verify the additive path *unions* lexical+dense. If that lands dense ≥ lexical on code, then SPLADE-on (Phase 2) is the next lever to test for the +6pp-on-code thesis.

---

## 2026-06-07 UPDATE — ran your 4th arm + the SPLADE test. Both come up empty on code. (laude)

I ran exactly the experiment above (cap lifted to `max_genes=500`/`sim=0`, additive) **plus** SPLADE-on, matched, on the ContextBench code set. Official evaluator, frozen v0.6.3 wheel.

**Result 1 — the cap fix restores dense, but dense ≤ lexical on code (it does NOT beat it):**

sklearn-2, @27k line-recall (matched, same 2 tasks):
| arm | file_R | line_R | sym_R |
|-----|--------|--------|-------|
| BM25 | 1.000 | 0.307 | 0.438 |
| **v063 lexical (dense OFF)** | 0.333 | **0.654** | 0.562 |
| densefix (dense ON, cap 500/0) | 0.667 | 0.430 | 0.625 |
| +SPLADE | 0.667 | 0.430 | 0.625 |
| packet (all 3 identical) | 1.000 | 0.906 | 0.812 |

So lifting the cap stops the bleeding (dense pool no longer collapses, gold is found) — but **dense-on still loses to lexical on line-recall (0.430 vs 0.654)**. Turning the shipped dense path on (even cap-fixed) is **net-negative** for code line-recall vs pure lexical here. requests-2 was a tie (both 0.466). So the cap fix is necessary (it's a real prod bug — keep it) but it is **not a recall win** on code.

**Result 2 — SPLADE adds exactly nothing on code:** `+SPLADE == densefix` to 3 decimals on **both** requests-2 (0.466) and sklearn-2 (0.430/0.667/0.625) — identical file/line/sym, only injected-token count differs trivially. SPLADE *is* firing (it reshuffles which spans rank), it just nets zero recall change. **The "+6pp-on-code" SPLADE thesis does not reproduce on ContextBench code.** Don't spend the SPLADE ingest cost on the strength of the e-rag number.

**Takeaway for your A/B:** the `max_genes` cap fix is correct and should ship (it's a genuine collapse bug), but neither dense nor SPLADE buys recall over lexical on code. The lever is the **encoder geometry** (re-embed / hard-neg fine-tune), consistent with the prose-track conclusion — not the fusion/cap wiring and not SPLADE. The wiring fix just gets dense back to *parity-or-below* lexical.

### Two side-deliverables from this run (your call to review/merge)
- **PR #177 / issue #176** — `BGEM3Codec.encode_batch` never released torch's CUDA cache; long-lived GPU dense ingest climbs to the card ceiling (measured 11.7 GB/95% on a single worker, 12 GB 3080 Ti) and spills to shared RAM. Added a periodic `empty_cache()` (`HELIX_DENSE_VRAM_RELEASE_EVERY`, default 256, cuda-only, byte-neutral; 8 tests). Off `master`, flagged for your review — **not self-merged** (your area).
- **issue #178** — operational runbook for dense ingest on ≤12 GB rigs (CPU-for-batch, 1-worker-GPU caps, the env knobs, confirmed OOM/thrash modes).

Repro for all of the above: `helix-context/benchmarks/cb_helix_pred.py` + `cb_score_sklearn2.py` / `cb_score_splade.py`; configs in `F:/tmp/cb_helix_probe/`; frozen venv `F:/Projects/_venvs/helix063`.
