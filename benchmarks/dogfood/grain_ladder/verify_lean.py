"""Lean-write equivalence gate on BEIR scifact at 580-char grains.

Builds two beds at the same SHA — "full" (legacy writes) and "lean"
(CYMATIX_BFM_PKI_WRITE=0, CYMATIX_BFM_ENTITY_AUTOLINK=0,
CYMATIX_LEAN_INDEXES=1) — both with SPLADE and dense backfill off, then runs
the charcap ladder arm on each and requires:
  * identical gene_id sets and content digests,
  * identical per-needle delivered_gene_ids, delivered_chars, and score /
    final rank of first gold.
Reports bytes per gene for both. Sequential ingest (workers 4, matching the
grain ladder beds).

Usage: python -P benchmarks/dogfood/grain_ladder/verify_lean.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
D = ROOT / "benchmarks" / "dogfood"
OUT = Path(r"F:\tmp\grain_lean_verify_2026-10-16")
TAG = "beir_scifact"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ARMS = {
    "full": {},
    "lean": {"CYMATIX_BFM_PKI_WRITE": "0", "CYMATIX_BFM_ENTITY_AUTOLINK": "0",
             "CYMATIX_LEAN_INDEXES": "1"},
}
FIELDS = ("delivered_gene_ids", "delivered_chars", "delivered_gold",
          "rank_of_first_gold", "final_rank_of_first_gold", "budget_tier")


def env(extra: dict) -> dict:
    e = dict(os.environ)
    for k in ("CYMATIX_ENCODER_URL", "CYMATIX_CONFIG", "CYMATIX_LEAN_INDEXES",
              "CYMATIX_BFM_PKI_WRITE", "CYMATIX_BFM_ENTITY_AUTOLINK"):
        e.pop(k, None)
    e.update({"PYTHONHASHSEED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT),
              "CYMATIX_DISABLE_LEARN": "1", "CYMATIX_BFM_CHUNK_CHARS": "580",
              "CYMATIX_BFM_SPLADE": "0", "CYMATIX_BFM_DENSE_BACKFILL": "0",
              "CYMATIX_BFM_HUB_CUTOFF": "200"})
    e.update(extra)
    return e


def run(name: str, cmd: list, extra: dict) -> None:
    with open(OUT / f"{name}.log", "w", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, cwd=str(ROOT), env=env(extra), stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW).returncode
    if rc:
        raise SystemExit(f"{name} rc={rc}; see {OUT / (name + '.log')}")


def digest(db: Path) -> tuple[int, str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        n = 0
        for gid, ch in con.execute("select gene_id, content_hash from genes order by gene_id"):
            h.update(f"{gid}:{ch}\n".encode())
            n += 1
        return n, h.hexdigest()
    finally:
        con.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    res = {}
    for arm, extra in ARMS.items():
        bed = OUT / arm
        db = bed / f"{TAG}.db"
        bench = OUT / f"bench_{arm}"
        bench.mkdir(exist_ok=True)
        for f in (D / TAG).iterdir():
            if f.is_file() and not f.name.startswith(("needles_resolved_", "gold_by_needle_")) \
                    and "resolve_meta" not in f.name:
                (bench / f.name).write_bytes(f.read_bytes())
        t0 = time.time()
        if not db.exists():
            run(f"build_{arm}", [sys.executable, "-P", str(ROOT / "scripts" / "build_fixture_matrix.py"),
                                 "--profile", TAG, "--out-dir", str(bed), "--parallel", "--workers", "4"], extra)
        build_s = time.time() - t0
        run(f"resolve_{arm}", [sys.executable, "-P", str(ROOT / "scripts" / "resolve_bench_needles.py"),
                               "--tag", TAG, "--db", str(db), "--bench-dir", str(bench),
                               "--corpus-root", rf"F:\Projects\beir\{TAG}\corpus", "--min-file-bytes", "1"], extra)
        ladder = OUT / f"ladder_{arm}.json"
        run(f"ladder_{arm}", [sys.executable, "-P", str(D / "erb" / "ablation_ladder.py"), "--genome", str(db),
                              "--resolved", str(bench / f"needles_resolved_{TAG}_full.json"),
                              "--gold", str(bench / f"gold_by_needle_{TAG}.json"), "--limit", "0",
                              "--k", "83", "--arms", "baseline",
                              "--config", str(HERE / "configs" / "grain_c580_charcap.toml"),
                              "--per-query", "--rank-dump", "1000", "--out", str(ladder)], extra)
        n, dig = digest(db)
        pq = json.loads(ladder.read_text(encoding="utf-8"))["arms"][0]["per_query"]
        res[arm] = {"genes": n, "digest": dig, "bytes": db.stat().st_size,
                    "b_per_gene": round(db.stat().st_size / n), "build_s": round(build_s),
                    "per_query": {r["needle"]: {k: r.get(k) for k in FIELDS} for r in pq},
                    "delivered_gold": sum(r.get("delivered_gold", 0) for r in pq)}
    f, l = res["full"], res["lean"]
    diffs = [nd for nd in f["per_query"] if f["per_query"][nd] != l["per_query"].get(nd)]
    out = {
        "tag": TAG, "chunk_chars": 580, "config": "grain_c580_charcap.toml",
        "git_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True,
                                  text=True, creationflags=NO_WINDOW).stdout.strip(),
        "gene_sets_identical": f["digest"] == l["digest"],
        "per_needle_diffs": len(diffs), "diff_needles": diffs[:20],
        "arms": {a: {k: v for k, v in r.items() if k != "per_query"} for a, r in res.items()},
        "bytes_saved_pct": round(100 * (1 - l["bytes"] / f["bytes"]), 1),
    }
    (OUT / "verify_lean.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0 if out["gene_sets_identical"] and not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
