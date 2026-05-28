# Benchmark Rationale — Why NIAH Doesn't Fit a 12-Tone Engine

> *"Curated NIAH 10/10. Synthetic NIAH 6-12%. Same engine. Same data.
>  What changed?"*

A discovery doc explaining how we ran into the limits of standard
needle-in-a-haystack benchmarking against a multi-axis retrieval
substrate, and why we built `bench_dimensional_lock.py` as the honest
replacement.

Date: 2026-04-13 (after the path_key_index + 4-layer federation work)

---

## What we observed

Two benchmark runs against the **same 17,961-document fresh knowledge store**, the
**same retrieval pipeline**, the **same answer model** (qwen3:8b), in
the **same session**:

| Bench | Retrieval | Answer | Notes |
|---|---|---|---|
| **SIKE N=10** (curated, natural) | **10/10 (100%)** | **7/10 (70%)** | Hand-written queries with project context |
| **KV-harvest N=50** (synthetic) | **12% (6/50)** | **10% (5/50)** | Auto-generated `"What is the value of {key}?"` |
| **KV-harvest N=1000** (gemma4:e4b, killed at 16) | **6% (1/16)** | **0% (0/16)** | Same pattern, smaller model amplifies |

Two questions immediately presented themselves:

1. Is helix actually broken, and SIKE is hiding it?
2. Or is the synthetic bench measuring something that can't be answered?

The answer turned out to be the second — but unpacking *why* required
realizing that **NIAH was designed for a different class of retrieval
system**.

## NIAH's original design assumptions

Needle-in-a-haystack benchmarking was developed (Anthropic's original
NIAH paper, then various follow-ups) to test:

- **Long-context language models** — can the model find a fact buried
  in 100K tokens of mostly-unrelated content?
- **Single-vector RAG systems** — can the retriever pick the one
  document chunk that matches the query embedding?

Both share three structural assumptions:

1. **There is exactly one correct answer.** The needle is a unique
   string the bench inserted; everything else is distractor.
2. **Recall@1 is the right metric.** Either you found the chunk or
   you didn't.
3. **The query maps to one dense vector.** Retrieval is essentially
   a nearest-neighbor lookup in embedding space.

Under these assumptions, *"What is the value of `port`?"* makes sense
as a test query: only one chunk in the corpus has the test needle, the
embedding lookup either finds it or doesn't, and the grade is binary.

## Why those assumptions break for helix

Helix's data model violates all three:

### 1. There is rarely *one* correct answer

The 17K-document knowledge store contains:
- 3,273 documents with `url=` keys
- ~500 documents with `port=` keys
- ~2,000 documents with `model=` keys

When the bench asks "What is the value of `url`?", there are 3,273
valid answers. The bench grades a hit ONLY if helix returns the *one
specific document* the bench randomly picked. Under this rule, even a
perfect retrieval system would score ~0.03% on average — telepathy
is not a retrieval property.

### 2. Recall@1 ignores the multi-axis index

Every helix document is addressed by **12 retrieval signals + 4–5
attribution axes** (see `MUSIC_OF_RETRIEVAL.md`, `FEDERATION_LOCAL.md`):

```
12 retrieval signals:                4-5 attribution axes:
  - path_key_index                     - org
  - exact promoter tag                 - device (party)
  - prefix promoter tag                - user (participant)
  - FTS5 content                       - agent
  - SPLADE sparse                      - authored_tz
  - SEMA semantic
  - harmonic boost
  - cymatics resonance
  - cymatics flux
  - TCM session drift
  - ray-trace evidence
  - access-rate
```

A query that specifies one axis (just a key name) leaves 11+ axes
unused. Recall@1 grading treats this as if the unused axes were
broken. They aren't — they just weren't given anything to work with.

### 3. Helix queries are dimensional descriptors, not single vectors

A real-world query like *"What port does the helix proxy server listen
on?"* carries multiple narrowing signals:

```
Axis 1: project       → "helix"           narrows from 17K → ~500 genes
Axis 2: component     → "proxy server"    narrows from ~500 → ~10 genes
Axis 3: target attr   → "port"            narrows from ~10 → 1 gene
```

This is **dimensional locking** — each axis multiplicatively narrows
the candidate set. The same compound-index pattern that databases use
for multi-column lookups, just with retrieval signals instead of B-tree
columns.

