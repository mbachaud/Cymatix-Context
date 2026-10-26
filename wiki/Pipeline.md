# Pipeline

- Seven stages run per turn. Stages 0–2 and 5–6 are algorithmic — deterministic
  CPU math, no model call. Stages 3 and 4 are the only model-capable steps, and
  both ship **off**.
- So the default retrieval path makes **zero model calls** on the way to an
  answer. The one LLM in the system is the downstream model that consumes the
  assembled window — reached through `POST /v1/chat/completions`, or through
  whatever agent called `/context` in the first place.
- Every turn ends at the same fork: `know { found, confidence, gene_id_match }`
  or `miss { reason, escalate_to }`. See [Agent Contract](Agent-Contract).
- This page documents **v0.11.3**. The wave-1 ranking flip
  ([#407](https://github.com/mbachaud/Cymatix-Context/pull/407)) and the
  `min_delivered_docs` seat floor
  ([#409](https://github.com/mbachaud/Cymatix-Context/pull/409)) shipped in
  v0.9.1 (2026-08-30) and are marked where they appear. No release from
  0.10.0 through 0.11.3 changed a shipped retrieval or delivery default, and
  every 947k release witness in that run reproduced the one before it exactly
  (314/470 delivered, 0/470 per-needle differences; 0.11.0 shipped without a
  witness by owner decision). Everything those releases added to this path is
  opt-in and marked as such below.

![The seven-stage pipeline](assets/pipeline-flow.svg)

## Stage 0 — Classify

- A rule-based classifier assigns the query one of five classes: `arithmetic`,
  `factual`, `procedural`, `multi_hop`, `default`. **Algorithmic** — pattern
  scans over the query string, no model call.
- The class selects the decoder mode for the turn and the per-rule assembly cap
  that bounds how many documents reach the window.
- **Decoder mode is resolved from the class *and* the caller**, not the class
  alone. For the default `generic` caller the modes are `minimal`
  (`arithmetic`), `condensed` (`factual`), `full` (`procedural`, `multi_hop`),
  and none at all for `default`. A `small_moe` caller gets its own slate-shaped
  modes for the same classes. Callers select their branch with
  `caller_model_class` — see [HTTP API](HTTP-API).
- `[classifier] enabled` is the only toggle that ships. Per-class caps and
  decoder hints are code constants, not config, pending
  [#205](https://github.com/mbachaud/Cymatix-Context/issues/205). With the
  classifier disabled, every query takes the global path.
- The 2026-08-06 retrieval-layer ledger graded this stage KEEP-CHEAP:
  recall-identical, and its cap shrinks the splice input for free.

## Stage 1 — Extract

- Heuristic keyword, domain, and entity extraction from the query — the same
  CPU tagger (spaCy NER + EntityRuler + regex) that runs at ingest.
  **Algorithmic.**
- A caller-supplied `session_context` contributes path tokens, which are
  injected into the entity set. Those tokens are what later give
  `/context/packet` its coordinate-confidence signal, so supplying them
  materially changes retrieval — an HTTP A/B that varies `session_context` is
  not a clean ablation of anything else.
- **The one optional model call on this path:** query-intent expansion, gated
  by `[compressor] query_expansion_enabled` (legacy: `[ribosome]`). It fires
  once per novel query string, is LRU-cached, and falls back to the raw query
  on any failure. Set it to `false` for a strictly model-free `/context`.
  `/context/packet` never invokes it — the agent-safe surface is model-free
  end to end.

## Stage 2 — Retrieve

- The whole ranking surface, and **entirely algorithmic**. Candidate signals
  that fire at shipped defaults: FTS5 BM25 over document content, exact and
  prefix tag lookup, `[synonyms]` query expansion, an IDF lexical anchor, a
  filename anchor, harmonic co-activation edges, the cymatics 256-bin term
  spectrum, TCM session drift, ray-trace evidence, and an access-rate
  tiebreaker — plus a `party_id` scoping gate. Detail per signal:
  [Retrieval Dimensions](Retrieval-Dimensions).
- **Opt-in, off at shipped defaults:** BGE-M3 dense recall (`[retrieval]
  dense_embedding_enabled`, off since 2026-08-15), SPLADE sparse expansion
  (`[ingestion] splade_enabled`, off since 2026-08-16), and the Tier-0
  path-key index (`[retrieval] pki_enabled`, off since 2026-08-17). Each flip
  is receipt-gated; the receipts are on
  [Benchmarks and Receipts](Benchmarks-and-Receipts).
- **Harmonic tier on large pools.** When a candidate pool would bind more
  parameters than SQLite allows, the harmonic co-activation tier is skipped
  (one warning per store) rather than failing the query. The experimental
  `[retrieval] harmonic_batching_enabled` (0.10.0,
  [#431](https://github.com/mbachaud/Cymatix-Context/issues/431), default
  `false`) stages the full pool in a connection-local temporary table so the
  tier runs at any size; its default flip waits on the paired gate in
  [#469](https://github.com/mbachaud/Cymatix-Context/issues/469).
- **0.11.2 fix:** the prefix tag tier no longer fails on queries with more
  than 500 terms (SQLite's compound-SELECT limit,
  [#483](https://github.com/mbachaud/Cymatix-Context/pull/483)). At 500 terms
  or fewer the SQL is byte-identical, so ordinary queries cannot change rank.
- **Fusion.** Candidates are ranked by Reciprocal Rank Fusion, the default
  since 2026-07-06 — measured **+12pp gold-document delivery** over the legacy
  additive accumulator on the hardest internal bed (**0.74 vs 0.62**).
  `fusion_mode = "additive"` restores the pre-Stage-3 accumulator and is
  scheduled for condition-gated removal (v(N+2) at earliest, not a calendar
  date).
- **Wave-1 ranking default (0.9.1,
  [#407](https://github.com/mbachaud/Cymatix-Context/pull/407)):** `[retrieval]
  rrf_k` moves **60 → 20**, tightening the rank-position discount so
  high-ranked candidates from any one signal survive fusion. The cross-shard
  merge constant deliberately stays at 60 — that surface is unmeasured and is
  annotated as such in the code.

## Stage 3 — Re-rank

- **Optional, and off.** Two different things are called "rerank" here, and
  only the first involves a model:
  1. **Cross-encoder rerank** — a CPU cross-encoder (`ms-marco-MiniLM-L-6-v2`)
     scores the candidate list. `[retrieval] rerank_enabled` has **always
     shipped `false`** ([#341](https://github.com/mbachaud/Cymatix-Context/issues/341));
     with it off, this stage is skipped entirely. This is the pipeline's only
     retrieval-path model stage.
  2. **Post-fusion rerank classes** — four algorithmic signals (authority,
     `sema_boost`, party attribution, access rate) that combine with the fused
     score through a per-class combinator. `eps_band` lets them break ties only
     inside a relative band of the leader's fused score, so they cannot
     overturn a decisive fusion result.
- **Wave-1 combinator default (0.9.1,
  [#407](https://github.com/mbachaud/Cymatix-Context/pull/407)):**
  `rerank_combinator_by_class` extends from `{multi_hop, default}` to **all
  five classifier classes**, all mapped to `eps_band`.
- **What wave-1 measured**, on the 829k bed at full needle power (n=470):
  gold-document delivery **0.555 → 0.630**, recall@12 **0.651 → 0.681**, median
  gold rank **3 → 2**, and **zero question-type regressions**. Both halves of
  the flip ship as a single commit, so the whole thing reverts with one
  `git revert`.

## Stage 4 — Splice

- **Optional compression, and off at shipped defaults.** Each surviving
  candidate is compressed so more documents fit the same token budget —
  Headroom Kompress (CPU, ModernBERT-class) for prose and code, or an LLM
  compressor when one is configured.
- `[compressor] enabled` (legacy: `[ribosome]`) defaults `false`, and the
  backend string is ignored entirely while it is false. When enabled, only
  `"litellm"` and `"deberta"` are honored; any other value — including the
  legacy `"ollama"` and `"claude"` — dispatches the no-op DisabledBackend.
- Splice is where latency concentrates when it is on: the 2026-08-06
  retrieval-layer ledger attributed the p95 tail to this stage in every arm it
  measured, superlinear in candidate count. That is a dated finding on a 100k
  bed, not a current shipped-defaults measurement — with splice off there is no
  such stage to pay for.

## Stage 5 — Assemble (and the Stage 7 freshness gate)

- Joins the surviving parts, enforces the token budget (`[budget]
  retrieval_tokens`, legacy `expression_tokens`, default **7000**) and the
  per-turn document cap (`max_docs_per_turn`, legacy `max_genes_per_turn`,
  default **12**). **Algorithmic.**
- **Legibility headers** (`legibility_enabled = true`) attach one metadata line
  per document: which tiers fired, a confidence marker, the short document id,
  and the raw-to-compressed character counts. Flip to `false` to restore plain
  dividers for a bench A/B.
- **Freshness gate (Stage 7)** runs during assembly and demotes stale, cold, or
  superseded documents — downgrading the response to
  `miss(reason="stale" | "cold" | "superseded")`. It is the reason a document
  can be retrieved and still not be answerable from.
- **Freshness basis (0.11.2, opt-in).** The default `[context] freshness_basis
  = "clock"` is age-since-verification decay, so a static store turns
  all-stale about 15 days after ingest, and a source this machine cannot find
  reads as stale. `"source"` checks the source instead: unchanged on disk since
  verification = verified, changed = needs refresh, not on this disk =
  unknown (`stale_risk` for ordinary tasks) rather than stale. On the ERB 947k
  bed the clock basis forced 470/470 queries to `miss(stale)` before know
  confidence was read.
- **Session delivery register** (`session_delivery_enabled = true`) tracks what
  each session has already received and elides repeats behind a pointer stub.
  The multi-turn savings figure (~40%) is an **unverified design estimate**
  pending the `cymatix_session_tokens_saved_total` counter. Pass
  `ignore_delivered: true` in the `/context` body for benchmarks.
- **Abstain tier.** When post-refinement retrieval is weak on both the absolute
  score and the score ratio, assembly returns a marker-only window rather than
  a broad fallback — the store says "I can't help" instead of spending tokens
  proving it. Under RRF these gates run ratio-only; the absolute floors were
  calibrated against additive fusion.
- **Control-tag neutralization** (`neutralize_control_tags = true`, default on
  since 2026-08-19) escapes a literal `<cymatix:` in retrieved content so an
  ingested document cannot forge the genuine no-match control tags.
- **0.9.1 seat floor (`[budget] min_delivered_docs`,
  [#409](https://github.com/mbachaud/Cymatix-Context/pull/409), default
  **12** since 2026-08-30; `0` = legacy eviction-only trim).** The floor lifts
  the classifier's per-rule assembly cap to at least N and truncates the
  largest parts instead of evicting whole documents. Measured at floor 12:
  delivered **0.630 → 0.668** (**+18/−0** needles), per-type completeness
  **0.65 → 0.85**, the constrained slice **0.80 → 0.97**; graduated on three
  corpora with zero paired delivered losses and the ranking bases untouched.
  The receipts grade delivery, not answer quality under a widened seat count —
  that lane is still owed.
- **The floor does not reach the TIGHT and FOCUSED budget-tier cuts** at
  shipped defaults: about a third of ERB queries still deliver 6 seats
  ([#430](https://github.com/mbachaud/Cymatix-Context/issues/430)). The
  experimental `[budget] tier_seat_floor_enabled` (0.10.0, default `false`)
  applies the floor to those cuts too, keeping tier labels and the final
  token budget.
- **Other 0.10.0 opt-ins at this stage**, all default off and all gated on
  [#469](https://github.com/mbachaud/Cymatix-Context/issues/469):
  - `[budget] full_text_delivery` delivers complete stored bodies plus up to
    `companion_chunks` (default 4) question-ranked chunks from sources already
    selected. Primary ranking is untouched; packets expose the supplements in
    `companions`. It uses more downstream tokens: on the 947k bed a 12 + 4
    packet averaged 15,917 tokens against 8,345 compressed.
  - `[budget] wire_format = "canonical"` writes `<DOCUMENT>` blocks,
    `[document=…]` headers and canonical JSON keys instead of the legacy
    `<GENE>` vocabulary. See [Lexicon](Lexicon).

## Stage 6 — Persist

- The query-and-response exchange is packed back into the knowledge store in
  the background, off the response path. **Algorithmic** (the CPU tagger path).
- This is what makes the store grow with use, and why co-activation edges
  strengthen between documents that keep getting retrieved together.
- **Not on the per-turn path:** `[sync]` (0.11.0, default off) runs its own
  in-process pass over tracked folders, re-ingesting changed files and
  tombstoning the stale chunks of changed or deleted ones. A store marked
  Freeze (0.11.2) opens read-only: its knowledge content is not written,
  including by queries. See [Architecture Map](Architecture-Map).

## After the pipeline: the packet trace (opt-in)

- Every `/context` and `/context/packet` response carries a ULID `packet_id`
  (body field and `X-Cymatix-Packet-Id` header) since 0.11.2, whether tracing
  is on or not.
- With `[trace] enabled = true` (default **false**,
  [#493](https://github.com/mbachaud/Cymatix-Context/issues/493)), the HTTP
  route writes one record per request **after** assembly and the know/miss
  decision: delivered chunk ids, keyed hashes, ranks, scores, the verdict,
  and a replay fingerprint. It reads what the pipeline already produced and
  changes nothing it delivers; the 0.11.3 witness with tracing on reproduced
  exactly. Cost at 947k is under 1 ms per request since 0.11.3. Details:
  [Observability](Observability).

## Where the model boundary actually sits

| Stage | Algorithmic | Model-capable | Ships on? |
|---|---|---|---|
| 0 Classify | yes | — | on |
| 1 Extract | yes | query expansion only, flag-gated | on (expansion opt-in) |
| 2 Retrieve | yes | — | on |
| 3 Re-rank | post-fusion classes | cross-encoder | **off** |
| 4 Splice | Headroom Kompress (CPU) | LLM compressor | **off** |
| 5 Assemble + 7 Freshness | yes | — | on |
| 6 Persist | yes | — | on (background) |

- The honest one-line version: *ingest → tag → encode → store → query →
  retrieve → weigh → label → emit* is all CPU; the pipeline crosses the model
  boundary exactly once, at answer generation, and that call is the caller's.
- Two opt-in know/miss inputs added in 0.11.2 sit outside this table because
  they run after ranking: `[know] model = "lanes"` is algorithmic, and
  `[know] lanes_ce_model` (empty = off) scores the fused top-1 and top-2
  through the cross-encoder, a model call when it is set. Neither changes
  ranking or delivery; both feed only the know confidence. See
  [Agent Contract](Agent-Contract).

## The four entry flows share these stages

| Endpoint | What it runs | What it returns |
|---|---|---|
| `POST /context` | Stages 0–7, full path | Assembled context + citations + know/miss |
| `POST /context/packet` | Stages 1–2 + the weighing layer; skips splice | Pointers labelled `verified` / `stale_risk` / `needs_refresh`, plus `refresh_targets` — no assembled content |
| `POST /context/refresh-plan` | Packet internals, filtered | `refresh_targets` only — the reread plan |
| `POST /fingerprint` | Stages 1–2 + refiner pass | Scored document pointers, tier contributions, and honest floor accounting — no content, no packet labels |

- The packet path is the **agent-safe** surface: model-free end to end, and it
  returns what to reread rather than a bundle to trust. Full request and
  response shapes: [HTTP API](HTTP-API).

## Go deeper

- [`docs/architecture/PIPELINE_LANES.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/PIPELINE_LANES.md) — the swim-lane reference: who writes what, which table each stage touches, the four flows in full
- [`docs/architecture/DIMENSIONS.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/architecture/DIMENSIONS.md) — the formal signal inventory behind Stage 2
- [`docs/api/context-endpoint.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/api/context-endpoint.md) — request and response fields for `/context` and `/context/packet`
- [`docs/specs/packet-trace-v0.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/specs/packet-trace-v0.md) — the packet trace record, levels, and read endpoints
- [`docs/benchmarks/2026-08-06-retrieval-layer-ledger.md`](https://github.com/mbachaud/Cymatix-Context/blob/master/docs/benchmarks/2026-08-06-retrieval-layer-ledger.md) — the dated cost/impact ledger this page cites for splice and classifier
- Next: [Retrieval Dimensions](Retrieval-Dimensions) · [Agent Contract](Agent-Contract) · [Configuration](Configuration) · [Benchmarks and Receipts](Benchmarks-and-Receipts)
