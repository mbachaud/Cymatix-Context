"""Grain-size (chunk_chars) ladder on small BEIR beds — pi-scaled grains.

Arms: chunk_chars 4000 (control, = shipped size), 1824 (18000/pi^2) and
580 (18000/pi^3). 5730 (18000/pi) is omitted: no scifact/nfcorpus/arguana
document exceeds 4000 chars, so it would rebuild the 4000 bed byte-for-byte.

Per tag x grain: build_fixture_matrix (round-1 convention: --parallel
--workers 4 = ingest_c=4, tagger v2, CYMATIX_BFM_HUB_CUTOFF=200, dense
backfill off) with CYMATIX_BFM_CHUNK_CHARS=<grain>; resolve needles into a
per-arm bench dir (gene ids differ per bed); ablation-ladder baseline arm on
configs/grain_c<grain>.toml (caps only: min_delivered_docs=0, ~18k-char
budget, seat cap scaled by grain); BEIR NDCG scorer. All builds first, then
grading, each bed at least COLD_GATE_S after its own build.

Resumable: every stage is skipped when its output exists. A PID file claims
the run dir; a live PID refuses to start.

Usage:
  python -P benchmarks/dogfood/grain_ladder/run_grain_ladder.py
  python -P benchmarks/dogfood/grain_ladder/run_grain_ladder.py --tags beir_scifact --grains 580
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
STAMP = os.environ.get("GRAIN_RUN_STAMP") or time.strftime("%Y-%m-%d")
RUN_DIR = Path(rf"F:\tmp\grain_ladder_{STAMP}")
COLD_GATE_S = 3600
RUN_TIMEOUT_S = 6 * 3600
RANK_DUMP = 1000   # grains, not docs: 580-char beds need depth to reach 10 docs
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TAGS = ["beir_scifact", "beir_nfcorpus"]
GRAINS = [4000, 1824, 580]
CAPS = {4000: 12, 1824: 26, 580: 83}

D = ROOT / "benchmarks" / "dogfood"
LADDER = D / "erb" / "ablation_ladder.py"
SCORER = D / "beir" / "beir_ndcg.py"

RUN_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(RUN_DIR / "grain.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("grain_ladder")


VARIANT = ""  # "" = seat-count tier cuts; "charcap" = char-basis cuts (set in main)


def paths(tag: str, grain: int) -> dict:
    bed_arm = f"{tag}_c{grain}"
    sfx = f"_{VARIANT}" if VARIANT else ""
    arm = bed_arm + sfx
    bench = HERE / bed_arm
    return {
        "arm": arm,
        "src_bench": D / tag,
        "bench": bench,
        "config": HERE / "configs" / f"grain_c{grain}{sfx}.toml",
        "corpus": Path(rf"F:\Projects\beir\{tag}\corpus"),
        "bed_dir": RUN_DIR / bed_arm,
        "db": RUN_DIR / bed_arm / f"{tag}.db",
        "resolved": bench / f"needles_resolved_{tag}_full.json",
        "gold": bench / f"gold_by_needle_{tag}.json",
        "ladder": bench / "receipts" / f"ladder_{arm}_seed0_{STAMP}.json",
        "ndcg": bench / "receipts" / f"beir_ndcg_{arm}_{STAMP}.json",
    }


def child_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update({"PYTHONHASHSEED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT),
                "CYMATIX_DISABLE_LEARN": "1", "CYMATIX_SQLITE_CACHE_SIZE": "-12582912"})
    for k in ("CYMATIX_ENCODER_URL", "CYMATIX_CONFIG", "CYMATIX_BFM_CHUNK_CHARS"):
        env.pop(k, None)
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
    wall = time.time() - t0
    log.info("END %s rc=%d wall=%.0fs", name, r.returncode, wall)
    with open(RUN_DIR / "progress.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"stage": name, "rc": r.returncode, "wall_s": round(wall, 1),
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    return r.returncode


def seed_bench(p: dict) -> None:
    """Copy the tag's emitted needles/grades into the per-arm bench dir.

    Resolved needles and gold_by_needle are per bed (gene ids are content
    hashes of the chunks), so they are regenerated, never copied.
    """
    p["bench"].mkdir(parents=True, exist_ok=True)
    (p["bench"] / "receipts").mkdir(exist_ok=True)
    for f in p["src_bench"].iterdir():
        if f.is_file() and not f.name.startswith(("needles_resolved_", "gold_by_needle_")) \
                and "resolve_meta" not in f.name and not (p["bench"] / f.name).exists():
            shutil.copy2(f, p["bench"] / f.name)


def build(tag: str, grain: int, p: dict) -> bool:
    seed_bench(p)
    if not (p["db"].exists() and (p["bed_dir"] / "manifest.json").exists()):
        cmd = [sys.executable, "-P", str(ROOT / "scripts" / "build_fixture_matrix.py"),
               "--profile", tag, "--out-dir", str(p["bed_dir"]), "--parallel", "--workers", "4"]
        # SPLADE off at query time, so its rows are dead weight (~80% of a
        # 580-char scifact bed, verify_lean 2026-10-16); the builder writes
        # them unless pinned. Beds from 2026-10-15 were built with them.
        env = {"CYMATIX_BFM_HUB_CUTOFF": "200", "CYMATIX_BFM_DENSE_BACKFILL": "0", "CYMATIX_BFM_SPLADE": "0",
               "CYMATIX_BFM_CHUNK_CHARS": str(grain)}
        if run(f"build_{p['arm']}", cmd, env) != 0:
            return False
    if not p["resolved"].exists():
        cmd = [sys.executable, "-P", str(ROOT / "scripts" / "resolve_bench_needles.py"),
               "--tag", tag, "--db", str(p["db"]), "--bench-dir", str(p["bench"]),
               "--corpus-root", str(p["corpus"]), "--min-file-bytes", "1"]
        if run(f"resolve_{p['arm']}", cmd) != 0:
            return False
    return True


def grade(tag: str, grain: int, p: dict) -> bool:
    if not p["ladder"].exists():
        age = time.time() - p["db"].stat().st_mtime
        if age < COLD_GATE_S:
            log.info("cold gate %s: sleeping %.0fs", p["arm"], COLD_GATE_S - age)
            time.sleep(COLD_GATE_S - age)
        cmd = [sys.executable, "-P", str(LADDER), "--genome", str(p["db"]),
               "--resolved", str(p["resolved"]), "--gold", str(p["gold"]),
               "--limit", "0", "--k", str(CAPS[grain]), "--arms", "baseline",
               "--config", str(p["config"]), "--per-query", "--rank-dump", str(RANK_DUMP),
               "--stamp", STAMP, "--out", str(p["ladder"])]
        if run(f"ladder_{p['arm']}", cmd) != 0 or not p["ladder"].exists():
            return False
    if not p["ndcg"].exists():
        cmd = [sys.executable, "-P", str(SCORER), "--tag", tag, "--receipt", str(p["ladder"]),
               "--db", str(p["db"]), "--bench-dir", str(p["bench"]),
               "--corpus-root", str(p["corpus"]), "--out", str(p["ndcg"])]
        if run(f"ndcg_{p['arm']}", cmd) != 0:
            return False
    return True


def claim() -> None:
    pid_file = RUN_DIR / "runner.pid"
    if pid_file.exists():
        old = int(pid_file.read_text().strip() or 0)
        out = subprocess.run(["tasklist", "/FI", f"PID eq {old}", "/NH"], capture_output=True,
                             text=True, creationflags=NO_WINDOW).stdout
        if str(old) in out and old != os.getpid():
            raise SystemExit(f"runner PID {old} still alive on {RUN_DIR}")
    pid_file.write_text(str(os.getpid()), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tags", default=",".join(TAGS))
    ap.add_argument("--grains", default=",".join(map(str, GRAINS)))
    ap.add_argument("--variant", default="", choices=("", "charcap"))
    args = ap.parse_args(argv)
    global VARIANT
    VARIANT = args.variant
    claim()
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    cells = [(t, int(g)) for t in args.tags.split(",") if t for g in args.grains.split(",") if g]
    failed, ok = [], []
    for t, g in cells:
        (ok if build(t, g, paths(t, g)) else failed).append((t, g))
    for t, g in sorted(ok, key=lambda c: paths(*c)["db"].stat().st_mtime):
        if not grade(t, g, paths(t, g)):
            failed.append((t, g))
    log.info("DONE failed=%s", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