The synthetic NIAH query *"What is the value of `port`?"* uses one of
those three axes. It has no way to lock on. Recall is the same as
random pick from documents containing `port=`.

## The diagnostic curve

The right measure of a multi-axis retrieval engine is **how recall
scales with axis count** — not recall at a single fixed axis count.

For each needle, generate four query variants:

| Variant | Axes | Example |
|---|---|---|
| 1 | 1 (just key) | "What is the value of `port`?" |
| 2 | 2 (key + project) | "What is the value of `port` in helix?" |
| 3 | 3 (key + project + module) | "What is the helix compressor `port`?" |
| 4 | 4 (key + project + module + filename) | "What is the `port` value in helix-context helix.toml?" |

Run all four through the same retrieval pipeline. Grade recall@1 for
each. The expected healthy curve:

```
recall@1
  90%  ┤                              ╭─── 4 axes
  80%  ┤                          ╭───
  70%  ┤                      ╭───
  60%  ┤                  ╭───
  50%  ┤              ╭───
  40%  ┤          ╭───
  30%  ┤      ╭───
  20%  ┤  ╭───
  10%  ┤──
   0%  └─────────────────────────────
        1     2     3     4    axes
```

**Curve shape IS the diagnostic:**
- **Flat at ~10%** → retrieval is broken (axes don't compose)
- **Monotonically rising** → retrieval is working as a multi-axis index
- **Steep early, plateau late** → ideal (locking quickly, no over-fit penalty)
- **Drops at variant 4** → over-specification penalty (axis weighting bug)

Every previous KV-harvest run was measuring **only the leftmost point**
on this curve. When that point is ~12%, it tells you nothing about the
shape of the curve, which is what actually matters.

## What this gives us

A bench result becomes a **diagnostic plot**, not a single number:

```
Healthy system:                    Broken retrieval:
  1ax  12%                           1ax  12%
  2ax  38%  (+26pp lift)             2ax  13%  (no lift)
  3ax  76%  (+38pp lift)             3ax  14%  (no lift)
  4ax  82%  (plateau, +6pp)          4ax  14%  (no lift)
  ───────                            ───────
  → multi-axis index works           → retrieval not composing axes
```

Per-step lift becomes informative:
- **1→2 lift: high** → project-context signal is engaging (PKI working)
- **2→3 lift: high** → narrowing within project is working (FTS5/SPLADE)
- **3→4 lift: small** → over-specification doesn't hurt (good)
- **3→4 lift: negative** → over-fit penalty (axis weighting bug)

This curve **CAN'T be measured by NIAH**. NIAH gives you only one
point on it.

## When SIKE is enough vs when you need the curve

| Question | Bench |
|---|---|
| "Does the system work for real users?" | **SIKE** — natural queries with normal axis density |
| "Is the multi-axis index composing correctly?" | **dimensional-lock** — explicit axis-count gradient |
| "What's the noise floor when queries are degenerate?" | KV-harvest, but **read it as the noise floor**, not as quality |
| "Did this commit improve retrieval?" | dimensional-lock A/B at variant 2 (most diagnostic level) |
| "Is compression working?" | bench_compression |

SIKE is sufficient for everyday quality signals. The dimensional-lock
bench is the truth-test when something looks weird in NIAH or when
making architectural changes that should affect axis composition (e.g.,
the path_key_index commit).

## The honest summary

We didn't build a new benchmark because the old one was wrong about
helix. We built it because the old one was **answering a different
question than the one we needed answered**.

NIAH asks: *"Did you find the one needle I hid?"*
Dimensional-lock asks: *"Does your multi-axis index compose correctly
as you add more narrowing dimensions?"*

For a single-vector RAG system, those questions converge. For a
12-tone retrieval engine with 4-layer attribution, they diverge — and
the second one is the question that matches the architecture.

---

## Addendum (2026-05-28): how Layer 3 / EnterpriseRAG-Bench answered the rationale's questions

The 2026-05-20 → 2026-05-21 bench investigation rebuilt the matrix harness
with [EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench)
(Onyx-dot-app's external corpus) as the test substrate. This was a direct
response to two of the three NIAH-doesn't-fit-helix problems above, and a
partial answer to the third:

1. **"There is rarely *one* correct answer"** → EnterpriseRAG-Bench
   questions ship with explicit `expected_doc_ids` gold-path lists per
   question. The bench grades a hit when ANY entry in the list appears in
   the delivered citations (multi-valid-gold — see
   [`MULTI_VALID_GOLD.md`](MULTI_VALID_GOLD.md)). Telepathy is no longer
   required to score.
2. **"Queries are dimensional descriptors, not single vectors"** →
   EnterpriseRAG-Bench questions are real natural-language enterprise-RAG
   queries with multiple narrowing signals built in. The single-axis
   *"What is the value of `port`?"* pathology is gone — questions look
   like *"What is the deployment process for the new authentication
   service?"* with project, component, and target-attribute axes all
   present.
3. **"Recall@1 ignores the multi-axis index"** → reported metric is
   recall@K (K=10 is the headline), with the gold path tested against
   the full delivered citation list. Multi-axis composition is allowed
   to contribute — though strict recall@1 is still reported alongside
   for legibility (recall@1 = 4% vs recall@10 = 28% on the v2 850K
   fixture shows the multi-axis lift directly).

### What this layer adds beyond dimensional-lock

The dimensional-lock 4-variant grid (`bench_dimensional_lock.py`, this
rationale's companion bench) remains the right diagnostic for *"is the
multi-axis index composing correctly under controlled axis-count
gradients?"* — a synthetic question that needs a synthetic harness.

EnterpriseRAG-Bench answers a different question: *"is the system
working at scale on real-shape queries on a leak-free corpus?"* It
trades the controlled axis gradient for natural-question variety and
corpus-size sweep (10K → 850K genes). The two benches are complementary;
both stay maintained.

### What the leak-free corpus revealed

Three findings that the own-corpus harness couldn't have shown:

- **Corpus-scale recall erosion is real**: 60% recall@10 at 10K genes,
  28% at 850K genes (same variant, same model, same code). This is the
  Wall-2 latency-cost-of-recall trade-off that motivated PR #160's
  SPLADE pre-filter design.
- **The 4%→43% correctness lift from the per-gene-budget clamp fix**
  (2026-05-22) was found because retrieval recall@10 on the 10K fixture
  was 83% but answer correctness was 4%. That gap pointed at delivery,
  not retrieval — a diagnostic the prior single-snapshot harness
  couldn't have produced.
- **SPLADE as corpus-regime feature**: SPLADE-on contributes 0 pp to
  recall on the EnterpriseRAG question set across 10K / v1 850K / v2 850K
  fixtures, while costing measurable p95 latency and 21.1% / 9.96 GB of
  disk. The 3-fixture "all pain, no gain" pattern motivated
  [Issue #164](https://github.com/mbachaud/helix-context/issues/164)'s
  hypothesis that SPLADE is useful below ~50K genes and net-negative
  above ~100K. The dimensional-lock harness couldn't have surfaced this
  — it doesn't sweep corpus size.

The 850K-gene v2 corpus + variant-A 100q result of **recall@10 = 28%**
(2026-05-28) is the first leaderboard-grade datapoint from this layer.
See [`BENCHMARKS.md`](BENCHMARKS.md) §"Layer 3 — EnterpriseRAG-Bench"
for the full results table.

---

## Companion docs

- [`MUSIC_OF_RETRIEVAL.md`](MUSIC_OF_RETRIEVAL.md) — the 12-signal +
  octave-gate periodicity that creates the multi-axis structure
- [`FEDERATION_LOCAL.md`](FEDERATION_LOCAL.md) — the 4-layer attribution
  axes (org / device / user / agent / tz)
- [`PIPELINE_LANES.md`](PIPELINE_LANES.md) — full ingest + query data flow
- [`BENCHMARKS.md`](BENCHMARKS.md) — practical bench harness reference
- [`GENOME_FIXTURE_MATRIX.md`](GENOME_FIXTURE_MATRIX.md) — fixture roots,
  including the EnterpriseRAG-Bench family that backs Layer 3

## Companion bench

- [`benchmarks/bench_dimensional_lock.py`](../benchmarks/bench_dimensional_lock.py)
  — implementation of the 4-variant grid described above. Reuses the
  same KV-harvesting code from `bench_needle_1000.py` so it's directly
  comparable to prior NIAH runs.
