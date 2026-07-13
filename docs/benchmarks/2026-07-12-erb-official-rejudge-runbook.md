# EnterpriseRAG-Bench official-judge re-judge — operator runbook

**Date:** 2026-07-12 · **Branch:** `feat/erb-official-rejudge` · **Worktree:** `.claude/worktrees/erb-rejudge`

**Status: environment is ready to run. No judged evaluation has been executed. No `LLM_API_KEY` exists
yet anywhere in this repo, this branch, or the ERB checkout.** Everything below was built and verified
without spending a cent; the only thing left for the operator to do is export a real key and run the
three commands in §6.

---

## 1. Purpose

Re-judge our 500-question ERB blob run — `benchmarks/results/erb500k_blob_additive_scored.jsonl`, scored
end-to-end by our own trinary judge (Claude Sonnet: CORRECT / INCORRECT / ABSTAINED) — with the
**official** EnterpriseRAG-Bench judge (`src.scripts.answer_evaluation.metrics_based_eval`, OpenAI
`gpt-5.4`), so the result is submittable to the public leaderboard instead of being an internal-only
number.

## 2. Framing — what this artifact is (and isn't)

- **This IS:** a re-judge of the **500-question scored ERB run**
  (`erb500k_blob_additive_scored.jsonl`, `scripts/bench_chain/erb500k_scored.py`'s output), previously
  reported as **47.2% end-to-end / 55% gold-delivered** (quote as a pair — see
  `docs/benchmarks/2026-07-10-erb-blob-829k-reproduction.md`).
- **This is NOT:** the 469-question retrieval-probe table from the 2026-07-10→12 overnight sweep
  (`bench/overnight-2026-07-10`). Different run, different question count, different purpose (retrieval
  A/B, not answer scoring). Do not conflate the two when writing up results.
- **Submission path:** `metrics_based_eval` (single-system scoring) is what we run and submit.
  `comparative_eval` (three-judge head-to-head between two systems) exists in the ERB repo
  (`src/scripts/answer_evaluation/comparative_eval.py`) but is **out of scope** here — we have one
  system's answers, not two to compare — unless the leaderboard maintainer specifically asks for a
  head-to-head.
- `scripts/bench_chain/erb500k_scored.py` (the script that *produced* our source JSONL) is untouched by
  this work. It shells out to `claude -p` for answer generation, judging, and audit — it is our own
  harness, not an OpenAI judge, and is not being repurposed as one.

## 3. Provenance record

Fill in the blanks marked `<fill after run>` once you've actually run the judge. Everything else below is
already verified as of this build. The converter also writes a machine-readable copy of the top half of
this table into `<output>.provenance.json` (see §5) — treat that JSON as the source of truth if this
table and the JSON ever disagree.

| Field | Value |
|---|---|
| ERB checkout | `F:/Projects/EnterpriseRAG-Bench-main` — **not a git repository** (`git rev-parse HEAD` fails with "not a git repository"). Folder name `EnterpriseRAG-Bench-main` matches GitHub's "Download ZIP of default branch" convention. |
| Upstream repo | `https://github.com/onyx-dot-app/EnterpriseRAG-Bench` (confirmed via the MIT license badge link in the checkout's own `README.md`; also the org that hosts the HF leaderboard space `onyx-dot-app/EnterpriseRAG-Bench-Leaderboard`) |
| Upstream commit SHA | **Unrecoverable from this checkout** (no `.git`, no version pin in `pyproject.toml`). Before the next submission, do a fresh `git clone https://github.com/onyx-dot-app/EnterpriseRAG-Bench` alongside this checkout and diff the two trees; if identical, record that clone's `git rev-parse HEAD` as the pinned SHA here. |
| `questions.jsonl` sha256 | `f9524b9157cd43aae36b99333a124738804306ea6d07f332d49faa6d3d147905` (500 questions, computed 2026-07-12) |
| Our source file sha256 | `b1cf42efbcb19cf9f463f80a272944822abbc0e0361926934c904313020437ce` (`erb500k_blob_additive_scored.jsonl`, 500 rows — also written into the sidecar JSON on every converter run) |
| `LLM_PROVIDER` | `openai` |
| `LLM_MODEL_NAME` | `gpt-5.4-2026-03-05` — **pinned dated snapshot, not the floating `gpt-5.4` alias** (see §4) |
| `CHEAP_LLM_MODEL_NAME` | Unused for this submission (see §7 — the only call-sites that execute under `--no-correction` with no `document_ids` all resolve through `get_llm()`, which defaults to `LLM_MODEL_NAME`; the cheap-model path is only touched by the document-correction flow we're skipping) |
| Reasoning effort | `medium` — verified: `src/llm/factory.py:get_llm()` default parameter `reasoning_level: ReasoningLevel = "medium"`, never overridden by any call-site that fires in our run |
| Max LLM retries | `3` — verified: `_MAX_LLM_RETRIES = 3` in both `metrics_based_eval.py` and `eval_utils.py` |
| Parallelism used | `<fill after run>` (recommended: 2 for smoke, 4 for full — see §6) |
| Error / retry count | `<fill after run>` (watch stderr for `[WARN]` / retry-exhaustion lines during the run) |
| Total cost | `<fill after run>` — **the script tracks zero cost/token usage itself** (grepped for `cost` across `src/`; no hits in the eval path). Read it off the OpenAI usage dashboard after each run. |

## 4. Setup

**Dependency check (verified 2026-07-12):** `python -c "import openai"` **succeeds** — `openai==2.31.0` is
already importable in this environment. No `pip install` needed. (If it ever isn't:
`pip install -r F:/Projects/EnterpriseRAG-Bench-main/requirements.txt`.)

**Environment variables** (bash, from `F:/Projects/EnterpriseRAG-Bench-main`):

```bash
export LLM_PROVIDER=openai
export LLM_API_KEY="<paste your real key here — NOT OPENAI_API_KEY, the ERB scripts read LLM_API_KEY>"
export LLM_MODEL_NAME=gpt-5.4-2026-03-05
```

- **Never** write `LLM_API_KEY` into a file that gets committed, into this runbook, or into shell history
  you intend to share — set it as a shell env var for the session only.
- `LLM_MODEL_NAME` is pinned to the dated snapshot `gpt-5.4-2026-03-05` rather than the floating `gpt-5.4`
  alias, so the provenance record in §3 stays true even if the alias is repointed later.
- `CHEAP_LLM_MODEL_NAME` is left at its default (`gpt-5-mini`) — it's a no-op for this run (see §3/§7) but
  costs nothing to leave set.

## 5. The converter

`scripts/bench_chain/erb_official_answers_export.py` reads our scored JSONL and writes the official
`{question_id, answer}` (+ optional `document_ids`) format `metrics_based_eval` expects.

Already run once against the real data (2026-07-12) — outputs exist at:

- `benchmarks/results/erb500k_blob_official_answers.jsonl` (500 rows)
- `benchmarks/results/erb500k_blob_official_answers.provenance.json` (sidecar — export timestamp, source
  + questions-file sha256, verdict tallies, empty-answer row detail)

**`benchmarks/results/` is gitignored** (see repo `.gitignore`), so neither file is committed with this
PR. Regenerate them with:

```bash
python scripts/bench_chain/erb_official_answers_export.py \
  --input-file benchmarks/results/erb500k_blob_additive_scored.jsonl \
  --output-file benchmarks/results/erb500k_blob_official_answers.jsonl \
  --expect-rows 500
```

(Run from the `helix-context` repo root; `--questions-file` defaults to
`F:/Projects/EnterpriseRAG-Bench-main/questions.jsonl` and is cross-checked automatically.)

Validation the converter enforces (exits non-zero on any violation — this gates a real-money judge run):

- every `question_id` matches `^qst_\d+$`
- zero duplicate ids in the export
- every id is present in the official `questions.jsonl` (sha256 of that file recorded in the sidecar)
- `--expect-rows N` asserts an exact row count if passed

Flags of note: `--exclude-abstained` (diagnostic variant, drops our old-judge `ABSTAINED` rows instead of
including them — default OFF, leaderboard honesty requires judging everything); `--keep-empty` (diagnostic
only — see §11); `--document-ids-jsonl <path>` (merge hook for a future retrieval recapture — see §12).

**Verified output (this run):** 500 rows in, 500 rows out. Old-judge verdict tallies:
`{"CORRECT": 236, "ABSTAINED": 143, "INCORRECT": 121}`.

## 6. Exact commands

All from `F:/Projects/EnterpriseRAG-Bench-main`, after exporting the env vars in §4. `ANSWERS` is the
absolute path to the converter's output from §5.

```bash
cd F:/Projects/EnterpriseRAG-Bench-main
ANSWERS="F:/Projects/helix-context/benchmarks/results/erb500k_blob_official_answers.jsonl"

# --- 1) Build a 5-row smoke slice --------------------------------------------
# NOTE: --limit truncates the REMAINING (not-yet-judged) rows from the FULL
# answers-file, not "the first 5 rows, period" — re-running the full file with
# the same --limit 5 --resume would judge the NEXT 5 unjudged questions, not
# confirm a no-op (verified by reading main()'s resume logic: new_qids is
# computed against the full valid_rows set before --limit is applied). To
# actually prove --resume is a true no-op, judge a standalone 5-row file
# instead of using --limit against the 500-row file:
python -c "
import itertools
with open(r'$ANSWERS', encoding='utf-8') as f_in, \
     open('answer_evaluation/smoke5.jsonl', 'w', encoding='utf-8') as f_out:
    for line in itertools.islice(f_in, 5):
        f_out.write(line)
"

# --- 2) Smoke: judge those 5 rows --------------------------------------------
python -m src.scripts.answer_evaluation.metrics_based_eval \
  --answers-file answer_evaluation/smoke5.jsonl \
  --results-file answer_evaluation/results-helix-blob829k-smoke.json \
  --no-correction --parallelism 2

# --- 3) Resume check: re-run the IDENTICAL command ---------------------------
# Expect: "Found 5 already-evaluated questions" / "0 new questions to evaluate"
# / "All questions already evaluated, nothing to do." and exit 0. Confirm on
# the OpenAI usage dashboard that request count did NOT increase from step 2.
python -m src.scripts.answer_evaluation.metrics_based_eval \
  --answers-file answer_evaluation/smoke5.jsonl \
  --results-file answer_evaluation/results-helix-blob829k-smoke.json \
  --no-correction --parallelism 2 --resume

# --- 4) Full 500-question run (distinct results file; --resume so a Ctrl+C
#        mid-run is safe to restart without re-billing completed questions) --
python -m src.scripts.answer_evaluation.metrics_based_eval \
  --answers-file "$ANSWERS" \
  --results-file answer_evaluation/results-helix-blob829k-full.json \
  --no-correction --resume --parallelism 4
```

Before step 4, sanity-check the smoke result's `aggregate_stats` in
`answer_evaluation/results-helix-blob829k-smoke.json` (correctness/completeness numbers should look
plausible — not all-zero, not all-error) and check the OpenAI dashboard cost for 5 questions before
extrapolating ×100 for the full run's budget.

## 7. `--no-correction` rationale (verified by reading `metrics_based_eval.py`)

Two independent things are true, and together they're why `--no-correction` is used for every command
above even though it isn't strictly forced by the data:

1. **The document-correction flow already can't fire without `document_ids`.** `process_question_docs`
   (the function that runs the three-judge document-consensus flow and can rewrite gold answers/facts) is
   only called when `not args.no_correction and row.get("document_ids") and has_expected_docs` (line ~816).
   Our export never sets `document_ids` (see §12), so this is skipped regardless of the flag.
2. **`--no-correction` does more than that gate implies.** Without it, `main()` unconditionally calls
   `resolve_document_path_map()` → `load_or_build_uuid_index()` (loading/validating the **511,958-entry**
   `generated_data/uuid_index.json`) even when nothing will end up using the resulting map, and at the end
   unconditionally rewrites `questions_updated.jsonl` (unchanged, since nothing was corrected, but still an
   I/O pass over all 500 questions). Worse: `resolve_document_path_map` can raise `MissingDocumentIdsError`
   and prompt an **interactive** `confirm_yes_no()` ("Regenerate the cache now?") if the UUID index cache
   looks stale — which would hang a non-interactive/backgrounded run with no one at the keyboard.

So: no gold-set mutation risk either way, but `--no-correction` is the only way to guarantee the run can't
block on a terminal prompt, and it's faster (skips a 512K-entry index load) and skips a needless rewrite of
`questions_updated.jsonl`. This is also why we don't want the gold set mutated by our submission — passing
`--no-correction` makes that a structural guarantee, not just an emergent property of missing data.

## 8. What raw judge output is (and isn't) preserved

Verified by reading `eval_utils.py` and `metrics_based_eval.py` end to end — **no raw LLM completion text
is preserved anywhere in this pipeline**:

- `validate_single_fact()` (per-fact completeness check) returns a **bare bool** — `True` if `"yes"`
  appears in the first line of the model's response. The response text itself is discarded immediately
  after the regex check. Nothing about *why* a fact passed/failed survives.
- `evaluate_answer_correctness()` (wholistic correctness check) parses the model's JSON response for an
  `"aligned"` field and a `"reason"` field, then discards the raw response. Only the `reason` string
  (the model's own one-line self-report, stored as `correctness_reasoning` in the results file) survives —
  per question, not per fact.
- `strip_answer_citations()` keeps only the cleaned answer text (used downstream), not the model's raw
  completion.

**What IS preserved:** `answer_evaluation/results-*.json` → `questions[].correctness_reasoning` — one
short free-text string per question, nothing else textual from the judge. If you need to audit *why* a
specific completeness fact failed, that information does not exist after this run completes; the only
lever is `--question-id <qid>` to re-run a single question (which re-spends the LLM calls for it).

## 9. Cost estimate

**Call count (exact, computed from `questions.jsonl` + our export):**

| Call type | Count | Notes |
|---|---|---|
| Correctness (wholistic) | 500 | one per question with non-empty `gold_answer` (all 500) |
| Citation stripping | 500 | one per question with non-empty `answer` (the 5 empty-answer rows now carry the `(no answer produced)` sentinel — see §11 — so citation-stripping fires for all 500, not 495) |
| Completeness (per-fact) | 2,427 | `sum(len(answer_facts))` across all 500 questions in `questions.jsonl` (avg 4.854/question, min 1, max 46) |
| **Total** | **3,427** | |

All 3,427 calls resolve through `get_llm()` with no cheap-model override (verified by reading
`validate_single_fact`, `evaluate_answer_correctness`, and `strip_answer_citations` — the only three
call-sites that execute under `--no-correction`), so every one bills against the pinned
`LLM_MODEL_NAME=gpt-5.4-2026-03-05`, reasoning effort `medium`.

**Dollar range — ASSUMPTIONS, not verified pricing** (I have no confirmed current price sheet for
`gpt-5.4`; confirm at your OpenAI account's pricing page before committing spend):

- Assumed avg tokens/call: citation-strip ≈ 600 in / 200 out; correctness ≈ 900 in / 100 out;
  completeness ≈ 400 in / 15 out → totals ≈ **1.72M input / 0.19M output** tokens.
- Assumed price: **$3.00 / 1M input, $12.00 / 1M output** (flagship-reasoning-tier placeholder, not a
  verified `gpt-5.4` rate).
- Estimate: 1.72M × $3.00 + 0.19M × $12.00 ≈ **$5.16 + $2.24 ≈ $7.40**, call it a **$5–$15 range** to
  absorb template-length uncertainty and any hidden reasoning-token billing at `reasoning_level="medium"`
  (OpenAI reasoning models often bill invisible reasoning tokens as output — this estimate does not model
  that separately).
- **The script reports none of this.** Check the OpenAI usage dashboard after the 5-row smoke (§6 step 2)
  and multiply by ~100 (500/5) before committing to the full run — that's a real number, this section
  isn't.

## 10. Verdict-mapping note

The official `metrics_based_eval` binary correctness check has **no ABSTAINED bucket** — only aligned /
not-aligned. Our 143 `ABSTAINED` rows (28.6% of 500) will be scored as "not aligned" (incorrect) by the
official judge, since an abstention is not the gold answer. Combined with the 121 our own judge already
called `INCORRECT`, expect **official correctness ≈ ≤47%** as a ballpark (≤(500-143-121)/500 = ≤47.2%,
before accounting for the wholistic judge disagreeing with our own Sonnet judge in either direction on the
remaining 236). **Completeness is a metric we have never computed before** — this run will produce the
first real completeness numbers for this dataset, not just a re-grade of correctness.

## 11. The 5 answer-generation failures (sentinel disclosure)

5 of 500 rows have empty answer text (`answer.status == "exit_nonzero"` in our source — the answer
generation subprocess itself failed for these). The official judge's own row filter
(`not row.get("document_ids") and not row.get("answer")`) would **silently skip** a row with an
empty-string answer and no `document_ids`, shrinking the scored denominator from 500 to 495 — in our
favor, silently. That is not acceptable for a leaderboard submission, so **by default the converter
substitutes the literal sentinel `"(no answer produced)"`** for these 5 rows, keeping them inside the
judge's scored set (where they will almost certainly be marked not-aligned / 0% complete — the correct
outcome, since these are real failures of our system, not the judge's).

**Disclosure line for the submission writeup:** *"5/500 answer-generation failures included as incorrect
via sentinel — see `docs/benchmarks/2026-07-12-erb-official-rejudge-runbook.md` §11."*

| question_id | old-judge verdict | answer.status |
|---|---|---|
| qst_0113 | ABSTAINED | exit_nonzero |
| qst_0184 | ABSTAINED | exit_nonzero |
| qst_0194 | ABSTAINED | exit_nonzero |
| qst_0202 | ABSTAINED | exit_nonzero |
| qst_0282 | ABSTAINED | exit_nonzero |

All 5 were already classified `ABSTAINED` by our own trinary judge (an empty answer reads as an
abstention under our own protocol too) — consistent, not a surprise, but worth confirming explicitly
rather than assuming. `--keep-empty` exists on the converter to export the raw empty string instead, for
diagnostic comparisons only — do not use it for the submission run, it reintroduces the denominator gap
above.

## 12. Known gap: no `document_ids` → no doc-recall metric

Our source rows never persisted which documents were actually delivered for a given answer (telemetry
only, not exported). Without `document_ids`, `metrics_based_eval` cannot compute document recall or
invalid-extra-docs — this submission will carry correctness + completeness only.

**Investigated (not built) recovery path**, per a direct, brief, read-only query against
`F:/tmp/erb_blob.db` (single-shot, `mode=ro`, 0.05s, does not disturb the other agent using that DB):

- The `genes` table has a `source_id` column holding an **absolute local file path**
  (e.g. `F:\tmp\enterprise_rag_500k\sources\confluence\applied-ml-and-evals\eval-harness\....json`) — not
  a `dsid_...` identifier directly. There is no gene→dsid column.
- `F:/Projects/EnterpriseRAG-Bench-main/generated_data/uuid_index.json` is a **511,958-entry** flat dict,
  `dsid_... -> relative/path/under/sources/....json` (confirmed by direct inspection) — i.e. dsid → path,
  not path → dsid.
- **Recovery is possible but indirect:** invert `uuid_index.json` once (path → dsid, O(n) one-time cost),
  strip a gene's `source_id` down to its relative suffix under `.../sources/...` (same relative structure
  `erb500k_scored.py` already relies on for gold-dsid resolution), and look it up in the inverted index.

**Follow-up script sketch (NOT run — spec only):**

1. For each of the 500 questions, `POST /context/packet` (same call `erb500k_scored.py` already makes) and
   record the delivered `gene_id`s from the response's evidence lists.
2. For each delivered `gene_id`, `SELECT source_id FROM genes WHERE gene_id = ?` against `erb_blob.db`
   (read-only).
3. Normalize `source_id` to its relative path under `sources/...` and look it up in the inverted
   `uuid_index.json` to recover the `dsid`.
4. Write `{"question_id": ..., "document_ids": [...]}` JSONL and merge via the converter's
   `--document-ids-jsonl <path>` hook (already wired, unused today).

Cost: ~106s/packet × 500 ≈ **15 hours** on the blob corpus — this is why it's a follow-up, not part of
this environment build. The answer-only export in §5 is the immediate artifact; doc-recall waits on this
capture pass.

## 13. Leaderboard submission

Once the full run (§6 step 4) completes, email **joachim@onyx.app** with:

1. The results JSON (`answer_evaluation/results-helix-blob829k-full.json`).
2. The reproduction guide: `docs/benchmarks/2026-07-10-erb-blob-829k-reproduction.md`.
3. A hardware statement: Ryzen 7 5800X, 48GB DDR4, RTX 3080 Ti idle during retrieval, single 980 Pro NVMe;
   **zero LLM calls in retrieval** — all LLM spend in this submission is in the official judge itself, not
   in answer generation or context retrieval.

Open-source submissions require a reproduction guide (ERB `README.md:92-100`, "Leaderboard" section) —
the doc in point 2 satisfies that.
