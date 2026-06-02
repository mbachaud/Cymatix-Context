# Widened cross-encoder rerank — semantic-recall experiment (2026-06-02)

**Status:** approved (interactive sign-off); experiment-only, all changes env-gated / default-off.
**Owner:** Max (raude session). **Corpus:** `enterprise_rag_onyx_full_2` (850,501 genes, 100 shards, 100% dense on `embedding_dense_v2`).

## Why

Saved-session lever-ladder item #1 said "`rerank_enabled=true` is a cheap flip → semantic@10 from 2.4% toward ~20%." Investigation (verified) found that wrong on four counts:

1. **Name mismatch** — `scoring/blend.py` calls `ribosome.rerank`; the cross-encoder method is `DeBERTaRibosome.re_rank`. `hasattr(DeBERTaRibosome,"rerank")==False` → never fires.
2. **Backend can't load** — `DeBERTaRibosome.__init__` eager-loads `training/models/splice` (+ nli), which don't exist → `OSError` → backend silently disabled.
3. **`/fingerprint` can't rerank** — recall harness sends no `profile`; `allow_rerank=(profile=="quality")` is False, and `len(candidates) > eval_budget` is never true.
4. **Working set capped at `max_genes*2`=24** — `ShardRouter.query_genes` returns ≤ `max_genes*2`; the dense 200/500 breadth is consumed *inside* `query_docs`, upstream of rerank. So rerank's ceiling is recall@24, NOT recall@200. The "~20% in-pool gold" lives at ranks 25–200, which `/context` never retrieves.

**Conclusion:** rerank as-shipped is structurally inert for semantic recall. To test its real potential we must (a) make it reachable, (b) feed it the ~200-pool, then cut to 12.

## Changes (all opt-in; no shipped behavior change with flags off)

- `backends/deberta_backend.py`: add `rerank = re_rank` alias (fix #1); make splice/nli load lazy/tolerant + `splice()` no-op fallback when the model is absent (fix #2). Cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`, HF-cached) is the only model required.
- `context_manager.py build_context`: env `HELIX_RERANK_POOL=N` widens the `_retrieve` budget so the candidate pool reaching `_apply_candidate_refiners` is ~2N (router doubling), while the rerank cut target stays `max_genes`=12. Default (unset/0) = current behavior.
- `scoring/blend.py`: env `HELIX_RERANK_CAPTURE=<path>` appends `{query, pre:[source_ids], post:[source_ids]}` JSONL inside the rerank block — `pre` = pool entering rerank, `post` = cross-encoder top-12. Default off.

## Config

`helix_splade_on_mg10_rerank.toml` = baseline `helix_splade_on_mg10_calibrated.toml` + `[ribosome] enabled=true backend="deberta" warmup=false device="cpu"` + `[ingestion] rerank_enabled=true`.

## Measurement

One `/context` run over the 125 EnterpriseRAG semantic queries with `HELIX_RERANK_POOL` + `HELIX_RERANK_CAPTURE` set. From the capture:
- **recall@10 pre vs post** — isolates the cross-encoder's reordering effect (same pool, same upstream cymatics/sr/harmonic).
- **recall@24 / recall@200 (pre)** — the retrieval ceilings; rerank can't beat recall@pool.
- Gold = `expected_doc_ids` → uuid_index rel-paths, matched against gene `source_id`.

## Expected outcome / kill criteria

- If recall@200_pre ≈ recall@10_pre (gold not arriving deeper in the pool), widening+rerank can't help → encoder/fine-tune is the only lever (go to Qwen3 A/B).
- If recall@200_pre >> recall@10_pre but post ≈ pre, the cross-encoder can't discriminate gold from topical neighbors → confirms the near-neighbor-dilution diagnosis; fine-tune on hard negatives.
- If post > pre meaningfully, rerank (with widening) is a real lever → productionize the pool-widening (PRD the retrieval-flow change).
