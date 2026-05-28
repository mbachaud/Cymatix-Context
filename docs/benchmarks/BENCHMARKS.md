# Helix Context Benchmarks

**Last updated:** 2026-05-28 *(Layer 3 / EnterpriseRAG-Bench added; Layer 1+2 figures unchanged since 2026-04-10)*

Helix solves a specific problem: **agents drowning in RAG search context.** A typical RAG
pipeline dumps 15-50 kilobytes of candidate chunks into the prompt per turn and hopes the
model can find the needle. Helix instead selects, compresses, and retrieves a minimal
window — then measures whether the answer survives.

This document describes the two benchmark layers, their methodology, and the current
results. All scripts live in `benchmarks/` and are reproducible with a pinned knowledge store
snapshot.

## Why two layers?

Benchmark results without a clear methodology are marketing. Helix reports two separate
scoreboards so you can see the ceiling AND the floor:

| Layer | Methodology | Question it answers |
|---|---|---|
| **Layer 1 — SIKE (curated)** | Hand-written needles with unambiguous phrasing | *Can retrieval scale invariantly across model sizes when queries are clear?* |
| **Layer 2 — KV-harvest (synthetic)** | Auto-generated from knowledge store KV facts, stratified by source | *What is the floor on noisy, real-world retrieval?* |

**A well-phrased question finds its answer across every model (Layer 1 = 10/10).** A
random KV fact template finds it ~45% of the time regardless of model size (Layer 2 =
floor). The truth for a real application lives between those bands.

## How little we inject

The single most important number Helix can report is the **average injected token count
per turn.** RAG pipelines compete on retrieval quality at the cost of prompt bloat. Helix
competes on *how much we can remove while keeping the answer*.

| Pipeline | Tokens injected per turn | Retrieval strategy |
|---|---:|---|
| Naive "stuff the context" RAG | 25,000 – 50,000 | Top-k chunks, no compression |
| Long-context API baseline | up to 128,000 – 1,000,000 | Full document dump |
| **Helix static (12 documents × 1000 char)** | **~15,000** | Selective retrieval |
| **Helix dynamic (3-tier budget)** | **~6,000 – 15,000** | Confidence-based tier |
| **Helix + Headroom (CPU codec)** | **~400** | Semantic compression over Helix output |

The Headroom integration pushes the injected-token count down by another order of
magnitude on top of Helix's already-compressed output. At 400 tokens/turn on a 128K
context window, **99.7% of the window stays free for the actual conversation** — that is
the "not drowning in RAG" property.

---

## Layer 1 — SIKE (Scale-Invariant Knowledge Engine)

**Script:** `benchmarks/bench_needle.py`
**Methodology:** Hand-written needles targeting specific, unambiguous project facts.
**Purpose:** Establish that retrieval quality is **scale-invariant** — a tiny model with
Helix should find the same answer as a frontier model with Helix.

### Needles

Hand-crafted questions, each targeting a literal fact in the knowledge store.
The original N=10 set was expanded to **N=50** in PR `feat/bench-needles-50`
(2026-05-15) to drive down the per-fixture correctness standard error from
~1.15-stddev (too noisy for single-knob tuning) to ~0.5-stddev. Coverage is
distributed across the 6 shards of the medium-sharded fixture roughly in
proportion to corpus size (Education × 16, helix-context × 14, BookKeeper × 7,
CosmicTasha × 5, two-brain-audit × 5, MaxExpressKit × 3).

Sample of the original N=10 (full list lives in `benchmarks/bench_needle.py`
and `benchmarks/bench_claude_matrix.py`; per-needle curation rationale is in
[`MULTI_VALID_GOLD.md`](MULTI_VALID_GOLD.md)):

```
What port does the Helix proxy server listen on?           → 11437
What is the ScoreRift divergence threshold?                → 0.15
How many skills does the BigEd fleet have?                 → 125
What type should BookKeeper use for monetary values?       → Decimal
How many steps are in the Helix expression pipeline?       → 6
What is the binary size of the Rust BigEd build in MB?     → 11
What is the target compression ratio for Helix Context?    → 5x
How many dimensions does the Python preset check?          → 8
How many tokens for the ribosome decoder prompt?           → 3000
What is the default local model for BigEd conductor?       → qwen3
```

Representative N=50 expansion entries (one per shard):

```
What port does the BookKeeper web dashboard listen on?     → 8080
What is the default Ollama model tag for CosmicTasha?      → qwen3:8b
How many defense layers does ScoreRift use?                → 6
What is the current version of MaxExpressKit?              → 0.1.3
What RAM ceiling % does BigEd target before scale-up stops?→ 97
What is the expression_tokens budget in helix.toml?        → 7000
```

> **Note on the legacy N=10 results table below.** All historical run
> data through 2026-05-12 used the 10-needle set. The N=50 run grid will
> re-baseline in the next round; treat the older numbers as a 10-needle
> reference until that lands.

### Results (N=10, curated)

