# Helix Roadmap

> Canonical forward-planning doc. Created 2026-07-03 from a full repo/issue/bench triage.
> Supersedes scattered planning in: [2026-06-09 next-steps-evidence](audits/2026-06-09-next-steps-evidence.md),
> [2026-06-10 test-tuning-roadmap](audits/2026-06-10-test-tuning-roadmap.md),
> [2026-07-01 next-bench-wave-consensus](research/2026-07-01-next-bench-wave-consensus.md),
> [2026-07-01 external-bench-run-plan](benchmarks/2026-07-01-external-bench-run-plan.md) — those remain the
> detailed specs; this doc is the sequencing layer on top.

## Status snapshot (2026-07-03 evening)

- **Version:** 0.7.1 on master (`pyproject.toml`). Docs claiming v0.5.0 are stale (see Docs refresh below).
- **Bench chain (external wave) is RUNNING** — do not disturb until `benchmarks/logs/chain_status.json` reads `COMPLETE-FINAL` (est. July 4 morning).
- **Open PRs:** #230 (WS2 symbol graph), #231 (WS3 PageRank, stacked on #230). Both CI-green/clean but **gated on the bench results** per the external-bench run plan's council rules.
- **Open issues:** 12 — triaged below; 2 are closable now (#203, #206), the rest are scheduled or bench-gated.

### Update 2026-07-05 (merge + roadmap session)

- **Merged:** #237 (bench harness fix: `body_has_answer` parser + `upstream_timeout=600` + `bench_chain`) → master `cb34284`. This unblocks valid future bench runs.
- **Opened:** #238 (test-suite consolidation, 6 commits, **green on all 3 platforms**, awaiting human review — the agent that authored it is not permitted to self-merge).
- **#230/#231:** status-commented, still **gated but not failed** — the 1556 run's arm-C probe referenced `symbol_graph`/`symbol_expansion_cap` keys absent from master (gap A3), so arm C was never validly measured. Blocked on #221 Run 1→3 + a fail-fast capability assert + a recorded review.
- **New issue #239:** know-confidence is **anti-signal** on every bed (hit-mean < miss-mean, AUC 0.35–0.44) — recalibrate + AUC>0.7 gate before agents trust the know contract (gap A5).
  - **Update 2026-07-07 (circuit-tracer §3–§4 — supersedes "recalibrate"):** recalibrating the 5 retrieval features is **REFUTED** — they carry no causal-use signal on ambiguous deliveries, and the logistic emits **0/72 KnowBlocks even on a balanced bed** (ceiling ~0.42 < `emit_floor` 0.45 = *structural* zero recall). The "coord is the lever / refit→0.72" win was a cell-imputation circularity (coord AUC 0.809 imputed → 0.462 measured). Fix = (a) **operating-point repair** (un-invert β1, lower floor/raise intercept so the gate can fire) + (b) a **new answer-presence feature** (answerability/NLI on the delivered span), the latter **gated on a validation spike** (see Phase 1b). Detail: [2026-07-07 faithfulness](research/2026-07-07-faithfulness-circuit-tracer.md) §3–§4 + issue #239. This also **re-points the J-space council's "fit the incumbent" prerequisite (kill-switch #4)** at the cheap operating-point repair, which unblocks the density/Bet-B track without waiting on the spike.
  - **Spike outcome 2026-07-08** ([answer-presence spike](research/2026-07-07-answer-presence-spike-239.md)): the answer-presence feature (B2) is **NO-GO as a causal-use discriminator** — no span-level scorer beats chance on the competition cell (both conflicting facts are in the delivered span, so answerability is blind to *which* one the model used). The answer-*absence* half is real but under-powered (MS-MARCO G24 0.71, p=0.044) → **scale-N first (§6)** before wiring the B3 abstain gate. **B1 operating-point repair is unaffected — ship it.** Open reframe: the competition case is answer-*selection*, not answer-presence → detect it with **span↔span NLI contradiction (delivery-coherence)**, not query↔span relevance.
- **Docs honesty + root-tidy:** on branch `chore/root-tidy` (see docs-refresh boxes below) + a new efficiency/cost-reduction design memo (`docs/design/2026-07-05-efficiency-cost-reduction.md`) answering the binary-storage / algorithm-vs-model / MCP-token-cost questions.

## The bench gate (everything sequences around this)

Currently running: **S2 SIKE 50-needle bed-sweep** (#221), third attempt, launched 15:56 by the
`s2_rerun_after_203.ps1` watcher. Beds: xl → enterprise_rag_10k → enterprise_rag_50k.
Corpus verified correct this time (46,517 genes on xl; the 07-02 and 07-03_0104 attempts served a stub corpus).

- Results land in `benchmarks/results/sike_bedsweep_<bed>_2026-07-03_1556.json` (written only at run end).
- Live status: `benchmarks/logs/chain_status.json`; logs in `benchmarks/logs/s2_*_2026-07-03_1556.log`.
- `claude -p --model sonnet` auth **verified working 2026-07-03 ~19:00** (the 0104 run's Sonnet rung failed 50/50 on a 401 signature; no preflight exists in the script).
- **Invalid results to quarantine after the run:** `sike_bedsweep_{xl,enterprise_rag_10k,enterprise_rag_50k}_2026-07-03_0104.json` and the 07-02_1252 attempt — wrong corpus, 0.0 gold delivery, 100% Claude-rung errors. Never quote these as #221 numbers.
- Already-valid from this chain: **S3b' dense-weight sweeps** (n=100 real ERB queries, done 07-03 15:55) — recall@10 monotone in `dense_additive_weight`: erb10k 0.58(w0)→0.64(w6), erb50k 0.47→0.56, zero gold evictions. This closes #203.
- Known chain defects to follow up: S1's AST assertion failed (586 regex-fallback chunk lines — arm B/C ContextBench comparison is suspect until explained); ~~28/153 HTTP-500 ollama ReadTimeouts in the current xl ladder~~ **root-caused 2026-07-05** (proxy `upstream_timeout=180s` < CPU-offloaded gemma generation time; fixed via `HELIX_SERVER_UPSTREAM_TIMEOUT=600` in the sweep — see [2026-07-05 issue-resolutions](benchmarks/2026-07-05-sike-bedsweep-issue-resolutions.md)); S4 (ERB-500q scored, #93) was skipped and still needs its own run.

### Decision rules (from the run plan; read results in this order)

1. **Arm B (master-cAST) vs BM25 foil:** if B ≤ BM25 → halt all code-track merges (including #230/#231).
2. **Arm C (ws2+ws3 symbol graph) vs +1pp threshold:** C ≥ +1pp on either external bench → merge #230 then #231 with default-on; C regresses on both → WS2/WS3 stays dark permanently (flip `symbol_graph` default to False or park the PRs).
3. PageRank-vs-in-degree ablation decides whether `symbol_pagerank.py` survives or simplifies (council flagged personalization as inert in the live path).

### Retrieval yardstick (added 2026-07-07)

Retrieval levers must show **causal-use lift** (does the model *causally use* the newly-retrieved content),
not merely delivery — measured by the self-hosted circuit-tracer faithfulness instrument
(`benchmarks/faithfulness/`; see [2026-07-07 faithfulness](research/2026-07-07-faithfulness-circuit-tracer.md)).
Under this yardstick the **complement / DNA-pair dense re-embedding and the ANN-threshold recalibration
were REFUTED** 2026-07-07 ([2026-07-07 semantic-ceiling](research/2026-07-07-semantic-ceiling-complement-refutation.md));
the surviving lever is **rerank over dense top-200**.

## Merge queue (in order, post-bench)

1. **PR #230** (WS2 symbol graph, +447/−5, CI green, CLEAN, no rebase needed) — merge iff arm C passes; record the arm-C number in the PR thread. If dark-shipping instead, flip `config.py` `symbol_graph` default to False first (currently True, which contradicts the "stays dark" rule).
2. **PR #231** (WS3 PageRank, stacked on #230) — after #230 squash-merges: rebase onto master, retarget to master (**this triggers CI for the first time** — the 5 ws3-only commits have zero CI coverage because ci.yml only runs on PRs targeting master), then merge per the same gate. Branch already dark-ships `symbol_graph=false` (17f275d) with an in-degree ablation knob.
3. **Bench harness commit:** untracked `scripts/bench_chain/` (incl. the 07-03 13:10 fixes), `docs/benchmarks/helix_probe_symbol.toml` (fix its stale header comment saying `helix_probe_lexical.toml`), and the 3-line progress-print diff in `benchmarks/sweep_dense_additive_weight.py`.
4. Neither PR has a recorded review — get one on record before merging (ingest-path changes are load-bearing).

## Issue board (triaged 2026-07-03)

| # | Title (short) | State | Action |
| --- | --- | --- | --- |
| #203 | dense_additive_weight sweep | **CLOSED 2026-07-03** | Done: sweep table posted (erb10k 0.58→0.64, erb50k 0.47→0.56, medium 0.23→0.40, zero evictions); no flip, 4.0 stands, raise-to-6 deferred to #205. Still owed in the harness commit: refresh stale comment block at `config.py:430-438`. |
| #206 | Fate of dense-latency PRs #158/#160 | **CLOSED 2026-07-03** | Done: decision comment posted (#158 re-landed via #172+#218; #160 superseded-by-evidence). Still owed: mark "closed by decision" in test-tuning-roadmap:179 during the docs pass; Wall-1 issue only if ~60s/query at 850K is unacceptable. |
| #221 | SIKE scale-sweep re-baseline | **in progress (running now)** | Validate xl JSON tonight (genes ~46.5K, delivery > 0, Sonnet errors 0). After COMPLETE-FINAL: comment results, commit harness, schedule the missing 829K/100-shard 4th bed, re-admit numbers to README. |
| #222 | Per-shard fetch depth | done-but-open (dark in #235) | Post-bench: confirmatory factor 2-vs-4 A/B on medium+xl; expected null (twice-measured) → close with "shipped dark, default stays 2". |
| #223 | Cross-shard co-act reserve | partially-done (dark in #235) | Post-bench: fixture A/B with graph-surfaced golds (`diag_blob_vs_shard_tiers.py`), pick reserve N, promote env var to config key, flip default, close. |
| #219 | Config unification epic | partially-done (2/5 slices) | Update body: check Slice 2 (3aa6c5c/#220). Then Slice 5 (dark-feature decision — cheapest, unblocks doc honesty), Slice 4 (config-reference generated from dataclasses + ratchet), Slice 3 (serve-lean MCP profile). |
| #209 | genai_telemetry module | partially-done (phases 1-2 landed) | Rewrite body to the remaining slice: implement `genai_telemetry.py` (OTel GenAI conventions), light up the 15 phantom `helix_genai_*` dashboard queries (or pull the dashboard), flip the 4 "planned" stubs in OBSERVABILITY.md. |
| #205 | Retrieval profiles 3-layer | in-progress (groundwork only) | Unblocked by #203 result (all beds prefer w=6 → Layer-3 per-class values). Update body checklist; implement after merge wave. |
| #93 | EnterpriseRAG-Bench adoption | partially-done | Rescope body to 3 items: full-480 rerun on 829K fixture (post-bench, ~8h rig), scored Q&A vs Onyx leaderboard (= the skipped S4), declare blob-vs-sharded answered-by-construction. |
| #204 | SPLADE scale curve | outstanding (deferred by consensus) | Comment: reuse #221 beds as the "on" twins; schedule twin builds + sweep next rig-free window after the external wave. |
| #207 | De-hardcoding wave 2 | partially-done (item 8 only) | Update body: check item 8; next slice = items 1-3 (model-ID knobs, citation root-stripping, truncation caps) — they block air-gap deploys. |
| #208 | SNOW-2 nav benchmark re-spec | blocked (on #205 ← #203/#204) | Spec done and reaffirmed 07-01; comment status, re-date after #205 lands. No respec. |

## Housekeeping checklist (ALL post-`COMPLETE-FINAL` — nothing while the bench runs)

Full forensic inventory (branch↔PR SHA matching) done 2026-07-03; summary:

- [ ] **Branches — delete 61 merged** (tip SHA == squash-merged PR head; needs `-D`): full list in the 2026-07-03 triage; includes all release/*, fix/*, feat/* through #220. 8 are checked out in worktrees — remove worktrees first.
- [ ] **Branches — delete 17 superseded** (closed PRs ported to master via #162/#172/#112/#113/#234 etc.).
- [ ] **Branches — review 4 before deciding:** `perf/dense-prefilter-via-splade-candidates` (#160 closed; Onyx v0.6.3 validation kit + codex runner not on master), `feat/onyx-full-v2-build-bundle` (`enterprise_rag_onyx_full_2` bench profile may still be needed for Onyx 500q), `bench/int-5fixture` (verify 977924b claude-p stdio fix landed elsewhere), `fix/dense-fusion-composite-sort` (tip says "do not merge"; salvage H10 investigation docs first).
- [ ] **Worktrees — remove 13 stale** (incl. `F:/Projects/helix-retrieval-upgrade`, `F:/tmp/helix-release-064`, codex scratch, 2 locked agent worktrees on merged branches — unlock then remove), then `git worktree prune`. **Keep:** `.worktrees/ws2-symbol-graph`, `.worktrees/ws3-pagerank` until PRs resolve.
- [ ] **Stashes — drop all 8** (0-6 superseded by merged PRs; glance at stash@{7}'s 90-line token-count helper first).
- [x] **Remote:** `git push origin --delete docs/external-review-gemini` (merged as #236). ✅ already gone (verified 2026-07-05).
- [x] **Quarantine invalid bench results** (07-02_1252 + 07-03_0104 sike_bedsweep JSONs). ✅ 2026-07-05 → moved to `benchmarks/results/_quarantine_invalid/` with a DO-NOT-QUOTE README.
- [ ] **Root runtime clutter** (git-ignored): root-level `genome.db{,-shm,-wal}` (canonical store is `genomes/main/` — verify nothing points at the root copy), `metrics.json`, `logs/`, `overnight_logs/`, `cwola_export/`, `dist/`, caches.
- [x] **Commit the bench harness** (`scripts/bench_chain/`, probe toml, sweep diff) — see merge queue item 3. ✅ merged via #237 (2026-07-05).
- Note (2026-07-05): **branch/worktree/stash deletion NOT auto-executed** — `git branch --merged origin/master` returns 0 (the 61 "merged" branches were *squash*-merged, undetectable by `--merged`). Deleting off the 2-day-old forensic list without squash-aware verification risks nuking live work; deferred to a verified pass. Root runtime clutter is all gitignored (`!!`), so it's local-disk tidiness, not a repo concern; only the stale `dist/` (0.5.0) was removed.

## Docs refresh (the "fresh baseline" pass — do together with the new bench numbers)

- [x] `CLAUDE.md:3` — v0.5.0 → v0.7.1. ✅ 2026-07-05.
- [x] `CLAUDE.md` Stage-2 text — `dense_embedding_enabled` and `splade_enabled` are **default ON** (config.py:355/:214), not off; drop the "no neural inference at query time" claim for the default path. ✅ 2026-07-05 (rewritten + links the algorithmic-profile memo).
- [x] `README.md:138` — phantom `[know]` keys `confidence_floor, margin_threshold` → real keys `emit_floor, betas, s_ref, g_ref, stale_after_days`. ✅ 2026-07-05.
- [x] Test count in 3 places (README badge + Testing section, CLAUDE.md): "~1950" → **~2,750** (measured 2,756 collected under `-m "not live"`). ✅ 2026-07-05.
- [ ] `[ribosome]` backend lists (README:125 + CLAUDE.md): honored values are only `litellm`/`deberta`; drop `claude`/`ollama` or mark legacy. *(not done — needs a code check of honored backend values first)*
- [x] Package count "16" → 15 (README:191 + CLAUDE.md). ✅ 2026-07-05 (table already listed 15; summary + CLAUDE corrected).
- [ ] README "Proof (30 seconds)" table + `docs/benchmarks/BENCHMARKS.md` (last updated 2026-05-28) — refresh from the #221 re-baseline once valid. **License caveat:** ContextBench/CodeRAG numbers stay internal until cleared.
- [ ] Commit/reconstruct `helix_probe_nosema.toml` (referenced by code-track-regression-log.md, absent from tree).
- [ ] Minor: README `[ingestion]` add `hybrid`; note `[mem_sync]` is consumed out-of-band; document `[vault]`/`[hardware]`.

## Sequenced plan

**Phase 0 — tonight (bench running, hands off the rig):**

1. ~~Verify `claude -p` Sonnet auth~~ ✅ verified 2026-07-03.
2. ~~Close #203 and #206 with decision comments~~ ✅ done 2026-07-03.
3. ~~Update issues: #219 (slice-2 box checked + comment), #209 (scope note prepended to body), #207/#222/#223/#204/#208/#93 (status comments)~~ ✅ done 2026-07-03.
4. When `sike_bedsweep_xl_*_1556.json` lands (~23:00-01:00): sanity-check genes/delivery/Sonnet-errors before beds 2-3 start their Claude pass.

**Phase 1 — July 4, after COMPLETE-FINAL:**

1. Validate all three bed JSONs; comment results on #221; quarantine invalid runs.
2. Read the gate: arm B vs BM25, arm C vs +1pp → merge/dark-ship/park #230 then #231 per the decision rules; commit bench harness.
3. ~~Investigate the S1 AST-assertion failure and the ollama ReadTimeout cluster.~~ ReadTimeout cluster **done 2026-07-05** (upstream_timeout root cause + fix in the harness PR); S1 AST assertion still open.

**Phase 1b — bench validity wave (added 2026-07-05, from the bedsweep review):**

Detail + numbers: [docs/benchmarks/2026-07-05-sike-bedsweep-issue-resolutions.md](benchmarks/2026-07-05-sike-bedsweep-issue-resolutions.md).
The 1556 results are the only valid run AND still mismeasure retrieval (dead
`body_has_answer` parser — fixed; 6/4/3 false-neg + 7/14/15 false-pos needles
per bed; 260 echo genes + 43 worktree-dupe genes contaminating xl; probe-TOML
no-op keys incl. `[abstain] enabled` and the arm-C `symbol_graph` keys that
don't exist on master). **Hold README re-admission of #221 numbers until Run 1
(clean baseline) reproduces them on decontaminated beds.** Sequence: Run 1
(harness+bed validity) → Run 2 (fts5_candidate_depth × additive/RRF A/B on xl)
→ Run 3 (**#239, re-scoped 2026-07-07 — recalibration of the 5 features is REFUTED, §4**):
**(3a) operating-point repair** (un-invert β1 / lower floor so the gate fires at all) on a
delivery-balanced bench — independent, ships recall regardless; **(3b) spike-then-conditionally-integrate**
a new answer-presence feature (a validation spike gates the ~9-file integration). Production-profile arm +
arm-C only after its fail-fast capability assert.

**Phase 2 — fresh baseline (same week):**

1. Housekeeping sweep (branches/worktrees/stashes/remote/clutter) — checklist above.
2. Docs refresh pass (checklist above) + README bench-number re-admission.
3. Tag the result — candidate 0.8.0 baseline: post-merge master, clean tree, honest docs, valid 3-bed scale curve.

**Phase 3 — next wave (re-dated per the 07-01 consensus):**

1. #221's missing 829K/100-shard bed; S4/#93 scored Q&A run (auth preflight now in place).
2. #223 reserve-N A/B → default flip; #222 confirmatory null → close.
3. #219 slices 5→4→3; #207 items 1-3; then #205 profiles (now data-fed by #203/#204) → unblocks #208 SNOW-2 build.
