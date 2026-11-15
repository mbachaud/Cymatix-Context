# BENCHMARK-TIMELINE ADVERSARIAL AUDIT — Helix EnterpriseRAG bench
**2026-06-02**

The load-bearing fact across this entire audit is an **encoder discontinuity**: v0.5.0 had **no BGE-M3 dim-1024 dense tier** and a 0.35 ANN cut that admitted everything (`fec9141` message). BGE-M3 Tier-0 (`f46460f` #140) + the 0.58 recalib (`fec9141` #142) land **after** v0.5.0, first shipping in v0.6.0. So any recall/semantic number measured on v0.5.0 is from a *different retrieval stack* and does not transfer. Layered on top: the live daemon is a **worktree (`vibrant-easley-73d68a`, HEAD `191ce31` + dirty), not v0.6.2**, and it *reimplements* rather than inherits the shipped RAM fixes — so the canonical numbers are "worktree-with-knobs," not "released-helix."

---

## (1) Version timeline × bench-relevant changes

Linear ancestry (confirmed via `merge-base --is-ancestor`): v0.5.0 → 826d053 (stale local master) → v0.6.0 → v0.6.1 → v0.6.2. The live worktree forks from **OLD master 826d053** and does **not** have v0.6.0/v0.6.1/v0.6.2 as ancestors.

| Version | Ref / commit | Date | Bench-relevant change | Retrieval-quality impact |
|---|---|---|---|---|
| **v0.5.0** | tag `e1bf20e`; stale local master HEAD `826d053` (2026-05-17) sits after the tag | 2026-05-08 | **NO BGE-M3 dim-1024 dense tier** (`f46460f`/`fec9141` confirmed NOT ancestors). Retrieval = lexical/BM25 + dim-256 dense with 0.35 ANN cut (admits everything). No `CHANGELOG.md` at this tag. | **Different stack.** Recall/semantic numbers here do NOT transfer to v0.6.x. |
| **826d053** | stale/un-pulled local master | 2026-05-17 | Mid-cycle: BGE-M3 Tier-0 merge (`f46460f`) **present**; v0.6.0 scaling/SQL-cap fixes **absent**. Cross-shard BM25 IDF norm (#119), RRF merge (#106), doc-type boost (#132), multi-gold needle 10→50 (#122/#125). | "BGE-M3-present, v0.6.0-fixes-absent." The worktree's parent. |
| **v0.6.0** | tag `2d3b7d0` (`9cd7fe7`) | 2026-05-28 | **First tagged release with BGE-M3 dim-1024 + 0.58 ANN cut.** SQL-variable-cap IN-batching (`23b1e80` #163) — fixes silent shard-drop on SPLADE-off path. Tagger ReDoS fix (#155/#162). `--isolated` leak-free answer bench (#170); MRR metric (#137). | **First clean recall baseline.** Numbers before `23b1e80` on SPLADE-off path are **understated**. |
| **v0.6.1** | tag `b7af5b0` (#174); bundles #172 + #173 | 2026-05-30 | BGE-M3 process-wide singleton `get_shared_codec` (#173, 120GB→7GB). Concurrent shard fan-out `HELIX_SHARD_WORKERS` (#172, "ranked output byte-identical to serial"). Optional fp16 dense (promotes to fp32 in matmul). SQLite `mmap_size=0`+tiny cache cap. | **Perf/RAM only.** Documented ranked-output byte-identical → recall transfers **unchanged** from v0.6.0. |
| **v0.6.2** | `3173c47`/`98514fd` (#175); **NOT tagged locally** | 2026-05-30 | `HELIX_MEM_PROFILE` RAM-aware SQLite budget (relaxes v0.6.1's over-throttling mmap=0). Profiles auto/aggressive/**conservative (byte-identical to v0.6.1)**/<N>gb. | **SQLite I/O posture only.** Scoring untouched → recall transfers **unchanged** from v0.6.0/v0.6.1. |
| **LIVE worktree** | `vibrant-easley-73d68a` @ `191ce31` + dirty; branch `perf/dense-prefilter-via-splade-candidates`; forks `826d053` | edits to 2026-06-02 | BGE-M3 Tier-0 (inherited) + **its OWN ANN recalib `f6286fb`** (parallel to `fec9141`). SPLADE pre-filter `9328111` (#159, default-off). Per-gene budget allocator `c419abc`. Committed: rerank clamp→sigmoid `7fec674`, pool-widening `191ce31`. **Uncommitted dirty:** singleton/batched-IN/fan-out reimpls (parallels of #173/#163/#172, NOT inherited). | **NOT any release.** New levers + reimplemented (not inherited) perf. Worktree↔v0.6.2 byte-identity is **unverified**. |

---

## (2) "What was tested on what version" matrix

| Conclusion | Version measured on | Gate / axis / denom | Status |
|---|---|---|---|
| Recall@10 = **29.8%** (canonical) | **Live worktree** (`hardware_investigation:30`) | splade-on · mg10 · binary · 470 | **version-contingent** — never run on v0.6.2 |
| Recall@10 = **27.66%** | Live worktree (Max + Spark) | splade-off · mg1 | version-contingent; different gate, not the same lineage as 29.8% |
| Semantic fused **2.4%@10** / dense 7.2 / 28.8@200 | Live worktree, 2026-06-02 | fused · n=125 · cross-join | **holds** (robust baseline) but worktree-only |
| EnterpriseRAG **83% (10K) / 71% (50K)** | **Pre-v0.6.0** subset, 2026-05-21 (`bench_investigation_2026-05:93-97`) | mg10 · sparse-only · n=100 | **superseded** — wrong version (pre-BGE-M3) + sparse-only ceiling |
| Out-of-domain **0% hallucination, "corpus-invariant"** | Pre-v0.6.0, keyword-heuristic 2026-05-21 | M2 heuristic | **superseded/dead** — refuted to 15% by Opus judge; wrong method + wrong version |
| Full-500 **correctness 13.8% / 15% halluc / doc_recall 17.6%** | Live worktree, Opus judge 2026-06-01 (`bench_session_2026-06-01:12-17`) | answer-correctness · n=500 | **holds** as measured; **different axis** from recall |
| CE rerank = **net-zero on semantic** | Live worktree, clamp-fixed 2026-06-02 (`bench_session_2026-06-02:16`) | n=125 · PRE 2.4→POST 2.4 | **holds** — the only fully version-current, e2e, properly measured conclusion |
| Semantic **1.6% (2/125)** | Live worktree, mg1 | mg1 · margin_over_random | **superseded** by 2.4@10/28.8@200 — config-crippled (mg1) |
| Qwen3-Embedding **+17.4pp R@10** | 736-doc **proxy pool** (`hardware_investigation:33`) | relative-to-control · NOT e2e | **projected/proxy-only** — not on any release tree |

**Structural gap: zero retrieval-quality numbers have been measured on v0.6.2 itself.** Every canonical recall/semantic/correctness number lives on the worktree. The byte-stable guarantee covers v0.6.0↔v0.6.2 but **does not bridge worktree↔v0.6.2** (the worktree carries its own `f6286fb` ANN recalib, `9328111` dense-prefilter, `c419abc` per-gene budget).

---

## (3) Provenance-trap findings (stale-master / old-worktree — may not hold on v0.6.2 / live)

| # | Conclusion | Measured on | Why it may not transfer | Re-test needed |
|---|---|---|---|---|
| **P1** | EnterpriseRAG recall@10 = **83% (10K) / 71% (50K)** | 10K/50K subset, 2026-05-21 (`bench_investigation_2026-05:93-97`) | **Deepest trap.** Predates v0.6.0 (2026-05-28) → almost certainly **pre-BGE-M3 / dim-256 / 0.35-cut**, AND measured on SPLADE-off path **before** `23b1e80` (#163) SQL-cap shard-drop fix → potentially understated by silent shard-drop too. Also a subset, not 850K. | Re-run recall@10 at 10K/50K on **v0.6.2** with SQL-cap fix present. Expect collapse toward 28-29.8%. |
| **P2** | Out-of-domain M2 = **0% hallucination, corpus-invariant** | 10K/50K keyword-heuristic, 2026-05-21 | Same pre-v0.6.0 stack as P1. "0%" was *also* a keyword-substring artifact (refuted to 15% by Opus, `semantic_rootcause:13`). **Doubly dead:** wrong method + wrong version. | None for 0% — already refuted. Do NOT resurrect "corpus-invariant 0%." |
| **P3** | Canonical recall@10 = **29.8%** (splade-on, mg10, full-470) | Live worktree (`hardware_investigation:30`) | Worktree forks OLD master 826d053; lacks #173/#175 as ancestors; carries **own dirty** singleton+batched-IN+fan-out. Without batched-IN committed/active, SPLADE-off path can still hit SQL-cap shard-drop at 850K. ANN recalib is worktree's **own** `f6286fb`, not guaranteed byte-identical to shipped `fec9141`. | **#1 re-test:** re-baseline 29.8% on **v0.6.2**. Until then label "worktree, not v0.6.x." |
| **P4** | Semantic fused recall@10 = **2.4%**; @200 geometry | Live worktree, 2026-06-02 | Baseline itself robust (matched pre-rerank exactly), but it is a **worktree** number inheriting worktree ANN recalib `f6286fb`. | Confirm 2.4% reproduces on v0.6.2 (cheap — `/fingerprint`, no LLM). |
| **P5** | Semantic = **1.6% (2/125)** earlier headline | Worktree, mg1, splade-off (`bench_calibrated_470q:73`) | Superseded by mg10/@200 diagnostic (semantic@200=21.6% → "buried not absent"). 1.6% was **config-crippled** (mg1). | Retire 1.6%; superseded by 2.4@10 / 28.8@200 dense. |

---

## (4) Conflated-axes + proxy-only / projected — must NOT be stated as fact

**Axis conflations (honest restatement required):**

| # | Conflation | Honest restatement |
|---|---|---|
| **C1** | dense-only vs fused: "semantic 2.4%" vs "7.2%" are **different systems** (fused vs pure global-dense probe, `bench_session_2026-06-02:24`) | Tag: "fused 2.4% / pure-dense 7.2% → the **4.8pp gap is the wiring loss**." |
| **C2** | recall vs correctness: "correctness 13.8%, doc_recall 17.6%" sit beside recall numbers | Never same scale. Correctness = retrieve+**generate**, Opus-judged. Onyx "Overall %" = answer-correctness, so recall@10 does NOT map to the leaderboard. **Submit the 29.8% fingerprint set, not the 17.6% delivered set.** |
| **C3** | mg1 vs mg10: 27.66 / 29.8 / 28 presented as one "recall" lineage | Tag every recall number with its gate. Canonical = **29.8% = splade-on + mg10**. 27.66 = mg1. Not the same experiment. |
| **C4** | proxy vs end-to-end: Qwen3 "+17.4pp" next to e2e 850K recall reads as a promised lift | Keep proxy numbers in a separate ledger (see X1). |
| **C5** | no-gold types drag raw @10: `info_not_found` (20) + `high_level` (10) = **30 no-gold-by-design** questions scoring 0 | State the denominator: "**29.8% over 470 recall-applicable**" vs "**answerable-only ≈ 42.6%**" — a ~13pp swing from denominator choice. Score no-gold types via **abstain**, not recall. |
| **C6** | encoder-attributed vs wiring-attributed semantic gap: old "embedding is THE semantic lever" framing | **Superseded 2026-06-02:** @10 gap is **WIRING** (routing+fusion discards 9 dense-good golds); re-embed is the @200 **residual (54%)**, not the @10 front-runner (`semantic_rootcause:37-43`). Don't attribute the @10 loss to the encoder. |

**Proxy-only / projected (do NOT state as measured fact):**

| # | Claim | Status | Correct framing |
|---|---|---|---|
| **X1** | Qwen3-Embedding-0.6B = **+17.4pp R@10** | **Proxy only** — 736-doc pool, relative-to-control, NOT a 2.4% e2e prediction; e2e blocked (GB10 GPU embed broken; CPU ~590h). | "Proxy suggests Qwen3 could help; **unmeasured e2e**; gated on GB10 GPU-embed." Never "+17.4pp on the bench." |
| **X2** | Routing+dense-fusion fix → semantic@10 **3→~12 (~4×)** | **Projected, untested.** Critical: route-all **alone was NULL**, dense-weight **alone was NULL**; only the *untested combination* is claimed. n=9 golds. | "Projected ~4×; the combination is **untested**; both halves individually null." Hypothesis, not result. |
| **X3** | **+8pp** delivery-clamp / conversion-gap config knob | **Projected.** 39 golds ranked@10 never delivered to answer ctx; consistent with shipped expression-budget fix but **not re-measured e2e on this fixture**. | "Projected +8pp correctness from a delivery-budget config; not yet re-run." |
| **X4** | Encoder leaderboard ranks (Qwen3 ~70.58 MTEB; BGE-M3 mid-pack ~7-9th) | **CLAIMED** — third-party blog aggregations (awesomeagents/Mixpeek), not the live HF MTEB-v2 page (unfetched 2026-06-02). BGE-M3 *architecture* specs ARE confirmed (official card). | "BGE-M3 = 2024-era encoder (confirmed specs); leaderboard standing **claimed-not-verified** as of 2026-06-02." |
| **X5** | CE rerank **net-zero on semantic** | **MEASURED** (valid 125-q, clamp-fixed, PRE 2.4→POST 2.4). The exception: properly e2e and version-current (worktree). | Genuinely measured. Safe to state. |

---

## (5) Verdict on the live picture

**HOLDS (measured, version-current on the worktree, e2e):**
- **CE rerank = net-zero on semantic** (X5) — the single fully-clean conclusion: properly e2e, clamp-fixed, version-current. The semantic ceiling is encoder-discrimination, not rerank.
- **Semantic @10 = WIRING, @200 = GEOMETRY** (P4/C1): fused 2.4% vs pure-dense 7.2% → 4.8pp wiring loss; @200 geometry residual = 68/125 = 54% of semantic. Robust diagnostic, worktree-measured.
- **Full-500 correctness 13.8% / 15% hallucination / 71% abstain** (Opus-judged) holds as measured — but is a **correctness axis**, not recall, and not leaderboard-mappable.

**VERSION-CONTINGENT (worktree-real, but v0.6.2-unverified):**
- **Canonical recall@10 = 29.8%** (P3) and **27.66% (mg1)** and **semantic 2.4@10 / 28.8@200** (P4) — all measured on the worktree, which carries its own ANN recalib (`f6286fb`), dense-prefilter (`9328111`), and per-gene budget (`c419abc`). The v0.6.0↔v0.6.2 byte-stable guarantee **does not bridge to the worktree**. Risk: without the worktree's uncommitted batched-IN active, the SPLADE-off path can silently drop shards at 850K → understated recall.

**UNMEASURED on the current branch / dead:**
- **Zero retrieval-quality numbers exist on v0.6.2 itself** — the entire v0.6.2 column is a GAP.
- **83% / 71% EnterpriseRAG** (P1) and **0% corpus-invariant hallucination** (P2) are **DEAD**: wrong version (pre-BGE-M3) + (for P2) wrong method (keyword heuristic, refuted to 15%). Strike from all "general recall" framing; keep 83/71 only as a dated, version-tagged *sparse-path historical ceiling*.
- **Qwen3 +17.4pp** (X1), **~4× wiring lift** (X2), **+8pp delivery** (X3) are **projections/proxy, not facts.**
- **Provenance of which dirty-edit state** each worktree number was captured under is **not pinned** (batched-IN active or not?).

---

## (6) Prioritized re-test list (highest-value first)

**RE-TEST #1 — Re-run the canonical recall bench on v0.6.2. (decisive, cheap)**
Spin a daemon from **`3173c47` (v0.6.2)** + the bench venv; run `/fingerprint` recall@10 at **splade-on + mg10** on `enterprise_rag_onyx_full_2`, plus the semantic-125 @10/@200 probe. Retrieval-only, **no LLM** (~hours CPU, ~$0). Use `HELIX_MEM_PROFILE=conservative` (documented byte-identical to v0.6.1) so any delta vs the worktree is attributable to **algorithm** (`f6286fb` ANN recalib / dense-prefilter / per-gene budget), not I/O. **This single run converts P3, P4, and the entire §2 v0.6.2 column from GAP to measured, and validates or breaks the worktree↔release byte-stable assumption.** De-risks the most claims.

**RE-TEST #2 — Re-measure EnterpriseRAG 10K/50K recall on v0.6.2. (kills the deepest trap)**
The 83/71% number is the most-cited and most-contaminated (pre-BGE-M3 stack + pre-SQL-cap-fix + subset + likely sparse-only). Re-run at 10K/50K on v0.6.2 with the SQL-var fix present. Expected: collapse toward ~28-30%, confirming 83/71 was a pre-encoder-change / pre-fix artifact → strike from "general recall" framing, retain only as a dated sparse-path ceiling.

**RE-TEST #3 — Pin the worktree dirty-edit state for the 29.8% capture.**
Confirm batched-IN was active when 29.8% was measured, ruling out the SQL-cap shard-drop risk at 850K scale. Lower priority — RE-TEST #1 supersedes it by moving to a clean release tree, but valuable if v0.6.2 re-baselining is deferred.

**One-line record-honesty rule:** state every bench number as
`<metric> = <value> @ <version/tree> · <gate: mg1/mg10, splade on/off> · <axis: fused/dense, recall/correctness, proxy/e2e> · <denominator: 470/answerable-only/incl-no-gold>` — drop any axis and the number becomes a trap.

**Evidence (absolute, read-only):**
- `C:/Users/max/.claude/projects/f--Projects/memory/project_helix_hardware_investigation_2026-06-02.md` — 29.8%, three-way provenance, Qwen3 proxy, v0.6.2 recheck
- `C:/Users/max/.claude/projects/f--Projects/memory/project_helix_semantic_rootcause_2026-06-01.md` — 6 refuted hyps, wiring-vs-geometry, lever ladder
- `C:/Users/max/.claude/projects/f--Projects/memory/reference_helix_bench_session_2026-06-02.md` — rerank net-zero, per-type, no-gold types
- `C:/Users/max/.claude/projects/f--Projects/memory/reference_helix_bench_session_2026-06-01.md` — full-500 correctness 13.8% / 15% halluc; doc_recall 17.6 vs submit-29.8
- `C:/Users/max/.claude/projects/f--Projects/memory/project_helix_bench_investigation_2026-05.md` — 83/71% subset trap; 0%-hallucination keyword artifact
- `C:/Users/max/.claude/projects/f--Projects/memory/project_helix_bench_calibrated_470q_2026-05-30.md` — 27.66 mg1, semantic 1.6→2.4 supersession, calibration=latency-only

Git refs: v0.5.0 `e1bf20e` · stale master `826d053` · v0.6.0 `2d3b7d0` · v0.6.1 `b7af5b0` (#172/#173) · v0.6.2 `3173c47` (#175, untagged) · worktree `vibrant-easley-73d68a` @ `191ce31`+dirty (`perf/dense-prefilter-via-splade-candidates`, forks `826d053`). Encoder discontinuity: `f46460f` (#140 BGE-M3 Tier-0) + `fec9141` (#142 0.35→0.58 ANN) NOT ancestors of v0.5.0; worktree's own recalib = `f6286fb`; SQL-cap fix = `23b1e80` (#163); dense-prefilter = `9328111` (#159); per-gene budget = `c419abc`; rerank clamp→sigmoid = `7fec674`; pool-widening = `191ce31`.
