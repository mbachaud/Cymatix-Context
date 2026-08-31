"""Emit the EnronQA corpus as ingestible files + a paired needle set.

Second-bench-corpus lane (user-approved 2026-08-28, ≤150 GB disk cap;
docs/research/2026-08-28-second-corpus-scouting.md). Source: HuggingFace
``MichaelR207/enron_qa_0922`` (CC BY 4.0; arXiv:2505.00263) — 73,772 unique
real Enron emails across 150 inboxes, each row carrying original questions,
``rephrased_questions`` paraphrase variants, and ``include_email`` flags
marking questions the paper's gate certified as unanswerable without the
email.

Outputs:
  <corpus-root>/<user>/<...>/<name>txt         one file per unique email
                                               (dataset paths end with '.',
                                               so path+'txt' yields clean
                                               Windows-safe '<n>.txt' names)
  <bench-dir>/needles_enronqa_pairs.json       PAIRED needle set: for each
                                               sampled (email, question idx),
                                               TWO needles sharing gold —
                                               question_type
                                               'enron_original' /
                                               'enron_rephrased'. The paired
                                               design is the point: same
                                               gold, two phrasings → a
                                               paraphrase-sensitivity delta
                                               ERB cannot measure.
  <bench-dir>/gold_paths_enronqa.json          needle_name -> [corpus file
                                               paths]; resolved to gene_ids
                                               AFTER ingest (gene_ids are
                                               content hashes — resolver
                                               joins on source_id).

Determinism: sampling is seeded; re-running with the same inputs emits
byte-identical needle files. In-script invariants assert email-content
consistency across duplicate paths and the ingest size gate (50..200,000
bytes, build_fixture_matrix.py).

Usage:
  python scripts/build_enronqa_corpus.py \
    --hf-dir F:/Projects/enronqa/hf_dataset \
    --corpus-root F:/Projects/enronqa/corpus \
    --bench-dir benchmarks/dogfood/enronqa \
    --n-pairs 250 --seed 20260828
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

log = logging.getLogger("build_enronqa_corpus")

SIZE_MIN, SIZE_MAX = 50, 200_000  # build_fixture_matrix ingest gate
_BAD_WIN = re.compile(r'[<>:"|?*\\]')


def _safe_rel(path: str) -> Path:
    """Dataset path -> corpus-relative .txt file path (Windows-safe)."""
    parts = [
        _BAD_WIN.sub("_", p).rstrip(" ")
        for p in path.split("/")
        if p not in ("", ".", "..")
    ]
    # Dataset paths end with '.' (e.g. 'phanis-s/sent_items/4.') so
    # appending 'txt' yields '4.txt'; otherwise add the extension.
    leaf = parts[-1]
    parts[-1] = leaf + "txt" if leaf.endswith(".") else leaf + ".txt"
    return Path(*parts)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--hf-dir", required=True)
    ap.add_argument("--corpus-root", required=True)
    ap.add_argument("--bench-dir", required=True)
    ap.add_argument("--n-pairs", type=int, default=250,
                    help="sampled (email, question) pairs; needles = 2x this")
    ap.add_argument("--seed", type=int, default=20260828)
    ap.add_argument("--needle-split", default="test",
                    help="parquet split needles are sampled from")
    ap.add_argument("--skip-emit", action="store_true",
                    help="corpus files already emitted — verify shas and "
                         "sample needles only")
    args = ap.parse_args()

    import pyarrow.parquet as pq

    t0 = time.time()
    files = sorted(Path(args.hf_dir).glob("data/*.parquet"))
    assert files, f"no parquet under {args.hf_dir}/data"

    corpus_root = Path(args.corpus_root)
    corpus_root.mkdir(parents=True, exist_ok=True)
    bench = Path(args.bench_dir)
    bench.mkdir(parents=True, exist_ok=True)

    # ── Pass 1: emit unique emails; verify duplicate-path consistency ──
    content_sha: dict[str, str] = {}
    n_written = n_dupe = 0
    size_viol = []
    needle_rows = []  # rows from the needle split only
    for f in files:
        split = f.name.split("-")[0]
        for batch in pq.ParquetFile(f).iter_batches(batch_size=20_000):
            for row in batch.to_pylist():
                path, email = row["path"], row["email"]
                sha = hashlib.sha256(email.encode("utf-8")).hexdigest()
                prev = content_sha.get(path)
                if prev is None:
                    content_sha[path] = sha
                    data = email.encode("utf-8")
                    if not (SIZE_MIN <= len(data) <= SIZE_MAX):
                        size_viol.append((path, len(data)))
                        continue
                    if not args.skip_emit:
                        dst = corpus_root / _safe_rel(path)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(data)
                    n_written += 1
                else:
                    n_dupe += 1
                    assert prev == sha, (
                        f"duplicate path {path!r} carries DIFFERENT email "
                        "content across splits — corpus identity broken"
                    )
                if split == args.needle_split:
                    needle_rows.append(row)

    log.info("emitted %d unique emails (%d dupes verified identical, "
             "%d size-gate exclusions) in %.1fs",
             n_written, n_dupe, len(size_viol), time.time() - t0)
    assert n_written > 70_000, f"suspiciously few emails: {n_written}"
    assert len(size_viol) < 0.02 * n_written, (
        f"size gate excluded {len(size_viol)} emails (>2%) — investigate"
    )

    # ── Pass 2: sample paired needles from the needle split ─────────────
    # Eligibility: a non-empty rephrased variant that differs from the
    # original, and an emitted gold file. NOTE the ``include_email`` column
    # is NOT an answerability marker — it is the paper's training-prompt
    # construction flag (whether the model saw the email inline) and is
    # all-zeros on the test split (measured 225/225); every released
    # question already passed the generation-time answerability gates
    # (arXiv:2505.00263 §3), so no per-question filter is needed.
    rng = random.Random(args.seed)
    by_user: dict[str, list] = defaultdict(list)
    for row in needle_rows:
        if row["path"] in {p for p, _ in size_viol}:
            continue
        qs, rqs = row["questions"], row["rephrased_questions"]
        elig = [
            i for i in range(min(len(qs), len(rqs)))
            if rqs[i] and rqs[i].strip()
            and rqs[i].strip() != qs[i].strip()
        ]
        if elig:
            by_user[row["user"]].append((row, elig))

    # Stratify: round-robin across the 150 inboxes so no user dominates.
    users = sorted(by_user)
    for u in users:
        rng.shuffle(by_user[u])
    picked = []
    while len(picked) < args.n_pairs and any(by_user[u] for u in users):
        for u in users:
            if len(picked) >= args.n_pairs:
                break
            if by_user[u]:
                row, elig = by_user[u].pop()
                picked.append((row, rng.choice(elig)))
    assert len(picked) == args.n_pairs, (
        f"only {len(picked)} eligible pairs found; lower --n-pairs"
    )

    needles, gold_paths, meta = [], {}, []
    for k, (row, qi) in enumerate(picked):
        rel = str(_safe_rel(row["path"])).replace("\\", "/")
        for variant, qtext in (("original", row["questions"][qi]),
                               ("rephrased", row["rephrased_questions"][qi])):
            name = f"enron_{k:03d}_{variant[0]}"
            needles.append({
                "name": name,
                "query": qtext.strip(),
                "question_type": f"enron_{variant}",
            })
            gold_paths[name] = [rel]
        meta.append({"pair": k, "path": rel, "user": row["user"],
                     "question_index": qi,
                     "gold_answer": (row["gold_answers"][qi]
                                     if qi < len(row["gold_answers"]) else None)})

    (bench / "needles_enronqa_pairs.json").write_text(
        json.dumps({"needles": needles,
                    "note": ("PAIRED design: enron_NNN_o (original) and "
                             "enron_NNN_r (rephrased) share identical gold; "
                             "the o-vs-r delta is the paraphrase-sensitivity "
                             "measure. Sampled from the "
                             f"'{args.needle_split}' split, include_email==1 "
                             f"only, seed {args.seed}, stratified round-robin "
                             "across inboxes.")},
                   indent=1) + "\n", encoding="utf-8")
    (bench / "gold_paths_enronqa.json").write_text(
        json.dumps(gold_paths, indent=1) + "\n", encoding="utf-8")
    (bench / "needles_enronqa_meta.json").write_text(
        json.dumps({"pairs": meta, "n_pairs": args.n_pairs,
                    "seed": args.seed, "split": args.needle_split,
                    "corpus_root": str(corpus_root),
                    "unique_emails": n_written,
                    "size_gate_excluded": size_viol,
                    "source": "hf:MichaelR207/enron_qa_0922 (CC BY 4.0)"},
                   indent=1) + "\n", encoding="utf-8")
    log.info("wrote %d needles (%d pairs) + gold paths + meta under %s",
             len(needles), args.n_pairs, bench)
    return 0


if __name__ == "__main__":
    sys.exit(main())
