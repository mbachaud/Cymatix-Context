"""Convert ERB questions.jsonl -> sweep_dense_additive_weight --queries JSON.

The sweep expects a JSON LIST of {"query": str, "gold_ids": [gene_id]} where
gold_ids are gene_ids IN THE TARGET GENOME. ERB ships JSONL with
expected_doc_ids (uuids) resolved to relative source paths via
generated_data/uuid_index.json. This adapter resolves uuid -> rel path ->
gene_id by suffix-matching the genome's source_id column (path-separator
tolerant). Questions with zero resolvable golds in the target genome are
dropped (correct per-bed subset for the 10K/50K fixtures) and counted.

Usage:
  python erb_to_sweep_queries.py --genome <db> --erb-root <dir> --out <json>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys


def norm(p: str) -> str:
    return p.replace("\\", "/").lower().lstrip("/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--genome", required=True)
    ap.add_argument("--erb-root", default=r"F:\Projects\EnterpriseRAG-Bench-main")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = all questions")
    args = ap.parse_args()

    root = args.erb_root.rstrip("\\/")
    # uuid -> relative source path (fixture-matrix doc: uuid_index.json lives
    # in generated_data/ root). Tolerate both root and generated_data layouts.
    idx = None
    for cand in (f"{root}\\generated_data\\uuid_index.json",
                 f"{root}\\uuid_index.json"):
        try:
            idx = json.load(open(cand, encoding="utf-8"))
            break
        except OSError:
            continue
    if idx is None:
        print("ERROR: uuid_index.json not found under", root, file=sys.stderr)
        return 2
    uuid_to_rel = {}
    for k, v in idx.items():
        if isinstance(v, str):
            uuid_to_rel[k] = norm(v)
        elif isinstance(v, dict):
            for key in ("path", "file", "relative_path", "source"):
                if isinstance(v.get(key), str):
                    uuid_to_rel[k] = norm(v[key])
                    break

    # Genome source_id -> gene_id (suffix match table).
    conn = sqlite3.connect(f"file:{args.genome}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT gene_id, source_id FROM genes WHERE source_id IS NOT NULL"
    ).fetchall()
    conn.close()
    by_src = [(norm(r["source_id"]), r["gene_id"]) for r in rows if r["source_id"]]

    def golds_for(rel: str) -> list[str]:
        out = []
        for src, gid in by_src:
            if src.endswith(rel):
                out.append(gid)
        return out

    qpath = None
    for cand in (f"{root}\\questions.jsonl",
                 f"{root}\\generated_data\\questions.jsonl"):
        try:
            open(cand, encoding="utf-8").close()
            qpath = cand
            break
        except OSError:
            continue
    if qpath is None:
        print("ERROR: questions.jsonl not found under", root, file=sys.stderr)
        return 2

    queries, dropped, total = [], 0, 0
    for line in open(qpath, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            q = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        text = q.get("question") or q.get("query") or ""
        gold_ids: list[str] = []
        for doc_id in q.get("expected_doc_ids") or []:
            rel = uuid_to_rel.get(str(doc_id))
            if rel:
                gold_ids.extend(golds_for(rel))
        gold_ids = sorted(set(gold_ids))
        if text and gold_ids:
            queries.append({"query": text, "gold_ids": gold_ids})
        else:
            dropped += 1
        if args.limit and len(queries) >= args.limit:
            break

    json.dump(queries, open(args.out, "w", encoding="utf-8"), indent=1)
    print(f"wrote {len(queries)} queries ({dropped} dropped of {total} total) "
          f"-> {args.out}")
    return 0 if queries else 1


if __name__ == "__main__":
    raise SystemExit(main())
