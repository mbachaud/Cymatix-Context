"""Cutoff retrieval A/B: EnronQA v2c (hub_cutoff=200) vs v2 (cutoff off).

Waits for the v2c bed and for the main v0.9.1 gate sweep to finish, then
runs the same v090/v091 ladder arms on v2c and diffs them per-needle
against the sweep's v2 receipts. Expected result at shipped defaults:
null deltas (COVER edges have no query-time consumer) — the #411 flip
evidence on EnronQA. A non-null delta is a finding, not a failure.
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

INT = Path(r"F:\Projects\cymatix-context\.claude\worktrees\v091-integration")
BENCH = Path(r"F:\Projects\cymatix-context\.claude\worktrees\wave-1-semantic-ranking-5f1dab")
SWEEP = Path(r"F:\tmp\sweep_v091")
LADDER = INT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py"
ENQ_DIR = BENCH / "benchmarks" / "dogfood" / "enronqa"
STAMP = "2026-08-30"
V2C_DB = r"F:/tmp/enronqa_bed_v2c/enronqa.db"
V2C_MAN = Path(r"F:\tmp\enronqa_bed_v2c\manifest.json")
GATE_FILE = BENCH / "benchmarks" / "dogfood" / "receipts" / f"sweep_v091_gate_{STAMP}.json"
COLD_GATE_S = 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CONFIGS = {"v090": SWEEP / "v090_defaults.toml", "v091": INT / "cymatix.toml"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(SWEEP / "v2c_ab.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("v2c_ab")


def env_for_run():
    env = dict(os.environ)
    env["CYMATIX_SQLITE_CACHE_SIZE"] = "-12582912"
    env.pop("CYMATIX_ENCODER_URL", None)
    return env


def load_arm(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    a = d["arms"][0]
    return a, {e["needle"]: e for e in a.get("per_query", [])}


def run_arm(arm, last_bed_touch):
    wait = COLD_GATE_S - (time.time() - last_bed_touch)
    if wait > 0:
        log.info("cold gate: sleeping %.0f min before v2c %s arm", wait / 60, arm)
        time.sleep(wait)
    out = ENQ_DIR / "receipts" / f"ladder_enronqa_v2c_{arm}_{STAMP}.json"
    cmd = [sys.executable, str(LADDER),
           "--genome", V2C_DB,
           "--resolved", str(ENQ_DIR / "needles_resolved_enronqa_full.json"),
           "--gold", str(ENQ_DIR / "gold_by_needle_enronqa.json"),
           "--limit", "0", "--k", "12", "--arms", "baseline",
           "--config", str(CONFIGS[arm]), "--per-query", "--stamp", STAMP,
           "--out", str(out)]
    log.info("START v2c %s", arm)
    with open(SWEEP / f"stage_v2c_{arm}.log", "w", encoding="utf-8") as fh:
        r = subprocess.run(cmd, cwd=str(INT), env=env_for_run(), stdout=fh,
                           stderr=subprocess.STDOUT, timeout=4 * 3600,
                           creationflags=NO_WINDOW)
    log.info("END v2c %s rc=%s", arm, r.returncode)
    if r.returncode == 0 and out.exists():
        d = json.loads(out.read_text(encoding="utf-8"))
        d["config_sha256"] = hashlib.sha256(
            CONFIGS[arm].read_bytes()).hexdigest()
        d["bed_note"] = "enronqa_bed_v2c: v2 code + PR#412, entity_autolink_hub_cutoff=200, ingest_c=1"
        out.write_text(json.dumps(d, indent=1), encoding="utf-8")
        a = d["arms"][0]
        log.info("ROLLUP v2c__%s: %s", arm, json.dumps(
            {k: a.get(k) for k in ("recall_at_k", "final_recall_at_k",
                                   "delivered_gold_rate",
                                   "median_rank_of_gold")}))
    return time.time()


def compare():
    res = {"stamp": STAMP, "beds": {"a": "enronqa_bed_v2 (cutoff off)",
                                    "b": "enronqa_bed_v2c (cutoff 200)"},
           "expectation": "null deltas at shipped defaults (COVER edges query-inert)",
           "arms": {}}
    for arm in CONFIGS:
        try:
            va, va_pq = load_arm(ENQ_DIR / "receipts"
                                 / f"ladder_enronqa_v2_{arm}_{STAMP}.json")
            vc, vc_pq = load_arm(ENQ_DIR / "receipts"
                                 / f"ladder_enronqa_v2c_{arm}_{STAMP}.json")
        except Exception as exc:
            res["arms"][arm] = {"status": "INCOMPLETE", "error": str(exc)}
            continue
        names = sorted(set(va_pq) & set(vc_pq))
        flips = [n for n in names
                 if bool(va_pq[n].get("delivered_gold"))
                 != bool(vc_pq[n].get("delivered_gold"))]
        res["arms"][arm] = {
            "v2": {k: va.get(k) for k in ("recall_at_k", "final_recall_at_k",
                                          "delivered_gold_rate",
                                          "median_rank_of_gold")},
            "v2c": {k: vc.get(k) for k in ("recall_at_k", "final_recall_at_k",
                                           "delivered_gold_rate",
                                           "median_rank_of_gold")},
            "delivered_delta": round(vc["delivered_gold_rate"]
                                     - va["delivered_gold_rate"], 4),
            "per_needle_delivered_flips": len(flips),
            "flip_needles": flips[:20],
            "null_confirmed": len(flips) == 0
            and abs(vc["recall_at_k"] - va["recall_at_k"]) < 1e-9,
        }
    out = BENCH / "benchmarks" / "dogfood" / "receipts" \
        / f"entity_hub_cutoff_retrieval_ab_enronqa_{STAMP}.json"
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    log.info("CUTOFF A/B VERDICT: %s", json.dumps(res, indent=1))


def main():
    log.info("waiting for v2c manifest...")
    while not (V2C_MAN.exists()
               and time.time() - V2C_MAN.stat().st_mtime > 120):
        time.sleep(300)
    log.info("v2c bed ready; waiting for main sweep verdict...")
    while not GATE_FILE.exists():
        time.sleep(300)
    log.info("main sweep done; running v2c arms")
    t = run_arm("v090", V2C_MAN.stat().st_mtime)
    t = run_arm("v091", t)
    compare()
    log.info("V2C AB COMPLETE")


if __name__ == "__main__":
    main()
