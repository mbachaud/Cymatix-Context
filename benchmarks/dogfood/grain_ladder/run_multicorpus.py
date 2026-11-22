"""c580 grain on five more corpora, each with a matched c4000 control.

Corpora (build_fixture_matrix profiles): enronqa (v2 bench, no padding),
locomo, muloc, swebench, cosqa. Per profile x grain in {580, 4000}:
  1. build   build_fixture_matrix --parallel --workers 4 (ingest_c=4, tagger
             v2, hub cutoff 200, no dense/SPLADE) with CYMATIX_BFM_CHUNK_CHARS
             and the lean writes (no PKI rows, no COVER autolink, lean indexes),
             identical for both grains so the pair is matched.
  2. resolve the profile's own resolver into a per-arm bench dir (gene ids are
             content hashes, so they are re-derived per bed, never copied).
  3. ladder  baseline arm on configs/grain_c<grain>_classcap.toml (the ERB
             classcap arm), k = 83 / 12, scoring clock pinned to build finish
             + 1 h, >= COLD_GATE_S after the bed's build.
  4. compare c580 vs c4000 joined by query text -> summary receipt.

All c580 builds first, then the c4000 controls, then grading in build order.
Resumable: each stage is skipped when its output exists. PID claim on RUN.

Usage:
  python -P benchmarks/dogfood/grain_ladder/run_multicorpus.py
  python -P benchmarks/dogfood/grain_ladder/run_multicorpus.py --tags cosqa --grains 580
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import statistics as st
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from summarize import miss_breakdown  # noqa: E402

STAMP = os.environ.get("GRAIN_RUN_STAMP") or "2026-10-20"
RUN = Path(rf"F:\tmp\grain_multicorpus_{STAMP}")
COLD_GATE_S = 3600
RUN_TIMEOUT_S = 24 * 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TAGS = ["cosqa", "locomo", "swebench", "muloc", "enronqa"]   # small beds first
GRAINS = [580, 4000]
CAPS = {580: 83, 4000: 12}
D = ROOT / "benchmarks" / "dogfood"
LADDER = D / "erb" / "ablation_ladder.py"
LEAN = {"CYMATIX_BFM_PKI_WRITE": "0", "CYMATIX_BFM_ENTITY_AUTOLINK": "0", "CYMATIX_LEAN_INDEXES": "1"}

CORPUS = {
    "enronqa": r"F:\Projects\enronqa\corpus",
    "locomo": r"F:\Projects\locomo-plus\corpus",
    "muloc": r"F:\Projects\mulocbench\corpus",
    "swebench": r"F:\Projects\swebench\corpus",
    "cosqa": r"F:\Projects\cosqa\corpus",
}
# The 2026-09-04 beta witness receipts (c4000, shipped config) — context only.
BETA = {
    "enronqa": D / "enronqa" / "receipts" / "ladder_enronqa_v2_beta_seed0_2026-09-04.json",
    "locomo": D / "locomo" / "receipts" / "ladder_locomo_beta_seed0_2026-09-04.json",
    "muloc": D / "muloc" / "receipts" / "ladder_muloc_beta_seed0_2026-09-04.json",
    "swebench": D / "swebench" / "receipts" / "ladder_swebench_beta_seed0_2026-09-04.json",
    "cosqa": D / "cosqa" / "receipts" / "ladder_cosqa_beta_seed0_2026-09-04.json",
}

RUN.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(RUN / "run.log", encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("grain_multicorpus")


def paths(tag: str, grain: int) -> dict:
    arm = f"{tag}_c{grain}"
    bench = HERE / arm
    return {
        "arm": arm, "bench": bench, "src_bench": D / tag,
        "config": HERE / "configs" / f"grain_c{grain}_classcap.toml",
        "bed_dir": RUN / arm, "db": RUN / arm / f"{tag}.db",
        "resolved": bench / f"needles_resolved_{tag}_full.json",
        "gold": bench / f"gold_by_needle_{tag}.json",
        "ladder": RUN / f"ladder_{arm}_classcap_{STAMP}.json",
    }


def child_env(extra: dict | None = None) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith(("CYMATIX_", "HEADROOM_"))}
    e.update({"PYTHONHASHSEED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT),
              "CYMATIX_DISABLE_LEARN": "1", "CYMATIX_COVER_INDEX_AUTOBUILD": "0"})
    e.update(LEAN)  # lean-index skip must also hold when the ladder opens the bed
    e.update(extra or {})
    return e


def run(name: str, cmd: list, extra: dict | None = None) -> int:
    log.info("START %s: %s", name, " ".join(map(str, cmd)))
    t0 = time.time()
    with open(RUN / f"stage_{name}.log", "a", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, cwd=str(ROOT), env=child_env(extra), stdout=fh, stderr=subprocess.STDOUT,
                            timeout=RUN_TIMEOUT_S, creationflags=NO_WINDOW).returncode
    log.info("END %s rc=%d wall=%.0fs", name, rc, time.time() - t0)
    with open(RUN / "progress.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"stage": name, "rc": rc, "wall_s": round(time.time() - t0),
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    return rc


def seed_bench(p: dict) -> None:
    """Copy the profile's emitted needles/pairs into the per-arm bench dir."""
    p["bench"].mkdir(parents=True, exist_ok=True)
    for f in p["src_bench"].iterdir():
        if f.is_file() and not f.name.startswith(("needles_resolved_", "gold_by_needle_", "ladder_")) \
                and "resolve_meta" not in f.name and not (p["bench"] / f.name).exists():
            shutil.copy2(f, p["bench"] / f.name)


