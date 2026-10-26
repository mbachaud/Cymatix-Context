"""Full ERB 500k lean c580 bed: build -> resolve -> ladder -> compare.

Stages (each skipped when its output exists, so a rerun resumes):
  1. build   scripts/build_erb_blob_v09x.py, full corpus, workers 6 under the
             ERB equivalence receipt, CYMATIX_BFM_CHUNK_CHARS=580 and the lean
             writes (no PKI rows, no COVER auto-link, no prefix-redundant
             indexes). A partial builder bed is resumed (--resume, file-level).
  2. resolve scripts/resolve_erb_needles.py --tag grain580 (gold gene ids are
             content hashes, so they are re-derived from the bench's own
             expected_doc_ids + this bed's source paths).
  3. ladder  baseline arm, configs/grain_c580_charcap.toml (caps only: no seat
             floor, ~18k-char packet, cap 83, char tier caps), k=83, with the
             release-witness env pins.
  4. compare against the caps-only c4000 reference on the 947k witness bed,
             joining needles by QUERY TEXT (bank-offset hazard), plus the
             v0.11.3 shipped witness.

Holds a PID claim on the run dir and a keep-awake request. Logs per stage.

Usage: python -P benchmarks/dogfood/grain_ladder/run_erb_full_c580.py
"""
from __future__ import annotations

import json
import logging
import os
import statistics as st
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from summarize import miss_breakdown  # noqa: E402

RUN = Path(r"F:\tmp\grain_erb_full_c580_2026-10-16")
BED_DIR = RUN / "bed"
BUILT = BED_DIR / "enterprise_rag_500k.db"      # builder's working file
FINAL = RUN / "erb_full_c580_lean.db"            # wrapper --out
ERB = ROOT / "benchmarks" / "dogfood" / "erb"
TAG = "grain580"
CONFIG = HERE / "configs" / "grain_c580_charcap.toml"
K = 83
GATE = Path(r"F:\Projects\cymatix-receipts\mirror\benchmarks\dogfood\receipts\ingest_equivalence_erb.json")
REF = Path(r"F:\tmp\grain_erb_ref_c4000_2026-10-16\ladder_erb947k_grain_c4000_charcap_2026-10-16.json")
WITNESS = Path(r"F:\tmp\release-witness-v0113\ladder_v0113-merged-witness-trace-2026-10-11_947k.json")
LADDER_OUT = RUN / "ladder_erb_full_c580_charcap_2026-10-16.json"
COMPARE_OUT = HERE / "erb_full_c580_vs_c4000_reference_2026-10-16.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LEAN = {"CYMATIX_BFM_PKI_WRITE": "0", "CYMATIX_BFM_ENTITY_AUTOLINK": "0", "CYMATIX_LEAN_INDEXES": "1"}

RUN.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(RUN / "run.log", encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("erb_full_c580")


def env(extra: dict | None = None) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith(("CYMATIX_", "HEADROOM_"))}
    e.update({"PYTHONHASHSEED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT),
              "CYMATIX_DISABLE_LEARN": "1"})
    e.update(LEAN)  # lean-index skip must also hold when the ladder opens the bed
    e.update(extra or {})
    return e