| Model | VRAM / Tier | Retrieval | Accuracy |
|---|---|---:|---:|
| qwen3:0.6b | 0.5 GB | 10/10 | 2/10 |
| qwen3:1.7b | 1.4 GB | 10/10 | 3/10 |
| qwen3:4b | 2.5 GB | 10/10 | 9/10 |
| gemma4:e2b (MoE) | 7.2 GB | 10/10 | 5/10 |
| gemma4:e4b (MoE) | 9.6 GB | 10/10 | 9/10 |
| qwen3:8b | 5.2 GB | 10/10 | 9/10 |
| gemma4:26b-a4b (MoE + DDR4 offload) | 8 GB + 13 GB RAM | 10/10 | 6/10 |
| Claude Haiku 4.5 + Helix | API | 10/10 | 10/10 |
| Claude Sonnet 4.6 + Helix | API | 10/10 | 10/10 |
| Claude Opus 4.6 + Helix | API | 10/10 | 10/10 |

**Finding:** Retrieval is perfect across 43x parameter range (0.6B → 26B). The
correlation between model size and retrieval quality is zero. Accuracy is bounded by
extraction ability, not retrieval.

### How to reproduce

```bash
# Start Helix proxy
python -m helix_context.server &

# Run local model
HELIX_MODEL=qwen3:4b python benchmarks/bench_needle.py

# Run Claude API tiers (requires Claude Code agent dispatch)
# See docs/RESEARCH.md for the sub-agent harness
```

---

## Layer 2 — KV-Harvest (synthetic floor)

**Script:** `benchmarks/bench_needle_1000.py`
**Methodology:** Stratified random sampling of pre-extracted key-value facts from the
knowledge store's `key_values` column. Each fact becomes a template query.
**Purpose:** Establish the **floor** on noisy, synthetic queries — the stress test.

### Generation

1. Load all documents with non-empty `key_values` from the knowledge store snapshot
2. Filter to quality KVs (value is meaningful, globally unique, literally in content)
3. Stratify sample across 7 source categories (`steam`, `education_public`, `helix`,
   `cosmic`, `tally`, `scorerift`, `other`) with a noise-weighted mix
4. Build template queries (`"What is the value of {key}?"`)
5. Seed `random.Random(42)` for reproducibility

### Harness versions

The harvest logic is versioned. Every run's result JSON stamps `harness_version`
so older runs remain identifiable after filter changes.

| Version | Date | Key changes |
|---|---|---|
| **v1** | pre-2026-04-10 | Original filter: length bounds, generic-type blacklist, value-appears-in-content sanity check, substring retrieval match |
| **v2** | 2026-04-10 | Rejects dotted Python identifier chains (`os.path.join`), function-call shapes (`foo(bar)`), single plain English words (TitleCase/lowercase/short acronyms); expanded prose-key blacklist (`note`, `description`, `comment`, `text`, …); requires value to appear in an assignment-context window near the key; word-boundary-aware retrieval match |

v2 was triggered by raude's forensic on the N=20 Headroom A/B run: three
"failures" turned out to be harness bugs (docstring fragments harvested as
"values", function-call retrievals captured verbatim, substring-matched
retrieval). See `tests/test_bench_harvest.py` for the pinned cases.

**v1 → v2 audit on `genome-bench-2026-04-10.db`:**

| Metric | v1 | v2 | Delta |
|---|---:|---:|---:|
| Raw quality KVs | 40,740 | 16,428 | −60% |
| Globally-unique KVs | 20,698 | 8,071 | −61% |
| Full candidate pool (post content-sanity) | 25,382 | 9,891 | −61% |

**~60% of the v1 pool was phantom-contaminated.** v2 needles on the same seed
are nearly disjoint from v1 needles (2/200 overlap in a control audit). The
raw retrieval/answer numbers also drop under v2 — not because the system got
worse, but because the easy wins from substring-matching docstring phrases
are gone. The v2 floor is a truer measurement.

Opt into legacy v1 behavior with `BENCH_LEGACY_HARVEST=1` for reproduction of
older runs.

### Stratification (targets; actual bucket size depends on pool depth)

```
education_public  30%   (largest signal source — BigEd fleet public repo)
steam             25%   (noise stress test — Hades/BeamNG/Factorio files)
helix             15%   (self-knowledge — Helix code and docs)
cosmic            12%   (private repo — CosmicTasha)
tally              8%   (private repo — financial ledger)
scorerift          5%   (public repo — audit tool)
other              5%
```

### KnowledgeStore snapshot

All Layer 2 runs use a pinned snapshot for reproducibility:

```
benchmarks/... uses GENOME_DB=F:/Projects/helix-context/genome-bench-2026-04-10.db
Snapshot at: 7,313 genes (subset of live 7,990+)
Size: 557 MB uncompressed, frozen at 2026-04-10 00:52
```

### Dynamic budget tiers

After the first static N=50 run showed 91% padding waste, Helix added confidence-based
tiers (commit in `context_manager.py`):

| Tier | Trigger (top_score / mean_score) | Documents | Est. tokens |
|---|---|---:|---:|
| **tight** | ratio ≥ 3.0 | 3 | ~6,000 |
| **focused** | ratio 1.8 – 3.0 | 6 | ~9,000 |
| **broad** (default) | ratio < 1.8 | up to 12 | ~15,000 |

A 4th "desperate" tier (ratio < 1.0, 18 documents, ~17K tokens) is designed but not shipped.

### Run history

