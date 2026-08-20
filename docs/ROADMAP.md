# Cymatix Roadmap

> Canonical forward-planning doc. **Rewritten 2026-07-27** against the 0.8.5 clean break.
> Supersedes the 2026-07-03 edition, archived verbatim at
> [`docs/archive/2026-07-03-roadmap.md`](archive/2026-07-03-roadmap.md) — that document's
> bench-gate decision rules and WS2/WS3 outcome box remain the historical record; everything
> it sequenced as "next" has since shipped, closed, or been superseded.
>
> Detailed specs still live in their own files; this doc is the sequencing layer on top.

## Status snapshot (2026-07-27; release facts updated 2026-08-20)

- **Version:** 0.9.0 on master (version bumped 2026-08-20 in the v0.9.0
  release-prep pass, gate tracker #377; previous release v0.8.6, tagged
  2026-08-05). The helix → cymatix rename is a
  **completed clean break** —
  no alias package, no `helix*` console scripts, no `HELIX_*` env mirror, no `helix.toml`
  fallback, no `helix_*` MCP tools. `tests/test_no_helix_leftovers.py` is the standing guard.
- **Release state:** ~~tag and GitHub Release still owed~~ — **done**: v0.8.5 and v0.8.6
  are both tagged; the v0.8.6 GitHub Release published 2026-08-06.
- **Test baseline:** green. The long-standing sharded-adapter parity failure was fixed on
  master by `a0f3fe3` (2026-07-22).
