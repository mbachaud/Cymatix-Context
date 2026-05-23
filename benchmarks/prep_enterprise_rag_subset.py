r"""Build a subset of the EnterpriseRAG-Bench corpus via hardlinks.

Hardlinks N files per source (proportional to source size) into a target
directory, mirroring the source/<src>/<path>/<file>.json structure.
Deterministic (alphabetical sort).

Goal: avoid copying 500K files; hardlinks are O(1) and share inode.

Usage:
  python benchmarks/prep_enterprise_rag_subset.py --size 10000 \
      --out F:/tmp/enterprise_rag_10k

  python benchmarks/prep_enterprise_rag_subset.py --size 50000 \
      --out F:/tmp/enterprise_rag_50k

The output dir gets a ``sources/`` subdir + a ``manifest.json`` recording
the file count per source + gold-doc coverage of the 500 questions.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path


REPO = Path(r"F:/Projects/EnterpriseRAG-Bench-main")
CORPUS_ROOT = REPO / "generated_data" / "sources"
UUID_INDEX = REPO / "generated_data" / "uuid_index.json"
QUESTIONS = REPO / "questions.jsonl"

# Actual file counts (verified by find):
SOURCE_COUNTS = {
    "confluence": 5190,
    "fireflies": 10174,
    "github": 8053,
    "gmail": 121391,
    "google_drive": 25108,
    "hubspot": 15018,
    "jira": 6121,
    "linear": 35309,
    "slack": 285606,
}
TOTAL_FILES = sum(SOURCE_COUNTS.values())


def proportional_alloc(total_target: int) -> dict[str, int]:
    """Allocate `total_target` slots across sources proportionally,
    with each source guaranteed at least 1 slot (round-up for small)."""
    alloc = {}
    for src, count in SOURCE_COUNTS.items():
        share = max(1, round(total_target * count / TOTAL_FILES))
        alloc[src] = min(share, count)
    # Adjust to exact target by trimming/extending the largest sources
    diff = total_target - sum(alloc.values())
    if diff > 0:
        # Add to largest
        for src in sorted(alloc, key=lambda s: -SOURCE_COUNTS[s]):
            if alloc[src] < SOURCE_COUNTS[src]:
                alloc[src] += min(diff, SOURCE_COUNTS[src] - alloc[src])
                diff -= min(diff, SOURCE_COUNTS[src] - alloc[src])
                if diff <= 0: break
    elif diff < 0:
        # Trim from largest
        for src in sorted(alloc, key=lambda s: -alloc[s]):
            if alloc[src] > 1:
                trim = min(-diff, alloc[src] - 1)
                alloc[src] -= trim
                diff += trim
                if diff >= 0: break
    return alloc


def resolve_gold_paths() -> dict[str, set[Path]]:
    """Collect absolute paths for every expected_doc_id across all 500
    questions, grouped by source. These are MANDATORY in any subset to
    avoid artificially low M3 ceiling."""
    uuid_idx = json.loads(UUID_INDEX.read_text(encoding="utf-8"))
    questions = [json.loads(l) for l in QUESTIONS.open(encoding="utf-8")]
    by_src: dict[str, set[Path]] = {}
    for q in questions:
        for dsid in q.get("expected_doc_ids") or []:
            rel = uuid_idx.get(dsid)
            if not rel:
                continue
            rel_norm = rel.replace("\\", "/")
            src = rel_norm.split("/", 1)[0]
            fp = CORPUS_ROOT / rel_norm
            by_src.setdefault(src, set()).add(fp)
    return by_src


def collect_files_per_source(per_source_caps: dict[str, int],
                             mandatory_per_source: dict[str, set[Path]]
                             ) -> dict[str, list[Path]]:
    """Mandatory gold files always included; remaining slots filled
    alphabetically with non-mandatory files (deterministic)."""
    out = {}
    for src, cap in per_source_caps.items():
        mandatory = mandatory_per_source.get(src, set())
        # Keep all mandatory; if mandatory > cap, allow overshoot (correctness
        # > strict size budget).
        files_set = set(mandatory)
        if len(files_set) < cap:
            src_root = CORPUS_ROOT / src
            for fp in sorted(src_root.rglob("*.json")):
                if fp.name.lower() == "agents.md":
                    continue
                if fp in files_set:
                    continue
                files_set.add(fp)
                if len(files_set) >= cap:
                    break
        out[src] = sorted(files_set)
        print(f"  {src:<14} {len(out[src]):>6}/{cap} "
              f"(mandatory={len(mandatory)})")
    return out


def hardlink_to_target(file_map: dict[str, list[Path]], out_root: Path) -> int:
    """Hardlink each file from sources/ to out_root/sources/, mirroring rel path."""
    target_sources = out_root / "sources"
    if target_sources.exists():
        print(f"  removing existing {target_sources}")
        shutil.rmtree(target_sources)
    total = 0
    t0 = time.perf_counter()
    for src, files in file_map.items():
        for fp in files:
            rel = fp.relative_to(CORPUS_ROOT)
            tgt = target_sources / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(str(fp), str(tgt))
            except FileExistsError:
                pass
            except OSError as exc:
                # Cross-device or unsupported; fall back to copy
                print(f"    hardlink failed on {fp}, copying: {exc}")
                shutil.copy2(str(fp), str(tgt))
            total += 1
    elapsed = time.perf_counter() - t0
    print(f"  hardlinked {total} files in {elapsed:.1f}s "
          f"({total/max(elapsed,0.01):.0f} files/s)")
    return total


def gold_coverage(file_map: dict[str, list[Path]]) -> dict:
    """For each of 500 questions, how many gold docs are in the subset?"""
    uuid_idx = json.loads(UUID_INDEX.read_text(encoding="utf-8"))
    # Inverse: rel-path → dsid
    path_to_dsid = {v.replace("\\", "/"): k for k, v in uuid_idx.items()}
    # All files in subset, as rel-paths
    subset_rels = set()
    for files in file_map.values():
        for fp in files:
            rel = fp.relative_to(CORPUS_ROOT).as_posix()
            subset_rels.add(rel)
    # Map subset rel-paths back to dsids
    subset_dsids = {path_to_dsid[r] for r in subset_rels if r in path_to_dsid}

    questions = [json.loads(l) for l in QUESTIONS.open(encoding="utf-8")]
    coverage = {
        "n_questions": len(questions),
        "n_with_gold": 0,
        "n_fully_covered": 0,
        "n_partially_covered": 0,
        "n_uncovered": 0,
        "subset_dsids": len(subset_dsids),
    }
    for q in questions:
        gold = q.get("expected_doc_ids") or []
        if not gold:
            continue
        coverage["n_with_gold"] += 1
        in_subset = sum(1 for d in gold if d in subset_dsids)
        if in_subset == len(gold):
            coverage["n_fully_covered"] += 1
        elif in_subset > 0:
            coverage["n_partially_covered"] += 1
        else:
            coverage["n_uncovered"] += 1
    return coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", type=int, required=True,
                        help="Target total file count (proportional per source)")
    parser.add_argument("--out", type=Path, required=True,
                        help="Output directory (creates ./sources/ inside)")
    parser.add_argument("--skip-link", action="store_true",
                        help="Skip the hardlink step (dry-run)")
    args = parser.parse_args()

    print(f"=== EnterpriseRAG subset prep ===")
    print(f"target size: {args.size:,}")
    print(f"out: {args.out}")
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"\n[1/3] Allocating {args.size:,} slots proportionally:")
    alloc = proportional_alloc(args.size)
    for src, n in alloc.items():
        share = n / args.size * 100
        full = SOURCE_COUNTS[src] / TOTAL_FILES * 100
        print(f"  {src:<14} {n:>5,} ({share:.1f}%, full={full:.1f}%)")
    print(f"  TOTAL          {sum(alloc.values()):,}")

    print(f"\n[2/3] Collecting files per source (forcing in all gold docs):")
    mandatory = resolve_gold_paths()
    for src, paths in mandatory.items():
        print(f"  mandatory {src:<14}: {len(paths)} gold docs")
    file_map = collect_files_per_source(alloc, mandatory)

    cov = gold_coverage(file_map)
    print(f"\n[3a/3] Gold-doc coverage (vs 500 questions):")
    print(f"  questions with gold:    {cov['n_with_gold']}")
    print(f"  fully covered:          {cov['n_fully_covered']} ({100*cov['n_fully_covered']/max(cov['n_with_gold'],1):.1f}%)")
    print(f"  partially covered:      {cov['n_partially_covered']}")
    print(f"  uncovered:              {cov['n_uncovered']}")
    print(f"  subset dsids known:     {cov['subset_dsids']}")

    if args.skip_link:
        print("\n[SKIP] --skip-link set, dry-run only")
        return 0

    print(f"\n[3b/3] Hardlinking files into {args.out / 'sources'}:")
    total = hardlink_to_target(file_map, args.out)

    manifest = {
        "target_size": args.size,
        "actual_count": total,
        "alloc": alloc,
        "gold_coverage": cov,
        "out": str(args.out),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nmanifest: {args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