| # | N | Model | Harness | Headroom | Retr | Accuracy | Time | proxy p50 | Notes |
|---|---:|---|---|---|---:|---:|---:|---:|---|
| 1 | 20 | qwen3:4b | v1 | truncation | 55% | 35% | 10.4 min | 26.8s | ⚠ dual-load (e4b + qwen3:4b in VRAM) |
| 2 | 50 | qwen3:4b | v1 | truncation | 58% | 28% | 20.1 min | 20.3s | ⚠ dual-load (same) |
| 3 | 20 | qwen3:4b | v1 | truncation | 45% | 30% | 5.0 min | 6.6s | clean VRAM, e4b unloaded |
| 4 | 20 | qwen3:8b | v1 | truncation | 45% | 35% | 1.4 min | 3.2s | clean VRAM |
| 5 | 50 | qwen3:8b | v1 | truncation | 44% | 28% | 6.5 min | 4.4s | clean VRAM, reference baseline |
| 6 | 20 | qwen3:8b | v1 | Headroom (Kompress) | 45% | 30% | 3.4 min | 4.7s | **avg injected: 399 tokens** (raude's A/B) |
| 7 | 20 | qwen3:8b | **v2** | Headroom | 20% | 20% | 3.5 min | 8.1s | v2 first run; small-N noise |
| 8 | 50 | qwen3:8b | **v1 legacy** | Headroom | **38.0%** | **28.0%** | 6.0 min | 4.9s | **v1-vs-v2 reference baseline** (clean triple) |
| 9 | 50 | qwen3:8b | **v2** | disabled | **16.0%** | **12.0%** | 14.1 min | 8.2s | **v2 honest floor — raw content** |
| 10 | 50 | qwen3:8b | **v2** | Headroom | **18.0%** | **14.0%** | 12.8 min | 7.8s | **v2 Headroom A/B** |
| 11 | 50 | qwen3:8b | **v2 post-B/C** | Headroom | **20.0%** | **16.0%** | 10.4 min | 6.6s | **post-recovery hot-only — Struggle 1 +4pp** |
| 12 | 50 | qwen3:8b | **v2 post-B/C** | Headroom + cold | **20.0%** | **16.0%** | 12.3 min | 6.6s | **hot+cold (96% fire) — net 0 vs hot-only** |

**⚠ Dual-load warning (runs #1 and #2):** During the initial static-budget runs the
GPU held both `gemma4:e4b` (compressor, 3.6 GB) and `qwen3:4b` (downstream, 3.7 GB)
simultaneously, putting the 3080 Ti at 10.9/12.0 GB VRAM with thermal pressure.
Subsequent runs unloaded the compressor via `/admin/ribosome/pause` before execution.

### The v1 vs v2 harness delta (headline finding)

Runs #8, #9, #10 are a single controlled triple: same N (50), same seed (42),
same model (qwen3:8b), same frozen knowledge store snapshot, same compressor-paused server
state. Only the harness version and the Headroom toggle vary.

| Variant | Retrieval | Answer | Δ vs v1 baseline |
|---|---:|---:|---|
| v1 legacy + Headroom (run #8, reference) | 38.0% | 28.0% | — |
| v2 + Headroom (run #10) | 18.0% | 14.0% | **−20pp retr, −14pp ans** |
| v2 + no Headroom (run #9) | 16.0% | 12.0% | −22pp retr, −16pp ans |

**The v1 → v2 drop is not a regression.** It is the direct measurement of how
much v1 was being inflated by phantom KVs matching docstring substrings during
the retrieval check. v2 rejects ~61% of the v1 candidate pool at harvest time,
and enforces word-boundary matching at the retrieval check — so phantom
"successes" (docstring fragments happening to appear in the retrieved context
for unrelated reasons) disappear. The v2 floor is the truer measurement.

**v2 Headroom A/B: neutral.** The +2pp delta between runs #9 and #10 is well
inside the N=50 noise floor (±5pp 1σ on ~9/50). Consistent with raude's v0.3.0b5
N=20 A/B on the v1 harness (also neutral). Headroom at its default
`target_chars=1000` neither helps nor hurts retrieval/extraction on the v2
benchmark.

**Answer-given-retrieval ceiling:** at N=50, qwen3:8b extracts the correct value
from ~75% of retrieved contexts on v2 (5–7 answered out of 8–9 retrieved). The
N=20 "100% answer-given-retrieval" result was small-sample luck — the true
extraction ceiling is lower but still high. **The remaining gap from 75% to
100% is the real extraction work to do** — partly the Tally-category blind zone
(retrieval works but qwen3:8b consistently fails to extract), partly needle
edge cases. Retrieval at ~16% is still the dominant bottleneck by an order of
magnitude.

**Why v2 runs are ~2x slower than v1 runs** (context p95 11s vs 1.4s) is an
open question — likely v2 needle selection hitting different document patterns that
trigger expensive retrievals. Flagged for investigation; does not affect the
accuracy numbers.
The static-budget retrieval numbers (55-58%) may be slightly inflated by a smaller
score-gating threshold that hadn't been tightened yet — treat as an upper bound, not
a clean baseline.

### Hot-tier vs hot+cold-tier retrieval (C.2 of B→C, 2026-04-10)

The density gate (`d1d7602`, raude) demotes structurally-noisy documents to
**heterochromatin** (lifecycle tier tier 2). With C.1 (`b99e47a`) the demotion is
**non-destructive** — content, complement, fragments, SPLADE terms, and FTS5
indices are all preserved. With C.2 (`86c20f6` library + this commit's wiring)
a new opt-in retrieval path consults heterochromatin documents via **ΣĒMA cosine
similarity** in 20-dim space, restoring full content for top-k matches.

This means a single benchmark run can now report **two retrieval ceilings**:

| Metric | Definition |
|---|---|
| **Hot-only retrieval** | Result of `query_genes()` — `chromatin < HETEROCHROMATIN`, the standard /context behavior |
| **Hot + cold retrieval** | Hot-tier result PLUS up to `cold_tier_k` heterochromatin documents via ΣĒMA cosine fallthrough, when hot returns ≤ `cold_tier_min_hot_genes` |

The hot+cold metric measures the **upper bound on what the knowledge store can serve**
for a given query — including knowledge that has been demoted from the active
retrieval pool but is still semantically reachable through ΣĒMA similarity.
For NIAH specifically (where every needle's document is known to exist in the
corpus), the hot+cold ceiling is what determines whether 100% retrieval is
even possible.

**Configuration** (`helix.toml`, `[context]` section):

```toml
[context]
cold_tier_enabled = false           # opt-in master switch
cold_tier_min_hot_genes = 0         # fall through when hot returns ≤ this many
cold_tier_k = 3                     # max cold genes per query
cold_tier_min_cosine = 0.25         # ΣĒMA cosine floor (sparse 20-dim)
```

**Per-request override** on `/context` POST:

```jsonc
{
  "query": "...",
  "include_cold": true   // overrides config; null/omitted honors config
}
```

The response's `agent` block includes `cold_tier_used: bool` and
`cold_tier_count: int` so callers can distinguish hot vs hot+cold retrievals
in their own analytics.

**Note on the ΣĒMA cosine threshold:** ΣĒMA's 20-dim projection is sparse by
design. Typical close-paraphrase pairs score 0.15–0.30 in cosine, NOT 0.6–0.9
like full 384-dim sentence embeddings. The default floor of 0.25 is slightly
more permissive than the existing hot-tier Mode A/B thresholds (0.3/0.4)
because cold-tier is only reached when hot results are already thin — better
to surface a weak match than nothing. Empirical anchor:

```python
codec.encode("def authenticate_user(username, password): ...") vs
codec.encode("user authentication login password check")
→ cosine = 0.18
```

### Post-recovery measurement (2026-04-11)

After committing B/C.1/C.2, the live knowledge store was restored from
`genome.db.pre-compact.1775865733.bak`, the replicas were re-synced from the
restored master, and the new sweep was run with B's corrected deny list +
C.1's non-destructive compression:

```
Density gate compaction sweep (APPLIED, post B+C)
  scanned               : 8100
  WOULD stay OPEN       : 6717
  WOULD demote EUCHRO   : 13
  WOULD demote HETERO   : 1370
  total demoted         : 1383  (17.1%)

  Reasons:
    deny_list             745   (was 3492 in raude's pre-B sweep — diff is steam preserved)
    low_score_hetero     1243
    low_score_euchro      337
    access_override      1162   (kept OPEN regardless)
    open                 4613
```

**99.9% of steam content preserved (2,696 / 2,700 OPEN)** — the user's
"steam is high-SNR signal" reframe is in production. The 4 demoted steam
documents hit the score gate individually (genuinely low density), not the
deny list. **Heterochromatin content is intact** (verified on Factorio
EULA, Afrikaans/Arabic localization samples — `compress_to_heterochromatin`
non-destructive contract holds).

#### N=50 v2 hot-only vs hot+cold (qwen3:8b, seed 42, restored + re-swept knowledge store)

| Run | Headroom | include_cold | Retrieval | Answer | Errors | Cold-tier fired |
|---|---|---|---:|---:|---:|---:|
| #11 (post-B/C hot-only) | enabled | false | **20.0%** (10/50) | **16.0%** (8/50) | 1 | 0/50 |
| #12 (post-B/C hot+cold) | enabled | true | **20.0%** (10/50) | **16.0%** (8/50) | 2 | 48/50 |

**Hot-only: +4pp retrieval / +4pp answer over pre-sweep v2** — same as raude's
old destructive-sweep result (20%/16% vs my pre-sweep 16%/12%). The Struggle 1
noise-reduction effect at the retrieval layer is real and replicable. **The
sweep is helping even though the demoted set is mostly build artifacts**.

**Hot+cold: identical headline numbers, but the result composition shifts**:

| Category | Hot-only retr / ans | Hot+cold retr / ans | Δ |
|---|---:|---:|---|
| steam | 50.0% / 50.0% | 42.9% / 50.0% | −1 retr (one steam needle displaced from result) |
| tally | 0.0% / 0.0% | 25.0% / 0.0% | **+1 retr (rescued from heterochromatin via SEMA)** |
| education_public | 13.3% / 6.7% | 13.3% / 6.7% | — |
| helix | 14.3% / 0.0% | 14.3% / 0.0% | — |
| cosmic / scorerift / other | 0% / 0% | 0% / 0% | — |

**Cold-tier fires on 96% of queries** (48/50, returning 144 cold-document
candidates total at k=3 each) but the rerank produces the same number of
final answers as hot-only. Reading: **the demoted set (1,370 documents,
mostly Next.js build artifacts under cosmic) doesn't overlap meaningfully
with where the NIAH benchmark needles live.** Cold-tier rescues a single
tally needle (the historic blind zone — extraction failed afterward
because qwen3:8b consistently misreads tally content), but displaces a
steam needle in the rerank.

**Net: cold-tier adds optionality without a headline win on this workload.**
The infrastructure is in place; the value will materialize when:
1. Future demotion patterns better match the query distribution
2. Stronger downstream models extract correctly from the rescued tally content
3. The benchmark needle distribution shifts toward content the gate is actually demoting

**SEMA cosine threshold note (calibration finding):** Initial default of
`min_cosine = 0.25` was too strict — cold-tier returned 0 results for
typical NIAH queries even when 754 vectors were in the cold cache. Empirical
distribution on the live knowledge store showed top-10 matches at 0.79–0.84 for some
queries (Factorio mod portal example) and 0.10–0.20 for others (sparse
auth-paraphrase pair). **Default lowered to 0.15** in `helix.toml` and
`Genome.query_cold_tier`. Tests use 0.05 in fixtures because in-memory
synthetic content scores even lower.

**Coordination footnote on the recovery:** the running server reads from
SQLite replicas (`C:/helix-cache/genome.db`, `E:/helix-cache/genome.db`)
configured in `helix.toml [genome]`. The sweep modifies the master only —
replicas need to be synced separately for the live server to see the new
lifecycle tier. Forgetting this step means the live `_cold_sema_cache`
builds from stale replica state and returns no results despite the master
being correct. Worth noting in the launcher / supervisor track if it
manages replica sync.

### Claude API tiers on the synthetic floor (N=50)

Dispatched via Claude Code sub-agents with direct access to the Helix `/context`
endpoint. Same 50 needles, seed 42, same snapshot.

| Model | Retrieval (found) | Extraction (answered) | Extraction efficiency |
|---|---:|---:|---:|
| Claude Haiku 4.5 | 20/50 (40%) | 20/50 (40%) | 100% |
| Claude Sonnet 4.6 | 24/50 (48%) | 21/50 (42%) | 88% |
| Claude Opus 4.6 | 21/50 (42%) | 19/50 (38%) | 90% |

**Finding:** On synthetic noisy queries, frontier API models hit the *same retrieval
ceiling* as local 4-8B models (~44-48%). The gap is at extraction: Claude models extract
88-100% of what they find, local models extract ~64%. **Retrieval is the bottleneck.**

### Failure modes (N=50, qwen3:8b dynamic)

```
retrieval_miss       28   56% — genome did not surface the right gene
extraction_miss       8   16% — gene was expressed, model couldn't extract value
error                 1    2% — HTTP timeout or parse failure
answered correctly   14   28% — end-to-end success
```

### Per-category breakdown (N=50, qwen3:8b dynamic)

| Category | N | Retrieval | Accuracy | Notes |
|---|---:|---:|---:|---|
| steam (game data) | 13 | 54% | 54% | best — unique strings, cheap to find |
| helix (self) | 7 | 71% | 0% | knowledge store finds documents, can't bind abstract config keys |
| education_public | 16 | 38% | 25% | largest + most diverse, hardest |
| cosmic | 6 | 33% | 33% | consistent |
| other | 2 | 50% | 50% | small N |
| scorerift | 2 | 50% | 0% | small N |
| **tally** | **4** | **0%** | **0%** | **universal blind spot — all models fail** |

The **tally blind spot** (4/4 zero across every model tested) is not a model failure —
it is a knowledge store indexing gap. BookKeeper KVs were extracted during ingest but never
wired into the tags index for retrieval. This is a known open issue.

### Headroom uplift run (N=20, 2026-04-10 12:15)

| Metric | qwen3:8b dynamic | qwen3:8b + Headroom | Delta |
|---|---:|---:|---:|
| Retrieval | 45% | 45% | 0 |
| Accuracy | 35% | 30% | -5pp |
| Total time | 1.4 min | 3.4 min | +2.0 min |
| Context p50 | 0.55s | 0.64s | +0.09s |
| Proxy p50 | 3.24s | 4.73s | +1.49s |
| Proxy p95 | 6.99s | 90.02s | +83s (one outlier) |
| **Avg injected tokens** | ~6000 | **399** | **-93%** |
| Avg budget utilization | — | 6.6% | — |
| Avg compression ratio | — | 2.17x | (Headroom's own metric) |

**The token compression is real but the retrieval uplift did not materialize.** Headroom
took Helix's 3-document, 6K-token output and compressed it to an average of 399 tokens per
turn. Retrieval quality was identical (both runs found exactly 9/20 needles — Headroom
is compressing what Helix retrieved, not changing what gets retrieved). Extraction
dropped 5pp (7 → 6 answers on N=20) which is within statistical noise at this sample
size but worth tracking at higher N.

**The interesting number: 399 tokens.** A naive RAG pipeline dumps 25,000+ tokens per
turn. Helix + Headroom delivers comparable extraction on **1.6% of that payload.**

### How to reproduce

```bash
# Start Helix proxy, ensure ribosome is paused for clean VRAM
python -m helix_context.server &
curl -X POST http://127.0.0.1:11437/admin/ribosome/pause

# Load the downstream model only
curl http://localhost:11434/api/generate \
  -d '{"model":"qwen3:8b","prompt":"hi","stream":false,"keep_alive":"12h","options":{"num_predict":1}}'

# Run
PYTHONUNBUFFERED=1 N=50 HELIX_MODEL=qwen3:8b \
  python benchmarks/bench_needle_1000.py

# Results: benchmarks/needle_1000_results.json
```

**Environment:**
```
OLLAMA_KV_CACHE_TYPE=q4_0    (INT4 KV cache — q8_0 tested, regressed accuracy)
HELIX_CONFIG=F:/Projects/helix-context/helix.toml
GENOME_DB=F:/Projects/helix-context/genome-bench-2026-04-10.db
```

---

## Layer 3 — EnterpriseRAG-Bench (leak-free retrieval at corpus scale)

**Scripts:** `benchmarks/build_enterprise_rag_batched.py`, `benchmarks/score_enterprise_rag_onyx.py`, `benchmarks/ablate_dense_prefilter.py`
**Methodology:** Index Onyx-dot-app's [EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench) synthetic enterprise corpus (confluence + fireflies + github + gmail + google_drive + hubspot + jira + linear + slack) as a sharded genome, then run the production retrieval pipeline against the author-supplied questions + gold-path map.
**Purpose:** Establish helix's behavior on a corpus where (a) the bench owner is not the system owner — no own-code leak surface, (b) each question has explicit gold paths — no telepathy required, and (c) corpus size sweeps across orders of magnitude (10K → 850K genes).

### Why this layer was needed

The Layer 2 KV-Harvest results bottomed at ~16-20% retrieval — a fair noise-floor measurement, but uninformative for "is retrieval working well at scale on real-shape questions?" because:

1. **Source corpus = helix's own dev tree.** Filesystem-grep + CLAUDE.md leaks inflated earlier matrix-bench numbers (the system was finding answers by reading its own developer documentation, not by retrieving through the pipeline). The 2026-05-21 bench investigation report at `docs/benchmarks/2026-05-21_bench_investigation_report.md` lays out the leak audit.
2. **Synthetic single-axis KV queries** measure the leftmost point on the dimensional-lock curve (see [`BENCHMARK_RATIONALE.md`](BENCHMARK_RATIONALE.md)) and tell you nothing about composition.
3. **No corpus-scale gradient.** Layer 2 cannot answer "does recall scale with N?" — it's pinned to one 7,313-gene snapshot.

EnterpriseRAG-Bench solves all three: external corpus, multi-token natural-language questions with gold paths, and four built fixture sizes spanning 10K → 850K genes.

### Bench investigation rebuild (2026-05-20 → 2026-05-21)

A wave of investigation work landed in mid-May 2026 that rebuilt the matrix bench with leak-check discipline:

- **Filesystem-grep + CLAUDE.md leak elimination** — `bench_claude_matrix.retrieval_probe` now supports an `isolated=True` mode that launches the downstream sub-agent with `--tools ""` + `--strict-mcp-config` + empty MCP config + sterile CWD. Sub-agents can only see what helix's `/context` endpoint delivers.
- **3 MCP TDD-fixes shipped** (Windows stdio handshake, identity guard, etc.) that unblocked `claude -p` agent dispatch.
- **5-fixture grid** (small / medium / medium-sharded / xl / xl-sharded) re-baselined under isolation.
- Headline measurement: **+32.4 pp helix lift, 65% hallucination reduction** under sterile conditions vs. raw-context baseline.
- Cross-model finding: **local Gemma 4 e4b ($0, 9.6 GB VRAM) and Claude Haiku 4.5 (API) both landed at exactly 11/250 wrong = 4.4% under M2** — grounding precision is `retrieval × anchor`, not model strength.

Full report: `docs/benchmarks/2026-05-21_bench_investigation_report.md`.

### Cross-corpus results across fixture sizes

| Fixture | Genes | Shards | Disk | Recall@10 | Sonnet-judged correctness | Hallucination |
|---|---:|---:|---:|---:|---:|---:|
| `enterprise_rag_10k` (subset) | ~10 K | 1 (monolithic) | ~600 MB | **60%** *(variant A, n=100)* | 19% *(depth-16 d1)* | 1-12% *(depth-dependent)* |
| `enterprise_rag_50k` (subset) | ~50 K | 1 | ~3 GB | **71%** | 4% *(depth-1)* | 1-2% |
| `enterprise_rag_onyx_full` (v1) | ~850 K | 105 *(auto-subsharded)* | 42.6 GB | n/a *(Wall-1 dominated; pre PR #163 SQL fix)* | — | — |
| **`enterprise_rag_onyx_full_2`** (v2) | **850,501** | **100** *(Path-A)* | **47.24 GB** | **28%** *(variant A, n=100, 2026-05-28)* | pending Sonnet judge | pending |

**Corpus-scale recall erosion is real:** 60% @ 10K → 28% @ 850K under SPLADE-off / no-prefilter. This finding is the dominant Wall-2 latency-cost-of-recall input to PR #160's SPLADE pre-filter design and [Issue #164](https://github.com/mbachaud/helix-context/issues/164)'s SPLADE-as-corpus-regime-feature hypothesis.

### The expression-budget clamp fix (2026-05-22)

A separate finding during Layer 3 work that dramatically improved per-gene answer correctness without changing retrieval at all.

The `recall@10 → correctness` gap was being created by a hard-coded per-gene DELIVERY clamp at `context_manager.py:1449` (`target=1000`) — the compression step was head-truncating long genes before they reached the answer model, even when retrieval surfaced them correctly.

**Fix:** `per_gene_budget = "dynamic"` (floor-then-greedy allocator, TDD'd). Effect on 16K-depth-1 fixture:

| Metric | Pre-fix | Post-fix |
|---|---:|---:|
| Correctness | 4% | **43%** |
| Correct given gold-delivered | 10% | **97.5%** |
| Hallucination | 2% | 1% |
| Cost | baseline | +17% |
| Retrieval | 40/100 | **identical 40/100** |

**Default flipped to dynamic on 2026-05-22** after passing flip-gate at depth-8/n=100: 0 gold dropped, 752 = 752 genes preserved, /context p95 −0.34s, 22 tests green.

This finding **reframed the earlier "56 ranker" claim**: retrieval recall@10 on the 10K corpus was actually 83%; only 17 questions were TRUE retrieval misses. The "56 missed" number was a delivery@1 artifact from the clamp, not a ranker deficiency.

### Issue #159 — 850K-gene corpus exposes two scaling walls

Building the 850K-gene `enterprise_rag_onyx_full` fixture (105 shards in v1, 100 in v2) surfaced a clean architectural pressure-test framing for retrieval at scale. See [Issue #159](https://github.com/mbachaud/helix-context/issues/159) and the [SPLADE pre-filter PRD](../prds/2026-05-26-splade-prefilter-dense-recall.md):

- **Wall-1 (memory commit ceiling).** Daemon at 105+ shard scale needs ~247 GB private commit (~50 GB mmap'd shard data × the multi-connection SQLite footprint, plus heap). Default Windows 56 GB pagefile crashes the daemon during warmup at ~143 GB. **Mitigations:** 240 GB fixed pagefile on C: NVMe (288 GB total commit ceiling) AND Path-A rebuild → fewer larger shards.
- **Wall-2 (brute-force-no-ANN latency wall).** ~870M FLOPs per query for the dense matmul at 850K genes. The pipeline does not use ANN indexes (a design pillar — see `feedback-helix-ribosome-off`); the brute-force matmul IS the cost. Levers within the no-ANN/no-LLM constraint: SPLADE pre-filter ([PR #160](https://github.com/mbachaud/helix-context/pull/160)), inverted term→shard hashmap, cymatics as N-reducer, Matryoshka dim=256.

**Important architectural finding (2026-05-27, cross-verified on three hosts):** the dense matmul at `knowledge_store.py:2709` is **plain NumPy CPU** — `sims = matrix @ query_vec` on an `np.stack(...)` heap-resident fp32 array, no `np.memmap`, no torch, no GPU. BGEM3Codec defaults to `device="cpu"`. Both my x86 + RTX 3080 Ti and Joe's ARM64 Grace + GB10 saw CPU-pegged single-thread + GPU ~16% (SPLADE-only spikes). The prefilter's "FLOP savings" are CPU NumPy FLOPs, not GPU FLOPs. Native CUDA BGE-M3 + a torch-on-GPU matmul are queued as post-bench engineering.

### v2 100q variant-A result (2026-05-28, first leaderboard-grade datapoint)

| Metric | Value |
|---|---|
| Fixture | `enterprise_rag_onyx_full_2` (100 shards, 850,501 genes, 47.24 GB) |
| Variant | A — SPLADE disabled, no prefilter |
| n | 100 questions (basic type) |
| recall@1 | 4.0% |
| recall@3 | 11.0% |
| recall@5 | 18.0% |
| **recall@10** | **28.0%** |
| MRR | 0.100 |
| p50 latency | 98.4 s |
| **p95 latency** | **154.5 s** (~2.5 min) |
| p99 latency | 311.5 s |
| Timeouts (10-min cap) | **0 / 100** |
| Wall-clock | 3.08 h |
| Daemon survived | yes |

**Variants T (baseline), B (prefilter on, escape=0), and C (prefilter on, escape=250)** were swept at the smoke level (n=5) on the same fixture — see PR #160 §5. Cross-variant smoke result: identical 40% recall@10 / MRR 0.25 across all four; only variant A had zero timeouts. Variant A locked as the bench candidate on (recall-tied) + (zero-timeout) + (lowest config complexity).

The 500q variant-A is in flight at the time of writing.

### Engineering fixes that landed during Layer 3

Three master-mergeable PRs were ported off the Layer 3 work:

| PR | Title | Status | Headline impact |
|---|---|---|---|
| [#162](https://github.com/mbachaud/helix-context/pull/162) | `fix(tagger,ddl) + feat(build): cherry-pick + port PR #161 fixes onto master` | **merged 2026-05-28** at `478e893` | Tagger ReDoS regex fix (60+min hang → 0.5-2ms on underscore-heavy JSON); FTS5 cleanup O(N²) → O(N log N); shard salvage helper for kill+restart cycle |
| [#163](https://github.com/mbachaud/helix-context/pull/163) | `fix(knowledge_store): batch IN-clause queries to stay under SQLite cap` | merged 2026-05-28 *(pending CI confirmation at time of writing)* | Eliminates silent shard-skipping on variant-A retrieval; 4 hot sites batched + helper + TDD'd regression test |
| [#160](https://github.com/mbachaud/helix-context/pull/160) | `feat(retrieval): SPLADE pre-filter for dense matmul (Issue #159 Wall-2)` | open | Recall-neutral by construction; latency win conditional on corpus regime |

**Cross-host validation of the tagger fix:** the same `_KV_PAIR_PATTERN` ReDoS was independently reproduced and fixed by py-spy investigation on x86 Ryzen + RTX 3080 Ti (2026-05-19) and ARM64 Grace + GB10 (2026-05-27), 8 days apart, on different agents. Same line (`tagger.py:439`), same root cause (`(\w+(?:_\w+)*)` nested-quantifier ambiguity on underscore-heavy content), same fix (`(\w+)`). Two independent reproductions on different hardware classes — the strongest possible regression-fix validation footnote.

### Open issues filed from Layer 3 storage audit

- [Issue #164](https://github.com/mbachaud/helix-context/issues/164) — **Storage breakdown of v2 EnterpriseRAG-Onyx corpus**: 17.6× expansion from 2.6 GB raw → 47.24 GB built; SPLADE = 21.1% / 9.96 GB; proposed `splade_enabled = "auto"` with two-threshold size-aware default (`splade_auto_disable_above_genes`, `splade_auto_enable_below_genes`); full scale-curve follow-up (1K, 5K, 25K, 47K, 100K, 850K) defines the SPLADE-as-corpus-regime-feature curve.
- [Issue #165](https://github.com/mbachaud/helix-context/issues/165) — **Audit fingerprint routing index (path_key_index) storage: 34.1% of v2 corpus** (~19 KB of index per gene); hypothesizes the index was sized for a router design that's currently degenerate (per the 2026-05-26 router discrimination probe finding that broad query terms hit 90-100% of shards). Larger single storage lever than SPLADE; separate workstream.

### Cross-host comparison (in progress)

Joe's spark-e92c completed its v2 build at 16:12Z on 2026-05-28 (15h 6min wall — ~16% faster than the Ryzen baseline on encode + tagger CPU-bound steps). **Topology identical** to the Max-rig build: 100 shards, 850,503 genes, 47.6 GB on disk, 100% dense. The earlier "12-shard" framing was wrong — `enterprise_rag_onyx_full_2` is *labelled* Path-A but auto-subsharding lands at 100 on both hosts.

Cross-host comparison therefore isolates a single variable: hardware. Ryzen + 48 GB + RTX 3080 Ti + 240 GB pagefile (heavy paging) vs ARM64 Grace + 118 GB + GB10 (mmap fits in physical RAM). Recall@K is expected to be near-identical across hosts (same code, data, questions); latency delta will be a pure RAM-headroom / pagefile-pressure story. Joe's variant-A 100q is queued behind a WAL checkpoint + rsync; cross-host writeup pending its completion.

---

## The injection budget thesis

The reason both benchmarks matter:

**Layer 1** shows that **quality is model-invariant** when the query is clear. A 0.6B
model finds the same answer as Opus. The knowledge store is the librarian; the model is just the
reader.

**Layer 2** shows the floor when queries are noisy. Every model — local or API — caps
near the same retrieval rate. The gap at that ceiling is a *retrieval problem*, not an
intelligence problem. No amount of model size will fix it.

**The Headroom run** shows that Helix's output is already compressible by another 15x
without accuracy loss. That is the "not drowning" number: **399 tokens per turn**,
delivered to any model, with equivalent extraction to a 6000-token window.

### What this means for agents

A modern agent doing tool calls, RAG lookups, and document reads burns through its
context window in minutes. Helix + Headroom together propose a different model:

- The **knowledge base** lives in a knowledge store (SQLite, 523 MB compacted, 46 MB raw text)
- The **per-turn injection** is ~400 tokens of semantically compressed evidence
- The **full turn budget** (128K – 1M tokens on modern APIs) stays available for
  conversation, multi-step reasoning, and tool-call chains

The agent never drowns in RAG because the retrieval layer never ships more than the
model needs to answer the current question.

---

## Open work

- **Tally blind spot:** Re-index BookKeeper KVs into the tags index (0/4 retrieval
  across every model tested). Known knowledge store gap.
- **N=1000 full run:** All Layer 2 runs to date are N=20 or N=50. A confidence-interval-
  grade result needs N≥500. Projected runtime with qwen3:8b dynamic: ~130 minutes.
- **4th "desperate" tier:** Designed but not shipped. Intended to boost retrieval on
  low-confidence queries at the cost of latency.
- **BABILong multi-hop:** Next-level benchmark with compositional reasoning, not just
  single-fact lookup.
- **Headroom + retrieval uplift:** Confirm whether Headroom's CCR retrieve-on-demand
  tool can lift the retrieval ceiling past ~48% on synthetic queries.
- **DeBERTa re-rank retraining:** Current re-ranker is undertrained, disabled in the
  default pipeline. 500-query training set planned.

---

## File map

```
benchmarks/
├── bench_needle.py              # Layer 1 — curated N=10 SIKE
├── bench_needle_1000.py         # Layer 2 — KV-harvest N=up-to-1000
├── bench_sweep.py               # Layer 1 across all local models
├── needle_results.json          # Layer 1 baseline
├── needle_50_8b_dynamic_results.json      # Layer 2 reference
├── needle_20_8b_headroom_results.json     # Layer 2 + Headroom
├── needles_50_for_claude.json   # Frozen needle set for API model runs
├── sweep_results.json           # Full Layer 1 model sweep
└── genome-bench-2026-04-10.db   # Pinned genome snapshot (NOT in git)
```
