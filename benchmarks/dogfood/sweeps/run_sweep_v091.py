"""v0.9.1 gate sweep — sequential ladder queue across EnronQA + ERB beds.

Runs every arm from the v0.9.1 integration worktree (master + PR #407 + #408
+ #409 + enronqa-bed + #406, merge commit e6dba22) so the sweep measures the
merged stack, not any single branch. Enforces the wave-1b cold-bed gate
(>= 1 h between manager-mediated accesses of the same bed), waits for the
padded bed build + needle resolve, annotates config_sha256 per rule 6, and
emits a verdict JSON pairing new arms against the old-defaults arm and the
frozen 2026-08-28 ERB receipts.
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
RESOLVER = INT / "scripts" / "resolve_enronqa_needles.py"
STAMP = "2026-08-30"
COLD_GATE_S = 3600
RUN_TIMEOUT_S = 6 * 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(SWEEP / "sweep.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("sweep")

ERB_DIR = BENCH / "benchmarks" / "dogfood" / "erb"
ENQ_DIR = BENCH / "benchmarks" / "dogfood" / "enronqa"
PAD_DIR = BENCH / "benchmarks" / "dogfood" / "enronqa_padded"

CONFIGS = {
    "v090": SWEEP / "v090_defaults.toml",          # origin/master cymatix.toml
    "v091": INT / "cymatix.toml",                   # integration shipped defaults
    "v091_floor": INT / "benchmarks" / "dogfood" / "erb" / "configs" / "w24_min_delivered_12.toml",
}

BEDS = {
    "enronqa_v2": {
        "db": r"F:/tmp/enronqa_bed_v2/enronqa.db",
        "resolved": ENQ_DIR / "needles_resolved_enronqa_full.json",
        "gold": ENQ_DIR / "gold_by_needle_enronqa.json",
        "out_dir": ENQ_DIR / "receipts",
        "out_stem": "ladder_enronqa_v2",
    },
    "erb_100k": {
        "db": r"F:/tmp/erb_carve/erb_100k.db",
        "resolved": ERB_DIR / "needles_resolved_carve.json",
        "gold": ERB_DIR / "gold_by_needle_carve.json",
        "out_dir": ERB_DIR / "receipts",
        "out_stem": "ladder_erb100k",
    },
    "erb_829k": {
        "db": r"F:/tmp/erb_blob_v09x.db",
        "resolved": ERB_DIR / "needles_resolved_v09x_full.json",
        "gold": ERB_DIR / "gold_by_needle_v09x.json",
        "out_dir": ERB_DIR / "receipts",
        "out_stem": "ladder_v09x_int",
    },
    "enronqa_padded": {
        "db": r"F:/tmp/enronqa_padded_bed/enronqa_padded.db",
        "resolved": PAD_DIR / "needles_resolved_enronqa_full.json",
        "gold": PAD_DIR / "gold_by_needle_enronqa.json",
        "out_dir": PAD_DIR / "receipts",
        "out_stem": "ladder_enronqa_padded",
    },
}

# (kind, bed, arm) — sequential; same-bed stages rely on the cold-gate waits.
STAGES = [
    ("ladder", "enronqa_v2", "v090"),
    ("ladder", "erb_100k", "v090"),
    ("ladder", "enronqa_v2", "v091"),
    ("ladder", "erb_100k", "v091"),
    ("ladder", "enronqa_v2", "v091_floor"),
    ("ladder", "erb_829k", "v091"),
    ("resolve_padded", "enronqa_padded", None),
    ("ladder", "enronqa_padded", "v090"),
    ("ladder", "enronqa_padded", "v091"),
    ("ladder", "enronqa_padded", "v091_floor"),
]

last_end = {b: 0.0 for b in BEDS}
manifest_rows = []


def env_for_run():
    env = dict(os.environ)
    env["CYMATIX_SQLITE_CACHE_SIZE"] = "-12582912"  # 12 GiB, user-approved band
    env.pop("CYMATIX_ENCODER_URL", None)            # match all prior receipts
    return env


def write_manifest():
    (SWEEP / "sweep_manifest.json").write_text(
        json.dumps({"stamp": STAMP, "integration_commit": "e6dba22",
                    "rows": manifest_rows}, indent=1), encoding="utf-8")


def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def preflight():
    """Field-level arm-config asserts, evaluated with the integration code."""
    code = (
        "import json,sys; sys.path.insert(0, r'%s')\n"
        "from cymatix_context.config import load_config\n"
        "out={}\n"
        "for name,p in %r:\n"
        "    c=load_config(p)\n"
        "    r=c.retrieval\n"
        "    out[name]={'rrf_k':r.rrf_k,"
        "'map':dict(getattr(r,'rerank_combinator_by_class',{}) or {}),"
        "'floor':getattr(c.budget,'min_delivered_docs',0)}\n"
        "print(json.dumps(out))\n"
    ) % (str(INT), [(k, str(v)) for k, v in CONFIGS.items()])
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=str(INT), timeout=300,
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"preflight load_config failed: {r.stderr[-2000:]}")
    got = json.loads(r.stdout.strip().splitlines()[-1])
    log.info("preflight configs: %s", json.dumps(got))
    v090, v091, floor = got["v090"], got["v091"], got["v091_floor"]
    assert v090["rrf_k"] == 60, v090
    assert set(v090["map"]) == {"multi_hop", "default"}, v090
    assert not v090["floor"], v090
    assert v091["rrf_k"] == 20, v091
    assert set(v091["map"]) == {"arithmetic", "factual", "procedural",
                                "multi_hop", "default"}, v091
    assert not v091["floor"], v091
    assert floor["floor"] == 12 and floor["rrf_k"] == 20, floor
    log.info("preflight PASS: v090/v091/v091_floor semantics verified")


def wait_cold(bed):
    dt = time.time() - last_end[bed]
    if dt < COLD_GATE_S:
        wait = COLD_GATE_S - dt
        log.info("cold-bed gate: waiting %.0f min before touching %s",
                 wait / 60, bed)
        time.sleep(wait)


def wait_padded_built():
    man = Path(r"F:\tmp\enronqa_padded_bed\manifest.json")
    db = Path(r"F:\tmp\enronqa_padded_bed\enronqa_padded.db")
    log.info("waiting for padded bed manifest...")
    while True:
        if man.exists() and time.time() - man.stat().st_mtime > 120 \
                and time.time() - db.stat().st_mtime > 120:
            break
        time.sleep(300)
    last_end["enronqa_padded"] = max(man.stat().st_mtime, db.stat().st_mtime)
    log.info("padded bed manifest present and settled: %s",
             man.read_text(encoding="utf-8")[:400].replace("\n", " "))


def run_stage_cmd(name, cmd, cwd):
    stage_log = SWEEP / f"stage_{name}.log"
    log.info("START %s", name)
    t0 = time.time()
    try:
        with open(stage_log, "w", encoding="utf-8") as fh:
            r = subprocess.run(cmd, cwd=str(cwd), env=env_for_run(),
                               stdout=fh, stderr=subprocess.STDOUT,
                               timeout=RUN_TIMEOUT_S, creationflags=NO_WINDOW)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = -999
        log.warning("TIMEOUT %s after %.0f s", name, RUN_TIMEOUT_S)
    except Exception as exc:
        rc = -998
        log.warning("EXC %s: %s", name, exc)
    dur = time.time() - t0
    log.info("END %s rc=%s dur=%.0fs", name, rc, dur)
    return rc, dur


def run_ladder(bed, arm):
    wait_cold(bed)
    b = BEDS[bed]
    cfg = CONFIGS[arm]
    out = Path(b["out_dir"]) / f"{b['out_stem']}_{arm}_{STAMP}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    name = f"{bed}__{arm}"
    cmd = [sys.executable, str(LADDER),
           "--genome", b["db"],
           "--resolved", str(b["resolved"]),
           "--gold", str(b["gold"]),
           "--limit", "0", "--k", "12", "--arms", "baseline",
           "--config", str(cfg), "--per-query", "--stamp", STAMP,
           "--out", str(out)]
    rc, dur = run_stage_cmd(name, cmd, INT)
    last_end[bed] = time.time()
    row = {"stage": name, "rc": rc, "dur_s": round(dur), "out": str(out)}
    if rc == 0 and out.exists():
        try:
            d = json.loads(out.read_text(encoding="utf-8"))
            d["config_sha256"] = sha256_file(cfg)
            d["config_provenance"] = {
                "v090": "origin/master cymatix.toml (v0.9.0 shipped defaults)",
                "v091": "integration e6dba22 cymatix.toml (v0.9.1 candidate)",
                "v091_floor": "w24_min_delivered_12.toml (PR #409 arm)",
            }[arm]
            d["integration_commit"] = "e6dba22"
            out.write_text(json.dumps(d, indent=1), encoding="utf-8")
            a = d["arms"][0]
            row["rollup"] = {k: a.get(k) for k in
                            ("recall_at_k", "final_recall_at_k",
                             "delivered_gold_rate", "median_rank_of_gold",
                             "n_queries")}
            log.info("ROLLUP %s: %s", name, json.dumps(row["rollup"]))
        except Exception as exc:
            log.warning("post-annotate failed for %s: %s", name, exc)
    manifest_rows.append(row)
    write_manifest()


def run_resolve_padded():
    wait_padded_built()
    wait_cold("enronqa_padded")
    cmd = [sys.executable, str(RESOLVER),
           "--db", BEDS["enronqa_padded"]["db"],
           "--bench-dir", str(PAD_DIR),
           "--corpus-root", r"F:/Projects/enronqa/corpus",
           "--seed", "20260829"]
    rc, dur = run_stage_cmd("resolve_padded", cmd, INT)
    last_end["enronqa_padded"] = time.time()
    manifest_rows.append({"stage": "resolve_padded", "rc": rc,
                          "dur_s": round(dur)})
    write_manifest()
    if rc != 0:
        raise RuntimeError("padded needle resolve failed — padded arms "
                           "would run without gold; aborting padded stages")


def load_arm(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    a = d["arms"][0]
    pq = {e["needle"]: e for e in a.get("per_query", [])}
    return a, pq


def paired(old_pq, new_pq, key="delivered_gold"):
    names = sorted(set(old_pq) & set(new_pq))
    gain = sum(1 for n in names
               if new_pq[n].get(key) and not old_pq[n].get(key))
    loss = sum(1 for n in names
               if old_pq[n].get(key) and not new_pq[n].get(key))
    return {"n_paired": len(names), "gained": gain, "lost": loss,
            "net": gain - loss}


def variant_rate(pq, suffix, key="delivered_gold"):
    rows = [e for n, e in pq.items() if n.endswith(suffix)]
    if not rows:
        return None
    return round(sum(1 for e in rows if e.get(key)) / len(rows), 4)


def verdict():
    v = {"stamp": STAMP, "integration_commit": "e6dba22", "beds": {},
         "decision_metric": "delivered_gold_rate"}
    fresh_pairs = [("enronqa_v2", "paraphrase"), ("erb_100k", None),
                   ("enronqa_padded", "paraphrase")]
    for bed, extra in fresh_pairs:
        b = BEDS[bed]
        try:
            old_a, old_pq = load_arm(
                Path(b["out_dir"]) / f"{b['out_stem']}_v090_{STAMP}.json")
            new_a, new_pq = load_arm(
                Path(b["out_dir"]) / f"{b['out_stem']}_v091_{STAMP}.json")
        except Exception as exc:
            v["beds"][bed] = {"status": "INCOMPLETE", "error": str(exc)}
            continue
        entry = {
            "v090": {k: old_a.get(k) for k in
                     ("delivered_gold_rate", "recall_at_k",
                      "final_recall_at_k", "median_rank_of_gold")},
            "v091": {k: new_a.get(k) for k in
                     ("delivered_gold_rate", "recall_at_k",
                      "final_recall_at_k", "median_rank_of_gold")},
            "delivered_delta": round(new_a["delivered_gold_rate"]
                                     - old_a["delivered_gold_rate"], 4),
            "paired_delivered": paired(old_pq, new_pq),
        }
        if extra == "paraphrase":
            entry["paraphrase_delivered"] = {
                "v090": {"orig": variant_rate(old_pq, "_o"),
                         "reph": variant_rate(old_pq, "_r")},
                "v091": {"orig": variant_rate(new_pq, "_o"),
                         "reph": variant_rate(new_pq, "_r")},
            }
        floor_path = Path(b["out_dir"]) / f"{b['out_stem']}_v091_floor_{STAMP}.json"
        if floor_path.exists():
            try:
                fa, fpq = load_arm(floor_path)
                entry["v091_floor_informational"] = {
                    "delivered_gold_rate": fa.get("delivered_gold_rate"),
                    "paired_vs_v091": paired(new_pq, fpq),
                }
            except Exception as exc:
                entry["v091_floor_informational"] = {"error": str(exc)}
        entry["lift_confirmed"] = bool(
            entry["delivered_delta"] > 0
            and entry["paired_delivered"]["net"] > 0)
        entry["status"] = "PASS" if entry["lift_confirmed"] else "FAIL"
        v["beds"][bed] = entry

    # ERB 829k: integration arm vs the frozen 2026-08-28 confirm pair.
    try:
        new_a, _ = load_arm(ERB_DIR / "receipts"
                            / f"ladder_v09x_int_v091_{STAMP}.json")
        old_base, _ = load_arm(ERB_DIR / "receipts"
                               / "ladder_v09x_w1c_baseline_full_2026-08-28.json")
        old_new, _ = load_arm(ERB_DIR / "receipts"
                              / "ladder_v09x_w1c_k20_eps_all_full_2026-08-28.json")
        e = {
            "v091_delivered": new_a["delivered_gold_rate"],
            "frozen_v090_delivered": old_base["delivered_gold_rate"],
            "frozen_wave1_delivered": old_new["delivered_gold_rate"],
            "lift_over_v090": round(new_a["delivered_gold_rate"]
                                    - old_base["delivered_gold_rate"], 4),
            "reproduce_delta_vs_wave1": round(
                new_a["delivered_gold_rate"]
                - old_new["delivered_gold_rate"], 4),
        }
        e["lift_confirmed"] = e["lift_over_v090"] > 0
        e["reproduces_wave1"] = abs(e["reproduce_delta_vs_wave1"]) <= 0.005
        e["status"] = ("PASS" if e["lift_confirmed"] and e["reproduces_wave1"]
                       else "FAIL")
        v["beds"]["erb_829k"] = e
    except Exception as exc:
        v["beds"]["erb_829k"] = {"status": "INCOMPLETE", "error": str(exc)}

    statuses = [b.get("status") for b in v["beds"].values()]
    v["all_pass"] = statuses and all(s == "PASS" for s in statuses)
    out = BENCH / "benchmarks" / "dogfood" / "receipts" \
        / f"sweep_v091_gate_{STAMP}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(v, indent=1), encoding="utf-8")
    log.info("VERDICT all_pass=%s -> %s", v["all_pass"], out)
    log.info("VERDICT detail: %s", json.dumps(v, indent=1))
    return v


def main():
    if "--dry" in sys.argv:
        preflight()
        for s in STAGES:
            log.info("DRY stage: %s", s)
        return
    preflight()
    for kind, bed, arm in STAGES:
        try:
            if kind == "ladder":
                run_ladder(bed, arm)
            elif kind == "resolve_padded":
                run_resolve_padded()
        except RuntimeError as exc:
            log.warning("stage chain aborted: %s", exc)
            break
        except Exception as exc:
            log.warning("stage %s/%s failed, continuing: %s", bed, arm, exc)
    verdict()
    log.info("SWEEP COMPLETE")


if __name__ == "__main__":
    main()
