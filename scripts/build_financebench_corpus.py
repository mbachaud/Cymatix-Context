"""Extract FinanceBench PDFs + emit corpus and needle set.

Semantic-portfolio lane, queued third by user ordering (2026-08-31). Source:
github.com/patronus-ai/financebench — 150 open-source QA rows over 84 SEC
filings, with page-level evidence (``evidence_page_num``); the repo ships 368
filing PDFs, so the 284 unreferenced filings ride along as distractor mass.

Corpus: one text file per PDF page —
``<corpus-root>/<doc_name>/page_<n:04d>.txt`` with a ``[{doc_name} | page N]``
header. Page numbering = pypdf reader order, 0-indexed, matching
``evidence_page_num`` (the dataset's page numbers are 0-indexed reader pages;
the emitter VERIFIES this per needle by substring-matching a normalized slice
of ``evidence_text`` against the extracted gold page and records hits in the
meta — needles whose evidence text is not found on the claimed page are
flagged ``evidence_text_verified: false`` but kept, with the mismatch
ledgered).

Outputs (under --bench-dir):
  needles_financebench.json       {"needles": [{name, query, question_type}]}
  gold_paths_financebench.json    needle -> [corpus-relative page paths]
  needles_financebench_meta.json  per-needle verification + drop ledger

Usage:
  python scripts/build_financebench_corpus.py extract   # PDFs -> page texts
  python scripts/build_financebench_corpus.py emit      # needles + gold
  python scripts/build_financebench_corpus.py all
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("build_financebench_corpus")

REPO = Path(r"F:/Projects/financebench")
PDFS = REPO / "pdfs"
CORPUS = Path(r"F:/Projects/financebench/corpus")
QA = REPO / "data/financebench_open_source.jsonl"
BENCH = Path("benchmarks/dogfood/financebench")

MIN_BYTES = 50  # build_fixture_matrix ingest size gate (lower bound)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def extract() -> None:
    from pypdf import PdfReader

    CORPUS.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(PDFS.glob("*.pdf"))
    done = failed = 0
    for i, pdf in enumerate(pdfs):
        doc = pdf.stem
        outdir = CORPUS / doc
        if outdir.exists() and any(outdir.iterdir()):
            done += 1
            continue
        try:
            reader = PdfReader(str(pdf))
            outdir.mkdir(parents=True, exist_ok=True)
            for n, page in enumerate(reader.pages):
                try:
                    text = page.extract_text() or ""
                except Exception:  # noqa: BLE001 — single bad page, keep going
                    text = ""
                body = f"[{doc} | page {n}]\n{text.strip()}\n"
                data = body.encode("utf-8", errors="replace")
                if len(data) < MIN_BYTES:
                    body = f"[{doc} | page {n}]\n(blank page)\n"
                    data = body.encode("utf-8")
                (outdir / f"page_{n:04d}.txt").write_bytes(data)
            done += 1
        except Exception as exc:  # noqa: BLE001 — one corrupt PDF must not kill the run
            log.warning("FAILED %s: %s", doc, exc)
            failed += 1
        if (i + 1) % 25 == 0:
            log.info("extracted %d/%d PDFs (%d failed)", i + 1, len(pdfs), failed)
    log.info("extract done: %d ok, %d failed of %d", done, failed, len(pdfs))


def emit() -> None:
    BENCH.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(l) for l in QA.read_text(encoding="utf-8").splitlines() if l.strip()]
    needles, gold, meta = [], {}, {}
    drops = {"no_evidence": [], "gold_page_missing": []}
    verified = 0
    for r in rows:
        name = r["financebench_id"]
        ev = r.get("evidence") or []
        if not ev:
            drops["no_evidence"].append(name)
            continue
        rels, checks = [], []
        for e in ev:
            doc = e.get("doc_name") or r["doc_name"]
            pn = e.get("evidence_page_num")
            if pn is None:
                continue
            rel = f"{doc}/page_{int(pn):04d}.txt"
            page_file = CORPUS / rel
            if not page_file.exists():
                continue
            rels.append(rel)
            # verify a slice of the evidence text occurs on the claimed page
            page_text = norm(page_file.read_text(encoding="utf-8", errors="replace"))
            probe = norm(e.get("evidence_text") or "")[:120]
            checks.append(bool(probe) and probe[:60] in page_text)
        rels = sorted(set(rels))
        if not rels:
            drops["gold_page_missing"].append(name)
            continue
        ok = any(checks)
        verified += ok
        needles.append({
            "name": name,
            "query": r["question"],
            "question_type": f"financebench_{r.get('question_type', 'unknown')}",
        })
        gold[name] = rels
        meta[name] = {
            "company": r.get("company"),
            "doc_name": r.get("doc_name"),
            "answer": r.get("answer"),
            "evidence_text_verified": ok,
            "n_gold_pages": len(rels),
        }

    (BENCH / "needles_financebench.json").write_text(
        json.dumps({"needles": needles}, indent=1, ensure_ascii=False), encoding="utf-8")
    (BENCH / "gold_paths_financebench.json").write_text(
        json.dumps(gold, indent=1), encoding="utf-8")
    (BENCH / "needles_financebench_meta.json").write_text(
        json.dumps({
            "source": "github.com/patronus-ai/financebench open_source split (150 QA, 368 PDFs, 84 referenced)",
            "page_numbering": "pypdf reader order, 0-indexed, assumed == evidence_page_num; per-needle substring verification recorded below",
            "n_needles": len(needles),
            "n_evidence_text_verified": verified,
            "drops": drops,
            "per_needle": meta,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("needles: %d (evidence text verified on claimed page: %d); drops: %s",
             len(needles), verified, {k: len(v) for k, v in drops.items()})


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stage", choices=["extract", "emit", "all"])
    args = ap.parse_args()
    if args.stage in ("extract", "all"):
        extract()
    if args.stage in ("emit", "all"):
        emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
