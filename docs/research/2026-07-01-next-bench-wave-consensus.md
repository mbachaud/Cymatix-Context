# Next benchmarking/tuning wave — swarm consensus (2026-07-01)

**Decision:** After the fable-5 session, what is the next benchmarking/tuning wave?
State: cAST (#228) + SEMA-gate (#227/#229) validated default-on (packet line recall
0.804 vs BM25 0.484, ContextBench 26-task smoke), ready to merge. WS2 (#230) + WS3
(#231) review-clean, dark-shipped opt-in (held-out sympy: +0.7pp packet / −7.6pp
fp@27k). Constraints: single rig, runs are hours–days, smoke set is 26 tasks
(overfit risk), production path = packet.

**Options:**
- **A** — broader held-out sweep to decide WS2/WS3 default
- **B** — merge #228/#229 (+ tree-sitter packaging) → external benches RepoBench-R / CodeRAG-Bench post-cAST (PRD Phase-1 acceptance)
- **C** — resume the idle 2026-06-10 prose/tuning roadmap (#202 plumb, #203 dense, #204 SPLADE, #205 profiles, SNOW-2)
- **D** — ERB semantic re-test / ERB github-source content-type audit
- **E** — close scope gaps (sharded-router wiring, multi-language symbol refs, live personalization)

## Panel
Pragmatic Engineer · Scale Architect · Operations Realist · Product Strategist ·
Devil's Advocate. (IR/lexical-first lens intentionally lighter this round — those
guards are already codified as tests and leak-guards from the 2026-06-28 councils.)

## Independent positions

**Pragmatic Engineer** — *Favors B.* Two validated, low-risk wins are sitting in
worktrees accumulating drift against a stale master; merging is cheap and removes
the biggest source of future integration pain. The packaging decision (tree-sitter
extras-only → silent regex fallback) is small and is the difference between "the
win exists" and "users get the win." Scores: **B 9**, A 6 (real, but can ride B's
bench runs), C 5 (#202 plumb is cheap — background it), D 3, E 2 (YAGNI while
dark). Changes mind on E if WS2/WS3 earns default-on.

**Scale Architect** — *Favors A-rigor, delivered via B.* The open technical
question is generalization: the smoke set already burned us once (sympy held-out
flipped the WS2/WS3 verdict). But RepoBench-R and CodeRAG-Bench *are* unseen
corpora — run them with `symbol_graph` off/on arms and they double as the broader
held-out sweep. Insists: multi-corpus, no knob tuned on the smoke set, cap sweep
only on held-out. Scores: A 8, **B 7** (conditional on the arms), E 5 (sharded gap
is where scale lives, but gated), C 4 (the 28%@850K cliff is the R3 program, gated
on the 500K fixture), D 4. Changes mind if external benches contradict the smoke
ranking — then chunking, not expansion, gets re-opened.

**Operations Realist** — *Favors B, with C's cheap unblockers early.* Four live
worktrees + a stale local master is operational drift; runbooks for both external
benches already exist; merged code makes every subsequent run reproducible from
one branch. #202's knob plumb (byte-identical defaults) and the telemetry top-5
are one-day items that make every later sweep produce calibration data instead of
anecdotes. A full ERB 850K re-ingest would block the rig for days. Scores: **B 8**,
C 6 (selective resume: plumb + telemetry only), A 6 (fold into B, don't build a
bespoke sweep harness), D 3, E 3. Changes mind on D if the audit shows a large
code-typed gold share in the github bucket.

**Product Strategist** — *Favors B.* The PRD's stated goal is converting
RepoBench-R parity into a citable win over BM25; the 0.804 number is an internal
smoke on our own scorer. RepoBench-R `acc@k` and CodeRAG-Bench `NDCG@10` against
published baselines are the credible external claim — and Phase-1 acceptance is
literally defined on them. ERB's published baselines matter later, but the honest
path above that line is the agentic arm (SNOW-2), not the encoder fight the
2026-06-04 strategy doc already demoted. Scores: **B 9**, A 5, D 4, C 4, E 3.

