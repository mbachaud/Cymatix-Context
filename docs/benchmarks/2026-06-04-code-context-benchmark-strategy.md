# Helix Code-Context Evaluation — Benchmark-Strategy Reference
**As-of 2026-06-04. Verification provenance: 4 independent fact-check clusters (arXiv + GitHub + Hugging Face). Web takes precedence over the Jan-2026 model cutoff. Every stat tagged MEASURED (web-confirmed) vs CLAIMED (GPT, corrected where wrong).**

---

## 1. TL;DR + the prose-vs-code strategic reframe

**Lead with the reframe, because it rewrites where bench effort should go.**

Helix is being optimized on its **worst terrain** and has **never been scored on its designed terrain.** That is the strategic error this reference exists to correct.

- **Prose terrain (where it's being ground):** On EnterpriseRAG-Bench / Onyx (prose: slack/gmail/gdrive/docs, 850K genes), **semantic recall@10 = 2.4%** — the lone broken bucket (other types 42–82%). On 2026-06-04 the *cheap* fix (semantic-aware routing-broaden + dense-dominant fusion) was **smoke-REFUTED** — 1/9 @10 at dense-weight 12; broadening the router *floods* the 200-pool, hurting rank. That **re-implicates the encoder** (geometry residual ≈ 54% of the semantic miss), so the next prose-semantic lever is **encoder re-embed — expensive, GB10-gated.** This is the most expensive possible capital aimed at the worst-fit terrain.
- **Code terrain (where the design should win, UNMEASURED):** Helix is *targeted* code-context injection — a small, project-aware packet (path/filename anchors, symbols, tags, cross-file structure), explicitly positioned **against grep/BM25 dumps and broad vector-RAG floods**. Its **measured** strength is lexical/structural: **SPLADE earns +6pp on CODE corpora (0pp on prose Onyx)** [MEASURED]; lexical_rescue is "apt for code, UNMEASURED." Retrieval is **LLM-free and deterministic** (ribosome off by design), so the clean test is retrieval quality (recall/precision/gold-density/injected-tokens), fully separable from generation. **Zero code-context retrieval numbers exist.**

**The encoder re-implication tie-in (the technical heart of the reframe).** The prose conclusion — "dense is the only lever, must re-embed" — **does not transfer to code.** On prose, semantic paraphrase is the whole game, so dense is the only knob (hence the stuck 2.4%). On code, the gold signal is dominated by **exact lexical tokens dense bi-encoders systematically under-weight**: identifiers, import paths, filenames, symbol/type names — precisely what SPLADE/lexical nails (the measured +6pp) and what dense blurs. External corroboration: **CodeRAG-Bench's own measured finding** — a 7B dense embedder (SFR-Mistral) gives better retrieval but **~5× index size and ~100× encode latency** (3.7ms GIST-base → 316ms SFR-Mistral) [MEASURED]. So on code the cheap lexical/structural levers Helix already owns are expected near the *top* of the lever ladder, not the bottom.

**Therefore the bottom line:** measuring code-context first is not just a new datapoint — it is the **cheaper path to a defensible win** *and* it **de-risks the GB10-gated encoder spend** by telling you whether the semantic problem even generalizes off prose. If code-context shows the encoder is not the bottleneck off-prose, the re-embed reframes from "necessary" to "prose-only, defer." High-value finding either way.

**The single highest-ROI action:** wire Helix as an **LLM-free retriever into ContextBench's offline gold spans** (Apache-2.0, on HF, file/block/line gold) over the 500-task `verified` subset, and report **recall/precision/F1 @ file/block/line + injected-token count**, with `princeton-nlp/SWE-bench_bm25_27K` as the side-by-side "dump" baseline. ContextBench is the only benchmark whose *own headline finding* — agents over-retrieve, flooding context and killing precision — **is literally Helix's positioning.**

---

## 2. Verified benchmark table (real stats, corrected vs GPT)

Legend: **R-iso** = retrieval-isolable without an LLM hiding failures. Confidence is the verification clusters' agreement level. "GPT" = the originating proposal being fact-checked.

| Benchmark | Real stats (MEASURED, corrected vs GPT) | R-iso | Gold granularity | License / access | Helix-fit | Conf | URL |
|---|---|---|---|---|---|---|---|
| **ContextBench** (arXiv 2602.05892, Feb 2026) | 1,136 issue-resolution tasks / 66 repos / **8 langs = Python, Java, JS, TS, Go, Rust, C, C++** ⚠ **NOT C#** (GPT implied C#; corrected). Gold = 522,115 lines / 23,116 blocks / 4,548 files. Lite/`verified` = 500. Leaderboard (2026-06-04): best Pass@1 Claude-Sonnet-4.5 **53.0%**, context-F1 **0.344**, avg line-F1 **0.325**; Gemini-2.5-Pro **0.632 block recall / 0.403 precision** (field maxes recall by flooding, sacrificing precision). | **YES** — process-oriented; recall/prec/F1/efficiency scored *independently* of patch Pass@1. ⚠ **Official harness is trajectory/agent-based, ships ZERO BM25/dense baselines** (GPT overstated "turnkey retriever socket"). Workaround = offline custom scorer (see §3/§4). | **file + block(AST symbol) + line** — best in cluster. ⚠ Gold provenance = **patch-SEEDED → human-traced → LLM-verified-sufficient**, not annotated-from-scratch (GPT's "human-annotated" is fair but imprecise). | **Apache-2.0** (harness); data on HF (`Contextbench/ContextBench`, full 1136 + `verified` 500). ⚠ Dataset-license vs harness-license + 66 source-repo licenses **not separately published — verify on HF card** before public/commercial claims. | **BEST FIT** | high | arxiv.org/abs/2602.05892 · github.com/EuniAI/ContextBench · huggingface.co/datasets/Contextbench/ContextBench · contextbench.github.io |
| **CodeRAG-Bench** (arXiv 2406.14497, NAACL'25 Findings) | **8 datasets** (GPT said "8 tasks" — read *datasets*): HumanEval, MBPP, LiveCodeBench, DS-1000, ODEX, RepoEval, SWE-bench-Lite, CodeSearchNet-Py. 10 retrievers × 10 LMs. Larger-embedder tradeoff **CONFIRMED VERBATIM** (SFR-Mistral best NDCG, ~5× index / ~100× latency). | **YES** — native retrieval track, **NDCG@10 primary** + Precision + Recall, retriever-swappable (cleanest out-of-box socket). | doc/snippet (mixed: function → file). Coarser than ContextBench. | ⚠ **CC-BY-SA-4.0 (ShareAlike copyleft)** — commercial-OK but viral attribution; per-source upstream licenses vary. GitHub + HF org. | **STRONG** (the efficiency-argument vehicle) | high | arxiv.org/abs/2406.14497 · github.com/code-rag-bench/code-rag-bench · code-rag-bench.github.io |
| **RepoBench-R** (arXiv 2306.03091, ICLR'24) | R/C/P decomposition confirmed exactly. **Python + Java only**. RepoBench-R = standalone retrieval, metric **acc@k** (acc@1/3 easy; acc@1/3/5 hard); settings XF-F / XF-R. | **YES** — dedicated retrieval split, zero generation, lowest wire-effort. | block/snippet (cross-file dependency snippet). ⚠ closed candidate-pool ranking (easier than open-repo localization). | **CC-BY-4.0** (permissive). HF `tianyang/repobench_python_v1.1` (+Java). ⚠ confirm HF card before commercial. | **STRONG, low-effort** | high | arxiv.org/abs/2306.03091 · github.com/Leolty/repobench · huggingface.co/datasets/tianyang/repobench_python_v1.1 |
| **CrossCodeEval** (arXiv 2310.11248, NeurIPS'23) | Static-analysis-built cross-file completion. **Languages Py/Java/TS/C# CONFIRMED — this is where C# actually lives** (not ContextBench). Ships BM25 / UniXCoder / Ada retrieval settings. | **PARTIAL** — retrieval measurable but judged largely via downstream completion EM/ES (downstream-proxy). Static-analysis cross-file symbol is a usable recall target. | file + symbol (static-analysis-pinned). | Apache-2.0 repo; permissively-licensed source repos. ⚠ **raw data EMAIL-GATED** (not one-click HF) — access friction. | **USABLE comparator** (the only multilingual / TS+C# one; ships B+C baselines for free) | high | arxiv.org/abs/2310.11248 · github.com/amazon-science/cceval · crosscodeeval.github.io |
| **SWE-bench (+ RAG tooling)** (arXiv 2310.06770, ICLR'24) | 2,294 issue→PR pairs / 12 Python repos; Lite/Verified/Multimodal. `create_text_dataset` + `bm25_retrieval` + `tokenize_dataset` + `eval_retrieval` all real. Pre-built BM25 datasets `SWE-bench_bm25_13K/27K/40K` + `_oracle`. **BM25-dump degradation MEASURED**: BM25@27K retrieves a superset of oracle files in **~40%** of instances, **none** in ~half; Claude-2 **4.8%→1.96%** oracle→BM25, GPT-4 **≈0%**. | **INDIRECT** — native metric = %Resolved (generation). Isolate via `eval_retrieval` / oracle-file recall (file-level). | **file-level only** (gold-patch files). | **MIT** (most permissive/established). HF `princeton-nlp/SWE-bench_*`. | **BEST "dump" baseline substrate** (canonical packet-vs-BM25 head-to-head; also Phase-2 e2e) | high | swebench.com/SWE-bench/guides/create_rag_datasets/ · github.com/SWE-bench/SWE-bench · huggingface.co/datasets/princeton-nlp/SWE-bench_bm25_27K |
| **FEA-Bench** (arXiv 2503.06680, ACL'25) | ⚠ GPT **understated**: **1,401 instances** / 83 repos / **Python-only** (GPT gave only "83 repos"). Execution-verified feature-impl; DeepSeek-R1 ~10% resolved. | **NO** — execution-only (% resolved); Oracle variant *bypasses* retrieval, doesn't score it. | new functions/classes (execution-verified); file-level recoverable but not a scored retrieval gold. ⚠ **full dataset NOT released** ("licensing/company policies") — reconstruct from GitHub. | Code MIT; HF card license "other". | **WEAK for retrieval-only**; Phase-2 e2e demo at best | high | arxiv.org/abs/2503.06680 · github.com/microsoft/FEA-Bench · huggingface.co/datasets/microsoft/FEA-Bench |
| **RACE-bench** (arXiv 2603.26337, Mar 2026) | **CONFIRMED real** (GPT's highest-risk pick, not a hallucination). 528 feature-addition instances / 12 repos (**Python**, SWE-bench-family) / pytest verification / 4-stage reasoning GT. **File Localization scored separately as Recall@RelevantFiles + OverPrediction@RelevantFiles** (over-prediction = literally Helix's injected-noise axis). | **YES (one track)** — file-localization isolable. | file-level (localization). | ⚠⚠ **arXiv-only; NO public GitHub/HF as-of 2026-06-04** → **NOT WIREABLE TODAY**. License = arXiv perpetual non-exclusive (paper only). | **CONCEPTUALLY IDEAL, BLOCKED** (best-named noise metric in existence for the thesis; watch for code drop) | medium | arxiv.org/abs/2603.26337 |
| **Legacy smoke: CodeSearchNet / CoSQA / CodeXGLUE** | Pure NL→code search. CodeSearchNet 6 langs, MRR/NDCG; CoSQA 20k+ real queries; CodeXGLUE AdvTest MRR. | **YES** (pure MRR/NDCG/MAP) | function/snippet | MIT (CodeSearchNet, CodeXGLUE code). ⚠ **CodeXGLUE *data* under C-UDA — not blanket-commercial**; CoSQA verify. | **SMOKE-ONLY** — NL→snippet *semantic* search = Helix's **WEAK** bucket; no repo structure | high | github.com/github/CodeSearchNet · arxiv.org/abs/2102.04664 · arxiv.org/abs/2105.13239 |

**GPT error/uncertainty summary:** (a) ContextBench language set — **C# WRONG**, corrected to Py/Java/JS/TS/Go/Rust/C/C++; (b) ContextBench "turnkey retriever socket" — **OVERSTATED**, official harness is agent-trajectory, no BM25/dense baselines; (c) ContextBench "human-annotated" — **NUANCE**, gold is patch-seeded→traced→LLM-verified; (d) CodeRAG-Bench "8 tasks" → **8 datasets**; (e) FEA-Bench scope **UNDERSTATED** (omitted 1,401 instances, Python-only); (f) RACE-bench — **CONFIRMED real** but **UNCONFIRMED accessible** (no release). One disambiguation: a separate "SWE Context Bench" (arXiv 2602.08316) is a **different paper** (context *learning*) — do not conflate; GPT's stats map to the real 2602.05892.

---

## 3. Fit ranking — Phase-1 retrieval-only vs Phase-2 assisted-coding, with the ContextBench adjudication

**Scoring axes (Helix-specific):** retrieval-isolable · gold granularity (file/block/line beats doc/snippet) · lexical/structural strength surface · access+license · effort-to-wire.

### Phase 1 — RETRIEVAL-ONLY (the clean test: recall/precision/gold-density/injected-tokens, no generation)
1. **ContextBench** — BEST FIT. file/block/line gold, separable scoring, Apache-2.0, data released. Needs an offline custom scorer (no socket).
2. **RepoBench-R** — STRONG, lowest wire-effort (acc@k, one-click HF, CC-BY-4.0). Closed candidate-pool = tests ranking, not open-repo localization at scale.
3. **CodeRAG-Bench** — STRONG, cleanest out-of-box socket (NDCG@10), and the vehicle for the cheap-vs-7B-embedder efficiency argument. Doc/snippet gold; ShareAlike.
4. **CrossCodeEval** — USABLE multilingual comparator (only TS/C#); ships BM25+dense baselines (= arms B/C for free). Retrieval is downstream-proxy; data email-gated.
5. **SWE-bench (`eval_retrieval`/oracle)** — file-level only, but the canonical packet-vs-BM25-dump substrate with a published strawman. MIT.
6. **RACE-bench** — conceptually ideal (OverPrediction@RelevantFiles), **blocked** (no release).
— **Legacy smoke** — regression layer only; NL→snippet semantic search is Helix's weak bucket.

### Phase 2 — ASSISTED-CODING E2E (does the packet help patch?)
1. **SWE-bench (Verified/Lite, Docker harness)** — drop Helix in as retriever via `create_text_dataset`, hold generator fixed, attribute Δ-resolved + token-budget to retrieval. Max already runs SWE-bench-like harnesses. Max external credibility.
2. **CodeRAG-Bench (generation arm)** — RACG lift on real codegen; embedder-latency finding stages the "deterministic, cheap" counter-pitch.
3. **FEA-Bench** — hard feature-impl arena; no separate retrieval score, dataset not turnkey.
4. **CrossCodeEval (completion-lift arm)** — cross-file context → EM/ES lift vs BM25/oracle; multilingual.
5. **RACE-bench (dual-track)** — blocked.

### The ContextBench adjudication
**GPT's "ContextBench first" is CORRECT — and it is *better* for Helix than GPT pitched it.** All four verification clusters converge: it exists (arXiv 2602.05892, Apache-2.0, on HF) and the headline stats verify near-exactly. This is **not** a case where RepoBench-R / CodeRAG-Bench / CrossCodeEval should displace it — each loses on the load-bearing axis:
- **RepoBench-R** — cleaner socket but closed candidate-pool + snippet gold + Python/Java only.
- **CodeRAG-Bench** — best plug-and-play socket but doc/snippet gold, heterogeneous, ShareAlike; weak on repo cross-file structure.
- **CrossCodeEval** — only multilingual one, but retrieval is downstream-proxy + email-gated.

ContextBench wins because its gold is **file/block/line** (Helix's exact claim surface) and its **own central finding** — "LLMs favor recall over precision, retrieve broad context introducing substantial noise, limited precision/F1 gains" — **is the documented failure mode Helix claims to fix.** If Helix hits comparable recall at materially higher precision/efficiency, that is a publishable, **third-party-scored** result against named agent baselines (Sonnet-4.5 0.344 F1, Gemini-2.5-Pro 0.632 recall / 0.403 precision).

---

## 4. Recommended sequence (highest-ROI first)

**Step 0 — MVV, the single highest-ROI action (target 1–2 days, $0–low).** Build the **ContextBench offline retrieval scorer** and run Helix **LLM-free** over the 500-task `verified` subset (start with a 100-task Python slice).
- Pipeline: for each instance, clone `repo_url` @ `base_commit` → ingest into a Helix genome → query with `problem_statement` → capture Helix's ranked packet (paths/symbols/spans) → emit ContextBench `pred_spans` JSON `{"file_path":[{"type":"line","start":N,"end":M}]}` → score with `python -m contextbench.evaluate --gold data/full.parquet --pred helix.traj.json --out results.jsonl`. **~50–100 LOC emitter; no agent, no LLM, no GPU re-embed.**
- Report exactly two things: **(a) block/line recall — Helix (arm D) vs BM25 dump (arm B); (b) injected-tokens / gold-density — D vs B.** One 2-D plot (recall@k on x, injected-tokens on y, one point per arm) is the whole story.
- Baseline foil: `princeton-nlp/SWE-bench_bm25_27K` (already on HF) + SWE-bench's published oracle→BM25 collapse (4.8%→1.96%, GPT-4 ≈0) as a third-party sanity anchor and the **best single citation for "the dump is the problem."**

**Step 1 — RepoBench-R `acc@k`.** Lowest-effort independent corroboration of cross-file lexical strength (CC-BY-4.0, one-click HF). Pure retrieval, no LLM.

**Step 2 — CodeRAG-Bench canonical NDCG@10 / Recall.** Stage the "cheap deterministic retriever vs 7B-embedder ~5× index / ~100× latency" efficiency argument *using their own measured numbers.*

**Step 3 — CrossCodeEval.** Multilingual (TS/C#) cross-file comparator; reuse its shipped BM25/UniXCoder/Ada settings as arms B/C rather than building them.

**Step 4 (Phase-2, only if Step 0 is positive) — SWE-bench e2e.** Swap BM25 for Helix in `create_text_dataset`, hold one fixed generator (the local Gemma/Haiku-4.5 rig), measure %Resolved lift + token efficiency vs `bm25_27K` and vs the oracle ceiling. The recognizable-name demo.

**Watchlist — RACE-bench.** Revisit the instant code/data drops; its `OverPrediction@RelevantFiles` is the best-named injected-noise metric in existence for Helix's thesis.

**Demote / hold:** prose-semantic Onyx as a **background** encoder experiment, explicitly gated on whether code-context shows the encoder is even the bottleneck off-prose. Legacy smoke set = regression layer only.

**Arm posture (applies across runs):** keep arm **A (no-retrieval floor)**, make arm **B (tuned BM25/grep dump)** the essential foil, keep **C (vector-RAG flood)**, **D (Helix packet)** is the deliverable, and **demote E (Helix+semantic-store) to a diagnostic ablation** — dense is Helix's weak bucket on *both* terrains, so present E as "does dense add over lexical+structural on code?" with an honest predicted near-zero lift, never as a hero arm.

---

## 5. The Helix-native spec→project benchmark (repeatable in-house option)

Use **third-party benches as the headline, the dogfood self-made bench as corroboration** — "and the same pattern holds on our own 22M/57M-gene production code." Never the reverse: you built the corpus, the gold method, *and* the system under test, so a self-made bench has a structural credibility ceiling no rigor fully removes. Build it only **after** the ContextBench MVV is positive.

**Design (clean two-score split, ribosome-off):**
- **Score 1 — Retrieval quality (the real Helix test, no LLM):** recall@k, precision@k, gold-density (gold-tokens / injected-tokens), injected-token count, latency.
- **Score 2 — Downstream (optional, expensive):** feed each arm's packet to ONE fixed generator, measure patch-pass / hallucinated-edits / unnecessary-touches. Run only after Score-1 shows a packet-vs-dump gap; hold the generator constant so context is the only variable.

**Gold construction — patch-derived, never author-chosen (borrow ContextBench's own method: patch-seed → trace → verify):**
- Pick real **merged PRs/commits from the repo's own history**; gold files/symbols/lines = what the diff touched. Do **not** hand-author "which file answers this" — that is the cherry-pick trap.
- **Query = the PR issue text / title, with filename, symbol names, and path tokens SCRUBBED** so lexical strength comes from Helix's index, not from the query leaking the path.
- **Temporal cut:** ingest the genome **at the parent commit** of each gold PR so the fix isn't already in the corpus.
- **Distractors must be present and counted:** the whole repo is the distractor pool → **report precision, not just recall**, or the bench is meaningless.

**Anti-cherry-pick / leak-guard (non-negotiable):**
- **Pre-register the task set** (all merged PRs in a date window touching ≥1 and ≤N files, auto-selected) — no manual curation of "good" questions.
- **Report the full distribution including losses** (carry the same honest posture used for the prose 2.4% bucket).
- Run B/C/D/E on the identical task set with the identical generator (for Score-2).

**Reuse vs new (≈70% port, not rewrite):**
- **Reuse the EnterpriseRAG patterns, not the corpus** (Onyx is prose; this is code — that's the whole point). Port `make_gold_index` / `load_needles` / the **FIXED `_rel_after_sources`** path-normalization (the `sources/`-prefix false-gold bug **will recur on code** — deeper/messier paths — so add a code-path unit test up front); carry `min_genes≥10` gate + `HELIX_SHARD_WORKERS` order-preserving fan-out unchanged; reuse the Sonnet-judge only for the optional Score-2 arm; reuse the bench venv but **relocate it out of volatile `/f/tmp` first**.
- **New:** the trajectory emitter (Helix packet → ContextBench/`pred_spans` JSON, ~50–100 LOC) and code-aware symbol/line-range gold matching.

**Prerequisite gap:** the dogfood corpora (`genomes/bench/matrix-sharded/medium` ≈22M, `xl` ≈57M gene-scale) have **no wired `questions.jsonl` gold — latency-only today.** ContextBench supplies external gold so you can start immediately; the patch-seed→trace→verify pattern is the template to bootstrap dogfood gold later.

---

## 6. Honest caveats — what could NOT be verified, and licensing blockers

**Could not verify (FLAGGED):**
- **ContextBench *dataset* license** (distinct from the Apache-2.0 *harness*) and the **66 source-repo licenses** are not separately published on the pages reviewed. Verify on the HF dataset card before any public/commercial claim.
- **ContextBench gold provenance precision** — confirmed patch-seeded→human-traced→LLM-verified, but the exact human-vs-LLM split per span is not enumerated.
- **RepoBench-R license** — HF card (`tianyang/repobench_python_v1.1`) not directly read; CC-BY-4.0 reported by the cluster — confirm before commercial use.
- **CrossCodeEval** — per-language instance counts not enumerated; **raw data is email-gated** (not one-click HF), so plan a request step.
- **RACE-bench** — paper real and stats confirmed, but **no public GitHub/HF as-of 2026-06-04**; dataset/code license unconfirmed. Not actionable until release. (Do not confuse with the unrelated `github.com/jszheng21/RACE` readability benchmark.)
- **The "Improving Code Localization" paper** (OpenReview 8yjWLJy2eX) PDF would not render; its specific BM25-vs-dense file-localization recall@k numbers are unconfirmed — but SWE-bench's own oracle-vs-BM25 collapse already carries the file-localization argument with MEASURED third-party numbers.

**Licensing blockers, ranked by how much they constrain a commercial/public claim:**
1. **RACE-bench — hard blocker (no artifact).** Watch + revisit; do not commit plans to it.
2. **CrossCodeEval — access friction.** Email-gated raw data; Apache-2.0 code, "permissively-licensed" repos not enumerated.
3. **CodeRAG-Bench — CC-BY-SA-4.0 ShareAlike copyleft.** Commercial-OK but any redistributed processed data must carry the same license; per-source upstream licenses vary (StackOverflow CC-BY-SA, etc.).
4. **CodeXGLUE *data* — C-UDA.** Permits computational/research use, **not** blanket commercial redistribution. Confirm before any product use.
5. **FEA-Bench — full dataset not released** (company policy); reconstruct from GitHub; HF card license "other."
6. **ContextBench dataset-license unstated; SWE-bench MIT, RepoBench-R CC-BY-4.0, CodeSearchNet/CodeXGLUE-code MIT** — the most permissive of the set; still verify per-card before public claims.

**Net:** the recommended primary track (ContextBench MVV → RepoBench-R → CodeRAG-Bench → SWE-bench) is buildable **this week, against published agent baselines, on released data, with no annotation and no LLM** — pending only the two ContextBench license confirmations above. The conceptually-best noise metric (RACE-bench's OverPrediction) is parked behind a release blocker, and the prose-semantic encoder fight should be held as a gated background experiment, not the headline.
