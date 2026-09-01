"""Emit the raw-Enron distractor padding for the padded EnronQA bed.

Companion to ``scripts/build_enronqa_corpus.py``.  The padded bed (scouting
doc ``docs/research/2026-08-28-second-corpus-scouting.md``) surrounds the
73,772-email QA corpus with the remaining raw Enron maildir dump (CMU
``enron_mail_20150507.tar.gz``, 517,401 messages, public record) as
distractors, without touching gold:

  * Any message whose maildir-relative path appears in the QA dataset's
    ``path`` column is EXCLUDED — the QA corpus already carries that email
    (cleaned form), so gold is never double-represented by path.
  * Everything else is emitted raw (full RFC822 headers kept — realistic
    distractor pressure, no artificial normalization), streamed straight
    from the tarball in one pass, size-gated 50..200,000 bytes to match
    the fixture walker.

Known measurement caveat (recorded here so the readout checks it, receipt
field ``gold_twin_caveat``): the raw dump duplicates messages across
folders (all_documents/ mirrors etc.), so padding may contain RAW near-
copies of gold emails under paths the QA dataset did not claim.  Those
carry the answer but are not gold gene_ids — retrieval that surfaces one
scores as a miss.  Both variants of a pair share gold, so the
original-vs-rephrased paraphrase delta is unaffected; absolute recall on
the padded bed is a lower bound.  Quantify at readout (audit missed
needles for delivered answer-carrying twins) instead of pre-cleaning.

Usage:
  python scripts/build_enron_padding.py \
    --tarball F:/Projects/enronqa/enron_mail_20150507.tar.gz \
    --hf-dir F:/Projects/enronqa/hf_dataset \
    --padding-root F:/Projects/enronqa/padding \
    --receipt benchmarks/dogfood/enronqa/padding_emit_receipt.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("build_enron_padding")

SIZE_MIN, SIZE_MAX = 50, 200_000  # matches build_fixture_matrix walker gate
_BAD_WIN = re.compile(r'[<>:"|?*\\]')


def _safe_rel(path: str) -> Path:
    """Maildir-relative path -> padding-relative .txt path (Windows-safe).

    Same transform as build_enronqa_corpus.py so the two roots use one
    naming convention (raw paths also end with '.': 'user/folder/4.').
    """
    parts = [
        _BAD_WIN.sub("_", p).rstrip(" ")
        for p in path.split("/")
        if p not in ("", ".", "..")
    ]
    leaf = parts[-1]
    parts[-1] = leaf + "txt" if leaf.endswith(".") else leaf + ".txt"
    return Path(*parts)


def _qa_paths(hf_dir: str) -> set[str]:
    """All maildir-relative paths the QA corpus already covers."""
    import pyarrow.parquet as pq

    paths: set[str] = set()
    files = sorted(Path(hf_dir).glob("data/*.parquet"))
    assert files, f"no parquet under {hf_dir}/data"
    for f in files:
        for batch in pq.ParquetFile(f).iter_batches(
            batch_size=50_000, columns=["path"]
        ):
            paths.update(batch.column("path").to_pylist())
    return paths


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tarball", required=True)
    ap.add_argument("--hf-dir", required=True)
    ap.add_argument("--padding-root", required=True)
    ap.add_argument("--receipt", required=True)
    args = ap.parse_args()

    t0 = time.time()
    qa_paths = _qa_paths(args.hf_dir)
    log.info("QA path exclusion set: %d entries", len(qa_paths))
    assert len(qa_paths) > 70_000, "QA path set suspiciously small"

    root = Path(args.padding_root)
    root.mkdir(parents=True, exist_ok=True)

    sha = hashlib.sha256()
    with open(args.tarball, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            sha.update(block)
    tar_sha = sha.hexdigest()
    log.info("tarball sha256=%s", tar_sha)

    n_members = n_qa_excl = n_size_excl = n_emitted = n_name_coll = 0
    emitted: set[str] = set()
    with tarfile.open(args.tarball, "r:gz") as tf:
        for m in tf:
            if not m.isfile():
                continue
            n_members += 1
            # tar member: 'maildir/<user>/<folder>/<n>.' -> QA-relative path
            rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
            if rel in qa_paths:
                n_qa_excl += 1
                continue
            if not (SIZE_MIN <= m.size <= SIZE_MAX):
                n_size_excl += 1
                continue
            dst = root / _safe_rel(rel)
            key = str(dst).lower()  # NTFS is case-insensitive
            if key in emitted:
                n_name_coll += 1
                continue
            emitted.add(key)
            dst.parent.mkdir(parents=True, exist_ok=True)
            fobj = tf.extractfile(m)
            assert fobj is not None, f"unreadable member {m.name}"
            dst.write_bytes(fobj.read())
            n_emitted += 1
            if n_emitted % 50_000 == 0:
                log.info("  %d emitted (%.0f/s)", n_emitted,
                         n_emitted / (time.time() - t0))

    elapsed = round(time.time() - t0, 1)
    log.info("DONE in %.1fs: members=%d qa_excluded=%d size_excluded=%d "
             "name_collisions=%d emitted=%d",
             elapsed, n_members, n_qa_excl, n_size_excl, n_name_coll,
             n_emitted)
    assert n_members > 500_000, f"suspiciously few tar members: {n_members}"
    assert n_qa_excl > 60_000, (
        f"only {n_qa_excl} QA exclusions — path-format mismatch between "
        "tar member names and the dataset path column?"
    )

    receipt = {
        "tool": "build_enron_padding",
        "stamp": datetime.now(timezone.utc).isoformat(),
        "tarball": args.tarball,
        "tarball_sha256": tar_sha,
        "padding_root": str(root),
        "members_total": n_members,
        "excluded_qa_paths": n_qa_excl,
        "excluded_size_gate": n_size_excl,
        "name_collisions_skipped": n_name_coll,
        "emitted": n_emitted,
        "elapsed_s": elapsed,
        "gold_twin_caveat": (
            "raw dump duplicates messages across folders; padding may hold "
            "raw near-copies of gold emails under unclaimed paths — "
            "answer-carrying non-gold. Paraphrase delta unaffected (both "
            "variants share gold); absolute recall is a lower bound. Audit "
            "missed needles at readout."
        ),
    }
    out = Path(args.receipt)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=1) + "\n", encoding="utf-8")
    log.info("receipt -> %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