**Devil's Advocate** — *Favors B, but as falsification, not victory lap.* Four
challenges: (1) "Held-out sweep first" is premature — WS2/WS3 is dark and safe;
its default decision has no deadline. (2) Merging cAST without the packaging fix
ships a dark path — the +15pp evaporates on a default install; packaging is the
real gate of B, not a rider. (3) 26 tasks means 0.804 could be partly luck;
external benches are the test that can *break* the claim — run them expecting
failure. (4) The 2026-06-10 roadmap is three weeks stale; re-triage before
executing its day-plan. On D: the question is malformed — nothing in the code
path touches prose semantic (all changes gate on `content_type == "code"` +
code-classified queries); only the github-bucket audit has information value.
Scores: **B 8** (packaging as hard gate), A 5, C 5 (re-triage first), D 3, E 2.

## Conflict map

| Topic | Agrees | Disagrees | Confidence |
|---|---|---|---|
| B first (merge + external benches) | All 5 | — | **High** |
| Packaging decision gates the merge | Pragmatic, Devil's (+2026-06-28 council #10) | — | **High** (cross-validated) |
| Fold A into B (symbol_graph off/on arms on external benches) | Scale, Ops, Pragmatic | — | **High** |
| Smoke-set overfit risk → no tuning on smoke | Scale, Devil's (+regression-log caution) | — | **High** (cross-validated) |
| Full ERB semantic re-run now | — | All 5 against | **High (against)** |
| Resume C wholesale | — | Ops, Devil's (re-triage; only plumb+telemetry now) | Medium |
| Invest in E now | — | All (gated on WS2/WS3 earning default-on) | High (against) |

**Blind spots:** no persona for downstream *generation* quality (SWE-bench e2e is
Phase-2, untouched here); no end-user lens (Continue/MCP consumers of the packet);
cross-file symbol resolution quality remains unrepresented (flagged 2026-06-28).

## Consensus recommendation

**Decision:** **Option B, with A folded in as bench arms** — merge, then externally
validate, letting the external corpora double as the held-out evidence for the
WS2/WS3 default decision. D reduces to a ~1-hour audit; C resumes selectively
(cheap unblockers only); E stays gated.
**Confidence:** High (5/5 first-choice B).
**Unanimous:** Yes, with scoped dissent: Scale requires the off/on arms; Devil's
requires packaging-as-gate and failure-expectation framing.

### Sequenced plan

