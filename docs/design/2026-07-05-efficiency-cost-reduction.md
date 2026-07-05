# Efficiency & Cost-Reduction Program (tokens · compute · disk)

> 2026-07-05. Design memo synthesizing three questions asked during the roadmap
> session: (1) binary vs JSON storage, (2) algorithm vs embedded model, (3) MCP
> per-prompt token cost. Grounded in the live code (paths + line numbers below),
> cross-checked against `config.py`/`helix.toml` shipped defaults, and tied to the
> existing roadmap issues that already own slices of this work.
>
> **Through-line.** Helix's stated thesis is *AI is the first-class user* and
> *local-first, CPU-cheap, no-neural-at-query-time*. The cheaper Helix is in
> tokens + compute + disk, the more adoption rises — and accuracy is the other
> adoption driver. The three questions are really one program: **stop paying for
> capability that isn't earning its cost on the default path.**

---

## TL;DR — the highest-leverage, lowest-risk wins first

| # | Win | Effort | Impact | Risk | Owner issue |
|---|-----|--------|--------|------|-------------|
| 1 | **Lean MCP profile** — expose ~4 core tools by default, gate the other ~20 behind `HELIX_MCP_FULL=1` | S | **−3 to −4K tokens *every agent turn*** | low | #219 Slice 3 |
| 2 | **Drop the 4 back-compat alias tools** (`helix_document_*`) from the MCP surface | S | −~600–800 tokens/turn, less agent confusion | low | #219 Slice 3 |
| 3 | **Flip `splade_auto_disable_above_genes` to a positive threshold** (e.g. 200 000) | S | reclaims the measured **9.96 GB / 21.1 % of disk** SPLADE cliff at enterprise scale, zero code | low | #204 |
| 4 | **Pack `genes.embedding` (20-d SEMA vector) as a float32/float16 BLOB** | S | **~5× (fp32) / ~10× (fp16)** on that column | low | new |
| 5 | **Ship a documented "algorithmic profile"** — config flip that removes all 3 encoder loads | S–M | **−~2.6 GB resident + kills the torch cold-start** on that path | med (recall delta unmeasured) | #205 |
| 6 | **Compact `/context` + `/context/packet` response mode** — cap/elide legibility headers | M | cuts the *dynamic* per-response token cost (compounds with session-delivery elision) | low | #219 Slice 3 |

Items 1–4 are near-free and should land first. Item 5 needs a bench A/B before it
becomes a *default*, but is shippable as an opt-in low-resource profile today.

---

## Q3 first (it's the cheapest, biggest, most-requested win): MCP per-prompt token cost

**Where the cost is.** `helix_context/mcp/mcp_server.py` registers **24 FastMCP
tools** (`@mcp.tool()` at lines 341–963). A host like Claude Code injects every
tool's name + description (the docstring) + JSON input schema into the model
context **on every single turn**. Issue #219 already measured this surface at
**~4–5K schema tokens per agent session** — i.e. the 2–3K the question worried
about is if anything an under-count.

**The 24 tools, by need:**

- **Core agent loop (keep default, ~4):** `helix_context`, `helix_context_packet`,
  `helix_health`, `helix_ingest`. (Arguably `helix_refresh_targets`,
  `helix_sessions_list` for multi-agent awareness.)
- **Back-compat alias duplicates (drop from MCP, ~4):** `helix_document_get`,
  `helix_document_query`, `helix_document_preview`, `helix_document_fingerprint`
  mirror the gene/context tools 1:1 (the biology→software rename in ROSETTA).
  They cost schema tokens *and* make the agent pick between synonyms. Keep them in
  CLI/HTTP; the MCP surface does not need both vocabularies.
- **Admin / diagnostic (gate behind a flag, ~16):** `helix_swap_db`,
  `helix_announce`, `helix_consolidate`, `helix_stats`, `helix_metrics_tokens`,
  `helix_bridge_status`, `helix_hitl_emit`, `helix_hitl_recent`, `helix_resonance`,
  `helix_gene_get`, `helix_neighbors`, `helix_splice_preview`, `helix_fingerprint`,
  `helix_session_recent`, … — rarely needed inline; an operator opts in.

