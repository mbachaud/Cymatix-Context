# cc-exchange task board — design

**Date:** 2026-08-11
**Status:** approved shape, spec for review
**Branch:** `claude/cc-exchange-task-board-ece78f`

## 1. Purpose

Give Joe a standing board of pull-ready cards in cc-exchange so cross-box work no
longer waits on a bespoke ask-turn per task. Every card is graded by
**delivered-gold / answer containment** — does the right content actually reach the
agent's delivered context — not recall@k, which credits admission and hides the rank
cliffs the 0011 decomposition exposed. Cards that structurally cannot be graded that
way (determinism, red-team re-runs) say so explicitly in their `graded_by` field.

Joe's approved pull scope (three classes):

1. **Box/bed receipts** — measurement runs needing his silicon (GB10 Grace `e92c`)
   or his beds (850,379-gene blob, e4b tagger bed, dogfood control bed
   `ad75993b…`).
2. **Independent verification / red-team** — re-derive or attack published claims;
   output is confirmations or retractions, not features.
3. **Fork PRs into Cymatix-Context** — `addiplus` is not a collaborator, so code
   arrives as fork PRs Max reviews. Review load lands on Max; the board caps
   fork-PR cards at 2 concurrent.

## 2. Mechanics — single-writer, pure git

The board lives inside the existing cc-exchange protocol. No file is ever written
by both parties; the CLAUDE.md lane law applies unchanged.

| path | writer | purpose |
|---|---|---|
| `from-max/board/BOARD.md` | max | the board: one row per card, status, pointers |
| `from-max/board/cards/<id>.md` | max | one file per card — full brief, acceptance criteria |
| `from-joe/board/claims/<id>.md` | joe | claim file: status, notes, delivery pointer |
| `changelog/joe.jsonl` | joe | append `{"action":"note","note":"board <id> claimed"}` |
| `changelog/max.jsonl` | max | append on card open / land |

**Status lifecycle:** `OPEN → CLAIMED → IN-FLIGHT → DELIVERED → LANDED`.

- Joe moves a card to CLAIMED/IN-FLIGHT/DELIVERED by writing/updating **his own**
  claim file (updating his own file is lane-legal; only thread turns are
  append-only).
- Max moves DELIVERED → LANDED by editing `BOARD.md` after the receipt is acted on
  (issue updated, PR merged, or verdict recorded in the cymatix repo).
- BOARD.md status is a **mirror** of the claim files, refreshed by Max; claim files
  are authoritative for claim state, BOARD.md is authoritative for card content and
  LANDED.
