# helix-context v0.6.3 — External-validation kit (Onyx / EnterpriseRAG-Bench)

This release freezes the exact code + configuration that produced our 2026-06-05
EnterpriseRAG-Bench numbers, so they can be reproduced independently.

## What this version is (provenance, stated plainly)
- **v0.6.3 is a validation snapshot.** It carries the current *retrieval/delivery* pipeline
  (question-conditioned dense recall + the semantic-scoped wiring + the dynamic per-gene delivery
  budget). It is a **sibling of public PyPI 0.6.2**, not a descendant — it does **not** include
  0.6.2's RAM/memory optimizations (shared-codec singleton, `HELIX_MEM_PROFILE`). Those are
  memory/latency features and are **irrelevant to recall and answer-correctness**, which is what
  this kit measures. A future public release will reconcile both lines.
- All numbers below were measured on this code with the profile + env block in this kit.

## Reproduction recipe

### 1. Install
```
pip install helix_context-0.6.3-*.whl        # the wheel shipped with this kit
# or: pip install -e .   from a checkout of the v0.6.3 tag
```

### 2. Configuration — TWO parts, both required
**(a)** Use the pinned profile: [`docs/validation/onyx-fixed-pipeline.toml`](./onyx-fixed-pipeline.toml)
(SPLADE-on, min-genes-10, cross-encoder rerank, additive fusion, dynamic per-gene delivery,
`semantic_dense_additive_weight=16`, `semantic_broaden_routing=true`).

**(b)** Export the env block — these knobs are env-only (no config key) and the numbers do NOT
reproduce without them:
```
export HELIX_QUESTION_DENSE=1      # dense tier encodes the QUESTION, not the extracted tag-bag
export HELIX_SEMANTIC_ARM=1        # activates the semantic-scoped weight + broaden (semantic queries only)
export HELIX_SHARD_WORKERS=8       # parallel shard fan-out — recall-IDENTICAL to serial; latency only
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export HELIX_CONFIG=docs/validation/onyx-fixed-pipeline.toml
export HELIX_GENOME_PATH=<your sharded genome>   # set [genome].path / HELIX_USE_SHARDS=1 for shards
```

### 3. Launch
```
python -m uvicorn helix_context._asgi:app --host 127.0.0.1 --port <PORT>
# wait for GET /health -> genes>0
```

### 4. Run the harness (under `benchmarks/`)
- **Retrieval recall** (LLM-free): `bench_enterprise_rag_recall.py --k 200 --helix-url http://127.0.0.1:<PORT>`
  (sends the needle `query_type`; honors the env block above).
- **Answer-correctness** (capture-once, replay across generators):
  `capture_context.py` (saves the assembled context per question) →
  `gen_from_context.py --model <haiku|sonnet|opus>` (replays — no re-retrieval) →
  `score_enterprise_rag_onyx.py --judge-model opus` (LLM-judged, both lenses).

The corpus is the EnterpriseRAG-Bench / Onyx synthetic set (500 needles across 10 question types,
incl. 30 no-gold "abstain" needles). Use your own Onyx corpus, or our builder under `scripts/`.

## Expected numbers (verified on this code, 2026-06-05)

### Retrieval recall — any-gold (first gold in top-k), k=200, n=500
| @1 | @3 | @5 | @10 | @200 | MRR |
|----|----|----|-----|------|-----|
| 11.6% | 18.0% | 21.2% | **27.2%** | **61.0%** | 0.169 |

Per-type recall@10: basic 26.9 · semantic **4.8** · intra-document-reasoning 35.0 ·
conflicting-info 70.0 · miscellaneous 65.0 · completeness 50.0 · constrained 46.7 ·
project-related 45.0 · high-level 0 · info-not-found 0 (last two are no-gold abstain types).

Delivery: **gold delivered in the assembled context for 105/500** questions at `max_genes=8`
(`doc_recall` 17.9%).

### Answer-correctness — opus-judged, "Onyx lens", full-500, identical captured context per model
| model | correctness | hallucination | abstain |
|-------|-------------|---------------|---------|
| haiku | 16.4% | 12.2% | 357/500 |
| sonnet | **19.2%** | 14.4% | 332/500 |
| opus | 15.4% | **6.4%** | 391/500 |

Conditioned on gold actually being delivered (n=105): haiku 71.4% · sonnet 75.2% · opus 67.6%.

## How to read these (metric-axis note)
- **Recall@k is retrieval recall, NOT the EnterpriseRAG-Bench "Overall %".** They are different
  axes; do not compare our 27.2% recall against a leaderboard "Overall %".
- The **leaderboard-comparable axis is answer-correctness** (retrieve + generate, LLM-judged) —
  the second table.
- **Retrieval is the ceiling, not the generator:** conditioned on gold being delivered, all three
  models cluster at 68–75%; global correctness is bounded by the 21% delivery rate (105/500).
  Swapping the generator (haiku→opus) does not move the axis. Semantic is the weak bucket
  (recall@10 4.8%) — a near-neighbor density limit at 850K genes, the target of an encoder upgrade.
- The semantic-scoped arm (`HELIX_SEMANTIC_ARM`) affects **only** `query_type=="semantic"` queries;
  factual/lexical retrieval is byte-identical to baseline. Net effect is small-positive and safe
  (full-500 A/B: semantic recall@10 2.4 → 4.8, no other type regressed).

## Reproduction status
These figures are from the tagged v0.6.3 code. The retrieval recall and the three graded runs are
re-confirmed on a clean checkout of the tag as the final gate before this kit is considered
authoritative.
