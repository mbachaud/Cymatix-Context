# cc-exchange Task Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open a pull-ready task board for Joe in cc-exchange (BOARD.md + 10 cards + announcement thread) and freeze the SCBench baselines (two annotated tags + `docs/benchmarks/BASELINES.md`).

**Architecture:** Two repos, two change-sets. Repo A (`Cymatix-Context`, branch `claude/cc-exchange-task-board-ece78f`) gets the baselines ledger and two annotated tags. Repo B (`f:/Projects/cc-exchange`) gets the board files in the max lane only, landed as ONE logical drop via the repo's push wrapper. Spec: `docs/superpowers/specs/2026-08-11-cc-exchange-task-board-design.md`.

**Tech Stack:** Markdown, git (annotated tags), bash, the cc-exchange protocol scripts (`bin/exchange-push.sh`, `bin/gen-index.sh`).

## Global Constraints

- **Single-writer law (cc-exchange):** write ONLY `from-max/**`, `changelog/max.jsonl`, `threads/<topic>/NNNN-max-*.md`. NEVER edit `index/**` by hand, NEVER edit any `from-joe/**` or joe-authored file, NEVER bare `git push` — always `bin/exchange-push.sh "<msg>"`.
- **ASCII-clean:** every cc-exchange file this plan creates must scan zero non-ASCII bytes (Joe's tooling checks this). Use `--`, `->`, `x` instead of em-dash, arrow, multiplication sign. Verify with the grep in Task 7.
- **Terra is in flight.** Do not touch `F:/Projects/cymatix-context/.claude/worktrees/scbench-codex-ab` working tree, its env, or branch `codex/scbench-cymatix-ab` beyond pushing tags (tag push only, no branch changes).
- **Pins:** every card `pin:` is `v0.8.6` (Joe's verified clean venv). No card may say "master".
- **Baseline commits (already resolved from campaign manifests — do not re-derive):** luna `0b125eabebb03c3566befa81be94443119877abd`; terra execution tree `9312cde` (manifest's implementation pin `aef12732ded2a43e92ee4a5e46b6c5956404caad` — record both).
- **Bench profile (spec section 6):** future testing = daemon ON + per-box workers (max rig w4, GB10 w2); shipped defaults stay daemon-off / workers-2. Exceptions: SCBench model-free arms, #339/C1 cards, c>1 validity gate.
- cc-exchange no-secrets rule: no tokens, no PICC data, nothing non-shareable.

**File map:**

| repo | path | task |
|---|---|---|
| A | `docs/benchmarks/BASELINES.md` (create) | 1 |
| A | tags `bench/scbench-luna-2026-08-08`, `bench/scbench-terra-2026-08-11` | 2 |
| B | `from-max/board/cards/A1..A2` (create, 2 files) | 3 |
| B | `from-max/board/cards/B1..B4` (create, 4 files) | 4 |
| B | `from-max/board/cards/C1..C4` (create, 4 files) | 5 |
| B | `from-max/board/BOARD.md` (create) | 6 |
| B | `threads/task-board/0000-max-board-opening.md`, `changelog/max.jsonl` (append), regenerated `index/*` | 7 |

---

### Task 1: BASELINES.md ledger

**Files:**
- Create: `docs/benchmarks/BASELINES.md` (in the `codebase-audit-weaknesses-96a7da` worktree, branch `claude/cc-exchange-task-board-ece78f`)

**Interfaces:**
- Produces: the ledger every card `pin:` and Task 2's tags resolve against. Tag names used verbatim in Task 2: `bench/scbench-luna-2026-08-08`, `bench/scbench-terra-2026-08-11`.

- [ ] **Step 1: Extract the terra manifest fields and both manifest sha256s**

```bash
cd "/f/Projects/cymatix-context/.claude/worktrees/scbench-codex-ab"
python - <<'EOF'
import json, hashlib
for camp in ("2026-08-08-codex-pilot", "2026-08-11-codex-terra"):
    p = f"benchmarks/scbench/campaigns/{camp}/manifest.json"
    raw = open(p, "rb").read()
    m = json.loads(raw)
    print("=", camp)
    print("  manifest sha256:", hashlib.sha256(raw).hexdigest())
    for k in ("cymatix_commit", "scbench_commit"):
        print(f"  {k}:", m.get(k))
    md = m.get("metadata", {})
    for k in sorted(md):
        if any(t in k.lower() for t in ("model", "effort", "seed", "docker", "image", "digest", "fork")):
            print(f"  metadata.{k}:", md[k])
EOF
```

Expected: two blocks. Luna block must show `cymatix_commit: 0b125eabebb03c3566befa81be94443119877abd`; terra must show `aef12732ded2a43e92ee4a5e46b6c5956404caad`. Record every printed value — they fill the `<FROM STEP 1>` slots in Step 2. If a field (model/effort/docker digest) does not appear in terra's metadata, pull it from `benchmarks/scbench/campaigns/2026-08-11-codex-terra/README.md` and cite that file in the row's notes.

- [ ] **Step 2: Write `docs/benchmarks/BASELINES.md`**

Write exactly this, filling the three `<FROM STEP 1>` slots with Step 1 output:

```markdown
# Benchmark baselines ledger

One row per measurement campaign. **Rules (from the 2026-08-09 receipt
invalidations):** compare arms only within their own row; measurement runs
against a frozen tag, never a moving branch; a receipt without a row here is
not comparable to anything.

## Standard bench profile (decision 2026-08-11)

Future testing runs the GPU encoder daemon ON with per-box worker counts.
Shipped defaults stay untouched (daemon off, workers 2); default flips remain
receipt-gated PRs.

| box | daemon | workers | receipt |
|---|---|---|---|
| max rig (8C/16T x86) | ON (`CYMATIX_ENCODER_URL=http://127.0.0.1:11439`) | 4 | spark-erb-receipts 0015 / `61ea459`: scaling only unlocks with daemon ON, x4 sweet spot |
| Joe GB10 Grace (e92c) | ON | 2 | spark-erb-receipts 0018: x4/x8 does NOT reproduce on Grace; ON beats OFF 1.16-1.31x; w2 c-curve positive to ~3.9 qps |

Exceptions: (a) SCBench model-free treatment arms — SPLADE/dense disabled, the
daemon has nothing to encode; (b) #339 determinism cards pin their own knob
values; (c) any arm at c>1 keeps the recall validity gate (one-sided recall
wobble, spark-erb-receipts 0018 gap G5, is unresolved).

## Campaigns

| campaign | cymatix commit | tag | scbench fork commit | manifest sha256 | config deltas from shipped defaults | status | receipts |
|---|---|---|---|---|---|---|---|
| 2026-08-08-codex-pilot ("luna") | `0b125eab` | `bench/scbench-luna-2026-08-08` | `2a246054` (branch `cymatix/codex-ab`) | `<FROM STEP 1>` | model-free treatment: SPLADE off, ribosome off; codex-cli 0.147.0, model gpt-5.6-luna, effort medium; docker `sha256:8bd6d4f3c221...`; seed 20260808 | complete | issue #359; `benchmarks/scbench/campaigns/2026-08-08-codex-pilot/` |
| 2026-08-11-codex-terra | `9312cde` (execution tree; manifest implementation pin `aef12732`) | `bench/scbench-terra-2026-08-11` | `bb40ec5e` (branch `cymatix/codex-ab`) | `<FROM STEP 1>` | `<FROM STEP 1: model/effort/docker/seed>` | in flight | `benchmarks/scbench/campaigns/2026-08-11-codex-terra/` |
| codex-sol (planned) | — | — | — | — | — | reserved (weekly-usage window permitting) | — |

Notes: the terra manifest pins `aef12732` as the frozen implementation
baseline; the run executes from `9312cde` (tree verified clean,
`git describe` = `v0.8.6-104-g9312cde`). Both are recorded; the tag anchors
the execution tree. A/B campaign rows (`claude/ab-data-cymatics-pki-dff9c6`)
may be added later.
```

- [ ] **Step 3: Verify no placeholder slots remain**

Run: `grep -n "FROM STEP 1" docs/benchmarks/BASELINES.md`
Expected: no output. If any slot survives, go back to Step 1's fallback (campaign README) before proceeding.

- [ ] **Step 4: Commit**

```bash
cd "/f/Projects/cymatix-context/.claude/worktrees/codebase-audit-weaknesses-96a7da"
git add docs/benchmarks/BASELINES.md
git commit -m "docs(bench): BASELINES.md — campaign ledger + standard bench profile

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Cut and push the two annotated tags

**Files:**
- None created; two annotated tags on existing commits of `codex/scbench-cymatix-ab` in the main `Cymatix-Context` repo.

**Interfaces:**
- Consumes: tag names + commit shas exactly as written in Task 1's ledger.
- Produces: origin-visible tags Joe can `git fetch --tags` and pin.

- [ ] **Step 1: Verify both commits exist and are what the ledger says**

```bash
cd /f/Projects/cymatix-context
git log --oneline -1 0b125eabebb03c3566befa81be94443119877abd
git log --oneline -1 9312cde
```

Expected: luna commit subject mentions historical replay compatibility; `9312cde` subject is "bench: repin Terra after preflight hardening". If either lookup fails, STOP — do not guess a different sha; re-open the campaign manifests.

- [ ] **Step 2: Create the annotated tags**

```bash
cd /f/Projects/cymatix-context
git tag -a bench/scbench-luna-2026-08-08 0b125eabebb03c3566befa81be94443119877abd \
  -m "SCBench codex pilot (luna) baseline. Campaign 2026-08-08-codex-pilot; manifest cymatix_commit pin. Model-free treatment (SPLADE/ribosome off). See docs/benchmarks/BASELINES.md."
git tag -a bench/scbench-terra-2026-08-11 9312cde \
  -m "SCBench codex terra baseline (execution tree; manifest implementation pin aef12732). Campaign 2026-08-11-codex-terra, run in flight from this commit. See docs/benchmarks/BASELINES.md."
```

- [ ] **Step 3: Verify locally, then push ONLY the tags**

```bash
cd /f/Projects/cymatix-context
git tag -n1 -l "bench/scbench-*"
git push origin bench/scbench-luna-2026-08-08 bench/scbench-terra-2026-08-11
git ls-remote --tags origin | grep scbench
```

Expected: both tags listed locally with their messages; `ls-remote` shows both on origin. No branch refs pushed.

---

### Task 3: Lane A cards (A1, A2)

**Files:**
- Create: `f:/Projects/cc-exchange/from-max/board/cards/A1-containment-in-receipts.md`
- Create: `f:/Projects/cc-exchange/from-max/board/cards/A2-delivery-loss-ledger.md`

**Interfaces:**
- Produces: card ids `A1-containment-in-receipts`, `A2-delivery-loss-ledger` — BOARD.md (Task 6) lists them verbatim; Joe's claim files will be `from-joe/board/claims/<id>.md`.
- No commit in Tasks 3-6: the whole board lands as one `exchange-push.sh` call in Task 7.

- [ ] **Step 1: Write `A1-containment-in-receipts.md`**

```markdown
---
id: A1-containment-in-receipts
lane: A
class: fork-pr
issue: 335
pin: v0.8.6
bed: any
graded_by: delivered-gold
effort: half-day
deliverable: fork PR adding answer-containment / delivered-gold columns to shipped bench receipts
---

# A1 -- containment scoring in the shipped bench receipts

## Context

Issue #335 (council-verified): the dense/ANN threshold gate is the pipeline's
candidate-volume throttle, but at min_genes=1 it cuts end-to-end delivered-gold
23 -> 15 of 30 queries, and rank-based recall@12 cannot see this by
construction. Your e4b drop (spark-erb-receipts 0017) scored answer
containment from day one and reproduced know_emit_floor at rank 1 plus the
proxy_port inversion (gold MISS, answer delivered pos 1). The scorer exists;
it lives in your harness, not in ours.

## Ask

Port your containment scorer into the shipped receipt writers so
delivered-gold and answer-containment appear as first-class columns next to
recall@12 in: scripts/bench_chain/ablation_ladder.py receipts and the server
bench receipt path (benchmarks/dogfood/run_server_scaling.py). Fork PR against
master, based on the v0.8.6 tag lineage. Keep the columns additive -- existing
receipt fields byte-identical when the scorer has no gold text to check.

## Acceptance

- Fork PR open on mbachaud/Cymatix-Context with tests (a fixture bed where
  gold is admitted-but-not-delivered must score recall hit + delivered-gold
  miss).
- One receipt file regenerated on any bed you hold, showing the new columns,
  committed to results/ in cc-exchange with sha256.

## Out of scope

ANN-gate tuning (that sweep is issue #335 work item 2 and runs AFTER the
metric exists); any default flip.
```

- [ ] **Step 2: Write `A2-delivery-loss-ledger.md`**

```markdown
---
id: A2-delivery-loss-ledger
lane: A
class: receipts
issue: 344
pin: v0.8.6
bed: 850k-blob
graded_by: delivered-gold
effort: box-day
deliverable: per-needle delivery-loss attribution table on the 850,379-gene blob
---

# A2 -- delivery-loss ledger on the 850k blob

## Context

Issue #344 separates two failure classes that both read as "gold was not
delivered": near-cutoff ranking loss (gold in the scored pool, outside the
delivered window) and static semantic miss (gold far down the pool at every
scale). At 829k the scored pool is often 93k-294k genes and on the probed
scale-loss needles gold was never absent from it -- so a global "widen first
pool" policy would add cost without touching the near-cutoff class. The
adaptive-admission design needs measured stage attribution first.

## Ask

On the byte-frozen 850,379-gene blob at v0.8.6: for every needle whose gold is
not delivered, attribute the loss to exactly one stage --
admission (not in scored pool) / fusion rank (in pool, outside window) /
trim (cut by budget enforcement) / session elision (delivered earlier,
elided). Output one row per undelivered gold: needle id, gene id, stage,
pool rank, pool size.

## Acceptance

- Attribution table (JSON or CSV) + the run config, committed under
  results/ with sha256 in the manifest.
- Row count reconciles against the run's own miss count (no unattributed
  losses; disclose any "unknown" bucket explicitly).

## Out of scope

Implementing adaptive admission; changing any knob. This card is measurement
only. Effort is box-day: post your run design in the claim file and get a
blessing note before burning it (spec section 3 rule).
```

- [ ] **Step 3: Verify front-matter parses and ids match filenames**

```bash
cd /f/Projects/cc-exchange
for f in from-max/board/cards/A*.md; do
  id=$(sed -n 's/^id: //p' "$f"); base=$(basename "$f" .md)
  [ "$id" = "$base" ] && echo "OK $base" || echo "MISMATCH $f ($id)"
done
```

Expected: two `OK` lines.

---

### Task 4: Lane B cards (B1-B4)

**Files:**
- Create: `f:/Projects/cc-exchange/from-max/board/cards/B1-static-miss-crosscheck.md`
- Create: `f:/Projects/cc-exchange/from-max/board/cards/B2-authority-idf-ladder.md`
- Create: `f:/Projects/cc-exchange/from-max/board/cards/B3-splade-pki-ablation.md`
- Create: `f:/Projects/cc-exchange/from-max/board/cards/B4-entry-path-coverage.md`

**Interfaces:**
- Produces: card ids `B1-static-miss-crosscheck`, `B2-authority-idf-ladder`, `B3-splade-pki-ablation`, `B4-entry-path-coverage` for BOARD.md (Task 6).

- [ ] **Step 1: Write `B1-static-miss-crosscheck.md`**

```markdown
---
id: B1-static-miss-crosscheck
lane: B
class: verification
issue: 340
pin: v0.8.6
bed: 850k-blob
graded_by: receipts-only
effort: <=2h
deliverable: set-identity verdict -- is the 99/469 static-miss class the same set as the 0010 fixed miss set
---

# B1 -- static-miss set crosscheck (99/469 vs the 0010 fixed set)

## Context

Issue #340: on the splade-less 829k bed, 99/469 needles never score gold
anywhere in the scored pool (full469_nosplade_c1.json carries per-needle
records naming each one). Your 0010 set-identity receipt: the same 62/125
semantic questions (108/469 all-types) miss gold in all six primary arms,
minimum pairwise Jaccard exactly 1.000000. Two independently derived miss
sets, never compared.

## Ask

Set-identity analysis, the same method as 0010: intersection / union /
Jaccard between the 99-needle static-miss set and your fixed 108-needle set,
per-needle ids in the receipt. Same set (or near-superset) means one root
cause and one fix; disjoint means two workstreams and issue #340 splits.

## Acceptance

- Per-needle id lists for both sets, the intersection, and Jaccard, in one
  JSON receipt under results/ with sha256.
- One paragraph stating which hypothesis survives.

## Out of scope

Fixing anything. This is pure attribution.
```

- [ ] **Step 2: Write `B2-authority-idf-ladder.md`**

```markdown
---
id: B2-authority-idf-ladder
lane: B
class: receipts
issue: 327, 355, 356
pin: v0.8.6
bed: e4b-tagger-bed
graded_by: delivered-gold
effort: half-day
deliverable: delivered-gold delta from the +2.0 authority path-match boost, split by tier and tag mode
---

# B2 -- authority-boost IDF ladder

## Context

Issue #327: the authority boost is IDF-blind -- query terms match against the
absolute path, so a project name grants +2.0 to 88% of its own corpus. You
already built the density-gate probe ladder on this issue (filename
dominance: rename-only moved stubs 8.3% -> 41.7%; stub band 4-8 tags at
~4KB), with a runnable repro in the 0017 drop. Two attribution gaps remain:
issue #356 (one gate wraps tag_exact AND tag_prefix -- attribution
unresolved) and issue #355 (the tags gate covers Tiers 1+2 only, so the
2.58GB promoter_index contribution is unmeasured).

## Ask

Extend your probe ladder on the e4b tagger bed at v0.8.6: measure the
delivered-gold delta of the +2.0 path-match boost, split (a) tag_exact vs
tag_prefix (issue #356), (b) Tiers 1+2 vs promoter_index (issue #355). Same
rung protocol as your #327 ladder.

## Acceptance

- Per-rung receipts (config + per-needle delivered-gold) under results/ with
  sha256.
- A 2x2 summary table (tag mode x tier) of delivered-gold deltas.

## Out of scope

Changing the boost. The receipts feed the issue #327 fix design and the
promoter_index storage decision; they do not make either call.
```

- [ ] **Step 3: Write `B3-splade-pki-ablation.md`**

```markdown
---
id: B3-splade-pki-ablation
lane: B
class: receipts
issue: 333, 334
pin: v0.8.6
bed: 850k-blob
graded_by: delivered-gold
effort: box-day
deliverable: 2x2 A/B (SPLADE on/off x PKI on/off) graded by delivered-gold
---

# B3 -- SPLADE and PKI earn-their-storage ablation

## Context

Issue #334: PKI is 8.56GB (18.5% of the blob) with no off-switch and an
unmeasured recall contribution. Issue #333: SPLADE has no measured benefiting
scale -- the profile default question is open, with a query-side size gate
and dead-weight compaction on the table. Both decisions are storage-econ
receipts we owe ourselves, and both need a big bed to mean anything.

## Ask

On the 850k blob at v0.8.6, run the 2x2: {SPLADE on, off} x {PKI consulted,
bypassed}, all four arms same needle set, graded by delivered-gold (and
recall@12 alongside for continuity with published numbers). Use the standard
bench profile for your box (daemon ON, workers 2 -- BASELINES.md). If PKI
cannot be bypassed by config at v0.8.6, say so in the claim file and we
scope a bypass switch first -- do not patch the tree ad hoc.

## Acceptance

- Four arm receipts + config diffs under results/ with sha256.
- Delivered-gold and recall@12 per arm, one table.
- c=1 arms only (the c>1 validity wobble is unresolved -- 0018 G5).

## Out of scope

The removal/off-switch decision itself (issues #333/#334 stay ours); any
compaction.
```

- [ ] **Step 4: Write `B4-entry-path-coverage.md`**

```markdown
---
id: B4-entry-path-coverage
lane: B
class: receipts
issue: 260
pin: v0.8.6
bed: 850k-blob
graded_by: delivered-gold
effort: half-day
deliverable: fts5_candidate_depth sweep against delivered-gold on the fixed 62-set
---

# B4 -- entry-path coverage: the FTS depth lever on the fixed 62-set

## Context

Your 0011 admission decomposition on the fixed 62-needle miss set: dense
recovers 0 at every fetchable depth (it holds gold for 59/62 but at
full-corpus ranks ~19,000-203,000); plain FTS at depth 1000 recovers 45/62 --
73% of the missing-gold mass, for a config change. That refuted
query-encoding as the semantic-arm lever and left entry-path coverage
(lexical depth, synonym-style entry terms) as the live one. Our knob is
[retrieval] fts5_candidate_depth (default 0 = auto).

## Ask

Sweep fts5_candidate_depth on the 850k blob at v0.8.6 over
{auto, 200, 500, 1000, 2000} against the fixed 62-set, graded by
delivered-gold end-to-end (not pool-recoverable -- 0011 already measured
that). Record per-arm p50 latency so the cost curve rides with the recall
curve.

## Acceptance

- Per-depth receipts (delivered-gold, recall@12, p50) under results/ with
  sha256, c=1 only.
- One line naming the knee of the curve, or "no knee".

## Out of scope

Synonym/entry-term work (separate card once this lands); any default change
(#205 profiles own that).
```

- [ ] **Step 5: Verify front-matter parses and ids match filenames**

```bash
cd /f/Projects/cc-exchange
for f in from-max/board/cards/B*.md; do
  id=$(sed -n 's/^id: //p' "$f"); base=$(basename "$f" .md)
  [ "$id" = "$base" ] && echo "OK $base" || echo "MISMATCH $f ($id)"
done
```

Expected: four `OK` lines.

---

### Task 5: Lane C cards (C1-C4)

**Files:**
- Create: `f:/Projects/cc-exchange/from-max/board/cards/C1-rank-hash-instrumentation.md`
- Create: `f:/Projects/cc-exchange/from-max/board/cards/C2-cymatics-armC-rerun.md`
- Create: `f:/Projects/cc-exchange/from-max/board/cards/C3-know-gate-redteam.md`
- Create: `f:/Projects/cc-exchange/from-max/board/cards/C4-n4-bracket.md`

**Interfaces:**
- Produces: card ids `C1-rank-hash-instrumentation`, `C2-cymatics-armC-rerun`, `C3-know-gate-redteam`, `C4-n4-bracket` for BOARD.md (Task 6).

- [ ] **Step 1: Write `C1-rank-hash-instrumentation.md`**

```markdown
---
id: C1-rank-hash-instrumentation
lane: C
class: fork-pr
issue: 339
pin: v0.8.6
bed: dogfood-control-bed
graded_by: receipts-only
effort: box-day
deliverable: rank-hash/byte-diff instrumentation (fork PR) + re-run of the one-sided c>1 recall degradation
---

# C1 -- settle G5: retrieval-timing vs encode drift

## Context

Your 0018 gap G5: recall movement at c>1 is one-sided -- 0 above baseline /
12 tied / 36 below (p on the order of 1e-11 under symmetric noise) -- and
your estate carries no byte-diff or rank-hash instrumentation, so the GEMM
batch-shape-drift mechanism stays a hypothesis it cannot settle. Our issue
#339 found a second source on this side: the env-gated encode-determinism
profile (slice-2) measured NO-GO (GPU capacity 51.4 -> 1.5 bundles/s) and
the c=8 deviation flag STILL fired -- proving a retrieval-timing source no
encode fix can zero. Two boxes, same symptom, mechanism unattributed.

## Ask

Two halves. (1) Fork PR: per-query rank-hash + candidate-set hash recorded
into the server bench receipt path (benchmarks/dogfood/run_server_scaling.py
plus whatever server-side hook it needs), default-inert -- byte-identical
receipts when the env gate is off. (2) Re-run your 0018 sweep-A shape on the
dogfood control bed (ad75993b...) with hashing ON at c in {1,4,16}: identical
hashes across c-levels = encode drift excluded on this silicon; divergent
candidate sets = retrieval-timing confirmed cross-box.

## Acceptance

- Fork PR with the inert-when-off property demonstrated by test.
- Hash-instrumented receipts for the three c-levels under results/ with
  sha256, plus a one-paragraph verdict naming which mechanism survives.

## Out of scope

Fixing the wobble; the canonical-v2 fixed-shape design (issue #339 keeps
that gated on a cost measurement). This card pins its own knobs -- the
standard bench profile does NOT apply.
```

- [ ] **Step 2: Write `C2-cymatics-armC-rerun.md`**

```markdown
---
id: C2-cymatics-armC-rerun
lane: C
class: verification
issue: 354
pin: v0.8.6
bed: e4b-tagger-bed
graded_by: receipts-only
effort: <=2h
deliverable: independent re-run of the retracted cymatics spread arm C with the fixed harness
---

# C2 -- cymatics arm C, actually run this time

## Context

Issue #354 (retraction): ablate_cymatics.py arm C set cfg.cymatics.peak_width,
which the runtime never reads (peak width derives from
[budget] splice_aggressiveness). Arm C was byte-identical to arm A on both
beds, so the published "spread does exactly nothing" verdict and its
clean-bed replication are RETRACTED -- the treatment was never applied. The
harness is fixed on our side (overrides m._cymatics_peak_width). Arms B and D
still stand (cymatics never helps a needle, loses to random bins), so the
deprecation case survives on behavioural grounds -- but the spread claim
specifically needs a run where the knob provably moves.

## Ask

Re-run arm C vs arm A on a bed we did NOT run it on (your e4b tagger bed),
fixed harness, at v0.8.6. Before the paired arms, verify the override bites:
one probe query with peak_width at two extreme values must produce different
tier activation profiles (/debug/resonance) -- if it does not, stop and
report, because that is the original bug again.

## Acceptance

- The bite-check receipt (two resonance profiles, different bytes).
- Paired arm receipts + rank tables under results/ with sha256, and a
  verdict: spread does/does not move ranks on this bed.

## Out of scope

The deprecation decision (ours); arms B/D (already standing).
```

- [ ] **Step 3: Write `C3-know-gate-redteam.md`**

```markdown
---
id: C3-know-gate-redteam
lane: C
class: verification
issue: 287
pin: v0.8.6
bed: e4b-tagger-bed
graded_by: containment
effort: half-day
deliverable: adversarial cases where V2 know-confidence is high and containment fails
---

# C3 -- red-team the know-gate V2 feature set

## Context

Issue #287: the shipped know-confidence scalar is a structural zero (0%
know_emitted across all 500 ERB questions; the shipped b1 top_score
coefficient is negative, so better retrieval lowers confidence). Council
verdict HOLD, 0/5 would_ship. The best-diagnosed direction is V2: swap the
scale-sensitive top_score/score_gap for top_dominance + coordinate_crispness.
Your 0017 already showed the failure shape that matters: know_emit_floor
answer-hollow at rank 1 (mechanism = chunk granularity) and the proxy_port
inversion. Before we recalibrate on those features, we want them attacked.

## Ask

On your e4b bed at v0.8.6: compute top_dominance and coordinate_crispness
per query (harness-side is fine -- no code change needed), then hunt inputs
where BOTH are high and answer containment fails, and inputs where both are
low and containment succeeds. Your chunk-granularity and proxy_port cases
are the seeds; find more classes, not more instances.

## Acceptance

- Per-case receipts (query, feature values, containment verdict) under
  results/ with sha256.
- A taxonomy note: each failure class named, with a one-line mechanism
  hypothesis. Zero found after an honest hunt is a publishable receipt too.

## Out of scope

Fitting coefficients, choosing emit_floor, any [know] config change (the
governing rule reserves [know] keys for issue #287 work on our side).
```

- [ ] **Step 4: Write `C4-n4-bracket.md`**

```markdown
---
id: C4-n4-bracket
lane: C
class: receipts
issue: spark-erb-receipts 0018 section 5
pin: v0.8.6
bed: dogfood-control-bed
graded_by: receipts-only
effort: <=2h
deliverable: the N4 bracket pack, as you offered it
---

# C4 -- N4 bracket: GO

## Context

Your 0018 section 5 offer, accepted as a card. The open items it closes: the
w=1 bracket (current claim is "do not tune workers up", not "2 is optimal"),
the ~8% n=1 noise band at the decision points (A_w4's 1.06x sits inside it),
the first measured open-loop capacity number (making the >=30 GPU / >=3.5
CPU bundles/s bars checkable by measurement), and the A_w2 arm that is ON by
reconstruction only.

## Ask

Exactly your N4 design: one w=1 arm; 3x repeats at the decision points; one
open-loop offered-load arm; re-run A_w2 under the fixed sweep driver so the
last reconstructed arm becomes receipted. <=2h on e92c, inside the box
rails.

## Acceptance

- Receipts under results/ per your own provenance standard (driver-written
  watermarks, POST /encode/* deltas, manifest with sha256s).
- The w-bracket claim updated in one line: "2 is optimal on GB10" or "w=1
  wins", with confidence intervals from the repeats.

## Out of scope

Nothing to add -- the design is yours; this card is the blessing your
section 5 asked for.
```

- [ ] **Step 5: Verify front-matter parses and ids match filenames**

```bash
cd /f/Projects/cc-exchange
for f in from-max/board/cards/C*.md; do
  id=$(sed -n 's/^id: //p' "$f"); base=$(basename "$f" .md)
  [ "$id" = "$base" ] && echo "OK $base" || echo "MISMATCH $f ($id)"
done
```

Expected: four `OK` lines.

---

### Task 6: BOARD.md

**Files:**
- Create: `f:/Projects/cc-exchange/from-max/board/BOARD.md`

**Interfaces:**
- Consumes: the ten card ids from Tasks 3-5, exactly as written there.
- Produces: the board Joe reads; the claim-path convention `from-joe/board/claims/<id>.md` his instance follows.

- [ ] **Step 1: Write `BOARD.md`**

```markdown
# Task board -- max lane

Cards Max opens, Joe pulls. Graded by delivered-gold / answer containment
wherever the work permits; cards that cannot be graded that way say
`graded_by: receipts-only` and mean it.

## How to claim (joe)

1. Pick any OPEN card. Priority is board order, but you choose.
2. Write `from-joe/board/claims/<card-id>.md` with front-matter
   `{id, status: CLAIMED, ts}` and whatever notes you want. Updating your
   own claim file later (IN-FLIGHT, DELIVERED + pointer) is lane-legal.
3. Append a note line to `changelog/joe.jsonl`.
4. Effort `box-day` or larger: post the run design in the claim file first
   and wait for a blessing note from me before burning the day (your own
   0017 section-9 practice, made a rule).
5. Deliver receipts under `results/` with sha256s, per your provenance
   standard. I move the card to LANDED here once the receipt is acted on.

Lifecycle: OPEN -> CLAIMED -> IN-FLIGHT -> DELIVERED -> LANDED.
Claim files are authoritative for claim state; this file is authoritative
for card content and LANDED. A card CLAIMED with no update for 14 days
reverts to OPEN. WIP guidance: <=2 fork-pr cards and <=1 box-day card in
flight at once.

Every card's `pin:` resolves against `docs/benchmarks/BASELINES.md` in the
Cymatix-Context repo (tags `bench/scbench-*` are pushed). The standard bench
profile for your box: daemon ON, workers 2 (0018 receipt). Exceptions are
named on the cards that carry them.

## Cards

| # | id | lane | class | issue | bed | effort | status |
|---|---|---|---|---|---|---|---|
| 1 | A1-containment-in-receipts | A | fork-pr | #335 | any | half-day | OPEN |
| 2 | A2-delivery-loss-ledger | A | receipts | #344 | 850k-blob | box-day | OPEN |
| 3 | B1-static-miss-crosscheck | B | verification | #340 | 850k-blob | <=2h | OPEN |
| 4 | B2-authority-idf-ladder | B | receipts | #327 #355 #356 | e4b-tagger-bed | half-day | OPEN |
| 5 | B3-splade-pki-ablation | B | receipts | #333 #334 | 850k-blob | box-day | OPEN |
| 6 | B4-entry-path-coverage | B | receipts | #260 | 850k-blob | half-day | OPEN |
| 7 | C1-rank-hash-instrumentation | C | fork-pr | #339 | dogfood-control-bed | box-day | OPEN |
| 8 | C2-cymatics-armC-rerun | C | verification | #354 | e4b-tagger-bed | <=2h | OPEN |
| 9 | C3-know-gate-redteam | C | verification | #287 | e4b-tagger-bed | half-day | OPEN |
| 10 | C4-n4-bracket | C | receipts | 0018 s5 | dogfood-control-bed | <=2h | OPEN |

Card briefs: `from-max/board/cards/<id>.md`.

## Owed by max (standing -- what currently blocks you)

- 0017 s8 pending decisions: d2a2177c identity digest + control
  summary.json; by_repo driftwatch/scorerift id lists; clean-bed provenance
  answer (which tree built it); delivered-containment tables off the CPU
  control + e2b beds.
- 0017 s9: bless/shoot the parallel-ingest equivalence-proof design.
  (C4 above IS the blessing for the N4 offer; s9 is still open.)
- 0018: bundles/s unit mapping for the capacity self-report; ack of your
  #313 yes/no when it arrives.
- sike-run1: the gold-pack tarball (64 files, 6 roots) and the xl decontam
  predicate ruling.

These arrive as thread turns, not board edits; this block just keeps the
ledger honest in both directions.
```

- [ ] **Step 2: Verify every card file referenced exists**

```bash
cd /f/Projects/cc-exchange
for id in A1-containment-in-receipts A2-delivery-loss-ledger B1-static-miss-crosscheck B2-authority-idf-ladder B3-splade-pki-ablation B4-entry-path-coverage C1-rank-hash-instrumentation C2-cymatics-armC-rerun C3-know-gate-redteam C4-n4-bracket; do
  [ -f "from-max/board/cards/$id.md" ] && echo "OK $id" || echo "MISSING $id"
done
```

Expected: ten `OK` lines.

---

### Task 7: Thread turn, changelog, index, push

**Files:**
- Create: `f:/Projects/cc-exchange/threads/task-board/0000-max-board-opening.md`
- Append: `f:/Projects/cc-exchange/changelog/max.jsonl`
- Regenerate: `index/INDEX.md`, `index/CHANGELOG.md` (via script only)

**Interfaces:**
- Consumes: BOARD.md and the ten cards from Tasks 3-6.
- Produces: the announcement Joe's instance reads on next pull; the single push landing the whole board.

- [ ] **Step 1: Write the thread opening turn**

Get the timestamp first: `date -u +%Y-%m-%dT%H:%M:%SZ` -- use its output for `ts:` below and in Step 2.

```markdown
---
thread: task-board
turn: 0
author: max
ts: <output of date -u>
title: task board open -- 10 pull-ready cards in from-max/board/, graded by delivered-gold, pins resolve against BASELINES.md + two new bench/scbench-* tags
---

Joe -- new standing surface, so cross-box work stops waiting on a bespoke
ask-turn per task: `from-max/board/BOARD.md`, ten cards, three lanes
(A: make delivered-gold measurable, B: miss-mass attribution, C:
red-team/determinism). Claim mechanics are in the board header and stay
inside the single-writer law: you write only `from-joe/board/claims/<id>.md`
plus your changelog; I never touch those, you never touch the board files.

Three things worth saying up front:

1. Grading: cards are graded by delivered-gold / answer containment wherever
   the work permits -- your e4b containment scoring is the reference
   implementation, and card A1 asks you to port it into our shipped
   receipts. Rank-only cards say `receipts-only` explicitly.
2. Pins: every card pins v0.8.6 and resolves against
   `docs/benchmarks/BASELINES.md` (new in the main repo), which also freezes
   the two SCBench campaign baselines as annotated tags
   (`bench/scbench-luna-2026-08-08`, `bench/scbench-terra-2026-08-11` --
   the terra run is in flight on my rig from that exact tree). Standard
   bench profile for your box: daemon ON, workers 2, per your own 0018
   receipt.
3. C4 is your N4 offer, accepted -- the card IS the go. The rest of my 0018
   answers (bundles/s mapping, the 0017 s8/s9 decisions) come as a
   spark-erb-receipts turn, not here; the board's "owed by max" block lists
   them so the ledger is honest in both directions.

No deadline semantics on any card. Pull what you want, in any order;
board order is my priority signal, nothing more.
```

- [ ] **Step 2: Append the changelog line**

```bash
cd /f/Projects/cc-exchange
printf '%s\n' '{"ts":"<output of date -u>","author":"max","action":"create","path":"from-max/board/BOARD.md","note":"task board open: 10 cards, 3 lanes, delivered-gold graded; thread task-board/0000"}' >> changelog/max.jsonl
```

- [ ] **Step 3: ASCII check on everything this plan wrote**

```bash
cd /f/Projects/cc-exchange
grep -rPln '[^\x00-\x7F]' from-max/board/ threads/task-board/ && echo "NON-ASCII FOUND -- fix before push" || echo "ascii clean"
```

Expected: `ascii clean`.

- [ ] **Step 4: Regenerate the index**

```bash
cd /f/Projects/cc-exchange
bin/gen-index.sh
grep -n "task-board" index/INDEX.md
```

Expected: the threads section lists **task-board** with 1 turn.

- [ ] **Step 5: Push via the wrapper (never bare git push)**

```bash
cd /f/Projects/cc-exchange
export EXCHANGE_AUTHOR=max
bin/exchange-push.sh "max: task board open -- 10 pull-ready cards (delivered-gold graded), BASELINES.md pins, thread task-board/0000"
```

Expected: wrapper reports a successful push (it retries on push races itself).

- [ ] **Step 6: Verify the drop landed**

```bash
cd /f/Projects/cc-exchange
git log --oneline -1 && git status --porcelain
```

Expected: HEAD is the board commit, status empty.

---

## Self-review notes

- Spec section 2 (mechanics) -> Tasks 6-7; section 3 (schema) -> card
  front-matter in Tasks 3-5; section 4 (10 cards) -> Tasks 3-5; section 5
  (owed-by-max) -> Task 6 Step 1 block; section 6 (freeze) -> Tasks 1-2;
  section 7 (rollout) -> task ordering; sections 8-9 respected (no code
  changes, no GitHub automation, Terra untouched, tag-only push).
- The two `<FROM STEP 1>` ledger slots are extraction targets with exact
  commands and a named fallback, not placeholders.
- Card ids are spelled identically in Tasks 3-5 (files), Task 6 (BOARD.md
  table + existence check), and Task 7 (announcement); the claim path
  convention `from-joe/board/claims/<id>.md` appears identically in the
  spec, BOARD.md, and the thread turn.
```
