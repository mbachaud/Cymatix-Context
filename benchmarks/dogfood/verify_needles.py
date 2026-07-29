"""verify_needles.py — derive and verify gold_source for the dogfood needles.

Hand-written gold paths rot silently: a file moves, the needle keeps pointing
at the old path, and the harness quietly scores a miss as a retrieval failure
rather than a bookkeeping failure. So gold is **derived**, not declared.

For each needle this scans its allowed repos under the same curation rules the
bed was built with (``scripts/build_dogfood_genome.py``) and collects every
file literally containing the anchor string. That set becomes ``gold_source``,
written out as ``needles_resolved.json``.

Two things make a needle fail, and they are different failures:

* **UNRESOLVED** — the anchor appears in no file. The expected value is wrong
  or stale; the needle is unanswerable and must not ship.
* **TOO_BROAD** — the anchor appears in more files than ``--max-gold``. The
  value is not distinctive (matching half the corpus makes "did retrieval find
  gold?" meaningless), so the needle needs a tighter anchor.

Both are hard failures. A needle set that cannot pass this is not a
measurement instrument.

Usage::

    python benchmarks/dogfood/verify_needles.py
    python benchmarks/dogfood/verify_needles.py --max-gold 12 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from build_dogfood_genome import (  # noqa: E402
    PROJECTS_ROOT, REPOS, collect,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from needles_dogfood import NEEDLES  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def build_index() -> dict[str, list[tuple[str, str]]]:
    """Map repo label -> [(``<repo>/<relpath>``, text), ...] over curated files."""
    index: dict[str, list[tuple[str, str]]] = {}
    for spec in REPOS:
        label, rd = spec["label"], PROJECTS_ROOT / spec["dir"]
        if not rd.is_dir():
            continue
        entries: list[tuple[str, str]] = []
        for f in collect(rd):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            entries.append((f"{label}/{f.relative_to(rd).as_posix()}", text))
        index[label] = entries
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-gold", type=int, default=15,
                    help="fail a needle whose anchor matches more files than this")
    ap.add_argument("--out", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args()

    index = build_index()
    missing_repos = [s["label"] for s in REPOS if s["label"] not in index]
    if missing_repos:
        print(f"ERROR: repos not on disk: {', '.join(missing_repos)}", file=sys.stderr)
        return 2

    resolved: list[dict] = []
    failures: list[tuple[str, str, int]] = []

    for nd in NEEDLES:
        anchor = nd.get("anchor", nd["expected"])
        hits: list[str] = []
        for label in nd["repos"]:
            for relpath, text in index.get(label, []):
                if anchor in text:
                    hits.append(relpath)
        hits.sort()

        # Self-reference guard: the harness states every anchor literally, so
        # needles_dogfood.py and needles_resolved.json match their own needles
        # and would be accepted as gold. Scoring a retrieval against the file
        # that defines the answer measures nothing.
        harness = [h for h in hits if "/dogfood/" in h or "build_dogfood" in h]
        if harness:
            hits = [h for h in hits if h not in harness]

        status = "ok"
        if not hits:
            status = "UNRESOLVED"
            failures.append((nd["name"], status, 0))
        elif len(hits) > args.max_gold:
            status = "TOO_BROAD"
            failures.append((nd["name"], status, len(hits)))

        resolved.append({
            "name": nd["name"],
            "query": nd["query"],
            "expected": nd["expected"],
            "accept": nd["accept"],
            "anchor": anchor,
            "repos": nd["repos"],
            "gold_source": hits,
            "gold_count": len(hits),
            "harness_excluded": harness,
            "status": status,
        })

    ok = [r for r in resolved if r["status"] == "ok"]
    print(f"needles: {len(resolved)}   resolved: {len(ok)}   failing: {len(failures)}")
    print(f"corpus:  {sum(len(v) for v in index.values())} curated files "
          f"across {len(index)} repos\n")

    for r in resolved:
        mark = "  " if r["status"] == "ok" else "!!"
        print(f"{mark} {r['name']:38s} gold={r['gold_count']:3d}  {r['status']}")

    if failures:
        print("\nFAILURES:", file=sys.stderr)
        for name, status, n in failures:
            hint = ("anchor matches nothing — expected value is wrong or stale"
                    if status == "UNRESOLVED"
                    else f"anchor matches {n} files — not distinctive, tighten it")
            print(f"  {name}: {status} — {hint}", file=sys.stderr)

    gold_files = sorted({g for r in ok for g in r["gold_source"]})
    payload = {
        "max_gold": args.max_gold,
        "needles_total": len(resolved),
        "needles_ok": len(ok),
        "distinct_gold_files": len(gold_files),
        "gold_files": gold_files,
        "needles": resolved,
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\ndistinct gold files: {len(gold_files)}")
    print(f"wrote {out}")

    if args.json:
        print(json.dumps({k: payload[k] for k in
                          ("needles_total", "needles_ok", "distinct_gold_files")}))

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