- Stale claims: a card CLAIMED with no IN-FLIGHT update for 14 days reverts to OPEN
  (Max edits BOARD.md; Joe's claim file stays as history).

**Announcement:** the board opens as a new thread `threads/task-board/0000-max-board-opening.md`
pointing at `from-max/board/BOARD.md`, so board discussion does not tangle with
`spark-erb-receipts`. The (separate) 0019 reply answering Joe's 0018 — N4 go/no-go,
bundles/s mapping, the five pending 0017 asks — is a companion deliverable, not part
of this spec; the board's "owed by max" block references it.

## 3. Card schema

Each `from-max/board/cards/<id>.md` carries front-matter:

```yaml
id: B1-static-miss-crosscheck
lane: B            # A = objective, B = miss-mass, C = red-team/determinism
class: receipts    # receipts | verification | fork-pr
issue: 340         # Cymatix-Context issue number(s)
pin: v0.8.6        # the frozen tag/commit the run must execute against
bed: 850k-blob     # which bed/box makes this Joe's (or "any")
graded_by: delivered-gold   # delivered-gold | containment | receipts-only
effort: box-day    # ≤2h | half-day | box-day | multi-day
deliverable: ...   # one line
```

Body sections: **Context** (what is known, with receipt pointers), **Ask** (exact
runs/analysis), **Acceptance** (what makes it DELIVERED — always receipt files with
shas, per Joe's own provenance standard), **Out of scope**.

Rules:
- Every `pin:` resolves against `docs/benchmarks/BASELINES.md` (§6). A card may not
  say "master".
- Effort ≥ box-day requires Joe to post the design in his claim file and get a
  blessing note from Max **before** burning the box-day (codifies his own
  section-9 practice from 0017).
- Cards never assign — Joe pulls what he wants; priority is the board order.

## 4. Opening card set — 10 cards, 3 lanes

Ordered by delivered-gold leverage within lane.

### Lane A — make the objective measurable

| id | issue | class | brief |
|---|---|---|---|
| A1-containment-in-receipts | #335 | fork-pr | Port Joe's answer-containment scorer (e4b drop, scored from day one) into the shipped bench receipts (`ablation_ladder`, server benches). Delivered-gold becomes a first-class column next to recall@12. Pin v0.8.6; PR arrives as fork PR. |
| A2-delivery-loss-ledger | #344 | receipts | On the 850k blob at the pinned tag: for every undelivered gold, attribute the loss to a stage — admission (not in scored pool), fusion rank (in pool, outside window), trim, or session elision. Output: per-needle attribution table. This is the measured input #344's adaptive-admission design needs. |

### Lane B — miss-mass attribution

| id | issue | class | brief |
|---|---|---|---|
| B1-static-miss-crosscheck | #340 | verification | Cross-check the 99/469 static-miss class (829k, `full469_nosplade_c1.json`) against Joe's fixed 62/125 (108/469) miss set from 0010. Same set ⇒ one root cause and one fix; disjoint ⇒ two workstreams. Set-identity method he already used (Jaccard, per-needle ids). |
| B2-authority-idf-ladder | #327, #355, #356 | receipts | Extend Joe's density-gate probe ladder (filename dominance: rename-only 8.3%→41.7% stubs) to the authority-boost IDF blindness: measure delivered-gold delta from the +2.0 path-match boost, split tag_exact vs tag_prefix (#356) and Tiers 1+2 vs promoter_index (#355). |
| B3-splade-pki-ablation | #333, #334 | receipts | A/B at the pinned tag on his beds: SPLADE off / PKI off / both, graded by delivered-gold. Decides whether 8.56GB PKI and the SPLADE weight earn their storage. Doubles as the storage-econ receipt. |
| B4-entry-path-coverage | #260 | receipts | Scoped to entry-path coverage only (lexical depth, synonym-style entry terms) — 0011 already refuted query-encoding as the lever (dense recovers 0 at every fetchable depth; FTS@1000 recovers 45/62). Sweep `fts5_candidate_depth` against delivered-gold on the fixed 62-set. |

### Lane C — red-team / determinism

| id | issue | class | brief |
|---|---|---|---|
| C1-rank-hash-instrumentation | #339 | fork-pr + receipts | His G5 made settleable: add rank-hash / byte-diff instrumentation to the serving path (fork PR), then re-run the c>1 one-sided recall degradation (0/12/36, p≈1e-11) to separate retrieval-timing from encode drift. `graded_by: receipts-only`. |
| C2-cymatics-armC-rerun | #354 | verification | Independent re-run of the retracted cymatics spread arm C on his bed with the fixed harness (`m._cymatics_peak_width` override). Confirms or refutes the spread null that was never actually tested. `graded_by: receipts-only`. |
| C3-know-gate-redteam | #287 | verification | Adversarial probe of the know-gate V2 feature set (`top_dominance` + `coordinate_crispness`) on his beds: find inputs where V2 confidence is high and containment fails, before recalibration ships. |
| C4-n4-bracket | 0018 §5 | receipts | Joe's own N4 offer, accepted as a card: w=1 arm, 3× repeats at decision points, one open-loop capacity arm, re-run `A_w2` under the fixed driver. ≤2h on e92c. `graded_by: receipts-only`. |

**WIP guidance on the board:** ≤2 fork-pr cards and ≤1 box-day card in flight at once.

## 5. "Owed by max" block

BOARD.md carries a standing block listing what currently blocks Joe, so the board is
honest in both directions. Opening content:

- 0017 §8 pending decisions: d2a2177c identity digest + control summary.json;
  by_repo id lists; clean-bed provenance answer; delivered-containment tables.
- 0017 §9: bless/shoot the parallel-ingest equivalence-proof design.
- 0018: N4 go/no-go (C4 card = the go), bundles/s unit mapping for the capacity
  self-report, ack of the #313 answer he owes separately.
- sike-run1: gold-pack tarball (64 files, 6 roots) and the xl decontam predicate
  ruling.

## 6. Baseline freeze — tags + ledger

SCBench campaigns are running on this rig; the config must be frozen where the
baselines were measured.

**Annotated tags** (pushed to origin so Joe can pin the same trees):

- `bench/scbench-luna-2026-08-08` — at the commit the pilot campaign's own
  preflight/manifest receipts record (campaign `2026-08-08-codex-pilot`, branch
  `codex/scbench-cymatix-ab`; resolve the exact sha from the campaign receipts at
  implementation time, do not guess).
- `bench/scbench-terra-2026-08-11` — at `9312cde` (verified: tree clean,
  `git describe` = `v0.8.6-104-g9312cde`, Terra run in flight from this commit).

**`docs/benchmarks/BASELINES.md`** — one ledger, one row per campaign:
campaign id → commit sha → tag → manifest sha256 → config deltas from shipped
defaults (SPLADE off, ribosome off for the model-free treatment; model, reasoning
effort, docker digest, seed) → status (complete / in-flight) → receipts pointer.
Opening rows: luna (complete), terra (in-flight), sol (planned — row reserved,
filled when/if the weekly-usage window allows). A/B rows from the
`claude/ab-data-cymatics-pki-dff9c6` campaign may be added later; out of scope here.

Rules the ledger states explicitly (from the 2026-08-09 receipt invalidations):
compare arms only within their own run's baseline; measurement runs against a
frozen tag, never a moving branch.

## 7. Rollout

1. **Cymatix repo** (this branch): this spec; `docs/benchmarks/BASELINES.md`; the
   two annotated tags cut and pushed (tags are on `codex/scbench-cymatix-ab`
   commits — tag push only, no branch changes).
2. **cc-exchange** (max lane): `from-max/board/BOARD.md`, 10 card files,
   `threads/task-board/0000-max-board-opening.md`, changelog append, `gen-index.sh`,
   push via wrapper.
3. Card briefs are written from the issue bodies + thread receipts already cited;
   no new measurement is needed to open the board.

## 8. Out of scope

- The `spark-erb-receipts` 0019 reply (answers to 0018) — companion deliverable,
  separate turn.
- Any change to retrieval code or defaults; the board only schedules work.
- GitHub Projects/issues automation; the board is markdown + git by design.
- Granting Joe collaborator access on Cymatix-Context.

## 9. Risks

- **Fork-PR review load** — capped at 2 concurrent; Max is the merge gate.
- **Board/claims drift** — BOARD.md mirror can lag claim files; the lifecycle names
  claim files authoritative for claim state, so drift is cosmetic.
- **Baseline staleness** — #332/#343 land later and re-baseline everything; the
  ledger's status column plus per-card `pin:` keeps old receipts comparable within
  their own row (never across rows).
- **Terra in flight** — tag `9312cde` now, before the branch advances; the run
  itself is untouched.
