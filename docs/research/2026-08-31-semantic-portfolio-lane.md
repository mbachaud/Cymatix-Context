# Semantic-portfolio lane — external benchmarks against the ERB semantic cap (2026-08-31)

**Motivation.** At shipped v0.9.1 on the 947k ERB bed, delivered gold is
314/470 (.668) and the semantic class is the cap: 43/125 (.344), +4 needles
from the floor flip and fusion-shape-immune since wave-1. The EnronQA
paraphrase-crater receipts (2026-08-30) name the mechanism — lexical anchor
loss on a fully algorithmic retrieval path — and killed the
coverage-discrimination lane. External benchmarks now serve two purposes:
(a) confirm the blindness taxonomy generalizes, (b) price which sub-case
each candidate fix addresses.

**Source.** Sol's benchmark reference (Cymatix_Benchmark_Reference_2026-08-30,
EnterpriseRAG leaderboard follow-up). User ruling 2026-08-30: run the smaller
two first (LoCoMo-Plus + MULocBench), queue FinanceBench after.

## Lane 1 — LoCoMo + LoCoMo-Plus (one bed, three needle families)

Adapter: `scripts/build_locomo_corpus.py` + `scripts/resolve_locomo_needles.py`,
profile `locomo`. 5,882 per-turn documents (headers carry conv/session/date —
load-bearing for the temporal class) + 401 cognitive cue dialogues; needles =
1,977 LoCoMo QA (multi-hop / temporal / common-sense / single-hop /
adversarial) + 401 cognitive triggers.

- The Cognitive split is the purest public probe of the exact ERB blindness:
  "semantic cue-trigger disconnect" — the trigger shares little surface
  vocabulary with the cue.
- The paper ships its own baseline ranks per pair (bm25 / mpnet / bge,
  ranked among the 401 cues only — `generation_pipeline/rank.py`). Receipts
  report BOTH bases: rank-among-cues (like-for-like vs the paper) and
  full-bed rank (turns as distractor mass; harder, realistic).
- 9 QA rows skipped (4 no evidence, 5 evidence ids not in the conversations)
  — listed in `needles_locomo_meta.json`.

## Lane 2 — MULocBench (issue → file localization, 45 Python repos)

Adapter: `scripts/build_mulocbench_corpus.py` (fetch/emit), profile `muloc`.
This is Cymatix's actual deployment regime (context engine for coding
agents) and the harshest anchor-loss setting: natural-language issue register
vs code identifier register.

Validity design (recorded up front):

- 842 issues span 804 distinct (repo, base_commit) pairs — per-issue
  snapshots are infeasible for a knowledge-store bench. One snapshot per
  repo (most frequent base_commit, ties by first occurrence), fetched as a
  GitHub tarball. Issues keep gold files present in that snapshot; the
  `exact_commit` subset (issue's own base_commit == snapshot) is the
  strict-validity slice and is graded separately.
- Corpus = allowlisted text/code extensions only (~97% of gold coverage;
  .py is 82% of gold). The builder's 50..200,000-byte gate applies; gated
  gold recorded at resolve time.
- Gold = file-level (any gene of any gold file delivered in top-12). The
  dataset's class/function granularity is a follow-up refinement.

## Queued — FinanceBench

github.com/patronus-ai/financebench, after the two lanes above (user
ordering). Expected diagnostic: domain-register synonymy — the sub-case of
anchor loss that ingest-time expansion could address in-charter.

## Comparability rules

New-corpus rows are NOT comparable to ERB or EnronQA rows (BASELINES.md).
Beds built after PR #413 are tagger_version=2 — these lanes start their
history on v2. Build mode: locomo sequential (tiny); muloc may run parallel
c=6 (CPU tagger is content-deterministic under parallel per the
entity-autolink equivalence receipt; only inert COVER edges drift — note in
the bed manifest either way).

## Lane 1 results — LoCoMo baseline (2026-08-31, tagger-v2 bed, shipped v0.9.1)

Receipt: `benchmarks/dogfood/locomo/receipts/ladder_locomo_baseline_2026-08-31.json`
(n=2,378, k=12, per-query; bed 6,276 genes; p50 22.7 ms/query).

| class | delivered | r@12 | pool-absent | med rank |
|---|---:|---:|---:|---:|
| singlehop | 489/841 (.581) | .637 | 197 | 5 |
| temporal | 174/320 (.544) | .597 | 64 | 5 |
| adversarial | 221/446 (.496) | .563 | 125 | 6 |
| multihop | 120/281 (.427) | .509 | 85 | 7 |
| commonsense | 27/89 (.303) | .348 | 47 | 6 |
| **cognitive** | **3/401 (.007)** | **.007** | **394** | 35 |

Two findings:

1. **The cognitive split is a near-total blackout** (98.3% pool-absent) —
   the cleanest external confirmation of the ERB/EnronQA blindness: when
   query and gold share ~no surface vocabulary, the algorithmic path
   retrieves nothing.
2. **The paper's own baselines fail it identically** — rank of the gold cue
   in their candidate pool: bm25 median 631, mpnet 385, bge 378, combined
   272; r@12 = .025 / .027 / .015 / .027 (needle meta,
   `paper_ranks_among_401_cues`). **Dense embeddings do NOT fix this
   class.** The blindness LoCoMo-Plus isolates is representational at a
   level neither sparse nor dense surface encoders reach — the lane it
   prices is semantic abstraction added at WRITE time (the parked
   LLM-ribosome doc-side enrichment path), not an encoder swap. That
   re-scopes the ERB dense-counterfactual cell too: dense may help the
   *drowned* sub-case (weak-but-nonzero overlap), but not the
   cue-disconnect sub-case.

Ready-made follow-up cell: LoCoMo ships an LLM-derived `observation` layer
(per-session abstraction sentences with dia_id provenance). Ingesting
observations as bridge documents is a zero-model-cost enrichment A/B for
the multihop/commonsense classes (the cognitive cues have no observation
layer — their enrichment requires generating one, i.e. the ribosome lane).
