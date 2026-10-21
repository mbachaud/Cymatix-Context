"""ERB 947k reference under a grain-ladder delivery policy.

Snapshots the frozen witness bed (F:/tmp/erb_blob_v09x.db, gene-id set
729ca5d9..., tagger v1, ingest_c=6) with sqlite3.backup (source opened
mode=ro + query_only, as the release witnesses do), then runs the ablation
ladder baseline arm on the snapshot with a grain-ladder config (default:
configs/grain_c4000_charcap.toml — caps only, no seat floor, ~18k-char
packet, cap 12) over the full ordered 470-needle v09x bank. Env pins match
the release witness (PYTHONHASHSEED=0, LEARN off, 12 GB SQLite cache, no
CYMATIX_*/HEADROOM_* inherited).

Resumable: the snapshot and the ladder receipt are each skipped when present.
A PID file claims the run dir.

Usage: python -P benchmarks/dogfood/grain_ladder/run_erb_reference.py [--config X.toml]
"""
from __future__ import annotations

import argparse
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
SRC = Path(r"F:\tmp\erb_blob_v09x.db")
BENCH = ROOT / "benchmarks" / "dogfood" / "erb"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "configs" / "grain_c4000_charcap.toml"))
    ap.add_argument("--run-dir", default=r"F:\tmp\grain_erb_ref_c4000_2026-10-16")
    ap.add_argument("--k", type=int, default=12)
    a = ap.parse_args()
    run = Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    pid = run / "runner.pid"
    if pid.exists():
        old = pid.read_text().strip()
        out = subprocess.run(["tasklist", "/FI", f"PID eq {old}", "/NH"], capture_output=True,
                             text=True, creationflags=NO_WINDOW).stdout
        if old and old in out:
            raise SystemExit(f"runner {old} still alive")
    pid.write_text(str(os.getpid()))
    cfg = Path(a.config)
    stem = cfg.stem
    snap = run / "erb_blob_v09x.db"
    receipt = run / f"ladder_erb947k_{stem}_2026-10-16.json"
    manifest = {"source": str(SRC), "config": str(cfg), "config_sha256": sha(cfg), "k": a.k}
    if not snap.exists():
        t0 = time.time()
        src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
        src.execute("PRAGMA query_only = 1")
        dst = sqlite3.connect(str(snap))
        src.backup(dst)
        dst.close()
        src.close()
        manifest["snapshot_s"] = round(time.time() - t0, 1)
    con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    h = hashlib.sha256()
    n = 0
    for (gid,) in con.execute("select gene_id from genes order by gene_id"):
        h.update(gid.encode() + b"\n")
        n += 1
    con.close()
    manifest.update({"snapshot_genes": n, "gene_id_set_sha256": h.hexdigest(),
                     "resolved_sha256": sha(BENCH / "needles_resolved_v09x_full.json"),
                     "gold_sha256": sha(BENCH / "gold_by_needle_v09x.json")})
    if not receipt.exists():
        cmd = [sys.executable, "-P", str(BENCH / "ablation_ladder.py"), "--genome", str(snap),
               "--resolved", str(BENCH / "needles_resolved_v09x_full.json"),
               "--gold", str(BENCH / "gold_by_needle_v09x.json"),
               "--limit", "0", "--k", str(a.k), "--arms", "baseline", "--config", str(cfg),
               "--per-query", "--rank-dump", "200", "--stamp", "2026-10-16", "--out", str(receipt)]
        pins = {"PYTHONHASHSEED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT),
                "CYMATIX_DISABLE_LEARN": "1", "CYMATIX_SQLITE_CACHE_SIZE": "-12582912"}
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CYMATIX_", "HEADROOM_"))}
        env.update(pins)
        manifest.update({"command": cmd, "env_pins": pins, "python": sys.executable})
        (run / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        t0 = time.time()
        with open(run / "ladder.log", "w", encoding="utf-8") as log:
            rc = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW).returncode
        manifest.update({"ladder_rc": rc, "ladder_wall_s": round(time.time() - t0)})
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (run / "DONE").write_text("ok", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
