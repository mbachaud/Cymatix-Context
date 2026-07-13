r"""erb_official_answers_export.py -- Convert our ERB blob scored run into the
OFFICIAL EnterpriseRAG-Bench answers-file format for re-judging with the
upstream judge (F:/Projects/EnterpriseRAG-Bench-main,
src.scripts.answer_evaluation.metrics_based_eval).

Background
----------
scripts/bench_chain/erb500k_scored.py runs our OWN trinary judge (Claude
Sonnet, CORRECT / INCORRECT / ABSTAINED) against the 500-question ERB set.
This script does NOT touch that pipeline (it shells out to `claude -p` and
is not an OpenAI judge -- see docs/benchmarks/2026-07-12-erb-official-
rejudge-runbook.md). Instead it reformats the *already-generated answers*
from one of its output files into the answers-file schema the official ERB
judge expects, so that judge can be run standalone against our answers with
no dependency on our own scoring:

    official row:  {"question_id": "qst_0001", "answer": "...", "document_ids": [...]}

Our source rows (one per question) carry (per scripts/bench_chain/
erb500k_scored.py's writer):
    id, type, question, gold_answer, expected_doc_ids, packet{...},
    answer{text, status, cost_usd, elapsed_s}, judge{verdict, ...}, audit, ts

`expected_doc_ids` on our rows is OUR harness's copy of the gold ids (for
our own scoring) -- it is NOT the set of documents actually delivered to
the model for this answer (that was never persisted; see the
--document-ids-jsonl hook below for the future recapture path). This
export is therefore answer-only: it drives the official judge's
correctness + completeness metrics, not its document-recall metric.

Leaderboard-honesty rule for answer-generation failures
--------------------------------------------------------
5 of our 500 rows have empty answer text (`answer.status == "exit_nonzero"`;
our own judge marked all 5 ABSTAINED). The official judge's own row filter
(`not row.get("document_ids") and not row.get("answer")`) would silently
SKIP a row with an empty-string answer and no document_ids -- which shrinks
the scored denominator from 500 to 495 in our favor. That is not
acceptable for a leaderboard submission: those are real failures of our
system and must be judged as such (almost certainly "not aligned" /
incorrect, with 0% completeness), keeping the denominator at the full row
count. So by DEFAULT this script substitutes the literal sentinel
"(no answer produced)" for empty answer text. Pass --keep-empty to opt out
(diagnostic use only) and emit the raw empty string instead.

Validation (fails loudly, exits non-zero -- this gates a real-money judge
run, silent leniency here is not acceptable):
  - every row's id matches ^qst_\\d+$
  - zero duplicate ids within the export
  - (unless --skip-questions-crosscheck) every id is present in the
    official questions.jsonl gold file; sha256 of that file is recorded
  - (optional) --expect-rows N asserts the row count matches exactly

Outputs:
  - <output-file>.jsonl                       -- the answers-file itself
  - <output-file-stem>.provenance.json        -- sidecar: export timestamp,
    source file + sha256, questions.jsonl path + sha256, verdict tallies,
    empty-answer rows (with old-judge verdict), sentinel-applied flag,
    document_ids-hook merge count.

Usage:
    python scripts/bench_chain/erb_official_answers_export.py \\
        --input-file benchmarks/results/erb500k_blob_additive_scored.jsonl \\
        --output-file benchmarks/results/erb500k_blob_official_answers.jsonl \\
        --expect-rows 500

Windows-safe, stdlib only (json, argparse, hashlib, re, sys, pathlib,
datetime, collections.Counter), explicit utf-8 encoding on every file open.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

QST_ID_RE = re.compile(r"^qst_\d+$")

SENTINEL_NO_ANSWER = "(no answer produced)"

# This machine's known-good locations (see docs/benchmarks/2026-07-12-erb-
# official-rejudge-runbook.md). Override with the matching --*-file flag if
# running elsewhere.
DEFAULT_INPUT_FILE = Path("benchmarks/results/erb500k_blob_additive_scored.jsonl")
DEFAULT_OUTPUT_FILE = Path("benchmarks/results/erb500k_blob_official_answers.jsonl")
DEFAULT_QUESTIONS_FILE = Path("F:/Projects/EnterpriseRAG-Bench-main/questions.jsonl")


def sha256_of_file(path: Path) -> str:
    """Stream a file through sha256 in 1 MiB chunks (safe for large files)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    """Load a JSONL file. Any malformed line is a hard failure (not skipped) --
    this feeds a validation-critical path, not a best-effort scan."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"[FATAL] {path}:{i}: invalid JSON: {exc}", file=sys.stderr)
                sys.exit(1)
    return rows


def load_document_ids_hook(path: Path | None) -> dict[str, list[str]]:
    """Optional merge hook: a JSONL file of {"question_id": ..., "document_ids": [...]}
    rows (the same shape a future retrieval-recapture pass would emit -- see
    the runbook's "Known gap: no document_ids" section) merged in by question_id.
    Absent by default since we have no delivered document_ids yet."""
    if path is None:
        return {}
    mapping: dict[str, list[str]] = {}
    for row in load_jsonl(path):
        qid = row.get("question_id")
        doc_ids = row.get("document_ids")
        if qid and doc_ids:
            mapping[qid] = doc_ids
    return mapping


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert our ERB blob scored run into the official "
            "EnterpriseRAG-Bench answers-file format."
        )
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        default=DEFAULT_INPUT_FILE,
        help=f"Our scored JSONL (default: {DEFAULT_INPUT_FILE})",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help=f"Official-format answers JSONL to write (default: {DEFAULT_OUTPUT_FILE})",
    )
    parser.add_argument(
        "--questions-file",
        type=Path,
        default=DEFAULT_QUESTIONS_FILE,
        help=(
            "Official questions.jsonl to cross-check ids against "
            f"(default: {DEFAULT_QUESTIONS_FILE})"
        ),
    )
    parser.add_argument(
        "--skip-questions-crosscheck",
        action="store_true",
        default=False,
        help=(
            "Skip cross-checking export ids against --questions-file. NOT "
            "recommended for a submission run; only for environments without "
            "a local ERB checkout."
        ),
    )
    parser.add_argument(
        "--exclude-abstained",
        action="store_true",
        default=False,
        help=(
            "Diagnostic variant: drop rows where our old judge.verdict == "
            "'ABSTAINED' instead of including them (default OFF -- leaderboard "
            "honesty requires judging all rows, abstentions included)."
        ),
    )
    parser.add_argument(
        "--keep-empty",
        action="store_true",
        default=False,
        help=(
            "Diagnostic use only: emit the raw empty string for answer-"
            "generation failures instead of the default sentinel "
            f"({SENTINEL_NO_ANSWER!r}). The default keeps these rows inside "
            "the judge's scored denominator instead of being silently "
            "skipped by its own row filter."
        ),
    )
    parser.add_argument(
        "--document-ids-jsonl",
        type=Path,
        default=None,
        help=(
            "Optional JSONL of {question_id, document_ids} rows to merge in "
            "(future retrieval-recapture output). Omitted by default."
        ),
    )
    parser.add_argument(
        "--expect-rows",
        type=int,
        default=None,
        help="If set, assert the input row count equals exactly this many.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if not args.input_file.exists():
        print(f"[FATAL] input file not found: {args.input_file}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading {args.input_file} ...")
    rows = load_jsonl(args.input_file)
    print(f"  {len(rows)} rows loaded")

    if args.expect_rows is not None and len(rows) != args.expect_rows:
        print(
            f"[FATAL] expected exactly {args.expect_rows} rows, found {len(rows)}",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- id validation: pattern + duplicates -------------------------------
    ids: list[str] = []
    verdict_tallies: Counter[str] = Counter()
    bad_id_rows: list[object] = []
    for row in rows:
        qid = row.get("id")
        ids.append(qid)
        verdict = (row.get("judge") or {}).get("verdict")
        verdict_tallies[str(verdict)] += 1
        if not qid or not QST_ID_RE.match(str(qid)):
            bad_id_rows.append(qid)

    if bad_id_rows:
        print(
            f"[FATAL] {len(bad_id_rows)} row(s) with malformed/missing id "
            f"(expected qst_<digits>): {bad_id_rows[:10]}",
            file=sys.stderr,
        )
        sys.exit(1)

    id_counts = Counter(ids)
    dup_ids = [qid for qid, n in id_counts.items() if n > 1]
    if dup_ids:
        print(
            f"[FATAL] {len(dup_ids)} duplicate question_id(s) in export source: "
            f"{dup_ids[:10]}",
            file=sys.stderr,
        )
        sys.exit(1)

    unique_ids = set(ids)
    print(f"  {len(unique_ids)} unique ids, 0 duplicates, all match ^qst_\\d+$")

    # --- cross-check against the official questions.jsonl ------------------
    questions_file_sha256 = None
    if args.skip_questions_crosscheck:
        print(
            "[WARN] --skip-questions-crosscheck set; NOT verifying ids against "
            "questions.jsonl",
            file=sys.stderr,
        )
    else:
        if not args.questions_file.exists():
            print(
                f"[FATAL] questions file not found: {args.questions_file}\n"
                "        Pass --questions-file <path>, or "
                "--skip-questions-crosscheck to bypass (not recommended for a "
                "submission run).",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Cross-checking ids against {args.questions_file} ...")
        questions_file_sha256 = sha256_of_file(args.questions_file)
        gold_ids: set[str] = set()
        for row in load_jsonl(args.questions_file):
            gqid = row.get("question_id")
            if gqid:
                gold_ids.add(gqid)
        missing = unique_ids - gold_ids
        if missing:
            print(
                f"[FATAL] {len(missing)} id(s) not found in {args.questions_file}: "
                f"{sorted(missing)[:10]}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"  OK: all {len(unique_ids)} ids present in {len(gold_ids)}-question "
            f"gold file (sha256={questions_file_sha256})"
        )

    # --- optional document_ids merge hook -----------------------------------
    doc_id_map = load_document_ids_hook(args.document_ids_jsonl)
    if args.document_ids_jsonl is not None:
        print(
            f"  Merged document_ids for {len(doc_id_map)} question(s) from "
            f"{args.document_ids_jsonl}"
        )

    # --- build output rows ---------------------------------------------------
    out_rows: list[dict] = []
    empty_answer_rows: list[dict] = []
    excluded_abstained_ids: list[str] = []
    for row in rows:
        qid = row["id"]
        verdict = (row.get("judge") or {}).get("verdict")
        if args.exclude_abstained and verdict == "ABSTAINED":
            excluded_abstained_ids.append(qid)
            continue

        answer_block = row.get("answer") or {}
        raw_answer_text = answer_block.get("text") or ""
        is_empty = not raw_answer_text.strip()

        if is_empty:
            empty_answer_rows.append(
                {
                    "question_id": qid,
                    "old_judge_verdict": verdict,
                    "answer_status": answer_block.get("status"),
                }
            )
            if args.keep_empty:
                answer_text = raw_answer_text
                print(
                    f"[WARN] {qid}: empty answer text (status="
                    f"{answer_block.get('status')!r}); --keep-empty set, "
                    "emitting raw empty string (diagnostic only -- this row "
                    "will be silently SKIPPED by the official judge's own row "
                    "filter, shrinking the scored denominator)",
                    file=sys.stderr,
                )
            else:
                answer_text = SENTINEL_NO_ANSWER
                print(
                    f"[WARN] {qid}: empty answer text (status="
                    f"{answer_block.get('status')!r}); substituting sentinel "
                    f"{SENTINEL_NO_ANSWER!r} so this row is judged as a real "
                    f"failure (denominator stays {len(rows)}, not silently "
                    "skipped)",
                    file=sys.stderr,
                )
        else:
            answer_text = raw_answer_text

        out_row: dict = {"question_id": qid, "answer": answer_text}
        if qid in doc_id_map:
            out_row["document_ids"] = doc_id_map[qid]
        out_rows.append(out_row)

    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_file, "w", encoding="utf-8", newline="\n") as f:
        for out_row in out_rows:
            f.write(json.dumps(out_row, ensure_ascii=False))
            f.write("\n")

    # --- provenance sidecar ---------------------------------------------------
    source_sha256 = sha256_of_file(args.input_file)
    provenance = {
        "export_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "source_file": str(args.input_file),
        "source_file_sha256": source_sha256,
        "questions_file": (
            None if args.skip_questions_crosscheck else str(args.questions_file)
        ),
        "questions_file_sha256": questions_file_sha256,
        "row_count_in": len(rows),
        "row_count_out": len(out_rows),
        "verdict_tallies_source": dict(verdict_tallies),
        "exclude_abstained": args.exclude_abstained,
        "excluded_abstained_ids": excluded_abstained_ids,
        "empty_answer_count": len(empty_answer_rows),
        "empty_answer_sentinel_applied": not args.keep_empty,
        "empty_answer_sentinel_text": (
            None if args.keep_empty else SENTINEL_NO_ANSWER
        ),
        "empty_answer_rows": empty_answer_rows,
        "document_ids_hook_file": (
            str(args.document_ids_jsonl) if args.document_ids_jsonl else None
        ),
        "document_ids_merged_count": len(doc_id_map),
        "output_file": str(args.output_file),
    }
    provenance_path = args.output_file.parent / (
        args.output_file.stem + ".provenance.json"
    )
    with open(provenance_path, "w", encoding="utf-8") as f:
        json.dump(provenance, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # --- summary ----------------------------------------------------------
    print()
    print("=== Summary ===")
    print(f"  Input:                {args.input_file}")
    print(f"    sha256:             {source_sha256}")
    print(f"  Rows read:            {len(rows)}")
    print(f"  Rows written:         {len(out_rows)}")
    if args.exclude_abstained:
        print(f"  Excluded (ABSTAINED): {len(excluded_abstained_ids)}")
    empty_desc = (
        "RAW EMPTY (--keep-empty)"
        if args.keep_empty
        else f"substituted with {SENTINEL_NO_ANSWER!r}"
    )
    print(
        f"  Empty answers:        {len(empty_answer_rows)} -> {empty_desc} "
        f"{[r['question_id'] for r in empty_answer_rows]}"
    )
    print(f"  Old-judge verdict tallies (source): {dict(verdict_tallies)}")
    print(f"  Output:               {args.output_file}")
    print(f"  Provenance sidecar:   {provenance_path}")


if __name__ == "__main__":
    main()
