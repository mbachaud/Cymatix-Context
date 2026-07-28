"""cut_gold_pack.py — package the dogfood gold files for transfer.

The gold pack is the set of files a second party needs on disk before they can
run the needle harness. Without it the ingest step cannot inject gold, the
per-bed recall rows come out empty, and the run scores a retrieval failure
that is really a missing-file failure.

The previous SIKE pack could not be cut at all: 51 of its 64 gold files lived
in private sibling repos. Every file here comes from the four owned repos, so
the pack ships as a unit.

**Layout matters.** Files are stored under their ``<repo>/<path>`` prefix, so
the receiving side points ``--roots`` at the unpacked directory and the same
``gold_source`` strings resolve unchanged. Flattening the tree would break
that and silently change which file answers which needle.

Emits, next to the archive:

* ``gold_pack_manifest.json`` — per-file sha256, size, and the needles each
  file serves, plus the archive's own digest
* a plain ``sha256`` line for the archive, for the pointer record

Usage::

    python benchmarks/dogfood/cut_gold_pack.py
    python benchmarks/dogfood/cut_gold_pack.py --out-dir dist/gold
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from build_dogfood_genome import PROJECTS_ROOT, REPOS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LABEL_TO_DIR = {s["label"]: s["dir"] for s in REPOS}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json",
                    help="output of verify_needles.py")
    ap.add_argument("--out-dir", default="benchmarks/dogfood/pack")
    ap.add_argument("--name", default="cymatix-dogfood-gold-pack")
    args = ap.parse_args()

    resolved_path = REPO_ROOT / args.resolved
    if not resolved_path.exists():
        print(f"ERROR: {resolved_path} not found — run verify_needles.py first",
              file=sys.stderr)
        return 2
    payload = json.loads(resolved_path.read_text(encoding="utf-8"))

    failing = [n["name"] for n in payload["needles"] if n["status"] != "ok"]
    if failing:
        print("ERROR: refusing to cut a pack with failing needles:", file=sys.stderr)
        for f in failing:
            print(f"  {f}", file=sys.stderr)
        print("Fix them in needles_dogfood.py and re-run verify_needles.py.",
              file=sys.stderr)
        return 1

    # file -> needles it serves
    serves: dict[str, list[str]] = {}
    for nd in payload["needles"]:
        for g in nd["gold_source"]:
            serves.setdefault(g, []).append(nd["name"])

    out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{args.name}.tar.gz"

    entries: list[dict] = []
    missing: list[str] = []

    with tarfile.open(archive, "w:gz") as tar:
        for rel in sorted(serves):
            label, _, sub = rel.partition("/")
            src = PROJECTS_ROOT / LABEL_TO_DIR.get(label, label) / sub
            if not src.is_file():
                missing.append(rel)
                continue
            tar.add(src, arcname=rel)
            entries.append({
                "path": rel,
                "bytes": src.stat().st_size,
                "sha256": sha256_path(src),
                "needles": sorted(serves[rel]),
            })

    if missing:
        print("ERROR: gold files listed in the resolved set are not on disk:",
              file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        archive.unlink(missing_ok=True)
        return 1

    archive_sha = sha256_path(archive)
    by_repo: dict[str, int] = {}
    for e in entries:
        by_repo[e["path"].split("/", 1)[0]] = by_repo.get(e["path"].split("/", 1)[0], 0) + 1

    manifest = {
        "name": args.name,
        "cut_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "archive": archive.name,
        "archive_sha256": archive_sha,
        "archive_bytes": archive.stat().st_size,
        "files": len(entries),
        "files_by_repo": by_repo,
        "needles_served": payload["needles_ok"],
        "layout": "<repo>/<path> — point --roots at the unpacked directory",
        "entries": entries,
    }
    mpath = out_dir / "gold_pack_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out_dir / f"{args.name}.tar.gz.sha256").write_text(
        f"{archive_sha}  {archive.name}\n", encoding="utf-8")

    print(f"gold pack: {archive}")
    print(f"  files:   {len(entries)}  ({', '.join(f'{k}={v}' for k, v in sorted(by_repo.items()))})")
    print(f"  bytes:   {manifest['archive_bytes']:,}")
    print(f"  sha256:  {archive_sha}")
    print(f"  needles: {payload['needles_ok']}")
    print(f"  manifest {mpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
