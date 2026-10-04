"""Tuning campaign (issue #482): tag-lanes-muted arm + know/miss replay, 30 beds.

User-scoped 2026-10-04: one tuning profile (tag lanes muted, the diagnostic
arm of benchmarks/dogfood/code/configs/tag_lanes_muted.toml), BEIR plus the
existing beds, know/miss refit reported but never written, run sequentially.

Jobs, one process at a time:
  muted   ablation ladder baseline arm on tag_lanes_muted.toml (BEIR beds add
          --rank-dump 200 and a beir_ndcg.py score)
  replay  benchmarks/dogfood/erb/know_abstain_replay.py, arm postflip_default
          on the shipped cymatix.toml: per-needle know/miss features + ranks,
          the input of benchmarks/dogfood/know/know_fit_report.py. Its ranks
          and delivery fields also re-witness each bed's shipped baseline at
          this commit.

Scheduling: the next job is the first pending one whose bed has been idle at
least COLD_GATE_S (bed .db / -wal mtime; a ladder or replay read bumps it), so
the 1 h cold-bed rule never serialises the whole queue behind one bed. Resumable:
a job whose receipt exists is skipped. Keep-awake held for the run. Env per
child: PYTHONHASHSEED=0, PYTHONUTF8=1, PYTHONPATH=<worktree>,
CYMATIX_DISABLE_LEARN=1, CYMATIX_SQLITE_CACHE_SIZE=-12582912; encoder and
CYMATIX_CONFIG unset.

Usage:
  python -P benchmarks/dogfood/sweeps/run_tuning_campaign.py [--only bed,bed] [--dry-run]
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
STAMP = os.environ.get("TUNING_RUN_STAMP") or time.strftime("%Y-%m-%d")
RUN_DIR = Path(rf"F:\tmp\tuning_campaign_{STAMP}")
COLD_GATE_S = 3600
RUN_TIMEOUT_S = 14 * 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

D = ROOT / "benchmarks" / "dogfood"
LADDER = D / "erb" / "ablation_ladder.py"
REPLAY = D / "erb" / "know_abstain_replay.py"
SCORER = D / "beir" / "beir_ndcg.py"
SHIPPED = ROOT / "cymatix.toml"
MUTED = D / "code" / "configs" / "tag_lanes_muted.toml"

FORUMS = ["android", "english", "gaming", "gis", "mathematica", "physics", "programmers",
          "stats", "tex", "unix", "webmasters", "wordpress"]
BEIR_R1 = ["beir_scifact", "beir_nfcorpus", "beir_arguana", "beir_scidocs", "beir_fiqa"]
BEIR_R2 = ["beir_trec_covid", "beir_webis_touche2020", "beir_quora"] + [f"beir_cqadupstack_{f}" for f in FORUMS]


def _beir(tag: str, jobs: list[str]) -> dict:
    return {"db": rf"F:\tmp\{tag}_bed\{tag}.db", "dir": D / tag,
            "resolved": D / tag / f"needles_resolved_{tag}_full.json",
            "gold": D / tag / f"gold_by_needle_{tag}.json", "beir": True, "jobs": jobs}


def _existing(db: str, subdir: str, resolved: str, gold: str, jobs: list[str]) -> dict:
    return {"db": db, "dir": D / subdir, "resolved": D / subdir / resolved, "gold": D / subdir / gold,
            "beir": False, "jobs": jobs}


# Registry verified 2026-10-04 (bed paths, needle/gold files and counts).
BEDS: dict[str, dict] = {
    "erb_947k": _existing(r"F:\tmp\erb_blob_v09x.db", "erb", "needles_resolved_v09x_full.json",
                          "gold_by_needle_v09x.json", ["muted", "replay"]),
    "enronqa_v2": _existing(r"F:\tmp\enronqa_bed_v2\enronqa.db", "enronqa",
                            "needles_resolved_enronqa_full.json", "gold_by_needle_enronqa.json",
                            ["muted", "replay"]),
    "enronqa_padded": _existing(r"F:\tmp\enronqa_padded_bed\enronqa_padded.db", "enronqa_padded",
                                "needles_resolved_enronqa_full.json", "gold_by_needle_enronqa.json",
                                ["muted", "replay"]),
    "locomo": _existing(r"F:\tmp\locomo_bed\locomo.db", "locomo", "needles_resolved_locomo_full.json",
                        "gold_by_needle_locomo.json", ["muted", "replay"]),
    "financebench": _existing(r"F:\tmp\financebench_bed\financebench.db", "financebench",
                              "needles_resolved_financebench_full.json",
                              "gold_by_needle_financebench.json", ["muted", "replay"]),
    # Muted arms for these five exist from the 2026-09-04 beta sweep.
    "muloc": _existing(r"F:\tmp\muloc_bed\muloc.db", "muloc", "needles_resolved_muloc_full.json",
                       "gold_by_needle_muloc.json", ["replay"]),
    **{t: _existing(rf"F:\tmp\{t}_bed\{t}.db", t, f"needles_resolved_{t}_full.json",
                    f"gold_by_needle_{t}.json", ["replay"])
       for t in ("coderag_solutions", "coderag_docs", "cosqa", "swebench")},
    **{t: _beir(t, ["muted", "replay"]) for t in BEIR_R2},
    **{t: _beir(t, ["replay"]) for t in BEIR_R1},
}

RUN_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(RUN_DIR / "campaign.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("tuning_campaign")


def outputs(name: str, job: str) -> list[Path]:
    bed = BEDS[name]
    rec = bed["dir"] / "receipts"
    if job == "replay":
        return [rec / f"know_replay_{name}_{STAMP}.json"]
    ladder = rec / f"ladder_{name}_diag_tag_lanes_muted_seed0_{STAMP}.json"
    if bed["beir"]:
        return [ladder, rec / f"beir_ndcg_{name}_diag_tag_lanes_muted_{STAMP}.json"]
    return [ladder]


def last_touch(db: str) -> float:
    return max((Path(db + s).stat().st_mtime for s in ("", "-wal") if Path(db + s).exists()), default=0.0)


def child_env() -> dict:
    env = dict(os.environ)
    env.update({"PYTHONHASHSEED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT),
                "CYMATIX_DISABLE_LEARN": "1", "CYMATIX_SQLITE_CACHE_SIZE": "-12582912"})
    env.pop("CYMATIX_ENCODER_URL", None)
    env.pop("CYMATIX_CONFIG", None)
    return env


def run(stage: str, cmd: list[str]) -> int:
    log.info("START %s: %s", stage, " ".join(cmd))
    t0 = time.time()
    with open(RUN_DIR / f"stage_{stage}.log", "w", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, cwd=str(ROOT), env=child_env(), stdout=fh, stderr=subprocess.STDOUT,
                            timeout=RUN_TIMEOUT_S, creationflags=NO_WINDOW).returncode
    wall = time.time() - t0
    log.info("END %s rc=%d wall=%.0fs", stage, rc, wall)
    with open(RUN_DIR / "progress.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"stage": stage, "rc": rc, "wall_s": round(wall, 1),
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    return rc


def do_job(name: str, job: str) -> bool:
    bed = BEDS[name]
    outs = outputs(name, job)
    base = [sys.executable, "-P"]
    if job == "replay":
        cmd = base + [str(REPLAY), "--genome", bed["db"], "--resolved", str(bed["resolved"]),
                      "--gold", str(bed["gold"]), "--limit", "0", "--k", "12",
                      "--arms", "postflip_default", "--config", str(SHIPPED),
                      "--stamp", STAMP, "--out", str(outs[0])]
        return run(f"replay_{name}", cmd) == 0 and outs[0].exists()
    if not outs[0].exists():
        cmd = base + [str(LADDER), "--genome", bed["db"], "--resolved", str(bed["resolved"]),
                      "--gold", str(bed["gold"]), "--limit", "0", "--k", "12", "--arms", "baseline",
                      "--config", str(MUTED), "--per-query", "--stamp", STAMP, "--out", str(outs[0])]
        if bed["beir"]:
            cmd[-4:-4] = ["--rank-dump", "200"]
        if run(f"muted_{name}", cmd) != 0 or not outs[0].exists():
            return False
    if bed["beir"] and not outs[1].exists():
        cmd = base + [str(SCORER), "--tag", name, "--receipt", str(outs[0]), "--db", bed["db"],
                      "--out", str(outs[1])]
        return run(f"ndcg_muted_{name}", cmd) == 0
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", default="", help="comma list of bed names")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    names = [n for n in BEDS if not args.only or n in args.only.split(",")]
    missing = [n for n in names for p in (BEDS[n]["db"], BEDS[n]["resolved"], BEDS[n]["gold"])
               if not Path(p).exists()]
    if missing:
        raise SystemExit(f"missing inputs for: {sorted(set(missing))}")
    queue = [(n, j) for n in names for j in BEDS[n]["jobs"] if not all(p.exists() for p in outputs(n, j))]
    log.info("stamp %s: %d jobs pending over %d beds", STAMP, len(queue), len({n for n, _ in queue}))
    if args.dry_run:
        for n, j in queue:
            print(f"{n:30s} {j:7s} -> {outputs(n, j)[0].relative_to(ROOT)}")
        return 0
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    (RUN_DIR / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")
    failed = []
    while queue:
        now = time.time()
        ready = [(n, j) for n, j in queue if now - last_touch(BEDS[n]["db"]) >= COLD_GATE_S]
        if not ready:
            wait = min(COLD_GATE_S - (now - last_touch(BEDS[n]["db"])) for n, _ in queue)
            log.info("all pending beds warm; sleeping %.0fs", max(wait, 30))
            time.sleep(max(wait, 30))
            continue
        name, job = ready[0]
        queue.remove((name, job))
        if not do_job(name, job):
            failed.append(f"{job}:{name}")
    log.info("DONE failed=%s", failed)
    with open(RUN_DIR / "progress.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"stage": "done", "rc": 1 if failed else 0, "failed": failed,
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
