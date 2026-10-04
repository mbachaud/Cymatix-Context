"""BEIR round 1 (issue #482): build, resolve, grade and BEIR-score small beds.

Per bed, in order: ``scripts/build_fixture_matrix.py`` (``--parallel
--workers 4`` = ingest_c=4, tagger v2, ``CYMATIX_BFM_HUB_CUTOFF=200``,
``CYMATIX_BFM_DENSE_BACKFILL=0`` — the 2026-09-04 new-bed convention),
``scripts/resolve_bench_needles.py``, the ablation ladder baseline arm on the
shipped ``cymatix.toml`` with ``--per-query --rank-dump 200``, then
``benchmarks/dogfood/beir/beir_ndcg.py``. All builds run first, then the
ladders, each at least COLD_GATE_S after its own bed's build (cold-bed rule).

Resumable: a stage is skipped when its output exists (bed manifest entry,
resolved needles, ladder receipt, NDCG receipt). Holds a per-process
"system required" request so the host does not sleep mid-run (no power
setting is changed). Env per child: PYTHONHASHSEED=0, PYTHONUTF8=1,
PYTHONPATH=<worktree>, CYMATIX_DISABLE_LEARN=1,
CYMATIX_SQLITE_CACHE_SIZE=-12582912; CYMATIX_ENCODER_URL and CYMATIX_CONFIG
unset (shipped defaults, daemon off — BASELINES exception (d)).

Usage:
  python -P benchmarks/dogfood/sweeps/run_beir_round1.py
  python -P benchmarks/dogfood/sweeps/run_beir_round1.py --tags beir_scifact
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STAMP = "2026-10-03"
RUN_DIR = Path(r"F:\tmp\beir_round1_2026-10-03")
COLD_GATE_S = 3600
RUN_TIMEOUT_S = 12 * 3600
RANK_DUMP = 200
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TAGS = ["beir_scifact", "beir_nfcorpus", "beir_arguana", "beir_scidocs", "beir_fiqa"]

D = ROOT / "benchmarks" / "dogfood"
LADDER = D / "erb" / "ablation_ladder.py"
SCORER = D / "beir" / "beir_ndcg.py"
SHIPPED = ROOT / "cymatix.toml"

RUN_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(RUN_DIR / "round1.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("beir_round1")


def paths(tag: str) -> dict:
    bench = D / tag
    return {
        "bench": bench,
        "corpus": Path(rf"F:\Projects\beir\{tag}\corpus"),
        "bed_dir": Path(rf"F:\tmp\{tag}_bed"),
        "db": Path(rf"F:\tmp\{tag}_bed\{tag}.db"),
        "resolved": bench / f"needles_resolved_{tag}_full.json",
        "gold": bench / f"gold_by_needle_{tag}.json",
        "ladder": bench / "receipts" / f"ladder_{tag}_seed0_{STAMP}.json",
        "ndcg": bench / "receipts" / f"beir_ndcg_{tag}_{STAMP}.json",
    }


def child_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update({"PYTHONHASHSEED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT),
                "CYMATIX_DISABLE_LEARN": "1", "CYMATIX_SQLITE_CACHE_SIZE": "-12582912"})
    env.pop("CYMATIX_ENCODER_URL", None)
    env.pop("CYMATIX_CONFIG", None)
    env.update(extra or {})
    return env


def run(name: str, cmd: list[str], extra_env: dict | None = None) -> int:
    log_path = RUN_DIR / f"stage_{name}.log"
    log.info("START %s: %s", name, " ".join(cmd))
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8") as fh:
        r = subprocess.run(cmd, cwd=str(ROOT), env=child_env(extra_env), stdout=fh,
                           stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT_S,
                           creationflags=NO_WINDOW)
    log.info("END %s rc=%d wall=%.0fs", name, r.returncode, time.time() - t0)
    progress(name, r.returncode, time.time() - t0)
    return r.returncode


def progress(stage: str, rc: int, wall: float) -> None:
    p = RUN_DIR / "progress.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"stage": stage, "rc": rc, "wall_s": round(wall, 1),
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")


def built(p: dict) -> bool:
    man = p["bed_dir"] / "manifest.json"
    return p["db"].exists() and man.exists()


def build(tag: str, p: dict) -> bool:
    if built(p):
        log.info("skip build %s (bed + manifest exist)", tag)
        return True
    cmd = [sys.executable, "-P", str(ROOT / "scripts" / "build_fixture_matrix.py"),
           "--profile", tag, "--out-dir", str(p["bed_dir"]), "--parallel", "--workers", "4"]
    rc = run(f"build_{tag}", cmd, {"CYMATIX_BFM_HUB_CUTOFF": "200", "CYMATIX_BFM_DENSE_BACKFILL": "0"})
    if rc != 0:
        return False
    if not p["resolved"].exists():
        cmd = [sys.executable, "-P", str(ROOT / "scripts" / "resolve_bench_needles.py"),
               "--tag", tag, "--db", str(p["db"]), "--bench-dir", str(p["bench"]),
               "--corpus-root", str(p["corpus"])]
        if run(f"resolve_{tag}", cmd) != 0:
            return False
    return True


def grade(tag: str, p: dict) -> bool:
    if not p["ladder"].exists():
        age = time.time() - p["db"].stat().st_mtime
        if age < COLD_GATE_S:
            wait = COLD_GATE_S - age
            log.info("cold gate %s: sleeping %.0fs", tag, wait)
            time.sleep(wait)
        cmd = [sys.executable, "-P", str(LADDER), "--genome", str(p["db"]),
               "--resolved", str(p["resolved"]), "--gold", str(p["gold"]),
               "--limit", "0", "--k", "12", "--arms", "baseline", "--config", str(SHIPPED),
               "--per-query", "--rank-dump", str(RANK_DUMP), "--stamp", STAMP, "--out", str(p["ladder"])]
        if run(f"ladder_{tag}", cmd) != 0 or not p["ladder"].exists():
            return False
    if not p["ndcg"].exists():
        cmd = [sys.executable, "-P", str(SCORER), "--tag", tag, "--receipt", str(p["ladder"]),
               "--db", str(p["db"]), "--out", str(p["ndcg"])]
        if run(f"ndcg_{tag}", cmd) != 0:
            return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tags", default=",".join(TAGS))
    args = ap.parse_args(argv)
    tags = [t for t in args.tags.split(",") if t]
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    (RUN_DIR / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")
    failed = []
    ok_built = [t for t in tags if build(t, paths(t)) or failed.append(f"build:{t}")]
    # Grade oldest bed first so the cold gate costs the least wall time.
    for t in sorted(ok_built, key=lambda t: paths(t)["db"].stat().st_mtime):
        if not grade(t, paths(t)):
            failed.append(f"grade:{t}")
    log.info("DONE failed=%s", failed)
    progress("done", 1 if failed else 0, 0.0)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
