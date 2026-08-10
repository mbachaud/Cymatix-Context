# A/B data campaign — closing the unmeasured-layer backlog + PKI recall design

Date: 2026-08-09 · Branch: `claude/ab-data-cymatics-pki-dff9c6`

Driver: the seven "immediate no-regret actions" in
[`docs/benchmarks/2026-08-06-retrieval-layer-ledger.md`](../../benchmarks/2026-08-06-retrieval-layer-ledger.md),
plus the standing question *"how do we use PKI to increase recall?"*.

## Why this order

One dependency dominates the schedule: **delivered-gold instrumentation (#335)
must land before the PKI / tags / coact arms run.**

Rank-based recall@12 is structurally blind to two effects the council already
measured:

- the ANN gate at `min_genes=1` moves **15 vs 23 of 30** queries reaching the
  window with gold — a 27% swing in what the model actually receives, invisible
  to rank metrics;
- coact/harmonic is a post-rank pull-forward, "invisible to rank-based r@12
  **by construction**".

PKI, tags and coact are all plausibly post-rank or admission-shaped. Arming them
under an r@12-only ladder would produce a confident null that means nothing —
the exact failure mode of #354, where arm C was a byte-identical duplicate of
arm A and shipped an 18/18 "identical ranks" headline that had to be retracted.
**A null result that is too clean is evidence the treatment was never applied.**

## Lane structure

- **Lane A — code prep (parallel).** Gates, arms, metric plumbing, bug fixes.
  Partitioned by file tree so concurrent agents cannot collide: `cymatix_context/`
  is single-writer at any time (the two monoliths, #349, make this mandatory),
  `benchmarks/` is a separate lane.
- **Lane B — measurement (strictly sequential, and only against a FROZEN tree).**
  GPU encoder daemon + 56.7GB bed I/O is one shared resource. Every verdict run is
  **order-reversed paired** — arm-order drift already inflated the SPLADE "+0.10
  recall" headline into a finding that had to be weakened.

**Lane A and Lane B must not overlap in time.** File-scope partitioning is not
enough. Wave 0a measured this directly: during its run HEAD advanced
`fe2ef73 → 2259cbf → cf101bc → 32e8bad` and seven tracked files went dirty,
including `knowledge_store.py` and the ladder itself (+297 lines). A run that
compiles its imports at T and finishes at T+9min describes a tree that no longer
exists. **Freeze protocol: all code agents stop and commit, `git status` is clean,
the commit SHA is recorded in the receipt, and only then does measurement start.**

## Environment recipe (Wave 0a, verified)

Interpreter: **`F:/tmp/venv-cuda/Scripts/python.exe`** (torch 2.11.0+cu128, CUDA
available; RTX 3080 Ti 12GB, driver 596.36).
Do **not** use `F:/Projects/cymatix-context/.venv` — torch 2.13.0+cpu, no CUDA.

Both venvs carry an editable install pointing at a *different* checkout
(`venv-cuda` → `perf-slice1`). The ladder wins anyway via
`REPO_ROOT = Path(__file__).resolve().parents[3]` + `sys.path.insert`, verified by
neutral-cwd import. **Rebinding the editable install would hijack the live
`perf-slice1` worktree, so it was deliberately left alone.** Consequence: always
invoke the harness by its worktree-relative path, and for ad-hoc scripts export
`PYTHONPATH=F:/Projects/cymatix-context/.claude/worktrees/ab-data-cymatics-pki-dff9c6`.

Encoder daemon (ready in 37s, all four models on `cuda:0`):

```bash
PYTHONPATH="F:/Projects/cymatix-context/.claude/worktrees/ab-data-cymatics-pki-dff9c6" \
  "F:/tmp/venv-cuda/Scripts/python.exe" -m cymatix_context.encoder_daemon --port 11439
```

`cymatix.toml` has **no `[encoder_daemon]` section**, so consumers must export
`CYMATIX_ENCODER_URL=http://127.0.0.1:11439` or they silently encode in-process on
CPU and every timing figure below becomes meaningless.

## Bed-completeness trap (Wave 0a)

The `erb10k` / `erb50k` gold sets look bed-matched by name but are **470-needle
supersets carved for the full corpus**. On `erb_10k.db` only 7 of the first 30
needles have any gold present — so `r@12 = 0.233` is the arithmetic **ceiling**,
not a score, and both arms hit it exactly. Measuring recall on 10k/50k with those
sets measures a truncated ceiling.

**Bed-complete pairings only:**

| bed | docs | complete set |
|---|---|---|
| erb_100k / erb_250k / erb_500k | 100k–500k | **carve** (141/141 needles, 374/374 ids) |
| erb_829k_nosplade / erb_blob / erb_blob_probe | 829k | **blob** (469/469, 1279/1279); `blob_probe30` = 30-needle subset |

## Timing budget (Wave 0a, measured anchors)

| bed | measured rate | 4-arm order-reversed paired |
|---|---|---|
| erb_10k (0.70 GB) | 2.95 s / arm-query | — |
| erb_100k (6.45 GB) | 5.42 s / arm-query | 141 carve needles ≈ **1.7 h** |
| erb_blob_probe (56.7 GB) | 33.7 s / arm-query | 30 `blob_probe30` needles ≈ **2.2 h** |

`no_dense` is a 4× cost outlier (p50 10,498ms vs baseline 2,951ms) — arm choice
dominates any estimate. Full 469 blob needles would be **44–66 h: do not schedule.**
Free disk on F: 269 GB — room for one blob_probe copy plus a 100k copy, not more.

Sanity anchor: Wave 0a reproduced the committed 100k baseline exactly
(`r@12 = 0.800` vs `ablation_ladder_100k.json` `recall_at_k: 0.8`).

## Queue

### Wave 0 — env + cheap fixes
| Item | Work | Issue |
|---|---|---|
| 0a | Env provenance (worktree source is authoritative, not a stale site-packages copy), GPU daemon, smoke ladder, bed inventory, timing budget | gate for all measurement |
| 0b | entity_graph read-path flag leak — write-side ingestion knob gates a read path | ledger #1 (bug) |
| 0c | sema query-side auto-gate on stored-vector count (`genes.embedding` = 0 non-null rows on carve *and* blob) | #337 |

### Wave 1 — instrumentation (blocks Wave 3)
| 1 | delivered-gold as a first-class receipt metric; per-needle granularity required so the #340 static-miss set can be cross-tabbed individually | #335 |

### Wave 2 — gates + arms
| 2a | PKI retrieval-side gate (`use_sr` per-call-resolve pattern) + `no_pki` arm | #334 |
| 2b | tags gate + `no_tags` arm (2.58GB, also ungated) | ledger #6 |
| 2c | coact/harmonic arm — only meaningful once delivered-gold exists | ledger #7 |
| 2d | synonyms retune arm (1 entry load-bearing, 2 pool-bloating) | ledger #3 |

### Wave 3 — measurement (sequential)
| 3a | #354 cymatics arm-C re-run, both beds — harness fixed, cheap, closes a retracted claim | #354 |
| 3b | Ladder @100k: baseline / no_pki / no_tags / no_coact / synonyms, order-reversed paired | #334, #335 |
| 3c | Static-miss cross-tab: do the 99/469 never-scoring needles move under PKI? | #340 |
| 3d | PKI + tags @blob (829k) — the 11.1GB decision | #334 |
| 3e | splice/headroom quality A/B at fixed candidates — needs an answer-quality judge that does not exist yet (build-then-measure) | #336 |

### Wave 4 — synthesis
| 4a | PKI amplification design, driven by 3b/3c/3d | #334 |
| 4b | Ledger update, issue comments, close-outs; SPLADE default-flip decision (data already complete per the blob addendum) | #333 + all |

## PKI: what the code actually does

`path_key_index(path_token, kv_key, gene_id)` — the materialized cartesian of each
document's path tokens × its kv keys. The Tier-0 scorer
(`knowledge_store.py:2747-2840`) fires on:

```sql
WHERE path_token IN (query_terms) AND kv_key IN (query_terms)
```

Both legs are drawn from the **same** query token list, so PKI only fires when a
single query contains *both* a path-ish token and a key-ish token
(e.g. "what **port** does **cymatix** use" → the `(cymatix, port)` pair).

Scoring is IDF-shaped and deliberately so: `PKI_BASE(10) / max(pair_cardinality,
PKI_FLOOR(2))`, pairs above `PKI_NOISE_CUTOFF(200)` skipped, total capped at
**12.0 — "roughly 3x the strongest single signal"**. When PKI fires it dominates
the blend.

Two consequences worth stating before any measurement:

1. ~~**PKI is a precision instrument with a narrow trigger.**~~ **REFUTED
   2026-08-09 — see [ledger addendum 5](../../benchmarks/2026-08-06-retrieval-layer-ledger.md#addendum-5-2026-08-09-pki--tags-armed-and-measured-at-two-scales-354-re-run-methodology-results).**
   The hypothesis was that a layer firing rarely but decisively would look
   "neutral" on aggregate recall@12 while being the sole reason a small class of
   queries works at all — the #340 static-miss class: needles whose *body text*
   never lexically matches the query, but whose path/key structure does. Measured:
   PKI fires on **141/141** needles at 100k (median 900 candidate genes/query) and
   **30/30** at 829k (median 2939). It is a broad, weak tier, not a narrow decisive
   one. The per-needle grading the plan demanded is what killed the hypothesis: the
   static-miss set is bit-identical with PKI on, off, and at weights 2/3/4.
2. **The storage case and the recall case are independent.** #165 already proved
   on the Onyx corpus that `idx_pki_lookup` is never used (EQP: the live lookup
   rides `sqlite_autoindex_path_key_index_1`, of which it is a strict prefix) and
   that 38% of rows sit in pairs above the noise cutoff and can never contribute
   score — both **provably score-invariant** to remove (40/40 identical on the
   replicated scorer). `/admin/compact-pki` implements this. So compaction does
   not wait on the recall verdict, and the recall verdict must not be used to
   justify compaction.

### Amplification hypotheses (to be tested in Wave 4, not assumed)

- **Query-side path-token extraction.** PKI matches raw query terms today. Queries
  carrying file paths, module names or identifiers could be tokenized path-wise to
  fire PKI more often.
- **Relax the AND.** Both legs come from one token pool; a path-only match at
  lower weight would widen the trigger — at obvious precision risk, which is why
  it needs the delivered-gold metric to grade.
- ~~**`_pki_weight` under RRF.**~~ **TESTED AND CLOSED 2026-08-09.** The
  commit-record half is true — `pki_weight = 1.0` was authored in the Stage-3 RRF
  PR (`e2be503`, #55) while `fusion_mode` still defaulted to `"additive"`, and
  became load-bearing uncalibrated when `e4889f6` (#247) flipped the default. The
  implied half is false: a w2/w3/w4 sweep regresses **monotonically** (14 / 28 /
  33 needles worse) and r@12 drops to 0.6738 at w4. Shipped 1.0 is at or above
  optimal.
- **Cross-read with #327.** Authority boost is IDF-*blind* and grants +2.0 to 88%
  of its own corpus by matching query terms against the absolute path. PKI solves
  the same matching problem IDF-*aware*. #327's fix may simply be to adopt PKI's
  cardinality weighting.

## Standing rules for every agent in this campaign

- Evidence before assertions. Paste the command and its real output; never claim a
  pass without it.
- Run tests and long jobs in the foreground. Backgrounding pytest and waiting on a
  notification stalls the agent.
- A refuted premise is a result. If the ledger or this plan is wrong, report that
  rather than forcing the work.
- No behaviour changes as a side effect of adding a gate. Shipped defaults must
  resolve byte-identically, or every arm that runs afterwards is poisoned.

## Outcome (2026-08-09, Wave 4)

Evidence recorded in
[ledger addendum 5](../../benchmarks/2026-08-06-retrieval-layer-ledger.md).
Verdicts, with the confirming receipt named:

| Wave | Item | Outcome |
|---|---|---|
| 0a | env provenance, GPU daemon, bed inventory, timing budget | done; `wave0a_*.json` |
| 0b | entity_graph read-path flag leak | fixed in master (`1fd34b4`) — and it **breaks comparability** with every pre-2026-08-06 receipt |
| 0c | sema query-side auto-gate | armed by probe (`2259cbf`) |
| 1 | delivered-gold receipt metric (#335) | shipped (`ab03426`); first use surfaced an **empty-window class** (2/141 queries at 100k discard a rank-1 gold) |
| 2a | PKI gate + `no_pki` arm (#334) | shipped (`32e8bad`) |
| 2b | tags gate + `no_tags` arm | shipped (`085cec6`) — **Tiers 1+2 only**; `lex_anchor` still reads `promoter_index` |
| 2c | coact/harmonic arm | **not run** — deferred |
| 2d | synonyms retune arm | **not run** — Addendum 4 already named the movers |
| 3a | #354 cymatics arm-C re-run | done; retraction **confirmed at the read site**, re-run verdict **null** |
| 3b | ladder @100k, order-reversed paired | done, 141 carve needles |
| 3c | static-miss cross-tab | done — static-miss set **bit-identical across all six arms** |
| 3d | PKI + tags @blob (829k) | done, 30 `blob_probe30` needles, both orders |
| 3e | splice/headroom quality A/B | **not run** — still needs an answer-quality judge (#336) |
| 4a | PKI amplification design | **cancelled by the data.** The tier fires on 100% of needles at both scales and every weight increase regresses. There is nothing to amplify; the design task is replaced by a removal decision. |
| 4b | ledger update, issue comments, follow-ups | done |

**Hypotheses this plan carried that the data refuted:** the "narrow-trigger
rescue tier" framing of PKI (fires on 141/141 and 30/30), and the implication
that an uncalibrated `pki_weight = 1.0` meant PKI was underweighted (raising it
only regresses).

**What the campaign added to the plan's own method rules:** order-reversal is a
measured non-effect on rank/delivery metrics (bit-identical per-needle, both
beds) and moves only wall time — but the reversal is what established that, so
retain it whenever latency is the claim. `--reverse` did not exist before this
campaign (`7294526`). And `RemoteCodec`'s silent CPU degradation means the
freeze protocol needs a companion rule: **check encoder-daemon liveness before
and after every run**; `remote_dense: true` in a receipt is not proof the daemon
served the query.

**Still owed** (filed as issues): a `no_lex_anchor` arm for the real 2.58GB tags
storage decision; `tag_exact` vs `tag_prefix` attribution; the `[cymatics]
peak_width` 3.0-vs-1.55 config/doc mismatch.