def resolve_cmd(tag: str, p: dict) -> list:
    common = ["--db", str(p["db"]), "--bench-dir", str(p["bench"]), "--corpus-root", CORPUS[tag]]
    if tag in ("enronqa", "locomo"):
        return [sys.executable, "-P", str(ROOT / "scripts" / f"resolve_{tag}_needles.py"), *common]
    return [sys.executable, "-P", str(ROOT / "scripts" / "resolve_bench_needles.py"), "--tag", tag, *common]


def build(tag: str, grain: int, p: dict) -> bool:
    seed_bench(p)
    if not (p["db"].exists() and (p["bed_dir"] / "manifest.json").exists()):
        cmd = [sys.executable, "-P", str(ROOT / "scripts" / "build_fixture_matrix.py"),
               "--profile", tag, "--out-dir", str(p["bed_dir"]), "--parallel", "--workers", "4"]
        env = {"CYMATIX_BFM_HUB_CUTOFF": "200", "CYMATIX_BFM_DENSE_BACKFILL": "0",
               "CYMATIX_BFM_SPLADE": "0", "CYMATIX_BFM_CHUNK_CHARS": str(grain)}
        if run(f"build_{p['arm']}", cmd, env) != 0 or not p["db"].exists():
            return False
    if not p["resolved"].exists():
        if run(f"resolve_{p['arm']}", resolve_cmd(tag, p)) != 0 or not p["resolved"].exists():
            return False
    return True


def scoring_epoch(p: dict) -> str:
    man = json.loads((p["bed_dir"] / "manifest.json").read_text(encoding="utf-8"))
    fin = next(iter(man["targets"].values()))["finished_at"]
    return f"{datetime.fromisoformat(fin).timestamp() + 3600:.0f}"


def shipped(p: dict) -> dict:
    """Shipped cymatix.toml (cap 12, seat floor 12) on the same c4000 bed."""
    return {**p, "arm": p["arm"] + "_shipped", "config": ROOT / "cymatix.toml",
            "ladder": RUN / f"ladder_{p['arm']}_shipped_{STAMP}.json"}


def grade(tag: str, grain: int, p: dict) -> bool:
    if p["ladder"].exists():
        return True
    age = time.time() - p["db"].stat().st_mtime
    if age < COLD_GATE_S:
        log.info("cold gate %s: sleeping %.0fs", p["arm"], COLD_GATE_S - age)
        time.sleep(COLD_GATE_S - age)
    cmd = [sys.executable, "-P", str(LADDER), "--genome", str(p["db"]),
           "--resolved", str(p["resolved"]), "--gold", str(p["gold"]),
           "--limit", "0", "--k", str(CAPS[grain]), "--arms", "baseline",
           "--config", str(p["config"]), "--per-query", "--rank-dump", "200",
           "--stamp", STAMP, "--out", str(p["ladder"])]
    env = {"CYMATIX_SQLITE_CACHE_SIZE": "-12582912", "CYMATIX_SCORING_EPOCH": scoring_epoch(p)}
    return run(f"ladder_{p['arm']}", cmd, env) == 0 and p["ladder"].exists()


def by_query(receipt: Path, resolved: Path) -> dict:
    q = {n["name"]: n["query"] for n in json.loads(resolved.read_text(encoding="utf-8"))["needles"]}
    pq = json.loads(receipt.read_text(encoding="utf-8"))["arms"][0]["per_query"]
    return {q[r["needle"]]: r for r in pq if r["needle"] in q}


