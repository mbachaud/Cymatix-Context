r"""s3_fts_depth_sweep.py -- Run-2 retrieval sweep for issue #205 (A4/B2).

Run-1 (the SIKE bedsweep) found the xl bed retrieval-capped, NOT
contamination-capped: decontaminating xl left gold_delivered_rate at 0.62
(~= the old contaminated 0.64), while the two enterprise_rag beds sat at
0.84. The open question Run-2 answers:

    Is xl's ceiling FTS candidate-pool STARVATION (A4 -- gold ranks below
    the 48-row FTS fetch, so it never enters tier scoring) or RANK SQUEEZE
    (B2 -- gold enters the pool but the tier scoring can't float it into
    the delivered top-K)?

The probe is a clean 2-axis sweep on ONE bed:

    fts5_candidate_depth  in {48, 200, 500}   (48 == legacy max_genes*4)
    fusion_mode           in {additive, rrf}

measured by gold_delivered_rate over the 50 curated SIKE needles. This is
RETRIEVAL-ONLY: no answer model runs (gold_delivered is a property of
/context, model-independent), so the whole 6-cell sweep is seconds-per-cell,
free, and GPU-free.

Reading the curve:
  * gold_delivered RISES with depth  -> A4 pool starvation was the cap;
    a deeper FTS fetch lets starved golds enter scoring. Fix: ship a larger
    default fts5_candidate_depth (or corpus-size-scaled) for big beds.
  * gold_delivered FLAT across depth -> B2 rank squeeze; golds already
    enter the pool but score below the cut. Fix lives in the tier weights /
    fusion, not the pool size. (The knob's unit test proves the FTS fetch
    depth actually changed, so a flat curve is a real B2 signal, not a
    no-op knob.)
  * additive vs rrf: which fusion better preserves gold rank at each depth.

Server lifecycle mirrors scripts/bench_chain/s2_sike_bed_sweep.ps1
(Start-HelixOnBed): uvicorn on the bed via HELIX_GENOME_PATH, the lexical
probe profile via HELIX_CONFIG (dense/splade/cymatics OFF -> no GPU
contention), HELIX_DISABLE_LEARN=1 (read-only serve, no echo genes). Each
cell rewrites the probe TOML's [retrieval] block with that cell's depth +
fusion (tomllib read / tomli_w write -- no string surgery).

Scoring reuses benchmarks/bench_needle.check_gold_delivery verbatim and
replicates find_needle's Step-1 gold_delivered logic exactly, so the
numbers are directly comparable to Run-1's retrieval.gold_delivered_rate.

OUTPUT: a single JSON (checkpointed after every cell) --
  benchmarks/results/sike_fts_depth_sweep_<bed>.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import tomli_w

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_BENCH_DIR = _REPO_ROOT / "benchmarks"
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))

import httpx  # noqa: E402  (repo dep)

import bench_needle  # noqa: E402  (NEEDLES + check_gold_delivery)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Per-cell config: rewrite the probe TOML's [retrieval] block.
# ---------------------------------------------------------------------------

def _write_cell_config(base_config: Path, bed_db: Path, depth: int,
                       fusion: str, dest: Path) -> None:
    """Clone the base probe profile, overriding only the two swept keys +
    the genome path. tomllib read / tomli_w write keeps the full lexical
    profile intact (no string surgery, no silent key drift)."""
    with base_config.open("rb") as fh:
        cfg = tomllib.load(fh)
    cfg.setdefault("retrieval", {})
    cfg["retrieval"]["fts5_candidate_depth"] = int(depth)
    cfg["retrieval"]["fusion_mode"] = str(fusion)
    # Pin the bed here too; HELIX_GENOME_PATH env also points at it (env wins
    # in the loader, but keeping them equal avoids any ambiguity on inspect).
    cfg.setdefault("genome", {})
    cfg["genome"]["path"] = str(bed_db)
    with dest.open("wb") as fh:
        tomli_w.dump(cfg, fh)


# ---------------------------------------------------------------------------
# Server lifecycle (uvicorn on one bed, lexical probe profile, read-only).
# ---------------------------------------------------------------------------

def _wait_healthy(url: str, timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _wait_port_free(url: str, timeout_s: int = 20) -> None:
    """Best-effort: wait until /health stops answering (prev server gone)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            httpx.get(f"{url}/health", timeout=2)
        except Exception:
            return
        time.sleep(0.5)