def run(name: str, cmd: list, extra: dict | None = None) -> int:
    log.info("START %s: %s", name, " ".join(map(str, cmd)))
    t0 = time.time()
    with open(RUN / f"stage_{name}.log", "a", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, cwd=str(ROOT), env=env(extra), stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW).returncode
    log.info("END %s rc=%d wall=%.0fs", name, rc, time.time() - t0)
    with open(RUN / "progress.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"stage": name, "rc": rc, "wall_s": round(time.time() - t0),
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    return rc


def claim() -> None:
    pid = RUN / "runner.pid"
    if pid.exists():
        old = pid.read_text().strip()
        out = subprocess.run(["tasklist", "/FI", f"PID eq {old}", "/NH"], capture_output=True,
                             text=True, creationflags=NO_WINDOW).stdout
        if old and old in out:
            raise SystemExit(f"runner {old} still alive on {RUN}")
    pid.write_text(str(os.getpid()))


def by_query(receipt: Path, resolved: Path) -> dict:
    """query text -> per-needle record (names differ across banks)."""
    q = {n["name"]: n["query"] for n in json.loads(resolved.read_text(encoding="utf-8"))["needles"]}
    pq = json.loads(receipt.read_text(encoding="utf-8"))["arms"][0]["per_query"]
    return {q[r["needle"]]: r for r in pq if r["needle"] in q}


def compare() -> None:
    lad = json.loads(LADDER_OUT.read_text(encoding="utf-8"))
    c580 = by_query(LADDER_OUT, ERB / f"needles_resolved_{TAG}_full.json")
    ref = by_query(REF, ERB / "needles_resolved_v09x_full.json")
    wit = by_query(WITNESS, ERB / "needles_resolved_v09x_full.json")
    common = sorted(set(c580) & set(ref))

    def pair(a, b):
        return {"wins": sum(1 for q in common if a[q].get("delivered_gold") and not b[q].get("delivered_gold")),
                "losses": sum(1 for q in common if b[q].get("delivered_gold") and not a[q].get("delivered_gold"))}

    def cell(recs, k):
        rs = [recs[q] for q in common]
        chars = [r.get("delivered_chars") for r in rs if r.get("delivered_chars") is not None]
        return {"delivered_gold": round(sum(r.get("delivered_gold", 0) for r in rs) / len(rs), 4),
                "chars_mean": round(st.mean(chars)) if chars else None,
                "misses": miss_breakdown(rs, k) if chars else None}

    out = {
        "c580_receipt": str(LADDER_OUT), "c580_git_sha": lad.get("git_sha"), "c580_config": str(CONFIG),
        "c580_bed": str(FINAL), "reference_receipt": str(REF), "witness_receipt": str(WITNESS),
        "join": "query text", "n_common": len(common),
        "n_c580_only": len(set(c580) - set(ref)), "n_ref_only": len(set(ref) - set(c580)),
        "c580_lean_charcap": cell(c580, K), "c4000_charcap_reference": cell(ref, 12),
        "witness_shipped": cell(wit, 12) if set(common) <= set(wit) else None,
        "paired_c580_vs_reference": pair(c580, ref),
        "paired_c580_vs_witness": pair(c580, wit) if set(common) <= set(wit) else None,
    }
    COMPARE_OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    log.info("compare written: %s", COMPARE_OUT)


def main() -> int:
    global CONFIG, LADDER_OUT, COMPARE_OUT
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--suffix", default="", help='e.g. "widecap": separate ladder/compare outputs')
    ap.add_argument("--trace-level", default="", choices=("", "metadata", "full"))
    a = ap.parse_args()
    CONFIG = Path(a.config)
    if a.suffix:
        LADDER_OUT = RUN / f"ladder_erb_full_c580_{a.suffix}_2026-10-17.json"
        COMPARE_OUT = HERE / f"erb_full_c580_{a.suffix}_vs_c4000_reference_2026-10-17.json"
    trace_args = (["--trace-dir", str(RUN / f"traces_{a.suffix or 'charcap'}"), "--trace-level", a.trace_level]
                  if a.trace_level else [])
    done = RUN / f"DONE{('_' + a.suffix) if a.suffix else ''}"
    claim()
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    if not FINAL.exists():
        BED_DIR.mkdir(exist_ok=True)
        cmd = [sys.executable, str(ROOT / "scripts" / "build_erb_blob_v09x.py"), "--out", str(FINAL),
               "--out-dir", str(BED_DIR), "--workers", "6", "--equivalence-receipt", str(GATE),
               "--notes", "grain ladder ERB full lean c580 (no PKI, no COVER autolink, lean indexes)"]
        if BUILT.exists():
            cmd.append("--resume")
        if run("build", cmd, {"CYMATIX_BFM_CHUNK_CHARS": "580"}) != 0 or not FINAL.exists():
            log.error("build failed; rerun this script to resume")
            return 1
    if not (ERB / f"needles_resolved_{TAG}_full.json").exists():
        if run("resolve", [sys.executable, str(ROOT / "scripts" / "resolve_erb_needles.py"),
                           "--db", str(FINAL), "--tag", TAG]) != 0:
            return 1
    if not LADDER_OUT.exists():
        cmd = [sys.executable, "-P", str(ERB / "ablation_ladder.py"), "--genome", str(FINAL),
               "--resolved", str(ERB / f"needles_resolved_{TAG}_full.json"),
               "--gold", str(ERB / f"gold_by_needle_{TAG}.json"), "--limit", "0", "--k", str(K),
               "--arms", "baseline", "--config", str(CONFIG), "--per-query", "--rank-dump", "200",
               "--stamp", "2026-10-16", "--out", str(LADDER_OUT)] + trace_args
        if run("ladder" + (f"_{a.suffix}" if a.suffix else ""), cmd, {"CYMATIX_SQLITE_CACHE_SIZE": "-12582912"}) != 0 or not LADDER_OUT.exists():
            return 1
    compare()
    done.write_text("ok", encoding="utf-8")
    log.info("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