1. **Merge wave (no rig time).** Land #228 + #229 on master. Hard gate: the
   tree-sitter packaging decision — promote to core deps *or* loud ingest warning
   on regex fallback (PRD Phase-0 / leak-guard 5; council finding #10). Sync
   `helix.toml` + CLAUDE.md for the three new knobs. Keep #230/#231 dark-shipped
   as-is (merge their branches only if rebase pain says otherwise).
2. **External bench wave (PRD Phase-1 acceptance + held-out sweep in one).**
   RepoBench-R `acc@k` and CodeRAG-Bench `NDCG@10` per existing runbooks, three
   arms each: BM25 foil / cAST-default / cAST + `symbol_graph=on` (cap=8). cAST
   arm must clear the BM25 bar (the parity-to-win conversion); the symbol arm is
   pure held-out signal for the WS2/WS3 default decision. No knob changes between
   arms; nothing tuned on the 26-task smoke.
3. **Cap sweep (only if step 2's symbol arm shows signal ≥ +1pp on either bench):**
   `symbol_expansion_cap ∈ {4, 8, 16}` on one held-out corpus. Otherwise WS2/WS3
   stays dark indefinitely and E investment is cancelled.
4. **ERB audit (~1 hour, parallel, no rig):** census file extensions under
   `sources/github`, count gold `expected_doc_ids` resolving there, check the
   fixture's stored content-type distribution. Outcome closes the lingering
   question either way (see appendix).
5. **Tuning-roadmap re-triage (background):** land #202 knob plumb (byte-identical
   defaults + `precision_probe`/`ab_flag_sweep` regression) and the telemetry
   top-5 so every subsequent run emits calibration data. Re-date the rest of the
   2026-06-10 plan (#203–#205, SNOW-2) *after* the code track's merge+bench wave
   completes — SNOW-2 remains the post-#205 acceptance bench, unchanged.

### Mitigations for top concerns
1. Dark AST path (cross-validated) → packaging decision is a merge *gate*, not a
   follow-up; add the AST-vs-regex path counter assertion to the bench harness so
   every external run proves the AST path fired.
2. Smoke overfit (cross-validated) → external benches run with frozen config;
   any surprise (cAST ≤ BM25) re-opens chunking before any new feature work.
3. Rig contention (Ops) → order is merge (no rig) → external benches → optional
   cap sweep; ERB audit is SQL/filesystem-only and runs anytime.
4. Stale roadmap (Ops + Devil's) → step 5 re-dates rather than executes the June
   day-plan.

### Reversibility
High. Merges are config-gated (`symbol_graph=false`, `sema_embed_on_ingest`
revertible; cAST falls back to regex). Bench waves are read-only. The only
low-reversibility item is the packaging decision — a core-dep promotion changes
the wheel for all users; the loud-warning option is the reversible fallback.

### Review triggers
- cAST arm fails to beat BM25 on RepoBench-R or CodeRAG-Bench → halt merges of
  further code-track features; re-open chunking (Phase-1 gate, PRD §3).
- Symbol arm ≥ +1pp on either external bench → schedule cap sweep + revisit
  default-on; also unlocks E (sharded wiring, multi-lang refs).
- Symbol arm regresses on both → keep dark permanently; close #230/#231 follow-ups.
- ERB audit shows >10% of gold in code-typed github files → schedule the one-shard
  re-ingest A/B (below); else close the semantic re-test question.
- GB10 encoder re-embed becomes available → *that* is the trigger for the ERB
  semantic re-run, not the code-track changes.

## Appendix — the lingering ERB question, resolved

**Q: With the code-path improvements, is it worth re-testing semantic on ERB?**

**A: No for the semantic bucket; a 1-hour audit for the github bucket.** Every
code-path improvement is gated twice: at ingest (`content_type == "code"` →
`CodonChunker._chunk_code`, fragments.py:78) and at retrieval (code-classified
queries gate symbol expansion). ERB is prose-typed enterprise data; its semantic
bucket (recall@10 = 2.4% @850K) is encoder-geometry-bound — the cheap routing fix
was already smoke-refuted on 2026-06-04, re-implicating the encoder (~54% of the
miss). The code changes are mechanically unreachable from that path; a re-run
would re-measure the same encoder. Moreover the 2026-06-04 strategy doc's own
gate — "if code-context wins without dense, the re-embed reframes to prose-only,
defer" — has now *fired*: code won on lexical+structure with dense off. The code
results strengthen the deprioritization; they don't argue for a re-test.

**The one exception:** ERB's `sources/github` subtree. Fixtures were built
~2026-05-20, pre-cAST; if that source contains real code files, they were chunked
with the old path. If the audit (extension census + gold-share count) shows a
material code-typed gold share, run a one-shard re-ingest A/B (re-chunk github
source post-#224+cAST, re-run only github-bucket questions) — cheap, targeted,
and it isolates the chunking effect from the encoder question.

**Q: Do we have a file-type path for flagging code vs prose?**

**Yes — extension-based, in two places (#224):**
- `helix_context/cli/cmd_ingest.py:_content_type_for` — 26 code extensions →
  `content_type="code"`, else `"text"`; `.md`/`.rst`/`.json` intentionally text.
- `scripts/build_fixture_matrix.py:184,519` — same `ct = "code" if ext in
  CODE_EXTS` logic in the fixture builder.
- HTTP `/ingest` and `api.ingest()` take an explicit `content_type` (default
  `"text"` — callers must set it); `mem_sync._infer_content_type` covers memory
  files.

**Known gaps:** extension-only (no content sniffing — a `.md` full of code fences
stays prose, which is the intended PRD leak-guard-6 behavior); notebook/`.ipynb`
and templated files (`.erb`, `.vue`, `.svelte`) are not in `CODE_EXTS`; the two
extension lists (CLI vs fixture builder) should be unified into one shared
constant to prevent drift.

---
> This analysis simulates multiple specialist perspectives to surface risks and
> tradeoffs. It is not a substitute for input from actual domain experts. The
> personas are heuristic models — real specialists may identify concerns not
> captured here. Use it as a structured starting point, not a final verdict.
