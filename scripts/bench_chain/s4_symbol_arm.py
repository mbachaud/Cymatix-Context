r"""s4_symbol_arm.py -- arm-C (symbol-graph) gate sweep for PRs #230/#231.

Measures whether WS2 symbol_defs + SYMBOL_REF edges plus WS3 PageRank-bounded
symbol expansion move gold delivery on the frozen xl bed:

    symbol_expansion_cap  in {0, 8}          (0 = expansion disabled)
    fusion_mode           in {rrf, additive}
    fts5_candidate_depth  pinned at 48       (Run-2 reference depth)

on a symbol-BACKFILLED copy of the bed (scripts/bench_chain/
s4_symbol_backfill.py -> xl_symbol.db) so the corpus stays byte-comparable
to Run-2. The cap=0 cells double as a drift check against the Run-2
references (sike_fts_depth_sweep_xl.json @ depth=48: additive 0.62,
rrf 0.74): if cap=0 drifts by more than the tolerance, the backfilled bed
or the rebased build shifted baseline behavior and the arm-C delta is NOT
cleanly interpretable.

FAIL-FAST CAPABILITY ASSERTS (gap A3 -- a previous "arm C" run served a
build where the symbol config keys didn't exist, so both arms measured the
same pipeline). Before ANY cell is scored, this script aborts unless:

  (a) the config loader imported FROM THE SERVED TREE honors
      ingestion.symbol_graph and retrieval.symbol_expansion_cap on a TOML
      round-trip with NON-DEFAULT values (a defaulting loader fails);
  (b) the bed (read-only) has symbol_defs rows > 0 AND SYMBOL_REF edges > 0;
  (c) the first server answers /health 200 (checked per cell as in s3).

All three results are recorded in the output JSON (capability_asserts).

DELIBERATE SPLIT (do not "simplify" it away):
  * Server code  = THIS worktree (repo root of this script): the served
    build must have the WS2/WS3 symbol capability.
  * Scoring harness = the MAIN checkout's benchmarks/ dir (sys.path insert):
    bench_needle.NEEDLES + check_gold_delivery are imported from the main
    checkout so the needle set and scoring are IDENTICAL to Run-2, keeping
    rates directly comparable.

Server lifecycle, TOML cell rewrite (tomllib/tomli_w), /context scoring and
checkpointed-JSON output are adapted from scripts/bench_chain/
s3_fts_depth_sweep.py (Run-2 harness).

OUTPUT (checkpointed after every cell):
  <main>/benchmarks/results/sike_symbol_arm_xl.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import tomli_w

_REPO_ROOT = Path(__file__).resolve().parents[2]   # the ws3 worktree (served tree)
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _main_root() -> Path:
    """MAIN checkout root (beds, benchmarks harness, results live there)."""
    parts = _REPO_ROOT.parts
    if ".worktrees" in parts:
        return Path(*parts[: parts.index(".worktrees")])
    return _REPO_ROOT


_MAIN_ROOT = _main_root()
_BENCH_DIR = _MAIN_ROOT / "benchmarks"
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))

import httpx  # noqa: E402  (repo dep)

import bench_needle  # noqa: E402  (MAIN checkout: NEEDLES + check_gold_delivery)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Run-2 references @ depth=48 on the frozen xl bed (sike_fts_depth_sweep_xl.json)
RUN2_REF = {"additive": 0.62, "rrf": 0.74}
DRIFT_TOL = 0.04


# ---------------------------------------------------------------------------
# Fail-fast capability asserts (gap A3): run BEFORE any cell is scored.
# ---------------------------------------------------------------------------

def _assert_config_capability() -> dict:
    """(a) The served tree's config loader must honor the two symbol keys.

    Round-trips a TOML carrying NON-DEFAULT values through the loader
    imported from the served tree; if the loader silently drops the keys
    (pre-WS2 build), the loaded values fall back to defaults and this fails.
    """
    from helix_context import config as served_config  # served tree (sys.path[0])
    probe = {
        "ingestion": {"symbol_graph": True},        # default is False
        "retrieval": {"symbol_expansion_cap": 3},   # default is 8
    }
    fd, tmp = tempfile.mkstemp(suffix=".toml")
    try:
        with os.fdopen(fd, "wb") as fh:
            tomli_w.dump(probe, fh)
        cfg = served_config.load_config(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    module_file = getattr(served_config, "__file__", "?")
    got_graph = getattr(cfg.ingestion, "symbol_graph", None)
    got_cap = getattr(cfg.retrieval, "symbol_expansion_cap", None)
    ok = got_graph is True and got_cap == 3
    return {
        "name": "config_loader_honors_symbol_keys",
        "ok": ok,
        "served_config_module": str(module_file),
        "ingestion.symbol_graph": got_graph,
        "retrieval.symbol_expansion_cap": got_cap,
    }


def _assert_bed_capability(bed_db: Path) -> dict:
    """(b) The bed must actually contain a symbol graph (read-only open)."""
    from helix_context.schemas import StructuralRelation
    conn = sqlite3.connect(f"file:{bed_db.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        try:
            n_defs = cur.execute("SELECT COUNT(*) FROM symbol_defs").fetchone()[0]
        except sqlite3.OperationalError:
            n_defs = 0  # table absent == no capability
        n_edges = cur.execute(
            "SELECT COUNT(*) FROM gene_relations WHERE relation = ?",
            (int(StructuralRelation.SYMBOL_REF),)).fetchone()[0]
    finally:
        conn.close()
    return {
        "name": "bed_has_symbol_graph",
        "ok": n_defs > 0 and n_edges > 0,
        "symbol_defs_rows": n_defs,
        "symbol_ref_edges": n_edges,
    }


# ---------------------------------------------------------------------------
# Per-cell config: probe TOML rewrite (tomllib/tomli_w, no string surgery).
# ---------------------------------------------------------------------------

def _write_cell_config(base_config: Path, bed_db: Path, cap: int,
                       fusion: str, depth: int, dest: Path) -> None:
    with base_config.open("rb") as fh:
        cfg = tomllib.load(fh)
    cfg.setdefault("retrieval", {})
    cfg["retrieval"]["symbol_expansion_cap"] = int(cap)
    cfg["retrieval"]["fusion_mode"] = str(fusion)
    cfg["retrieval"]["fts5_candidate_depth"] = int(depth)
    cfg.setdefault("genome", {})
    cfg["genome"]["path"] = str(bed_db)
    with dest.open("wb") as fh:
        tomli_w.dump(cfg, fh)


# ---------------------------------------------------------------------------
# Server lifecycle (uvicorn from the WS3 worktree, read-only serve).
# ---------------------------------------------------------------------------

def _wait_healthy(url: str, timeout_s: int = 180) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=30)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _wait_port_free(url: str, timeout_s: int = 20) -> None:
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
    env.pop("HELIX_EXPANSION_RANK", None)     # default = WS3 PageRank ranking
    args = [sys.executable, "-m", "uvicorn", "helix_context._asgi:app",
            "--host", "127.0.0.1", "--port", str(port)]
    log_fh = log_path.open("w", encoding="utf-8")
    # cwd = the WS3 WORKTREE: the served code must be the symbol-capable build.
    return subprocess.Popen(
        args, stdout=log_fh, stderr=subprocess.STDOUT,
        env=env, cwd=str(_REPO_ROOT), creationflags=NO_WINDOW,
    )


def _stop_server(proc: subprocess.Popen, url: str) -> None:
    if proc is None:
        return
    try:
        if os.name == "nt":
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
# Retrieval scoring: identical to s3 (Run-2 parity).
# ---------------------------------------------------------------------------

def _score_needle(client: httpx.Client, url: str, needle: dict) -> dict:
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
        content_has = gold["content_has_answer"]
    else:
        gold_delivered = any(a.lower() in content.lower() for a in accept)
        n_gold = 0
        n_deliv = len(bench_needle.parse_delivered_genes_from_response(data))
        body_has = gold_delivered
        content_has = gold_delivered
    return {
        "name": needle["name"], "status": "ok",
        "gold_delivered": bool(gold_delivered),
        "body_has_answer": bool(body_has),
        "content_has_answer": bool(content_has),
        "n_gold_blocks": n_gold, "n_delivered_blocks": n_deliv,
        "latency_s": latency,
    }


def _run_cell(url: str, needles: list[dict]) -> dict:
    client = httpx.Client(timeout=60)
    rows = []
    gold = body = content = 0
    lat_sum = 0.0
    try:
        for nd in needles:
            r = _score_needle(client, url, nd)
            rows.append(r)
            lat_sum += r.get("latency_s", 0.0)
            if r["gold_delivered"]:
                gold += 1
            if r.get("body_has_answer"):
                body += 1
            if r.get("content_has_answer"):
                content += 1
    finally:
        client.close()
    n = max(len(rows), 1)
    return {
        "n_needles": len(rows),
        "gold_delivered": gold,
        "gold_delivered_rate": round(gold / n, 4),
        "body_has_answer": body,
        "body_has_answer_rate": round(body / n, 4),
        "content_has_answer": content,
        "content_has_answer_rate": round(content / n, 4),
        "latency_mean_s": round(lat_sum / n, 3),
        "per_needle": rows,
    }


# ---------------------------------------------------------------------------
# Drift check: cap=0 cells vs the Run-2 references.
# ---------------------------------------------------------------------------

def _drift_check(cells: list[dict]) -> dict:
    out = {"references": dict(RUN2_REF), "tolerance": DRIFT_TOL,
           "cells": [], "ok": True}
    for c in cells:
        if c.get("status") != "ok" or c.get("symbol_expansion_cap") != 0:
            continue
        ref = RUN2_REF.get(c["fusion"])
        if ref is None:
            continue
        delta = round(c["gold_delivered_rate"] - ref, 4)
        drifted = abs(delta) > DRIFT_TOL
        out["cells"].append({
            "fusion": c["fusion"], "gold_delivered_rate": c["gold_delivered_rate"],
            "run2_reference": ref, "delta": delta, "drifted": drifted,
        })
        if drifted:
            out["ok"] = False
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bed-db",
                    default=str(_MAIN_ROOT / "genomes/bench/sike_beds/xl_symbol.db"),
                    help="Symbol-backfilled bed (s4_symbol_backfill.py output).")
    ap.add_argument("--bed-label", default="xl_symbol")
    ap.add_argument("--base-config",
                    default=str(_REPO_ROOT / "docs/benchmarks/helix_probe_symbol.toml"))
    ap.add_argument("--caps", default="0,8",
                    help="Comma-separated symbol_expansion_cap values.")
    ap.add_argument("--fusions", default="rrf,additive")
    ap.add_argument("--depth", type=int, default=48,
                    help="fts5_candidate_depth, pinned (Run-2 reference depth).")
    ap.add_argument("--port", type=int, default=11453)
    ap.add_argument("--out",
                    default=str(_MAIN_ROOT / "benchmarks/results/sike_symbol_arm_xl.json"))
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap needles (0 = all 50; smoke only).")
    args = ap.parse_args(argv)

    bed_db = Path(args.bed_db)
    base_config = Path(args.base_config)
    if not bed_db.exists():
        print(f"bed db not found: {bed_db} (run s4_symbol_backfill.py first)",
              file=sys.stderr)
        return 2
    if not base_config.exists():
        print(f"base config not found: {base_config}", file=sys.stderr)
        return 2

    caps = [int(c) for c in args.caps.split(",") if c.strip()]
    fusions = [f.strip() for f in args.fusions.split(",") if f.strip()]
    needles = list(bench_needle.NEEDLES)
    if args.limit:
        needles = needles[: args.limit]

    url = f"http://127.0.0.1:{args.port}"
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir = _MAIN_ROOT / "benchmarks" / "logs"
    cfg_dir = logs_dir / "symbol_arm_cells"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # ---- FAIL-FAST CAPABILITY ASSERTS (before any cell) -------------------
    asserts = {"config": _assert_config_capability(),
               "bed": _assert_bed_capability(bed_db),
               "health": {"name": "server_health_200", "ok": None,
                          "note": "set after first cell's server start"}}
    for key in ("config", "bed"):
        a = asserts[key]
        status = "OK" if a["ok"] else "FAIL"
        print(f"[capability-assert] {a['name']}: {status} "
              f"{json.dumps({k: v for k, v in a.items() if k not in ('name', 'ok')})}",
              flush=True)
    if not (asserts["config"]["ok"] and asserts["bed"]["ok"]):
        print("ABORT: capability assert failed -- the served build or bed "
              "lacks the symbol graph; scoring would repeat gap A3 "
              "(a fake arm C).", file=sys.stderr)
        return 3

    result: dict = {
        "benchmark": "sike_symbol_arm",
        "issue": "#230/#231 (WS2 symbol graph + WS3 PageRank-bounded expansion)",
        "bed": args.bed_label,
        "bed_db": str(bed_db),
        "base_config": str(base_config),
        "served_tree": str(_REPO_ROOT),
        "harness_tree": str(_MAIN_ROOT),
        "caps": caps,
        "fusions": fusions,
        "fts5_candidate_depth": args.depth,
        "n_needles": len(needles),
        "capability_asserts": asserts,
        "run2_reference": {"depth": 48, **RUN2_REF},
        "cells": [],
        "errors": [],
    }

    first_cell = True
    for cap in caps:
        for fusion in fusions:
            label = f"cap={cap} fusion={fusion}"
            print(f"=== CELL {label} ===", flush=True)
            _wait_port_free(url, timeout_s=15)
            cell_cfg = cfg_dir / f"probe_cap{cap}_{fusion}.toml"
            _write_cell_config(base_config, bed_db, cap, fusion, args.depth,
                               cell_cfg)
            srv_log = logs_dir / f"symbol_arm_srv_cap{cap}_{fusion}.log"
            proc = _start_server(bed_db, cell_cfg, args.port, srv_log)
            cell: dict = {"symbol_expansion_cap": cap, "fusion": fusion}
            healthy = _wait_healthy(url, timeout_s=120)
            if first_cell:
                asserts["health"]["ok"] = healthy
                first_cell = False
            if not healthy:
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
                print("    {} genes={} gold_delivered_rate={} "
                      "content_has_answer_rate={} lat_mean={}s".format(
                          label, cell.get("bed_genes"),
                          cell.get("gold_delivered_rate"),
                          cell.get("content_has_answer_rate"),
                          cell.get("latency_mean_s")), flush=True)
            except Exception as exc:
                cell["status"] = "error"
                cell["error"] = str(exc)[:300]
                result["errors"].append(f"{label}: {exc}")
            finally:
                _stop_server(proc, url)
            result["cells"].append(cell)
            _write(result, out_path)  # checkpoint after every cell

    result["drift_check"] = _drift_check(result["cells"])
    result["complete"] = True
    _write(result, out_path)

    print("\nTABLE (gold_delivered_rate / content_has_answer_rate / lat_mean):")
    for c in result["cells"]:
        if c.get("status") == "ok":
            print(f"  cap={c['symbol_expansion_cap']} fusion={c['fusion']:>8}: "
                  f"{c['gold_delivered_rate']} / {c['content_has_answer_rate']} "
                  f"/ {c['latency_mean_s']}s")
    dc = result["drift_check"]
    print(f"drift_check ok={dc['ok']}: " + "; ".join(
        f"{x['fusion']} cap=0 {x['gold_delivered_rate']} vs ref "
        f"{x['run2_reference']} (d={x['delta']:+})" for x in dc["cells"]))
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
