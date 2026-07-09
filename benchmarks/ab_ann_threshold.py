r"""Live A/B: dense-admission threshold on the retrieval pipeline (Phase 1a follow-up).

The offline sweep (benchmarks/sweep_ann_sigma.py) showed the shipped
``ann_similarity_threshold = 0.58`` admits only ~7% of golds on
query-doc geometry (it was calibrated on doc-doc pairs). This driver
tests whether that offline gap survives the FULL pipeline — where
``dense_pool_floor_genes`` (#214) already force-admits the top-N dense
hits regardless of threshold, and RRF fuses dense by rank.

Cells (all fusion_mode=rrf, the shipped default since PR #247):
  * dense_off        — lexical tiers only (reference floor)
  * dense_on @ 0.58  — dense on, shipped threshold
  * dense_on @ 0.47  — dense on, candidate threshold (query-doc p99)

Read the result:
  * 0.47 > 0.58 on gold_delivered/content  -> the threshold gates real
    golds past the pool floor; ship the lower value (live-confirmed).
  * 0.47 == 0.58                            -> dense_pool_floor already
    catches them; the threshold recalibration is cosmetic under the
    shipped floor (still worth fixing for floor=0 deployments + honesty).

Server lifecycle + scoring reuse scripts/bench_chain/s3_fts_depth_sweep.py
verbatim (uvicorn on the bed, HELIX_DISABLE_LEARN=1, retrieval-only
/context, bench_needle.check_gold_delivery). Dense on => the BGE-M3 query
encoder loads (GPU), so this is seconds-to-minutes per cell, not free.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # py<3.11
    import tomli as tomllib  # type: ignore
import tomli_w

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_S3 = _REPO_ROOT / "scripts" / "bench_chain"
if str(_S3) not in sys.path:
    sys.path.insert(0, str(_S3))

import bench_needle  # noqa: E402
import s3_fts_depth_sweep as s3  # noqa: E402  (reuse server + scoring)


def _write_cell_config(base_config: Path, bed_db: Path, *, dense_on: bool,
                       threshold: float, dest: Path) -> None:
    with base_config.open("rb") as fh:
        cfg = tomllib.load(fh)
    r = cfg.setdefault("retrieval", {})
    r["fusion_mode"] = "rrf"
    r["dense_embedding_enabled"] = bool(dense_on)
    r["ann_threshold_mode"] = "absolute"
    r["ann_similarity_threshold"] = float(threshold)
    cfg.setdefault("genome", {})["path"] = str(bed_db)
    cfg.setdefault("ingestion", {})["dense_embed_on_ingest"] = False  # vectors preexist
    with dest.open("wb") as fh:
        tomli_w.dump(cfg, fh)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bed-db", default="genomes/bench/matrix/xl_clean.db")
    ap.add_argument("--bed-label", default="xl_clean")
    ap.add_argument("--base-config",
                    default="docs/benchmarks/helix_probe_lexical.toml")
    ap.add_argument("--port", type=int, default=11440)
    ap.add_argument("--limit", type=int, default=0, help="first N needles (0=all)")
    ap.add_argument("--out",
                    default="benchmarks/results/ab_ann_threshold_xl_clean.json")
    args = ap.parse_args()

    bed_db = Path(args.bed_db).resolve()
    base_config = Path(args.base_config).resolve()
    url = f"http://127.0.0.1:{args.port}"
    needles = list(bench_needle.NEEDLES)
    if args.limit:
        needles = needles[:args.limit]

    cells_spec = [
        {"label": "dense_off", "dense_on": False, "threshold": 0.58},
        {"label": "dense_on@0.58", "dense_on": True, "threshold": 0.58},
        {"label": "dense_on@0.47", "dense_on": True, "threshold": 0.47},
    ]

    cfg_dir = _REPO_ROOT / "benchmarks" / "logs" / "ann_threshold_cells"
    log_dir = _REPO_ROOT / "benchmarks" / "logs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    results = {"benchmark": "ab_ann_threshold", "bed_db": str(bed_db),
               "bed_label": args.bed_label, "n_needles": len(needles),
               "fusion_mode": "rrf", "cells": [], "errors": []}

    for spec in cells_spec:
        print(f"=== CELL {spec['label']} ===", file=sys.stderr)
        cell_cfg = cfg_dir / f"probe_{spec['label'].replace('@','_')}.toml"
        _write_cell_config(base_config, bed_db, dense_on=spec["dense_on"],
                           threshold=spec["threshold"], dest=cell_cfg)
        log_path = log_dir / f"ann_thr_srv_{spec['label'].replace('@','_')}.log"
        proc = s3._start_server(bed_db, cell_cfg, args.port, log_path)
        try:
            if not s3._wait_healthy(url, timeout_s=120):
                results["errors"].append(f"{spec['label']}: server unhealthy")
                continue
            cell = s3._run_cell(url, needles)
            cell.update({k: spec[k] for k in ("label", "dense_on", "threshold")})
            results["cells"].append(cell)
            print(f"  {spec['label']}: gold={cell['gold_delivered_rate']} "
                  f"content={cell['content_has_answer_rate']} "
                  f"body={cell['body_has_answer_rate']}", file=sys.stderr)
        finally:
            s3._stop_server(proc, url)
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print("\nCURVE (gold_delivered / content_has_answer):")
    for c in results["cells"]:
        print(f"  {c['label']:<16} gold={c['gold_delivered_rate']:.2f} "
              f"content={c['content_has_answer_rate']:.2f} "
              f"body={c['body_has_answer_rate']:.2f}")
    print(f"-> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
