# WS3 slice-2 design — swarm consensus

**Decision:** How to wire WS3 (symbol-graph PageRank) so structural centrality recovers the WS2 fingerprint@27k regression (−14pp) while preserving the packet gain (+2.1pp), without breaking lexical-first.

**Constraints:** pure CPU, no query-time model, no prose regression, additive-only (never displace an exact lexical hit).

## Panel
- **Pragmatic Engineer** — smallest change that recovers the regression; minimize hot-path surface area.
- **Scale Architect** — candidate-local cost, subgraph fan-out, query latency.
- **Devil's Advocate** — is WS3 even needed, or just bound the expansion? magic-number / overfit risk.
- **Relevance / IR Specialist** — lexical-first integrity, ranking quality, guard tests.

## Independent positions

**Pragmatic Engineer** — *Favors: bound the expansion first.* The regression's root cause is **unbounded** expansion dumping every referenced def into the budget. The minimal fix is to cap it (keep top-K by centrality) inside the existing `expand_coactivated` SYMBOL_REF branch — no new fusion plumbing yet. Strongest argument: a full fusion tier touches `retrieval/fusion.py` + scoring + classifier — a lot of hot-path surface to regress prose for a +2pp gain. Score: bound-only **8**, full-fusion-now **5**. Changes mind if bounding alone fails to recover fingerprint or loses the packet gain.

**Scale Architect** — *Favors: candidate-local + hard fan-out caps.* PageRank power-iteration over top-N + 1-hop is cheap, **but** a popular symbol referenced by hundreds of chunks blows up the 1-hop subgraph. Must cap per-chunk refs and neighbor count. Strongest argument: keep the subgraph bounded and iterations capped (already 50) and it stays well within the query budget. Key concern: per-query PageRank CPU on dense files without caps. Score: fusion-tier OK **7** *if* subgraph-bounded. Changes mind if subgraph stays unbounded.

**Devil's Advocate** — *Favors: ship WS2 with a bound, defer PageRank fusion (YAGNI).* The packet is the production path and already gains +2pp; the fingerprint is a bench/diagnostic arm. The PageRank personalization weights (10×/50×) are **unvalidated magic numbers** — tuning them on the 26-task smoke risks overfitting. Strongest argument: don't build a centrality re-ranker until a *bounded-expansion* A/B proves the fingerprint still regresses. Key concern: a centrality tier can itself regress by over-weighting central-but-irrelevant defs. Score: bound-only **7**, PageRank-fusion-now **4**. Changes mind if bounded expansion still shows the −14pp.

**Relevance / IR Specialist** — *Favors: additive, capped fusion tier under lexical + bounding.* PageRank must re-order **within** the lexically-relevant set, never promote a non-lexical hit above an exact-identifier match. Strongest argument: an additive tier with a hard weight cap + a guard test ("query is a literal function name → that function at rank 1") preserves §4 lexical-first. Key concern: if the weight is too high or PageRank is a primary re-rank, it buries exact matches. Score: additive-capped tier **9**; primary re-rank **2**. Insists the personalization weights become **knobs**, not constants.

## Conflict map

| Topic | Agree | Disagree | Confidence |
|---|---|---|---|
| Bound the expansion (cap top-K) | Pragmatic, Devil's, IR, Scale | — | **High** |
| Build full fusion tier *now* | IR, Scale (if capped) | Pragmatic, Devil's (measure first) | Medium |
| Lexical-first = additive + weight cap + guard test | Pragmatic, Scale, Devil's, IR | — | **High** |
| Personalization weights are magic numbers → make knobs | Devil's, IR | — | **High** |
| Subgraph fan-out must be capped | Scale, Pragmatic | — | **High** |
| PageRank as primary re-rank | — | IR (hard no), all | **High (against)** |

**Blind spot flagged:** no persona represents *cross-file* resolution quality — current edges are intra-file, so WS3 centrality is computed on intra-file graphs only. Cross-file is a separate future lever; don't conflate.

## Consensus recommendation

**Decision:** **Phased.** Phase 2a: bound + rank the expansion (cap top-K referenced defs by centrality) — the minimal change targeting the root cause. Phase 2b (only if 2a leaves recall on the table): add the PageRank centrality as an **additive, weight-capped fusion tier under the lexical tiers** + budget-trim ordering, gated to code queries, with a lexical-first guard test.
**Confidence:** High on 2a; Medium on 2b (gated on 2a's measurement).
**Unanimous:** Yes on bounding + lexical-first + caps; split on building the full tier immediately (resolved by phasing).

### Why
The regression is a *dump* problem, not a *ranking-everywhere* problem. Bounding-by-centrality (2a) is small, reversible, and directly attacks it; it reuses the already-built PageRank module to pick which K defs survive. Only escalate to the full fusion tier (2b) if the bounded version under-delivers — which keeps the hot-path change proportional to the measured need (Pragmatic/Devil's) while the IR/Scale guard-rails apply the moment a tier does land.

### Mitigations for top concerns
1. Over-weighting central-but-irrelevant defs (IR/Devil's) → additive tier strictly under lexical + hard weight cap + **guard test** (exact-identifier → rank 1).
2. Subgraph blow-up on popular symbols (Scale) → cap per-chunk refs and 1-hop neighbor count; candidate-local only.
3. Magic-number overfit (Devil's/IR) → personalization weights + cap + pagerank_weight are **config knobs**, defaulting to Aider values; do **not** tune on the smoke set — validate on a held-out corpus.
4. Hot-path regression risk (Pragmatic) → everything gated by `symbol_graph` + code-query classifier; prose path untouched (data-gated: no edges → no-op).

### Reversibility
High — all config-gated. `symbol_graph=false` or `symbol_expansion_cap=0` reverts to cAST-only. No schema migration to undo (edges already shipped in WS2).

### Review triggers
- Fingerprint@27k line recall back to ≥ WS2-off (0.679); packet ≥ 0.825.
- Lexical-first guard test must stay green (exact-identifier at rank 1).
- Prose ContextBench recall unchanged.
- Per-query latency within budget on a dense-file corpus.

## Knob → impact map (regression tracking)

| Knob | Default | Primarily moves | Neighboring impacts |
|---|---|---|---|
| `symbol_graph` (on/off) | on | packet recall ↑, fingerprint recall ↓ (when unbounded) | ingest time (edge emission), genome size (edges table) |
| `symbol_expansion_cap` (top-K defs kept) | ~8 | **fingerprint regression lever**: ↑K → recall↑ but precision/fingerprint↓ | budget consumption; interacts with `pagerank_weight` |
| `pagerank_weight` (fusion tier, 2b) | low/capped | ranking-sensitive metrics (acc@k, fingerprint order) | **lexical-first risk** if too high (buries exact hits) |
| `damping` | 0.85 | centrality distribution (global vs restart-local) | minor; rarely tuned |
| `neighbor_hops` / subgraph cap | 1-hop, capped fan-out | coverage vs CPU | latency; dilution if too wide |
| personalization: query-symbol weight | 10× | biases ranking toward query-relevant defs | overfit risk if tuned on smoke |
| personalization: session weight | 50× | biases toward in-context chunks | needs session-delivery wiring (2b) |
| code-query gating (classifier) | on | confines effect to code | prose non-regression; classifier accuracy |

> This analysis simulates multiple specialist perspectives to surface risks and tradeoffs. It is not a substitute for input from actual domain experts. The personas are heuristic models. Use it as a structured starting point, not a final verdict.