def _start_server(bed_db: Path, cell_config: Path, port: int,
                  log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["HELIX_GENOME_PATH"] = str(bed_db)
    env["HELIX_CONFIG"] = str(cell_config)
    env["HELIX_DISABLE_LEARN"] = "1"          # read-only serve (gap A2)
    env.pop("HELIX_USE_SHARDS", None)         # single-file bed, not sharded
    args = [sys.executable, "-m", "uvicorn", "helix_context._asgi:app",
            "--host", "127.0.0.1", "--port", str(port)]
    log_fh = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        args, stdout=log_fh, stderr=subprocess.STDOUT,
        env=env, cwd=str(_REPO_ROOT), creationflags=NO_WINDOW,
    )


def _stop_server(proc: subprocess.Popen, url: str) -> None:
    if proc is None:
        return
    try:
        if os.name == "nt":
            # /T kills the process tree (uvicorn reloader / any children).
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
    except Exception:
        pass
    _wait_port_free(url, timeout_s=15)


# ---------------------------------------------------------------------------
# Retrieval scoring: gold_delivered, model-independent (Run-1 parity).
# ---------------------------------------------------------------------------

def _score_needle(client: httpx.Client, url: str, needle: dict) -> dict:
    """POST /context (decoder off, delivery-elision off) and compute
    gold_delivered EXACTLY as bench_needle.find_needle Step 1 does."""
    t0 = time.perf_counter()
    try:
        resp = client.post(f"{url}/context", json={
            "query": needle["query"],
            "decoder_mode": "none",
            "ignore_delivered": True,
        })
    except Exception as exc:
        return {"name": needle["name"], "status": "error",
                "error": str(exc)[:200], "gold_delivered": False,
                "latency_s": round(time.perf_counter() - t0, 3)}
    latency = round(time.perf_counter() - t0, 3)
    if resp.status_code != 200:
        return {"name": needle["name"], "status": "http_error",
                "http": resp.status_code, "gold_delivered": False,
                "latency_s": latency}
    data = resp.json()
    entry = data[0] if isinstance(data, list) and data else {}
    content = entry.get("content", "") if isinstance(entry, dict) else ""
    accept = needle.get("accept", [needle.get("expected", "")])
    gold_sources = needle.get("gold_source", [])
    if gold_sources:
        gold = bench_needle.check_gold_delivery(
            content, gold_sources, accept, response=data)
        gold_delivered = gold["gold_delivered"]
        n_gold = gold["n_gold_blocks"]
        n_deliv = gold["n_delivered_blocks"]
        body_has = gold["body_has_answer"]
    else:  # find_needle's substring fallback for gold_source-less needles
        gold_delivered = any(a.lower() in content.lower() for a in accept)
        n_gold = 0
        n_deliv = len(bench_needle.parse_delivered_genes_from_response(data))
        body_has = gold_delivered
    return {
        "name": needle["name"], "status": "ok",
        "gold_delivered": bool(gold_delivered),
        "body_has_answer": bool(body_has),
        "n_gold_blocks": n_gold, "n_delivered_blocks": n_deliv,
        "latency_s": latency,
    }