- **Open issues:** 15. Two are closable on sight (#311, #307); the rest are triaged below.
- **The rig is idle.** No bench chain is running locally. The blocking work is now
  **cross-box** — see the data pipeline section.

### What changed since the previous edition

| Previous roadmap item | Outcome |
| --- | --- |
| The bench gate (arm B vs BM25, arm C vs +1pp) | **Resolved 2026-07-20.** Arm C cleared the threshold on the held-out ContextBench re-run (packet +2.8pp line / +3.8pp sym). Dark-ship chosen deliberately over "merge default-on" — SIKE 2026-07-19 showed a prose-bed regression under the current code-query gating. |
| Merge queue #230 → #231 | **Both merged** (`1161092`, `8c149f2`). `[ingestion] symbol_graph` ships default `False`, fully available opt-in. |
| Phase 3: #222, #223 | **Both closed** (2026-07-12, 2026-07-13). |
| Phase 3: #204, #93, #209 | **All closed** (2026-07-18, 07-10, 07-18). |
| Scoring-invariance lane: #255, #256 | **Both closed** (2026-07-18, 07-10). |
| Phase 2: "tag the 0.8.0 baseline" | **Overtaken.** 0.8.0 tagged 07-22; 0.8.5 clean break landed 07-25. |
| Housekeeping checklist | **Still entirely undone** — carried forward below. |

Three issue clusters now dominate the board and **postdate the previous roadmap entirely**:
the sharded structural gap (#275 umbrella), the know-gate trust problem (#287), and the
semantic-arm admission ceiling (#260).

---

## The three live tracks

Everything on the board sequences under one of these.

### Track 1 — Sharded structural gap (#275 umbrella)

The sharded path trails unsharded by **+31.1pp recall@10 / +30.3pp MRR** on xl. Per-shard
fetch depth is ruled out (#222, confirmed null). This is disclosed in the README as of #318;
"local-first at scale" is scoped to the unsharded engine until it closes.

Live sub-issues: **#264** (doc-type ×1.15 boost decisive under RRF margin compression),
**#265** (global-IDF splice scale-corrupt under RRF). Both have **fixes shipped default-inert**
in #292; what remains is a graduation decision, not a fix.

**The independent action that needs no bench:** `sharding.py:251` hard-wires
`_dense_embedding_enabled = False`, so **production sharded mode runs with no dense recall at
all**. That is a product decision owed regardless of how the A/B lands, and it confounds every
sharded-vs-unsharded number measured so far.

### Track 2 — Know-gate trust (#287, absorbing #239)

The `/context/packet` know/miss contract is the core of the "AI is the first-class user"
thesis, and its `confidence` scalar is a **structural zero**: 0% `know_emitted` across all
500 ERB questions, on both fusion modes, including provably-correct retrievals.

Root cause is settled and is a **location bug, not a ranking bug**: the shipped `b1`
(`top_score`) coefficient is negative, so better retrieval yields lower confidence, and a
structurally perfect retrieval scores 0.4137 < `emit_floor` 0.45. Every know-feature is
oriented higher = stronger, so every coefficient must be ≥ 0.

**Council verdict: HOLD — 0/5 would_ship.** No operating point is shippable today. The
best-diagnosed direction is **V2** (swap the scale-sensitive `top_score` / `score_gap` for
`top_dominance` + `coordinate_crispness`). #318 already labels the scalar experimental in the
README, so this no longer blocks doc honesty.

**#239 is now the historical parent; #287 carries the forward plan.** Work the latter.

### Track 3 — Retrieval profiles → navigation benchmark (#205 → #208)

#205's 3-layer implementation plan was posted 2026-07-16 as an explicit review artifact and
**no code has been written since**. It is the critical path to #208 (SNOW-2), which is blocked
two hops upstream and needs no respec.

Governing rule (council): every PR ships **default-inert** — untouched configs reproduce
today's constants byte-identically; default flips are separate receipt-gated PRs. No profile
or override may set `[know]` keys (that gate belongs to #287).

**New as of 2026-07-26:** the cross-box cell-5 decomposition (below) hands Layer 1 a measured,
cheap lever — `fts5_candidate_depth`. See the next section.

---

## Cross-box data pipeline (cc-exchange)

Collaborator receipts arrive through `github.com/addiplus/cc-exchange`. Two threads are live
and **both currently block on us**. This section is the ledger; nothing here is optional
sequencing — the beds and the box are the collaborator's, and each stalled turn costs a window.

### Owed BY us — clears two blockers on `sike-run1` (gates #221)

The SIKE Run-1 ladder is fully accepted on the collaborator side and cannot start. Two items:

1. **Gold pack (raised 2026-07-25, `sike-run1/0005`).** `scripts/bench_chain/sike_bed_ingest.py`
   `_resolve` requires every `gold_source` substring to resolve to a real file under `--roots`.
   The gold set is **50 needles / 64 distinct files across 6 repo roots**; **51 of the 64 live
   in sibling repos** (`Education/`, `BookKeeper/`, `CosmicTasha/`, `two-brain-audit/`,
   `MaxExpressKit/`) that do not exist on the collaborator's disk. Without them the ingest step
   cannot inject gold, `recall_rows n == 50` per bed cannot be met, and the xl
   `gold_delivered_rate >= ~0.75` validity gate cannot pass.
   **Action:** ship a tarball of the 64 files preserving `<repo>/<path>` prefixes (a few hundred
   KB, text and code). This is the collaborator's stated preference — it keeps the published bed
   checksums valid and keeps the ingest step in the loop so both runs stay byte-comparable.
2. **The xl decontamination predicate matches zero rows (raised 2026-07-26, `sike-run1/0006`).**
   `DELETE FROM genes WHERE source_id LIKE '%worktree%'` matches **0 rows** in the published
   41,898-gene `xl.db` — the `source_id` values on that build are Windows paths under
   `F:\Projects` and none contain the substring (0 matches in `repo_root` and `gene_id` too;
   61 in `content` only, which the predicate does not touch). The documented recipe expects
   46,479 → 41,803. `xl_clean.db` was deliberately **not** created pending our answer.
   **Action:** answer with an updated predicate, an explicit gene-id list, or a ruling that the
   shipped 41,898 is already clean and the 41,803 expectation is stale for this build.
3. **Two small confirms owed in the same reply:** the code pin (collaborator proposes tag
   `v0.7.2b1` = `a20817a`; say whether `b83e1bc` is preferred) and the `gemma4:26b` /
   `gemma4:31b` / `gemma4:26b-a4b-it-q4_K_M` registry + rough blob sizes.
4. **Post-0.8.5 note that must ride along:** the collaborator is pinned pre-rename and uses
   `HELIX_USE_SHARDS=1`. On master that env var is **`CYMATIX_USE_SHARDS`**. Any pin at or
   after `876698c` needs the new name.

### Owed TO us — already delivered, needs acting on

**`spark-erb-receipts/0010` (2026-07-25) — set-identity receipt.** The semantic ceiling is a
**fixed set**, not a config artifact: the same **62 of 125** semantic questions miss gold in all
six primary arms (cell-1 lexical, all four cell-2 gate cells, cell-4 with SPLADE live);
intersection = union = 62, minimum pairwise Jaccard **exactly 1.000000**, every arm at
`pool_present` 0.504. Same shape at all-types n=469: the same **108** miss everywhere.
Independently verified, 184/184 checks.

**`spark-erb-receipts/0011` (2026-07-26) — cell-5 admission decomposition.** Run once against
that fixed 62-set. This is the most actionable retrieval datapoint we have:

| leg | recoverable @50 | @200 | @1000 |
| --- | --- | --- | --- |
| fts (lexical) | 14 | 24 | **45** |
| splade | 2 | 8 | 17 |
| dense | **0** | **0** | **0** |
| any leg | 15 | 27 | 48 |

- **Dense recovers zero at every fetchable depth.** It *holds* gold for 59 of the 62, but at
  full-corpus ranks of ~19,000–203,000 — the cell-1 rank cliff restated at the depths that
  actually govern admission. This **refutes the collaborator's own 0010 prediction**, and it
  refutes query-encoding work as the semantic-arm lever.
- **Plain FTS at depth 1000 recovers 45 of 62 — 73% of the missing-gold mass, for a config
  change.** Our knob for exactly this is `[retrieval] fts5_candidate_depth`
  (`config.py:468`, default 0 = auto = `limit * 2`; consumed at
  `knowledge_store.py:2184`).
- Query-encoding work would target only the **SPLADE-marginal 3** (any-leg 48 − fts 45).
- Chunk/ingest surgery is implicated only for the **14 inadmissible** at depth 1000 in every
  leg — and even those hold dense full-corpus ranks, so "inadmissible" here is depth-relative,
  not absent from the bed.
- **Bed footnote — do not misreconcile:** the blob carries **850,379** genes, not the sharded
  fixture's 850,578. `shard_to_blob` dropped 199 rows at build on 07-17. The blob is byte-frozen
  since cell-4 close, so cells 1/2/4, the set-identity receipt, and cell 5 all pertain to the
  850,379 bed.
- **One observation for us to verify in our own code:** `cell1_fused`'s per-record
  `pool_present` vector is byte-identical to `cell1_lexical`, median `pool_size` 50, while
  `cell1_dense` on the same run has median `pool_size` 156,941. In the fused arm the dense
  candidates do not appear to widen the delivered pool at all.

**Cell 3 (the #275 shard A/B) is in flight** on the rebased fixture, pacing 2–5 days out from
07-25 — i.e. **due now**. The ruling stands: no shard numbers enter #275 until the adapter's
smoke gate is green on the collaborator's bed.

**Adapter audit (0010): 6/7 claims confirmed, zero defects — but three real follow-ups for us:**

1. **The smoke gate has a blind spot.** `scripts/rebase_shard_fixture.py` derives its probes
   from the same `fingerprint_index` table routing consults, so the gate asks whether the
   fixture can retrieve its own hottest tags. **It cannot detect a partially truncated
   fingerprint index** — precisely the failure that yields plausible low numbers instead of an
   honest red. Fix: support explicit `--probe` terms drawn from the question set, and
   cross-check `fingerprint_index` row count against the sum of shard gene counts.
2. **`SHARD_FIXTURE_RELOCATION.md` needs two lines:** (a) a relocated fixture serves zeros
   through the normal consumer path unless `CYMATIX_USE_SHARDS=1` is exported — the verify-only
   gate passes anyway because the tool constructs `ShardRouter` directly; (b) serve verification
   opens the coordinator through `open_main_db`, which connects **read-write** and sets
   `journal_mode=WAL`, so "verify-only" is not byte-read-only against the coordinator — back it
   up first.
3. **Doc nuance:** the path-matching floor is weaker than "longest suffix" implies — acceptance
   is any candidate sharing at least one trailing component, so a basename-only hit qualifies,
   and longest-suffix only breaks ties among qualified candidates.

---

## Issue board (triaged 2026-07-27)

### Ready now — no gate

| # | Title (short) | Action |
| --- | --- | --- |
| #311 | Sharded adapter missing `_sweep_symbol*` | **Close.** Fixed on master by `a0f3fe3` (2026-07-22); `tests/test_sharded_adapter_parity.py` is green (12 passed). |
| #307 | Same parity failure, filed from the #231 merge | **Close as duplicate of #311**, same receipt. |
| #313 | Fresh install: 63 tests fail without `en_core_web_sm` | Implement the agreed (1)+(3) shape: legible error in `_get_nlp()` naming the download command; `spacy.util.is_package()` skip guard on the affected tests; pinned install line in `setup-cymatix.ps1` + `docs/SETUP.md` **not** in published extras (PyPI rejects direct-URL deps and `publish.yml` uses trusted publishing); reconcile the `spacy>=3.7` floor against the 3.8.0 model wheel. |

### Sequenced feature work — unblocked, needs a start

| # | Title (short) | Action |
| --- | --- | --- |
| #219 | Config unification epic | Slices 1–4 all merged; **slice 5** (dark-feature decision: `ray_trace` / `seeded_edges` / `sr`) is the cheapest remaining and unblocks doc honesty. Also tick the slice-3 checkbox — the 2026-07-13 audit confirmed #241 shipped it. |
| #207 | De-hardcoding wave 2 | **Item 7 only** remains: de-Windows tray edges (`com.swiftwing21` plist id + orphaned auto-migration, PowerShell-only installer spawn, `.bat`-only restart). Items 1–6 and 8 are done. |
| #205 | Retrieval profiles, 3 layers | Review the 2026-07-16 plan, then build **Layer 1 first**. The cc-exchange cell-5 receipt now hands it a measured lever: an `fts5_candidate_depth` sweep is the highest-value Layer-1 experiment on the board. |
| #208 | SNOW-2 navigation benchmark | No action. Stays blocked on #205; spec current, no respec. Re-date when #205 lands. |

### Data-gated — the action is collecting or receiving

| # | Title (short) | Action |
| --- | --- | --- |
| #260 | Semantic arm: 20% gold_delivered | **Reframed by cell-5.** The lever is depth on the lexical leg, not dense query encoding — dense recovers 0/62 at every fetchable depth. Run the local `fts5_candidate_depth` sweep; chase the 850K `rrf_gate` receipt (off vs `top_m` 5/10/20) that graduates the rank-gated RRF knob; verify the fused-arm pool-width observation against our code. |
| #275 | Sharded structural gap (umbrella) | Receive cell 3. Independently: make the **dense-off product decision** for sharded mode (`sharding.py:251`). Fix the adapter smoke-gate blind spot + land the three `SHARD_FIXTURE_RELOCATION.md` doc lines. |
| #264 | Doc-type boost decisive under RRF | Fix shipped default-inert (#292). Needs a full seeded-bed bench to graduate a default flip. Honest finding on record: **`doc_type_boost_mode="off"` is the mode that satisfies #121's hard constraint under RRF**; `"rank"` still flips on the adversarial fixture. |
| #265 | Global-IDF splice scale-corrupt under RRF | Capability guard shipped (#292) — byte-identical no-op under RRF, #182 contract preserved on additive-pinned shards. Follow-up (rrf-native global-IDF as a rank input to per-shard fusion) is deferred, not lost. |
| #221 | SIKE scale-sweep re-baseline | **Blocked on us**, not on the rig: ship the gold pack and answer the decontam question (above). Then the 829K injection point + the README re-admission write-up with fixture/date citations. |
| #287 | Know/miss gate RRF-era recalibration | **Ship nothing.** Council HOLD stands. Next step is the V2 feature swap, not another re-fit of the current five. Second actionable: **harden the #268 AUC gate** to median (or worst) held-out AUC across k splits — at n=300 the seed alone swings the verdict 0.36 → 0.71. |
| #239 | know-confidence anti-signal | Historical parent of #287. Fold forward and close, or relabel as the tracking record. |
| #284 | AssetOpsBench external eval | NO-GO stands; issue is now a tracking record for the gated program. Next slice is **docs-only**: upstream appetite issue draft + redistribution-clean corpus licensing inventory, 3-week kill-switch. |

---

## Housekeeping (carried forward, still undone)

The 2026-07-03 forensic inventory is now three weeks stale and must be **re-derived**, not
executed off the old list. `git branch --merged origin/master` returns nothing useful — the
branches were *squash*-merged and are undetectable that way, so deleting off a stale list risks
nuking live work.

- [ ] **Branches** — re-derive the squash-merged set with a content-equality check, then delete.
      The 2026-07-27 pass found one live example worth noting: `bench/blend-serving-receipt` is
      **fully superseded** (its receipt landed via #282 and the rename sweep de-helixed the probe
      TOMLs) and would only re-add three stale-named duplicates.
- [ ] **Worktrees** — prune stale entries, then `git worktree prune`.
- [ ] **Stashes** — 8 outstanding; glance at the token-count helper before dropping.
- [ ] **Root runtime clutter** (all gitignored, local-disk tidiness only): root-level
      `genome.db{,-shm,-wal}` — verify nothing points at the root copy rather than
      `genomes/main/` — plus `metrics.json`, `logs/`, `overnight_logs/`, `cwola_export/`, caches.

## Docs refresh (remaining from the previous edition)

- [ ] `[ribosome]` backend lists (README + CLAUDE.md): verify which values are actually honored
      and drop or mark-legacy the rest. *(Needs a code check first — do not edit blind.)*
- [ ] README "Proof (30 seconds)" table + `docs/benchmarks/BENCHMARKS.md` (last refreshed
      2026-05-28) — re-admit numbers only from a valid re-baseline. **License caveat:**
      ContextBench / CodeRAG numbers stay internal until cleared.
- [ ] Commit or reconstruct `cymatix_probe_nosema.toml` (referenced by
      `code-track-regression-log.md`, absent from the tree).
- [ ] Minor: README `[ingestion]` add `hybrid`; note `[mem_sync]` is consumed out-of-band;
      document `[vault]` / `[hardware]`.

---

## Sequenced plan

**Now — release + unblock the collaborator (nothing here needs the rig):**

1. ~~Tag `v0.8.5` and publish the GitHub Release~~ — **done** (v0.8.5 tagged; v0.8.6
   followed 2026-08-05, Release published 2026-08-06).
2. Close #311 and #307.
3. **Reply on `sike-run1`**: gold pack + the decontam ruling + the two small confirms + the
   `CYMATIX_USE_SHARDS` rename note. This is the single highest-leverage item on the board —
   it is the only thing standing between us and a full SIKE Run-1 ladder on a second box.
4. Land #313 so a fresh clone is green.

**Next — the cheap measured lever:**

5. `fts5_candidate_depth` sweep on our own beds, replicating the cell-5 ladder (50 / 200 / 1000)
   against the semantic arm. This is simultaneously #260's reframed lever and #205's Layer-1
   auto-calibration input.
6. Verify the fused-arm pool-width observation (does dense widen the delivered pool at all?)
   against our code path.
7. Make the sharded dense-off product decision (#275 suspect 1); fix the adapter smoke-gate
   blind spot and land the three relocation-doc lines.

**Then — feature work, in cost order:**

8. #219 slice 5 → #207 item 7 → #205 Layer 1 (now data-fed) → Layers 2 and 3 → unblocks #208.
9. #287 V2 feature swap + #268 gate hardening. Ship nothing on the current five features.

**Receiving:**

10. Cell 3 (#275 shard A/B) — due now. 850K `rrf_gate` receipt (#260 graduation cell).
11. Once #221 is unblocked and run: README bench-number re-admission, then a fresh baseline tag.

---

## 2026-08-06 addendum — Fork-1 / retrieval-economics era

Everything below post-dates the 2026-07-27 triage. Landed on master via
#330 + #331 (perf instrumentation, encoder daemon + RemoteCodec); receipts
and fixes ride PR #332. Evidence base:
`docs/benchmarks/2026-08-06-retrieval-layer-ledger.md` (+ the fork-1
receipts and worker-sweep docs).

### New Track 4 — Retrieval-layer economics (receipts-gated)

| # | Item | State |
| --- | --- | --- |
| #333 | SPLADE: no benefiting scale — profile default off, query-side #164 gate, compaction | Ready; 4-scale receipts in hand; splade-less 829k bed carved (34.4GB) |
| #338 | FTS5 external-content rebuild (−8.67GB, signal-neutral) | Ready; with #333 → blob ≈28GB, RAM-cacheable |
| #334 | PKI + tags: add gates + ladder arms (11.1GB combined, unmeasured) | Ready |
| #336 | Splice/headroom answer-quality A/B (owns the p95 tail) | Ready; blocks trim-as-default |
| #337 | Sema query tier removal decision (null even with vectors) | Ready; auto-gate landed |
| #335 | Delivered-gold receipt metric + ANN `min_genes` sweep | **Metric SHIPPED** (2026-08-07, in the ladder per-needle: `delivered_gold*`, `final_rank_of_first_gold`, `gate_kept`); blocker retired — re-read ANN-gate/coact/synonym verdicts on the delivery basis |
| #341 | Rerank production wiring (pre-cap top-50) | **SHIPPED default-off** (2026-08-07): probe GO replicates (+6.7pp rank AND delivered on probe-30, 0 losses, summary+domains pair text); negative on no-headroom populations; dogfood cost +32–180% relative → gating is population-headroom-first. Receipts: `2026-08-07-rerank-wiring-receipts.md` |
| #340 | Static-miss class: 99/469 needles, per-needle receipts | The recall workstream's quantified target; pairs with #260's depth sweep. #341 receipts confirm the boundary empirically: rank 81–516 golds pass through rerank untouched; near-cutoff (≤depth 50) is convertible |
| #339 | Wobble second source + determinism NO-GO (tracking) | Record; no action unless stability is required |

### Effects on the existing tracks

- **Track 1 (#275):** dense-off product decision inherits the ledger's
  claim-1 warning — cap the non-ANN candidate list before any dense-off
  ships, or the splice explosion applies per shard.
- **Track 3 (#205 → #208):** Layer 1 now has its evidence base (the
  ledger + static→dynamic map); the `fts5_candidate_depth` sweep doubles
  as the #340 entry-path lever. #208 unchanged, still gated on #205.
- **Operating profile (this box):** GPU encoder daemon +
  `CYMATIX_EXECUTOR_WORKERS=4`, trim, `splade_enabled=false` on the
  34.4GB bed — full 469-needle benchmark = 2.65h at c=1 (was 18-20h).