**Recommendations (all land under #219 Slice 3, "serve-lean MCP profile"):**

1. **Profile-gate the surface.** Default = the ~4 core tools. `HELIX_MCP_FULL=1`
   (or a `[mcp] profile = "full"` key) restores all 24. Est. **−60–80 % of the
   4–5K = ~3–3.5K tokens saved per session**, before any retrieval.
2. **Remove the 4 `helix_document_*` aliases** from the MCP registration entirely
   (they remain reachable via HTTP/CLI).
3. **Tighten docstrings.** Each tool's docstring *is* its description. Trim to one
   crisp line + param docs; a 100-token docstring × 24 ≈ 2.4K, halving them ≈ −1.2K
   even on the full surface.
4. **Dynamic side — compact response mode.** `/context` and `/context/packet`
   payloads carry per-document legibility headers (fired tiers, confidence marker,
   compression ratio) + the know-block. Offer `compact: true` that caps headers to
   the confidence marker + gene-id and elides the ratio/tier detail. This compounds
   with the existing session-delivery elision (already ~40 % on multi-turn).

**Why this matters for the first-class-AI-user thesis:** every token the tool
surface spends is a token the agent can't spend on the task. A 4-tool contract
that an agent reads once and trusts beats a 24-tool buffet it re-parses each turn.

---

## Q2: algorithm instead of an embedded model — *mostly already done, and under-shipped*

**Key finding (verified against shipped defaults, corrects CLAUDE.md).** The
pipeline is **already almost entirely algorithmic**:

- **Rerank and splice need no model.** `scoring/cymatics.py` (256-bin MD5-hash
  spectral scoring + cosine/W1) already replaces the cross-encoder rerank
  (`resonance_rank`) and the LLM splice (`interference_trim`), both default-on and
  numpy-optional. The cross-encoder (`rerank_enabled=False`), the compressor LLM
  (`ribosome.enabled=False` / `backend="none"`), DeBERTa, and NLI are all **off by
  default**. Cymatics *is* the shipped "algorithm not model" template — reuse its
  hash-spectrum machinery for any new substitute rather than inventing math.
- **The query classifier, intent router, freshness gate, RRF/additive fusion,
  tie-break, and `/context/expand` graph walk are already pure algorithms.**

**What still loads transformer weights on the default path — and it's more than the
docs claim.** Verified in `config.py` + `helix.toml`:

| Encoder | Shipped default | Cost | CLAUDE.md says |
|---|---|---|---|
| BGE-M3 dense (`dense_embedding_enabled`) | **True** (config.py:355) | ~2 GB | "default off" ❌ stale |
| SPLADE (`splade_enabled`) | **True** (config.py:214) | ~500 MB | "default off" ❌ stale |
| SEMA/all-MiniLM (`sema_embed_on_ingest`) | **True** (config.py:234) | ~90 MB | — |

So the "no neural inference at query time" narrative is **not true of the current
default** — a docs-honesty gap (also on the roadmap docs-refresh list). That makes
the algorithmic route a real, *unshipped* opportunity, not a description of today.

**Is it possible? Yes, partial — with an honest accuracy caveat.** For each encoder
there is a corpus-derived algorithmic substitute that removes the model load
entirely:

| Model | Algorithmic substitute | Keeps | Loses |
|---|---|---|---|
| BGE-M3 dense | **Random Indexing** (Kanerva ternary random-projection, numpy-only) or **TruncatedSVD/LSA** over the term–doc matrix, stored where `embedding_dense_v2` lives | distributional synonymy ("shutdown"~"stop server"), no pretrained weights | true out-of-corpus paraphrase; quality scales with corpus size |
| SPLADE | **BM25 + PMI co-occurrence expansion** mined offline (reuses `[synonyms]`, co-activation, entity graph, and the existing `splade_terms` inverted-index contract) | "findable by related vocabulary" | context-sensitive per-occurrence weighting |
| SEMA/MiniLM (20-d primes) | **keyword-lexicon per prime** *or* a **cymatics prime-spectrum** via `resonance_score` (reuses hash machinery; #227 already added the text-fallback seam) | coarse 20-d structure TCM/cold-tier consume | MiniLM paraphrase sensitivity |
| NLI/DeBERTa (coherence) | **do not** approximate — gate off | — | (already off; entailment is the one place a model decisively beats any algorithm — keep it honest, drop the signal rather than ship a weak heuristic) |

**Important correction to a tempting wrong answer:** LSH / SimHash / MinHash / SRP
are *approximate-NN indexes over vectors you already have* — they do **not**
substitute an embedding model, and MinHash/SimHash collapse to lexical/set
similarity FTS5 already provides. The genuine model-free semantic route is
**corpus-derived** (Random Indexing / LSA / PMI), which needs enough corpus to be
meaningful and degrades on tiny genomes.

**Recommendations:**

1. **Ship the "algorithmic profile" now as an opt-in** (item 5): a documented config
   set that flips the three encoders + LLM stages off, leaving cymatics + FTS5/BM25
   + synonyms + co-activation + classifier. **Zero code, removes ~2.6 GB resident
   and the entire torch import from the query path** (directly relevant to the
   documented daemon RAM ramp). This is also the honest baseline to measure every
   substitute against.
2. **Measure before making it the default.** A/B the algorithmic profile vs the
   current default on the 50-needle matrix (`benchmarks/bench_claude_matrix.py`).
   That number decides "shipped default" vs "low-resource opt-in." (Blocked on the
   #221 clean bench beds — don't measure on contaminated data.)
3. **Then invest in substitutes in priority order:** SEMA→cymatics-prime (cheapest,
   removes the last sentence-transformers dep at ingest) → SPLADE→PMI (removes
   ~500 MB + both forward passes) → BGE-M3→Random-Indexing (biggest, removes ~2 GB).

---

## Q1: binary instead of JSON storage — *one clean win, and the real cliffs are elsewhere*

**Key finding.** Helix already does the right thing for its **biggest** vector:
BGE-M3 `embedding_dense_v2` is a **packed float32 BLOB** (`storage/ddl.py:113`,
`backends/bgem3_codec.py:37-52`). And the premise's headline example doesn't apply:
the **256-bin cymatics spectrum is never persisted** — it's computed on the fly and
LRU-cached in-process (`scoring/cymatics.py:227-260`). No DB column, no disk cost.

**What *is* still JSON-of-floats text:** the **20-dim SEMA `genes.embedding`
column** (`storage/ddl.py:68` = `TEXT -- JSON list[float]`, written via
`json_dumps` at `knowledge_store.py:1381`). Measured live: **~403–411 bytes/row**
for 20 numbers, vs **80 bytes** as a float32 BLOB — a **measured 5×** (10× at fp16).

**But the largest *measured* storage cliffs are not JSON-vs-binary at all** — they're
relational-row-explosion, and the roadmap already has issues on them:

- **`splade_terms`** (one TEXT row per sparse term + two secondary indexes): **21.1 %
  of disk / 9.96 GB** at 850K genes for **0 pp recall@10** (#164). The cheapest fix
  is not a repack — it's flipping `splade_auto_disable_above_genes` off `0` (item 3).
- **`path_key_index`**: **34.1 % / ~19 KB/gene** (#165).
- Both are dominated by the **16-hex-char `gene_id` TEXT foreign key** repeated
  across ~8 auxiliary tables. `gene_id = sha256(content).hexdigest()[:16]` = 8 raw
  bytes wearing a 2×-inflated text costume (~17 B/occurrence TEXT vs ~9 B BLOB(8)).

**Recommendations (in ascending effort/risk):**

1. **`genes.embedding` → float32 (or fp16) BLOB** (item 4). Reuse the exact
   `vec_to_blob`/`frombuffer` pattern already shipped for BGE-M3; add `embedding_v2
   BLOB` with a dual-column read fallback, exactly like the `embedding_dense →
   embedding_dense_v2` rollout. Precision loss is immaterial (feeds cosine ranking
   only). **S effort, ~5×, low risk.** Read paths to update: `knowledge_store.py`
   :849, :918, :2406, :3442, :4487.
2. **Delete the dead `embedding_dense` TEXT column** (`ddl.py:82,109`) — confirmed
   unwritten today; costs ~0 bytes (NULL) but is dead schema weight. Cleanup migration.
3. **`splade_terms` BLOB repack** (uint16 term-id into a shared vocab table + int8/fp16
   weight ≈ 4 B/term → ~512 B/gene at top_k=128): **L effort, 10–20× on that table,
   med-high risk** (moves the SQL-indexed term join to app-side dot-products). Only
   worth it *if* SPLADE stays on at scale — but item 3 argues it shouldn't. **Prefer
   the config flip over the repack.**
4. **`gene_id` TEXT→BLOB(8)** across ~8 tables: **XL effort, ~1.9× compounding on the
   two biggest cliffs, highest risk** — breaks everything that pattern-matches hex
   gene-ids (`GET /genes/{gene_id}`, MCP surface, vault export, logs). Needs a
   hex↔blob adapter at every API/CLI boundary to keep the public contract. **Not this
   pass; scope it deliberately if disk becomes the binding constraint.**

**Caveat on measurement:** the 21.1 %/34.1 % figures are from the #164/#165 850K-gene
corpus; the local dev genome (756 genes, splade_terms 80:1 over genes) is *not*
representative — don't extrapolate the local ratio to 15K–80K production beds.

---

## How the three compound

- **Binary storage + algorithmic profile** both shrink disk *and* cold-start: item 3
  (SPLADE off at scale) reclaims 9.96 GB *and* removes the ~500 MB model; item 5
  removes ~2.6 GB resident.
- **Lean MCP + compact responses** cut per-turn *and* per-response tokens, and a
  4-tool contract makes the **know/miss agent contract** (the thing #239 is fixing)
  tighter — fewer tools to reason about, one confidence signal to trust.
- **The honest default:** items 1–5 also close a docs-honesty gap — the shipped
  defaults (encoders ON, SPLADE cliff live, MCP surface at 24 tools) don't match the
  "cheap, local-first, no-neural" story the README tells. Fixing the defaults *and*
  the docs is the same move.

## Sequenced action list

1. **Now (near-free):** items 1, 2 (#219 Slice 3), item 3 (#204), item 4 (new issue).
2. **After #221 clean beds:** A/B the algorithmic profile (item 5, #205) and record
   the recall delta; recalibrate the know contract (#239) on the same clean data.
3. **Deliberate, later:** compact response mode (item 6), `splade_terms`/`gene_id`
   binary repacks — only if the config-level wins prove insufficient.

## Open questions (need data, not opinion)

- Measured recall/answer-quality delta between the current default (3 encoders on)
  and the fully algorithmic profile, on the 50-needle matrix. Decides default vs opt-in.
- Is the BGE-M3 dense leg actually firing on real agent traffic, or are
  BM25+cymatics+synonyms already carrying most queries? (Telemetry on dense-hit
  contribution would size the prize before building a substitute.)
- Typical target-genome size — Random Indexing/LSA/PMI need enough corpus.
