"""Copy BEIR ladder receipts to cymatix-receipts and write a sha256 manifest.

Ladder receipts with ``--rank-dump`` run 2-8 MB each, too large to commit to
the code repo for every arm. They are copied to the private evidence archive
(``cymatix-receipts/captures/beir/<round>/``) and pinned here by sha256 so a
reader can verify the archived file is the one the NDCG receipt scored.

Usage:
  python benchmarks/dogfood/beir/stash_ladder_receipts.py --round round1-2026-10-03
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ARCHIVE = Path(r"F:\Projects\cymatix-receipts\captures\beir")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--round", required=True)
    args = ap.parse_args(argv)
    dest = ARCHIVE / args.round
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    for src in sorted((ROOT / "benchmarks" / "dogfood").glob("beir_*/receipts/ladder_beir_*.json")):
        data = src.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        target = dest / src.name
        shutil.copyfile(src, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
            raise SystemExit(f"copy mismatch: {target}")
        rows.append({"receipt": src.relative_to(ROOT).as_posix(), "bytes": len(data),
                     "sha256": sha, "archived_at": f"cymatix-receipts/captures/beir/{args.round}/{src.name}"})
    manifest = ROOT / "benchmarks" / "dogfood" / "beir" / f"ladder_receipts_{args.round}.json"
    manifest.write_text(json.dumps({"round": args.round, "receipts": rows}, indent=1), encoding="utf-8")
    print(f"{len(rows)} receipts -> {dest}; manifest {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