def compare(tag: str, ref: str = "") -> dict | None:
    """c580 classcap vs the c4000 bed under classcap (ref "") or shipped config."""
    a, b = paths(tag, 580), paths(tag, 4000)
    if ref == "shipped":
        b = shipped(b)
    if not (a["ladder"].exists() and b["ladder"].exists()):
        return None
    c580, c4000 = by_query(a["ladder"], a["resolved"]), by_query(b["ladder"], b["resolved"])
    common = sorted(set(c580) & set(c4000))

    def cell(recs, k):
        rs = [recs[q] for q in common]
        chars = [r["delivered_chars"] for r in rs if r.get("delivered_chars") is not None]
        walls = [r["wall_ms"] for r in rs if isinstance(r.get("wall_ms"), (int, float))]
        return {"delivered_gold": round(sum(bool(r.get("delivered_gold")) for r in rs) / len(rs), 4),
                "chars_mean": round(st.mean(chars)) if chars else None,
                "wall_ms_p50": round(st.median(walls)) if walls else None,
                "misses": miss_breakdown(rs, k) if chars else None}

    wins = sum(1 for q in common if c580[q].get("delivered_gold") and not c4000[q].get("delivered_gold"))
    losses = sum(1 for q in common if c4000[q].get("delivered_gold") and not c580[q].get("delivered_gold"))
    beta = None
    if BETA[tag].exists():
        arm0 = json.loads(BETA[tag].read_text(encoding="utf-8"))["arms"][0]
        beta = arm0.get("delivered_gold_rate", arm0.get("summary", {}).get("delivered_gold_rate"))
    out = {"tag": tag, "stamp": STAMP, "join": "query text", "n_common": len(common),
           "n_c580_only": len(set(c580) - set(c4000)), "n_c4000_only": len(set(c4000) - set(c580)),
           "c580": {**cell(c580, 83), "receipt": str(a["ladder"]), "bed": str(a["db"]),
                    "git_sha": json.loads(a["ladder"].read_text(encoding="utf-8")).get("git_sha")},
           "c4000": {**cell(c4000, 12), "receipt": str(b["ladder"]), "bed": str(b["db"]),
                     "git_sha": json.loads(b["ladder"].read_text(encoding="utf-8")).get("git_sha")},
           "paired_c580_vs_c4000": {"wins": wins, "losses": losses},
           "beta_witness_2026_09_04_delivered_gold_rate": beta}
    out["c4000"]["config"] = str(b["config"])
    dst = HERE / f"{tag}_matched_c580_vs_c4000_{ref or 'classcap'}_{STAMP}.json"
    dst.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    log.info("compare %s: c580 %.4f vs c4000 %.4f, +%d/-%d (n=%d)", tag, out["c580"]["delivered_gold"],
             out["c4000"]["delivered_gold"], wins, losses, len(common))
    return out


def claim() -> None:
    pid = RUN / "runner.pid"
    if pid.exists():
        old = pid.read_text().strip()
        out = subprocess.run(["tasklist", "/FI", f"PID eq {old}", "/NH"], capture_output=True,
                             text=True, creationflags=NO_WINDOW).stdout
        if old and old in out:
            raise SystemExit(f"runner {old} still alive on {RUN}")
    pid.write_text(str(os.getpid()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tags", default=",".join(TAGS))
    ap.add_argument("--grains", default=",".join(map(str, GRAINS)))
    ap.add_argument("--shipped", action="store_true",
                    help="only grade the c4000 beds under the shipped cymatix.toml and compare")
    a = ap.parse_args(argv)
    tags = [t for t in a.tags.split(",") if t]
    if a.shipped:
        claim()
        failed = [t for t in tags if not grade(t, 4000, shipped(paths(t, 4000)))]
        for t in tags:
            compare(t, "shipped")
        log.info("DONE shipped failed=%s", failed)
        return 1 if failed else 0
    grains = [int(g) for g in a.grains.split(",") if g]
    claim()
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    cells = [(t, g) for g in grains for t in tags]
    ok, failed = [], []
    for t, g in cells:
        (ok if build(t, g, paths(t, g)) else failed).append((t, g))
    for t, g in sorted(ok, key=lambda c: paths(*c)["db"].stat().st_mtime):
        if not grade(t, g, paths(t, g)):
            failed.append((t, g))
    for t in tags:
        compare(t)
    (RUN / "DONE").write_text(json.dumps({"failed": failed}), encoding="utf-8")
    log.info("DONE failed=%s", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
