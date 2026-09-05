"""Beta witness sweep (2026-09-04) — code beds first, then RAG beds.

Runs every stage from ONE worktree checked out at the beta head so the
receipts measure the integration branch, not any single PR. Sequential
queue, one bed at a time, so latency axes are not cross-loaded.

Stages (in order):

  code   dogfood four-repo bed        benchmarks/dogfood/run_baseline.py
  code   MULocBench 108k (seed 0)     ablation_ladder.py baseline arm
  code   RepoBench-R python_cff/cfr   repobench_r_cymatix_global.py, shipped
         cymatix.toml + legacy lexical-probe toml (June-2026 comparability)
  rag    LoCoMo 6k (seed 0)           ablation_ladder.py baseline arm
  rag    FinanceBench 74k (seed 0)    ablation_ladder.py baseline arm
  rag    EnronQA v2 85k               ablation_ladder.py baseline arm
  rag    EnronQA padded 597k          ablation_ladder.py baseline arm
  seed   MULoc / LoCoMo / FinanceBench at PYTHONHASHSEED=1 — prices the
         filename_anchor hash-order hazard (list({...})[:64]) per corpus

Env pins per stage: PYTHONHASHSEED (recorded — the frozen references left it
unpinned), PYTHONUTF8=1, PYTHONPATH=<worktree> (pins the import to the beta
tree), CYMATIX_DISABLE_LEARN=1, CYMATIX_SQLITE_CACHE_SIZE=-12582912 (the
12 GiB band every prior ladder used), CYMATIX_ENCODER_URL / CYMATIX_CONFIG
unset. Cold-bed gate: >= 1 h between manager-mediated accesses of one bed.

Every ladder receipt is annotated with a ``sweep`` block (config sha256,
seed, interpreter, sqlite, bed mtimes, wall) and the verdict pairs it
per-needle against its frozen reference receipt.

Usage:
  python -P benchmarks/dogfood/sweeps/run_sweep_beta.py            # full queue
  python -P benchmarks/dogfood/sweeps/run_sweep_beta.py --stages muloc_s0,locomo_s0
  python -P benchmarks/dogfood/sweeps/run_sweep_beta.py --verdict-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SWEEP = Path(r"F:\tmp\sweep_beta_2026-09-04")
SWEEP.mkdir(parents=True, exist_ok=True)
STAMP = "2026-09-04"
EXPECTED_HEAD = "21606a07b7d6604f5229bd8bf7def5d84c2d6540"  # origin/beta
COLD_GATE_S = 3600
RUN_TIMEOUT_S = 8 * 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LADDER = ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py"
DOGFOOD = ROOT / "benchmarks" / "dogfood" / "run_baseline.py"
REPOBENCH = ROOT / "benchmarks" / "repobench_r_cymatix_global.py"
SHIPPED = ROOT / "cymatix.toml"
W24 = ROOT / "benchmarks" / "dogfood" / "erb" / "configs" / "w24_min_delivered_12.toml"
LEGACY_PROBE = ROOT / "docs" / "benchmarks" / "cymatix_probe_lexical.toml"
CODE_RECEIPTS = ROOT / "benchmarks" / "dogfood" / "code" / "receipts"
VERDICT_OUT = ROOT / "benchmarks" / "dogfood" / "receipts" / f"sweep_beta_witness_{STAMP}.json"

# The dogfood bed + its gene_id gold live in the main checkout (gitignored).
MAIN = Path(r"F:\Projects\cymatix-context")
DOGFOOD_BED = MAIN / "genomes" / "dogfood" / "genome.db"
DOGFOOD_GOLD = MAIN / "benchmarks" / "dogfood" / "gene_ids" / "gold_by_needle.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(SWEEP / "sweep.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("sweep_beta")

D = ROOT / "benchmarks" / "dogfood"
BEDS = {
    "muloc": {
        "db": r"F:/tmp/muloc_bed/muloc.db",
        "resolved": D / "muloc" / "needles_resolved_muloc_full.json",
        "gold": D / "muloc" / "gold_by_needle_muloc.json",
        "out_dir": D / "muloc" / "receipts",
        "stem": "ladder_muloc_beta",
        "reference": D / "muloc" / "receipts" / "ladder_muloc_baseline_cold_2026-08-31.json",
        "lane": "code",
    },
    "locomo": {
        "db": r"F:/tmp/locomo_bed/locomo.db",
        "resolved": D / "locomo" / "needles_resolved_locomo_full.json",
        "gold": D / "locomo" / "gold_by_needle_locomo.json",
        "out_dir": D / "locomo" / "receipts",
        "stem": "ladder_locomo_beta",
        "reference": D / "locomo" / "receipts" / "ladder_locomo_baseline_2026-08-31.json",
        "lane": "rag",
    },
    "financebench": {
        "db": r"F:/tmp/financebench_bed/financebench.db",
        "resolved": D / "financebench" / "needles_resolved_financebench_full.json",
        "gold": D / "financebench" / "gold_by_needle_financebench.json",
        "out_dir": D / "financebench" / "receipts",
        "stem": "ladder_financebench_beta",
        "reference": D / "financebench" / "receipts" / "ladder_financebench_baseline_2026-08-31.json",
        "lane": "rag",
    },
    "enronqa_v2": {
        "db": r"F:/tmp/enronqa_bed_v2/enronqa.db",
        "resolved": D / "enronqa" / "needles_resolved_enronqa_full.json",
        "gold": D / "enronqa" / "gold_by_needle_enronqa.json",
        "out_dir": D / "enronqa" / "receipts",
        "stem": "ladder_enronqa_v2_beta",
        "reference": D / "enronqa" / "receipts" / "ladder_enronqa_v2_v091_floor_2026-08-30.json",
        "lane": "rag",
    },
    "enronqa_padded": {
        "db": r"F:/tmp/enronqa_padded_bed/enronqa_padded.db",
        "resolved": D / "enronqa_padded" / "needles_resolved_enronqa_full.json",
        "gold": D / "enronqa_padded" / "gold_by_needle_enronqa.json",
        "out_dir": D / "enronqa_padded" / "receipts",
        "stem": "ladder_enronqa_padded_beta",
        "reference": D / "enronqa_padded" / "receipts" / "ladder_enronqa_padded_v091_floor_2026-08-30.json",
        "lane": "rag",
    },
}


def _new_bed(tag: str, label: str, corpus_root: str) -> dict:
    """Second-wave code bed: built + resolved by this runner, no frozen reference."""
    return {
        "db": f"F:/tmp/{tag}_bed/{tag}.db",
        "out_dir_build": f"F:/tmp/{tag}_bed",
        "profile": tag,
        "tag": tag,
        "bench_dir": D / tag,
        "corpus_root": corpus_root,
        "resolved": D / tag / f"needles_resolved_{tag}_full.json",
        "gold": D / tag / f"gold_by_needle_{tag}.json",
        "out_dir": D / tag / "receipts",
        "stem": f"ladder_{tag}_beta",
        "reference": None,
        "lane": "code",
        "label": label,
    }


BEDS.update({
    "coderag_solutions": _new_bed("coderag_solutions",
                                  "CodeRAG-Bench programming-solutions (humaneval + mbpp)",
                                  r"F:\Projects\coderag\corpus_solutions"),
    "coderag_docs": _new_bed("coderag_docs",
                             "CodeRAG-Bench library-documentation (ds1000 + odex)",
                             r"F:\Projects\coderag\corpus_docs"),
    "cosqa": _new_bed("cosqa", "CoIR CosQA (web query -> Python function)",
                      r"F:\Projects\cosqa\corpus"),
    "swebench": _new_bed("swebench", "SWE-bench Verified (issue -> file localization)",
                         r"F:\Projects\swebench\corpus"),
})

# (stage_name, kind, spec) — sequential.
STAGES = [
    ("dogfood", "dogfood", {}),
    ("muloc_s0", "ladder", {"bed": "muloc", "seed": 0}),
    ("repobench_cff_shipped", "repobench", {"config": "python_cff", "arm": "shipped"}),
    ("repobench_cfr_shipped", "repobench", {"config": "python_cfr", "arm": "shipped"}),
    ("repobench_cff_legacy", "repobench", {"config": "python_cff", "arm": "legacy_probe"}),
    ("repobench_cfr_legacy", "repobench", {"config": "python_cfr", "arm": "legacy_probe"}),
    ("locomo_s0", "ladder", {"bed": "locomo", "seed": 0}),
    ("financebench_s0", "ladder", {"bed": "financebench", "seed": 0}),
    ("enronqa_v2_s0", "ladder", {"bed": "enronqa_v2", "seed": 0}),
    ("enronqa_padded_s0", "ladder", {"bed": "enronqa_padded", "seed": 0}),
    ("muloc_s1", "ladder", {"bed": "muloc", "seed": 1}),
    ("locomo_s1", "ladder", {"bed": "locomo", "seed": 1}),
    ("financebench_s1", "ladder", {"bed": "financebench", "seed": 1}),
    # Second wave (user-approved 2026-09-04): the non-Python RepoBench-R
    # configs — closed candidate pools, one snippet per gene, so the chunker
    # is out of the picture and only retrieval is priced. Not part of the
    # default queue; run with --stages after the RAG lane so its CPU load
    # cannot touch the RAG latency axes.
    ("repobench_java_cff_shipped", "repobench", {"config": "java_cff", "arm": "shipped"}),
    ("repobench_java_cfr_shipped", "repobench", {"config": "java_cfr", "arm": "shipped"}),
    ("repobench_java_cff_legacy", "repobench", {"config": "java_cff", "arm": "legacy_probe"}),
    ("repobench_java_cfr_legacy", "repobench", {"config": "java_cfr", "arm": "legacy_probe"}),
]
for _tag in ("coderag_solutions", "cosqa", "coderag_docs", "swebench"):
    STAGES.append((f"build_{_tag}", "build", {"bed": _tag}))
    STAGES.append((f"resolve_{_tag}", "resolve", {"bed": _tag}))
for _tag in ("coderag_solutions", "cosqa", "coderag_docs", "swebench"):
    STAGES.append((f"{_tag}_s0", "ladder", {"bed": _tag, "seed": 0}))
SECOND_WAVE = {name for name, _, _ in STAGES
               if name.startswith(("repobench_java", "build_", "resolve_"))
               or name in {"coderag_solutions_s0", "cosqa_s0", "coderag_docs_s0", "swebench_s0"}}

last_end: dict[str, float] = {}
manifest_rows: list[dict] = []
MANIFEST_TAG = ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                       text=True, timeout=30, creationflags=NO_WINDOW)
    return r.stdout.strip()


def env_for_run(seed: int) -> dict:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    env["CYMATIX_DISABLE_LEARN"] = "1"
    env["CYMATIX_SQLITE_CACHE_SIZE"] = "-12582912"
    env.pop("CYMATIX_ENCODER_URL", None)
    env.pop("CYMATIX_CONFIG", None)
    return env


def bed_mtimes(db: str) -> dict:
    out = {}
    for suffix in ("", "-wal", "-shm"):
        p = Path(db + suffix)
        out[suffix or "db"] = p.stat().st_mtime if p.exists() else None
    return out


def write_manifest(extra: dict | None = None) -> None:
    payload = {
        "stamp": STAMP,
        "runner": "benchmarks/dogfood/sweeps/run_sweep_beta.py",
        "beta_head": git("rev-parse", "HEAD"),
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "rows": manifest_rows,
    }
    if extra:
        payload.update(extra)
    (SWEEP / f"sweep_manifest{MANIFEST_TAG}.json").write_text(
        json.dumps(payload, indent=1), encoding="utf-8")


def run_stage_cmd(name: str, cmd: list[str], seed: int) -> tuple[int, float, float]:
    log_path = SWEEP / f"stage_{name}.log"
    started = time.time()
    log.info("START %s: %s", name, " ".join(cmd))
    with open(log_path, "w", encoding="utf-8") as fh:
        r = subprocess.run(cmd, cwd=str(ROOT), env=env_for_run(seed), stdout=fh,
                           stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT_S,
                           creationflags=NO_WINDOW)
    ended = time.time()
    log.info("END %s rc=%s wall=%.0fs", name, r.returncode, ended - started)
    return r.returncode, started, ended


def cold_gate(bed_key: str, db: str) -> float:
    """Block until >= COLD_GATE_S since this bed was last touched. Returns gap."""
    last = last_end.get(bed_key)
    if last is None:
        mt = [v for v in bed_mtimes(db).values() if v]
        last = max(mt) if mt else 0.0
    gap = time.time() - last
    if gap < COLD_GATE_S:
        wait = COLD_GATE_S - gap
        log.info("cold gate %s: waiting %.0fs", bed_key, wait)
        time.sleep(wait)
        gap = time.time() - last
    return gap


def preflight(allow_dirty: bool = False) -> dict:
    head = git("rev-parse", "HEAD")
    if head != EXPECTED_HEAD:
        raise SystemExit(f"HEAD {head} != expected beta head {EXPECTED_HEAD}")
    dirty = [ln for ln in git("status", "--porcelain").splitlines()
             if ln and not ln.startswith("??")]
    if dirty and not allow_dirty:
        raise SystemExit(f"tracked files modified in the worktree: {dirty}")
    sys.path.insert(0, str(ROOT))
    from cymatix_context.config import load_config  # noqa: E402

    def fields(cfg) -> dict:
        r = cfg.retrieval
        return {
            "rrf_k": r.rrf_k,
            "fusion_mode": r.fusion_mode,
            "map": dict(getattr(r, "rerank_combinator_by_class", {}) or {}),
            "floor": getattr(cfg.budget, "min_delivered_docs", 0),
            "dense": r.dense_embedding_enabled,
            "pki": getattr(r, "pki_enabled", None),
            "cover_walk": getattr(r, "cover_walk_enabled", None),
            "fts5_candidate_depth": getattr(r, "fts5_candidate_depth", None),
            "expression_tokens": cfg.budget.expression_tokens,
            "ribosome_backend": cfg.ribosome.backend,
            "splade": cfg.ingestion.splade_enabled,
            "hub_cutoff": getattr(cfg.ingestion, "entity_autolink_hub_cutoff", None),
        }

    shipped = fields(load_config(str(SHIPPED)))
    w24 = fields(load_config(str(W24)))
    assert shipped["rrf_k"] == 20, shipped
    assert shipped["floor"] == 12, shipped
    assert set(shipped["map"]) == {"arithmetic", "factual", "procedural", "multi_hop", "default"}, shipped
    assert all(v == "eps_band" for v in shipped["map"].values()), shipped
    assert shipped["dense"] is False and shipped["splade"] is False, shipped
    assert shipped["ribosome_backend"] == "none", shipped
    assert shipped == w24, (shipped, w24)
    info = {
        "beta_head": head,
        "shipped_config": str(SHIPPED),
        "shipped_config_sha256": sha256_file(SHIPPED),
        "w24_reference_config_sha256": sha256_file(W24),
        "legacy_probe_config_sha256": sha256_file(LEGACY_PROBE),
        "resolved_fields": shipped,
        "fields_identical_to_w24_reference": True,
        "dirty_tracked_files": dirty,
    }
    log.info("preflight ok: %s", json.dumps(info))
    return info


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------

def annotate(receipt: Path, block: dict) -> None:
    d = json.loads(receipt.read_text(encoding="utf-8"))
    d["sweep"] = block
    receipt.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


def stage_ladder(name: str, spec: dict, pre: dict) -> dict:
    bed = BEDS[spec["bed"]]
    seed = spec["seed"]
    out = bed["out_dir"] / f"{bed['stem']}_seed{seed}_{STAMP}.json"
    gap = cold_gate(spec["bed"], bed["db"])
    before = bed_mtimes(bed["db"])
    cmd = [sys.executable, "-P", str(LADDER),
           "--genome", bed["db"],
           "--resolved", str(bed["resolved"]),
           "--gold", str(bed["gold"]),
           "--limit", "0", "--k", "12", "--arms", "baseline",
           "--config", str(SHIPPED), "--per-query",
           "--stamp", STAMP, "--out", str(out)]
    rc, started, ended = run_stage_cmd(name, cmd, seed)
    last_end[spec["bed"]] = ended
    row = {"stage": name, "kind": "ladder", "bed": spec["bed"], "seed": seed,
           "rc": rc, "out": str(out), "started": started, "ended": ended,
           "wall_s": round(ended - started, 1), "cold_gap_s": round(gap)}
    if rc == 0 and out.exists():
        annotate(out, {
            "stamp": STAMP, "runner": "benchmarks/dogfood/sweeps/run_sweep_beta.py",
            "lane": bed["lane"], "beta_head": pre["beta_head"],
            "config_sha256": pre["shipped_config_sha256"],
            "config_fields_identical_to_w24_reference": True,
            "PYTHONHASHSEED": str(seed), "python": sys.version,
            "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
            "CYMATIX_SQLITE_CACHE_SIZE": "-12582912",
            "bed_mtimes_before": before, "bed_mtimes_after": bed_mtimes(bed["db"]),
            "cold_gap_s": round(gap), "wall_s": round(ended - started, 1),
            "reference_receipt": (str(bed["reference"].relative_to(ROOT)).replace("\\", "/")
                                  if bed.get("reference") else None),
            "reference_seed": "unpinned (process default)" if bed.get("reference") else None,
            "note": ("baseline arm only, shipped beta cymatix.toml; rank axes are "
                     "deterministic given PYTHONHASHSEED (filename_anchor.py:164 "
                     "list({...})[:64] makes stem selection hash-order dependent "
                     "above 64 distinct query terms); latency is wall ms on a "
                     "shared box, sequential queue, one bed at a time."),
        })
        d = json.loads(out.read_text(encoding="utf-8"))
        base = d["arms"][0]
        row["delivered_gold_rate"] = base.get("delivered_gold_rate")
        row["recall_at_k"] = base.get("recall_at_k")
        row["final_recall_at_k"] = base.get("final_recall_at_k")
        row["latency_ms"] = base.get("latency_ms")
    return row


def stage_dogfood(name: str, spec: dict, pre: dict) -> dict:
    out = CODE_RECEIPTS / f"dogfood_baseline_beta_{STAMP}.json"
    CODE_RECEIPTS.mkdir(parents=True, exist_ok=True)
    before = bed_mtimes(str(DOGFOOD_BED))
    cmd = [sys.executable, "-P", str(DOGFOOD),
           "--genome", str(DOGFOOD_BED), "--gold", str(DOGFOOD_GOLD),
           "--k", "12", "--out", str(out)]
    rc, started, ended = run_stage_cmd(name, cmd, 0)
    row = {"stage": name, "kind": "dogfood", "rc": rc, "out": str(out),
           "started": started, "ended": ended, "wall_s": round(ended - started, 1)}
    if rc == 0 and out.exists():
        annotate(out, {
            "stamp": STAMP, "runner": "benchmarks/dogfood/sweeps/run_sweep_beta.py",
            "lane": "code", "beta_head": pre["beta_head"],
            "config": "auto-discovered worktree cymatix.toml (run_baseline.py load_config())",
            "config_sha256": pre["shipped_config_sha256"],
            "PYTHONHASHSEED": "0", "python": sys.version, "sqlite": sqlite3.sqlite_version,
            "bed": str(DOGFOOD_BED), "bed_bytes": DOGFOOD_BED.stat().st_size,
            "bed_mtimes_before": before, "bed_mtimes_after": bed_mtimes(str(DOGFOOD_BED)),
            "gold": str(DOGFOOD_GOLD), "wall_s": round(ended - started, 1),
            "note": ("four-repo dogfood code bed (cymatix-context, driftwatch, "
                     "scorerift, MaxExpressKit), 18 value-pinning needles, gene_id "
                     "set membership scoring; NOT paired against the July receipts "
                     "(pre-wave-1 defaults, k=50 run) — standalone beta receipt"),
        })
        d = json.loads(out.read_text(encoding="utf-8"))
        row["rollup"] = d["rollup"]
    return row


def stage_repobench(name: str, spec: dict, pre: dict) -> dict:
    cfg_path = SHIPPED if spec["arm"] == "shipped" else LEGACY_PROBE
    out = CODE_RECEIPTS / f"repobench_r_{spec['config']}_global_{spec['arm']}_beta_{STAMP}.json"
    CODE_RECEIPTS.mkdir(parents=True, exist_ok=True)
    gdir = SWEEP / f"repobench_genome_{spec['config']}_{spec['arm']}"
    cmd = [sys.executable, "-P", str(REPOBENCH),
           "--config", spec["config"], "--levels", "easy,hard", "--limit", "0",
           "--cymatix-config", str(cfg_path), "--genome-dir", str(gdir),
           "--out", str(out)]
    rc, started, ended = run_stage_cmd(name, cmd, 0)
    row = {"stage": name, "kind": "repobench", "config": spec["config"],
           "arm": spec["arm"], "rc": rc, "out": str(out), "started": started,
           "ended": ended, "wall_s": round(ended - started, 1)}
    if rc == 0 and out.exists():
        gdb = gdir / "genome.db"
        annotate(out, {
            "stamp": STAMP, "runner": "benchmarks/dogfood/sweeps/run_sweep_beta.py",
            "lane": "code", "beta_head": pre["beta_head"],
            "cymatix_config_sha256": sha256_file(cfg_path),
            "PYTHONHASHSEED": "0", "python": sys.version, "sqlite": sqlite3.sqlite_version,
            "genome_bytes": gdb.stat().st_size if gdb.exists() else None,
            "wall_s": round(ended - started, 1),
            "dumps": "benchmarks/results/repobench_r_<config>_<level>_n200.json "
                     "(2026-06-13 per-example dumps, copied from the main checkout)",
            "reference": "benchmarks/results/repobench_r_<config>_global_20260613T17*.json "
                         "(helix 0.6.3-era frozen run, main checkout, gitignored)",
            "note": ("fresh ingest of the deduped candidate union under the named "
                     "config (shipped = beta cymatix.toml incl. entity_autolink_hub_cutoff "
                     "200 at ingest; legacy_probe = additive-fusion lexical probe template "
                     "for June-2026 comparability). B = pool-rank acc@k vs the foils, "
                     "C = global-rank recall@k. Matched floored-IDF BM25 foil inside."),
        })
        d = json.loads(out.read_text(encoding="utf-8"))
        row["levels"] = d["levels"]
    return row


def stage_build(name: str, spec: dict, pre: dict) -> dict:
    """Ingest a second-wave corpus with scripts/build_fixture_matrix.py."""
    bed = BEDS[spec["bed"]]
    cmd = [sys.executable, "-P", str(ROOT / "scripts" / "build_fixture_matrix.py"),
           "--profile", bed["profile"], "--out-dir", bed["out_dir_build"],
           "--parallel", "--workers", "4"]
    os.environ["CYMATIX_BFM_HUB_CUTOFF"] = "200"    # beta shipped default at ingest
    os.environ["CYMATIX_BFM_DENSE_BACKFILL"] = "0"  # parity with every other bench bed (dense_coverage 0)
    try:
        rc, started, ended = run_stage_cmd(name, cmd, 0)
    finally:
        os.environ.pop("CYMATIX_BFM_HUB_CUTOFF", None)
        os.environ.pop("CYMATIX_BFM_DENSE_BACKFILL", None)
    last_end[spec["bed"]] = ended          # the build is the bed's last touch -> cold gate
    row = {"stage": name, "kind": "build", "bed": spec["bed"], "rc": rc,
           "started": started, "ended": ended, "wall_s": round(ended - started, 1),
           "ingest_c": 4, "hub_cutoff": 200, "dense_backfill": 0}
    man = Path(bed["out_dir_build"]) / "manifest.json"
    if man.exists():
        t = json.loads(man.read_text(encoding="utf-8"))["targets"].get(bed["profile"], {})
        row.update({k: t.get(k) for k in ("mode", "tagger_version", "files", "total_genes",
                                          "skipped", "errors", "elapsed_s", "bytes",
                                          "dense_coverage")})
    return row


def stage_resolve(name: str, spec: dict, pre: dict) -> dict:
    """Join gold paths to gene_ids after the build (read-only sqlite, no manager)."""
    bed = BEDS[spec["bed"]]
    cmd = [sys.executable, "-P", str(ROOT / "scripts" / "resolve_bench_needles.py"),
           "--tag", bed["tag"], "--db", bed["db"], "--bench-dir", str(bed["bench_dir"]),
           "--corpus-root", bed["corpus_root"]]
    rc, started, ended = run_stage_cmd(name, cmd, 0)
    row = {"stage": name, "kind": "resolve", "bed": spec["bed"], "rc": rc,
           "started": started, "ended": ended, "wall_s": round(ended - started, 1)}
    meta = bed["bench_dir"] / f"needles_{bed['tag']}_resolve_meta.json"
    if meta.exists():
        m = json.loads(meta.read_text(encoding="utf-8"))
        row.update({k: m.get(k) for k in ("needles_total", "needles_resolvable",
                                          "gold_files_size_gated", "twin_fallbacks")})
        row["dropped_n"] = len(m.get("dropped", []))
    return row


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

PAIR_FIELDS = ["delivered_gold", "recall_hit", "final_recall_hit", "rank_of_first_gold",
               "final_rank_of_first_gold", "delivered_gold_rank", "delivered_count",
               "gold_ranks", "floor_appended", "gate_kept", "splice_n_candidates",
               "cover_walk"]
SUMMARY_FIELDS = ["recall_at_k", "final_recall_at_k", "delivered_gold_rate",
                  "mean_rank_of_gold", "median_rank_of_gold", "gold_found_queries",
                  "delivered_gold_queries", "pool_delivery_gap", "n_queries"]


def _baseline(receipt: Path) -> dict:
    d = json.loads(receipt.read_text(encoding="utf-8"))
    rows = [r for r in d["arms"] if r.get("arm") == "baseline"]
    if not rows:
        raise ValueError(f"no baseline arm in {receipt}")
    return rows[0]


def pair(new: Path, ref: Path, label: str) -> dict:
    a, b = _baseline(new), _baseline(ref)
    apq, bpq = a["per_query"], b["per_query"]
    names_a = [r["needle"] for r in apq]
    names_b = [r["needle"] for r in bpq]
    if names_a != names_b:
        return {"label": label, "new": str(new), "ref": str(ref),
                "error": "needle order differs — per-needle pairing refused",
                "n_new": len(names_a), "n_ref": len(names_b)}
    field_diffs = {f: 0 for f in PAIR_FIELDS}
    gained, lost, rank_moves = [], [], []
    for ra, rb in zip(apq, bpq):
        for f in PAIR_FIELDS:
            if ra.get(f) != rb.get(f):
                field_diffs[f] += 1
        da, db = bool(ra.get("delivered_gold")), bool(rb.get("delivered_gold"))
        if da and not db:
            gained.append(ra["needle"])
        elif db and not da:
            lost.append(ra["needle"])
        if ra.get("rank_of_first_gold") != rb.get("rank_of_first_gold"):
            rank_moves.append({"needle": ra["needle"], "ref": rb.get("rank_of_first_gold"),
                               "new": ra.get("rank_of_first_gold")})
    lat_a = [r["wall_ms"] for r in apq if isinstance(r.get("wall_ms"), (int, float))]
    lat_b = [r["wall_ms"] for r in bpq if isinstance(r.get("wall_ms"), (int, float))]
    summary = {f: {"new": a.get(f), "ref": b.get(f),
                   "delta": (round(a[f] - b[f], 6) if isinstance(a.get(f), (int, float))
                             and isinstance(b.get(f), (int, float)) else None)}
               for f in SUMMARY_FIELDS}
    total_diff = sum(field_diffs.values())
    return {
        "label": label,
        "new": str(new.relative_to(ROOT)).replace("\\", "/"),
        "ref": str(ref.relative_to(ROOT)).replace("\\", "/"),
        "n": len(apq),
        "summary": summary,
        "per_needle_field_diffs": field_diffs,
        "needles_with_any_rank_axis_diff": len({m["needle"] for m in rank_moves}),
        "delivered_gained": gained,
        "delivered_lost": lost,
        "paired_delivered": f"+{len(gained)}/-{len(lost)}",
        "rank_moves_first_gold": rank_moves[:200],
        "latency_ms": {"new_p50": round(statistics.median(lat_a), 1) if lat_a else None,
                       "ref_p50": round(statistics.median(lat_b), 1) if lat_b else None,
                       "citable": False,
                       "why": "environment-loaded wall ms; rank axes are the citable part"},
        "verdict": ("EXACT_REPRODUCE" if total_diff == 0 else
                    "RANK_AXES_MOVED (see field diffs; seed-dependent lanes expected "
                    "to jitter where queries exceed 64 distinct terms)"),
    }


def verdict(pre: dict) -> dict:
    pairs = []
    beds_summary = {}
    for key, bed in BEDS.items():
        s0 = bed["out_dir"] / f"{bed['stem']}_seed0_{STAMP}.json"
        s1 = bed["out_dir"] / f"{bed['stem']}_seed1_{STAMP}.json"
        if s0.exists():
            b = _baseline(s0)
            beds_summary[key] = {f: b.get(f) for f in SUMMARY_FIELDS}
            beds_summary[key]["latency_ms"] = b.get("latency_ms")
            beds_summary[key]["receipt"] = str(s0.relative_to(ROOT)).replace("\\", "/")
            beds_summary[key]["has_frozen_reference"] = bool(bed.get("reference"))
        if s0.exists() and bed.get("reference") and bed["reference"].exists():
            pairs.append(pair(s0, bed["reference"], f"{key}: beta seed0 vs frozen reference"))
        if s0.exists() and s1.exists():
            pairs.append(pair(s1, s0, f"{key}: beta seed1 vs beta seed0 (hash-order sensitivity)"))
    code = {}
    for p in sorted(CODE_RECEIPTS.glob(f"*_{STAMP}.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        code[p.name] = d.get("rollup") or d.get("levels")
    out = {
        "stamp": STAMP,
        "runner": "benchmarks/dogfood/sweeps/run_sweep_beta.py",
        "preflight": pre,
        "python": sys.version, "sqlite": sqlite3.sqlite_version,
        "pairs": pairs,
        "beds": beds_summary,
        "code_receipts": code,
        "manifest": str(SWEEP / "sweep_manifest.json"),
    }
    VERDICT_OUT.parent.mkdir(parents=True, exist_ok=True)
    VERDICT_OUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    log.info("verdict written: %s", VERDICT_OUT)
    for p in pairs:
        log.info("  %s -> %s (%s)", p["label"], p.get("verdict"), p.get("paired_delivered"))
    return out


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stages", default="", help="comma list of stage names (default all)")
    ap.add_argument("--verdict-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="tolerate modified tracked files (recorded in the receipts)")
    ap.add_argument("--tag", default="", help="suffix for this run's manifest file")
    args = ap.parse_args()

    global MANIFEST_TAG
    MANIFEST_TAG = f"_{args.tag}" if args.tag else ""
    pre = preflight(allow_dirty=args.allow_dirty)
    if args.verdict_only:
        verdict(pre)
        return 0
    wanted = {s.strip() for s in args.stages.split(",") if s.strip()}
    queue = [s for s in STAGES if (s[0] in wanted) if wanted] if wanted else             [s for s in STAGES if s[0] not in SECOND_WAVE]
    log.info("queue: %s", [s[0] for s in queue])
    if args.dry_run:
        return 0
    write_manifest({"preflight": pre})
    for name, kind, spec in queue:
        try:
            if kind == "ladder":
                row = stage_ladder(name, spec, pre)
            elif kind == "dogfood":
                row = stage_dogfood(name, spec, pre)
            elif kind == "repobench":
                row = stage_repobench(name, spec, pre)
            elif kind == "build":
                row = stage_build(name, spec, pre)
            elif kind == "resolve":
                row = stage_resolve(name, spec, pre)
            else:
                raise ValueError(kind)
        except Exception as exc:  # noqa: BLE001 — one failed stage must not kill the queue
            log.exception("stage %s failed", name)
            row = {"stage": name, "kind": kind, "error": repr(exc)}
        manifest_rows.append(row)
        write_manifest({"preflight": pre})
    verdict(pre)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
