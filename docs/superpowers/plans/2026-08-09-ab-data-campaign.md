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
- **Lane B — measurement (strictly sequential).** GPU encoder daemon + 56.7GB bed
  I/O is one shared resource. Every verdict run is **order-reversed paired** —
  arm-order drift already inflated the SPLADE "+0.10 recall" headline into a
  finding that had to be weakened.

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

1. **PKI is a precision instrument with a narrow trigger.** A layer that fires
   rarely but decisively will look "neutral" on aggregate recall@12 while being
   the sole reason a small class of queries works at all. This is precisely the
   #340 static-miss hypothesis: needles whose *body text* never lexically matches
   the query, but whose path/key structure does. The arm must be graded per-needle,
   not only in aggregate.
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
- **`_pki_weight` under RRF.** The tier is fed to the fuser with a weight that
  appears untuned since the additive→RRF switch.
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
