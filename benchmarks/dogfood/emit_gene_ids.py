"""emit_gene_ids.py — identity and gold gene_id lists for the dogfood bed.

A gene_id list is how two machines agree on what a bed contains without
shipping the bed. The 2026-07-27 SIKE decontamination proved the pattern
works: gene_ids survive deletion unchanged, so a list computed on one host
selects exactly the right rows on another, and the delete count is itself the
verification.

Three artifacts, each answering a different question:

* ``identity`` — every gene_id in the bed, sorted, with a rollup sha256.
  Two beds with the same digest are the same corpus. Cheap to exchange
  (~17 bytes per gene) and settles "are we measuring the same thing?"
  before anyone compares numbers.
* ``by_repo`` — gene_ids grouped by owning repo, so a partial or
  single-repo bed can be checked without the whole list.
* ``gold`` — the gene_ids whose ``source_id`` is a gold file for some needle.
  This is the one a harness needs: it turns "was gold delivered?" into a set
  membership test against delivered gene_ids, with no path normalisation and
  no substring matching between two machines' directory layouts.

Usage::

    python benchmarks/dogfood/emit_gene_ids.py
    python benchmarks/dogfood/emit_gene_ids.py --genome genomes/dogfood/genome.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from build_dogfood_genome import PROJECTS_ROOT, REPOS  # noqa: E402

LABELS = [s["label"] for s in REPOS]


def digest(ids: list[str]) -> str:
    return hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest()


def norm(path: str) -> str:
    return (path or "").replace("\\", "/")


def repo_of(source_id: str) -> str | None:
    """Return the owning repo label for an absolute source_id, or None."""
    p = norm(source_id)
    root = norm(str(PROJECTS_ROOT)).rstrip("/")
    if not p.lower().startswith(root.lower() + "/"):
        return None
    rest = p[len(root) + 1:]
    head = rest.split("/", 1)[0]
    for spec in REPOS:
        if head.lower() == spec["dir"].lower():
            return spec["label"]
    return None


def rel_of(source_id: str, label: str) -> str:
    """``<repo>/<path>`` form of an absolute source_id."""
    p = norm(source_id)
    root = norm(str(PROJECTS_ROOT)).rstrip("/")
    rest = p[len(root) + 1:]
    _, _, sub = rest.partition("/")
    return f"{label}/{sub}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genome", default="genomes/dogfood/genome.db")
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--out-dir", default="benchmarks/dogfood/gene_ids")
    args = ap.parse_args()

    gpath = REPO_ROOT / args.genome
    if not gpath.exists():
        print(f"ERROR: {gpath} not found — build the bed first", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{gpath}?mode=ro", uri=True)
    rows = conn.execute("SELECT gene_id, source_id FROM genes").fetchall()
    conn.close()

    all_ids = sorted(r[0] for r in rows)
    by_repo: dict[str, list[str]] = {label: [] for label in LABELS}
    unattributed: list[str] = []
    gene_to_rel: dict[str, str] = {}

    for gid, sid in rows:
        label = repo_of(sid)
        if label is None:
            unattributed.append(gid)
            continue
        by_repo[label].append(gid)
        gene_to_rel[gid] = rel_of(sid, label)
    for label in by_repo:
        by_repo[label].sort()

    out = REPO_ROOT / args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    # ── identity ───────────────────────────────────────────────────────
    (out / "identity.txt").write_text("\n".join(all_ids) + "\n", encoding="utf-8")

    # ── per repo ───────────────────────────────────────────────────────
    for label, ids in by_repo.items():
        (out / f"by_repo_{label}.txt").write_text(
            ("\n".join(ids) + "\n") if ids else "", encoding="utf-8")

    # ── gold ───────────────────────────────────────────────────────────
    gold_ids: list[str] = []
    gold_by_needle: dict[str, list[str]] = {}
    resolved_path = REPO_ROOT / args.resolved
    if resolved_path.exists():
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
        gold_set = set(payload["gold_files"])
        rel_to_genes: dict[str, list[str]] = {}
        for gid, rel in gene_to_rel.items():
            rel_to_genes.setdefault(rel, []).append(gid)
        gold_ids = sorted({g for rel in gold_set for g in rel_to_genes.get(rel, [])})
        for nd in payload["needles"]:
            ids = sorted({g for rel in nd["gold_source"]
                          for g in rel_to_genes.get(rel, [])})
            gold_by_needle[nd["name"]] = ids
        (out / "gold.txt").write_text(
            ("\n".join(gold_ids) + "\n") if gold_ids else "", encoding="utf-8")
        (out / "gold_by_needle.json").write_text(
            json.dumps(gold_by_needle, indent=2) + "\n", encoding="utf-8")

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "genome": str(gpath),
        "genes_total": len(all_ids),
        "identity_sha256": digest(all_ids),
        "by_repo": {k: {"genes": len(v), "sha256": digest(v)} for k, v in by_repo.items()},
        "unattributed": len(unattributed),
        "gold_genes": len(gold_ids),
        "gold_sha256": digest(gold_ids) if gold_ids else None,
        "needles_with_zero_gold_genes": sorted(
            n for n, v in gold_by_needle.items() if not v),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                      encoding="utf-8")

    print(f"genes total : {len(all_ids):,}")
    print(f"identity    : {summary['identity_sha256']}")
    for label in LABELS:
        info = summary["by_repo"][label]
        print(f"  {label:18s} {info['genes']:6,d}  {info['sha256'][:16]}…")
    if unattributed:
        print(f"  {'(unattributed)':18s} {len(unattributed):6,d}")
    print(f"gold genes  : {len(gold_ids):,}  {summary['gold_sha256']}")
    if summary["needles_with_zero_gold_genes"]:
        print("\nWARNING — needles whose gold files produced no genes "
              "(gold file was excluded from the bed, or not yet ingested):",
              file=sys.stderr)
        for n in summary["needles_with_zero_gold_genes"]:
            print(f"  {n}", file=sys.stderr)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