def _run_cell(url: str, needles: list[dict]) -> dict:
    client = httpx.Client(timeout=60)
    rows = []
    gold = 0
    body = 0
    try:
        for nd in needles:
            r = _score_needle(client, url, nd)
            rows.append(r)
            if r["gold_delivered"]:
                gold += 1
            if r.get("body_has_answer"):
                body += 1
    finally:
        client.close()
    n = max(len(rows), 1)
    return {
        "n_needles": len(rows),
        "gold_delivered": gold,
        "gold_delivered_rate": round(gold / n, 4),
        "body_has_answer": body,
        "body_has_answer_rate": round(body / n, 4),
        "per_needle": rows,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bed-db",
                    default=str(_REPO_ROOT / "genomes/bench/matrix/xl_clean.db"),
                    help="Path to the decontaminated bed (default: xl_clean).")
    ap.add_argument("--bed-label", default="xl")
    ap.add_argument("--base-config",
                    default=str(_REPO_ROOT / "docs/benchmarks/helix_probe_lexical.toml"))
    ap.add_argument("--depths", default="48,200,500",
                    help="Comma-separated FTS candidate depths.")
    ap.add_argument("--fusions", default="additive,rrf",
                    help="Comma-separated fusion modes.")
    ap.add_argument("--port", type=int, default=11439)
    ap.add_argument("--out",
                    default=str(_REPO_ROOT / "benchmarks/results/sike_fts_depth_sweep_xl.json"))
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap needles (0 = all 50; smoke only).")
    args = ap.parse_args(argv)

    bed_db = Path(args.bed_db)
    base_config = Path(args.base_config)
    if not bed_db.exists():
        print(f"bed db not found: {bed_db}", file=sys.stderr)
        return 2
    if not base_config.exists():
        print(f"base config not found: {base_config}", file=sys.stderr)
        return 2

    depths = [int(d) for d in args.depths.split(",") if d.strip()]
    fusions = [f.strip() for f in args.fusions.split(",") if f.strip()]
    needles = list(bench_needle.NEEDLES)
    if args.limit:
        needles = needles[: args.limit]

    url = f"http://127.0.0.1:{args.port}"
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir = _REPO_ROOT / "benchmarks" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = _REPO_ROOT / "benchmarks" / "logs" / "fts_depth_cells"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "benchmark": "sike_fts_depth_sweep",
        "issue": "#205",
        "bed": args.bed_label,
        "bed_db": str(bed_db),
        "base_config": str(base_config),
        "depths": depths,
        "fusions": fusions,
        "n_needles": len(needles),
        "baseline_ref": ("Run-1 xl gold_delivered_rate=0.62 (depth=48, "
                         "additive); erb10k/erb50k=0.84"),
        "cells": [],
        "errors": [],
    }

    for depth in depths:
        for fusion in fusions:
            label = f"depth={depth} fusion={fusion}"
            print(f"=== CELL {label} ===", flush=True)
            _wait_port_free(url, timeout_s=15)
            cell_cfg = cfg_dir / f"probe_d{depth}_{fusion}.toml"
            _write_cell_config(base_config, bed_db, depth, fusion, cell_cfg)
            srv_log = logs_dir / f"fts_depth_srv_d{depth}_{fusion}.log"
            proc = _start_server(bed_db, cell_cfg, args.port, srv_log)
            cell: dict = {"depth": depth, "fusion": fusion}
            if not _wait_healthy(url, timeout_s=90):
                cell["status"] = "server_unhealthy"
                cell["server_log_tail"] = _tail(srv_log)
                result["errors"].append(f"{label}: server did not become healthy")
                _stop_server(proc, url)
                result["cells"].append(cell)
                _write(result, out_path)
                continue
            try:
                stats = httpx.get(f"{url}/stats", timeout=15).json()
                cell["bed_genes"] = stats.get("total_genes")
            except Exception as exc:
                result["errors"].append(f"{label}: /stats {exc}")
            try:
                cell.update(_run_cell(url, needles))
                cell["status"] = "ok"
                print("    {} genes={} gold_delivered_rate={}".format(
                    label, cell.get("bed_genes"),
                    cell.get("gold_delivered_rate")), flush=True)
            except Exception as exc:
                cell["status"] = "error"
                cell["error"] = str(exc)[:300]
                result["errors"].append(f"{label}: {exc}")
            finally:
                _stop_server(proc, url)
            result["cells"].append(cell)
            _write(result, out_path)  # checkpoint after every cell

    # Compact curve summary for quick reading (depth -> {fusion: rate}).
    curve: dict = {}
    for c in result["cells"]:
        if c.get("status") == "ok":
            curve.setdefault(str(c["depth"]), {})[c["fusion"]] = c["gold_delivered_rate"]
    result["curve"] = curve
    result["complete"] = True
    _write(result, out_path)
    print("\nCURVE (gold_delivered_rate):")
    for d in sorted(curve, key=int):
        print(f"  depth={d:>4}: " + ", ".join(
            f"{fus}={curve[d][fus]}" for fus in fusions if fus in curve[d]))
    print(f"-> {out_path}")
    return 0


def _tail(path: Path, n: int = 1500) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:]
    except Exception:
        return ""


def _write(result: dict, out_path: Path) -> None:
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
