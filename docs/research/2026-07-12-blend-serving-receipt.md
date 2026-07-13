# blend_mode graduation — SERVING-profile receipt (2026-07-12)

Branch: `bench/blend-serving-receipt` (worktree `.claude/worktrees/blend-serving`, from `origin/master` @ 1221e96).

## Why this run exists

The only prior `blend_mode` receipt (overnight P1a/P1b,
`docs/research/2026-07-11-overnight-bench-results.md`) ran under the **lexical
probe** profile (`docs/benchmarks/helix_probe_lexical.toml`), which disables
cymatics, dense recall, SPLADE, and abstain. With cymatics **off**, the blend
layer's only live refiner there was `harmonic_bin`; the receipt therefore never
exercised the cymatics block. On the serving profile cymatics is **on**, and it
runs a **pre-truncation re-sort** (`helix_context/scoring/blend.py`: sort at
~L202, then truncate to `max_genes` at ~L206-219), so `blend_mode="off"` changes
**which documents survive into assembly**, not just their order. The scoring
council blocked `blend_mode` graduation until a serving-profile A/B existed.
This is that A/B.

## Profile — serving-equivalent

Built by starting from the authoritative serving `helix.toml` and flipping off
**only** bench-hygiene / output-format knobs; every serving retrieval/scoring
knob is inherited unchanged. Configs committed at
`docs/benchmarks/helix_serving_probe_blend_{legacy,off,scale_relative}.toml`
(the only line that differs across the three: `[retrieval] blend_mode`).

Serving knobs **ON** (vs the lexical probe): `[cymatics] enabled=true`,
`[retrieval] dense_embedding_enabled=true` (+ dense pool floor/size, ann
threshold 0.58), `[ingestion] splade_enabled=true`, `[budget]
abstain_enabled=true`, `[know] emit_floor=0.45` + calibrated betas,
`fusion_mode="rrf"` with the shipped RRF weights, synonym expansion.

Flipped **OFF** for bench only (do not affect gene ranking / which docs enter
assembly): legibility headers + decoder wrapper (keep assembled text
comparable to the P1a/P1b lexical receipt), session-delivery + synthetic-session
(hygiene; drivers pass `read_only=True, ignore_delivered=True`), PLR (feeds the
packet's `plr_confidence` only, not ranking), headroom (launcher-only),
background compaction.

## Beds + dense-coverage pre-check

`SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL`:

| bed | genes | dense_v2 non-null | dense-on live? |
|---|---|---|---|
| `genomes/bench/matrix/xl.db` | 46,479 | 46,479 (100%) | **yes** |
| `genomes/bench/matrix/xl_clean.db` | 41,803 | 41,803 (100%) | **yes** |
| `genomes/bench/matrix/enterprise_rag_10k_batched.db` | 15,598 | 15,598 (100%) | **yes** |

All three beds are fully dense-backfilled, so dense recall is **live on every
cell** (including xl / xl_clean — the task's fallback worry that dense would be
inert there does not apply; no backfill was needed).

## Method

- **Cell 1** (`benchmarks/ab_rerank_combinator.py`, the P1a/P1b driver): xl +
  xl_clean × 3 blend modes, `--combinators additive,off` (additive = serving
  default `rerank_combinator`; off isolates the blend layer's own inversions),
  `--topk 12`, 50 `bench_needle.NEEDLES`. Metric of record = **exact
  inversions** (emitted top-K order vs pure `last_fused_scores`), plus
  `gold_delivered_text`, `gold_delivered_id`, answerability
  (`content_has_answer`). The ONLY intended delta vs the P1a/P1b cells is the
  serving profile in place of the lexical probe.
- **Cell 2** (`benchmarks/ab_semantic_probe.py`): `enterprise_rag_10k_batched`
  × 3 blend modes, `--arms fused` (full serving stack), `--types semantic`
  (n=125). Metrics: `gold_delivered_id`, `gold_delivered_text`, `pool_present`,
  median gold rank, mean gold-answer overlap. (This driver does not compute
  exact inversions.)

Both drivers run `build_context(read_only=True, ignore_delivered=True)` on
read-only beds. Steady-state ≈ 9 s/needle (dense-vector blob loading over the
46K-gene bed dominates — honest serving cost).

## Results — Cell 1: xl / xl_clean × blend_mode (serving profile, n=50 each)

Data: `docs/research/data/2026-07-12-blend-serving-receipt-cell1-xl-{legacy,off,scale_relative}.json`.
Zero per-needle warnings/errors; pool non-empty on all 600 probes.

| bed | blend_mode | combinator | n | exact_inv (total) | gd_text | gd_id | answerability | Δgd_id vs legacy | Δans vs legacy |
|---|---|---|---|---|---|---|---|---|---|
| xl | legacy | additive | 50 | 768 | 0.32 | 0.40 | 0.72 | — | — |
| xl | off | additive | 50 | 617 | 0.22 | 0.30 | 0.54 | **−0.10** | **−0.18** |
| xl | scale_relative | additive | 50 | 740 | 0.32 | 0.42 | 0.76 | +0.02 | +0.04 |
| xl | legacy | off | 50 | 1199 | 0.38 | 0.48 | 0.80 | — | — |
| xl | off | off | 50 | **0** | 0.34 | 0.44 | 0.66 | −0.04 | **−0.14** |
| xl | scale_relative | off | 50 | 52 | 0.38 | 0.50 | 0.80 | +0.02 | 0.00 |
| xl_clean | legacy | additive | 50 | 752 | 0.34 | 0.36 | 0.72 | — | — |
| xl_clean | off | additive | 50 | 543 | 0.26 | 0.28 | 0.54 | **−0.08** | **−0.18** |
| xl_clean | scale_relative | additive | 50 | 714 | 0.34 | 0.36 | 0.80 | 0.00 | +0.08 |
| xl_clean | legacy | off | 50 | 1275 | 0.44 | 0.48 | 0.80 | — | — |
| xl_clean | off | off | 50 | **0** | 0.36 | 0.40 | 0.64 | −0.08 | **−0.16** |
| xl_clean | scale_relative | off | 50 | 97 | 0.38 | 0.42 | 0.80 | −0.06 | 0.00 |

Two structural observations vs the lexical-probe P1a/P1b receipt:

1. **The blend layer dominates emitted order under serving.** Legacy's
   off-combinator (pure-fused) cells carry 1199/1275 exact inversions — vs
   32/55 under the lexical probe. With cymatics live, the blend layer is not a
   mild nudge; it re-writes roughly 24 of the ~66 top-12 pairs per needle.
2. **The serving legacy baseline delivers less than the lexical baseline**
   (xl additive gd_id 0.40 vs lexical 0.62). Expected: abstain is ON
   (marker-only windows on weak retrievals) and the dense/SPLADE serving stack
   shifts ranking on these literal-needle beds (#250's "dense harmful on
   literal needles"). All three modes share the profile, so the A/B is fair.

## Results — Cell 2: enterprise_rag_10k_batched, semantic n=125 (fused arm)

Data: `docs/research/data/2026-07-12-blend-serving-receipt-cell2-erb10k-{legacy,off,scale_relative}.json`.
Zero per-question errors. (This driver has no inversion metric.)

| blend_mode | n | gd_id | gd_text | pool_present | median gold rank | mean gold-answer overlap | Δgd_id vs legacy |
|---|---|---|---|---|---|---|---|
| legacy | 125 | 0.360 | 0.376 | 0.856 | 10.0 | 0.539 | — |
| off | 125 | 0.248 | 0.304 | 0.856 | 11.0 | 0.346 | **−0.112** |
| scale_relative | 125 | 0.392 | 0.424 | 0.856 | 10.0 | 0.538 | **+0.032** |

`pool_present` is identical (0.856) across modes — the blend layer runs
post-fusion, so recall is untouched; the entire delta is ranking/selection into
assembly, exactly the pre-truncation mechanism the council flagged.

## Verdict — does the lexical-profile result replicate under serving profile?

**`off`: NO — half replicates, half inverts.** The mechanism half holds
perfectly: `blend_mode="off"` still zeroes the pure-fused-cell exact-inversion
floor (1199→0 xl, 1275→0 xl_clean), and it is the only mode that does. But the
delivery half **inverts**: where P1a measured off *improving* pure-fused
delivery (+0.12/+0.14 gd_id) with ≤0.04 additive-cell cost, the serving profile
shows off **losing delivery on all six cells** — gd_id −0.04..−0.11,
answerability −0.14..−0.18, and a −0.112 gd_id / −0.19 gold-overlap collapse on
10k semantic. The explanation is the pre-truncation re-sort itself: under the
lexical probe (cymatics off) the blend layer was mostly harmonic noise worth
deleting; under serving, cymatics carries real signal that selects better docs
into `max_genes` — the blend layer is **load-bearing**, and deleting it deletes
delivery. **`scale_relative`: YES, flat-to-positive replicates** (5 of 6 cell-1
rows flat-or-positive; the exception is xl_clean/off-combinator at −0.06 gd_id)
and it is the **best mode on 10k semantic** (+0.032 gd_id, +0.048 gd_text, best
answerability on every cell-1 row). Its inversion reduction is large but not
total: 1199→52 / 1275→97 (−96%/−92%) on pure-fused cells — a bigger residual
than the lexical 4/13. **Graduation recommendation therefore flips from P1a's
"ship off":** `off` is NOT serving-shippable; `scale_relative` is the surviving
candidate — scale-invariance mostly restored, delivery flat-to-positive, and no
answerability regression anywhere. Abstain/know: abstain (ON) produced no
anomaly — delivery deltas above already include its gating; know/miss emit
could not be measured on this path (below).

## Limitation — know/miss not measured

The two reused drivers call `build_context`, whose `ContextWindow` carries **no**
`know`/`miss` block (the know/miss agent contract is built only on the packet /
`/context` server routes). The in-process packet builder
(`context_packet.build_context_packet`) is reachable, but its `_query_genes`
re-runs retrieval via `genome.query_docs` **without** the blend layer, so a
packet-path know measurement would be **blend-insensitive** (identical across
the three modes) and therefore misleading. The faithful `blend_mode → know`
coupling lives on the `/context` route
(`server/helpers._compute_know_or_miss_block` reads the **blend-mutated**
`last_query_scores` produced by `build_context`), which needs the server path —
out of scope for this in-process receipt without new infrastructure (the audit
already sequences the `[know]` s_ref/g_ref re-fit **behind** blend_mode
graduation, so a know delta here would not gate the decision anyway). Abstain is
ON in the profile; its gate effect is already reflected in the delivery rates
above.
