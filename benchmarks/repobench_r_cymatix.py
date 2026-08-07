"""RepoBench-R (Step-1) Cymatix arm -- per-example genome variant.

For each example, spins up a fresh in-process genome, ingests each candidate snippet
as one gene (metadata path = "cand_{i}"), then fingerprints the query and ranks
candidates by their best gene score.  Metric: acc@k (acc@1/3 easy; acc@1/3/5 hard).

This "per-example" approach gives Cymatix the smallest possible retrieval corpus
(~5-17 snippets) which degrades lexical IDF statistics.  See repobench_r_cymatix_global.py
for the "global genome" arm which injects the whole candidate union into one corpus --
a fairer test of Cymatix's retrieval pipeline at realistic corpus sizes.

Reads the per-example dumps written by repobench_r.py (same examples as the foils),
so random/overlap/bm25/cymatix are all scored on an IDENTICAL set.

LLM-free, GPU-free (lexical config: dense/splade/ribosome OFF).
Run in the cymatix063 venv DIRECTLY, never via uv (ProcessPoolExecutor/trampoline issue):
  F:/Projects/_venvs/cymatix063/Scripts/python.exe -u benchmarks/repobench_r_cymatix.py

Config:
  CYMATIX_CONFIG env var or --cymatix-config flag -- path to a cymatix.toml with
  dense/splade/ribosome all disabled (the "lexical probe" profile).
  Defaults to the repo's docs/benchmarks/cymatix_probe_lexical.toml template.

Writes:
  benchmarks/results/repobench_r_{config}_cymatix_{timestamp}.json  (summary)
"""
from __future__ import annotations

import argparse
import datetime
import gc
import glob
import json
import os
import shutil
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# Default lexical-probe config search order.
_DEFAULT_CONFIG_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "cymatix_probe_lexical.toml",
    Path("F:/tmp/cb_cymatix_probe/cymatix_probe.toml"),
]

GOLD_KEY = "golden_snippet_index"
GOLD_KEY_ALT = "gold_snippet_index"


def _find_default_config():
    for p in _DEFAULT_CONFIG_CANDIDATES:
        if Path(p).exists():
            return str(p)
    return None


def _ks_for_level(level):
    """acc@1/3 easy; acc@1/3/5 hard -- per strategy doc section 3."""
    return [1, 3, 5] if level == "hard" else [1, 3]


# ---------------------------------------------------------------------------
# Cymatix wiring
# ---------------------------------------------------------------------------

def build_cymatix(genome_dir, config_path):
    """Build an in-process CymatixContextManager with a fresh genome."""
    os.environ.pop("CYMATIX_USE_SHARDS", None)
    os.environ["CYMATIX_CONFIG"] = config_path
    os.environ["CYMATIX_GENOME_PATH"] = os.path.join(genome_dir, "genome.db")
    os.makedirs(genome_dir, exist_ok=True)

    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager

    return CymatixContextManager(load_config())


def gene_cand_idx(g):
    """Recover the candidate index from a retrieved gene's source_id/metadata."""
    src = getattr(g, "source_id", None)
    if not src and getattr(g, "promoter", None) and g.promoter.metadata:
        src = g.promoter.metadata.get("path")
    if src and str(src).startswith("cand_"):
        try:
            return int(str(src).split("cand_", 1)[1])
        except ValueError:
            return None
    return None


def rank_cymatix(cymatix, query, n_cands):
    """Rank candidate indices by best gene score (desc); unretrieved appended last."""
    _eq, dom, ent = cymatix._prepare_query_signals(query, session_context=None,
                                                 expand_query=False)
    cands = cymatix._retrieve(dom, ent, 400, query_text=query, include_cold=None,
                            party_id="default", use_harmonic=False, use_sr=False)
    cands, _ = cymatix._apply_candidate_refiners(query, cands, 400, use_cymatics=False,
                                               use_harmonic_bin=False, use_tcm=True)
    base_scores = dict(cymatix.genome.last_query_scores or {})
    best = {}
    for g in cands:
        ci = gene_cand_idx(g)
        if ci is None:
            continue
        s = base_scores.get(g.gene_id, 0.0)
        if ci not in best or s > best[ci]:
            best[ci] = s

    ranked = sorted(best, key=lambda i: best[i], reverse=True)
    # Append candidates never retrieved (score 0) to complete the ranking.
    ranked += [i for i in range(n_cands) if i not in best]
    return ranked


def process_example(payload, config_path, genome_root):
    """Run one example in isolation -- build genome, ingest, rank, teardown."""
    ex, ei = payload
    gdir = os.path.join(genome_root, str(ei))
    shutil.rmtree(gdir, ignore_errors=True)
    cymatix = None
    try:
        cymatix = build_cymatix(gdir, config_path)
        for i, c in enumerate(ex["candidates"]):
            try:
                cymatix.ingest(c, content_type="code", metadata={"path": f"cand_{i}"})
            except Exception:  # noqa: BLE001
                pass
        order = rank_cymatix(cymatix, ex["query"], len(ex["candidates"]))
        gold = ex["gold"]
        ks = _ks_for_level(ex.get("level", "easy"))
        return {"a": {k: (1.0 if gold in order[:k] else 0.0) for k in ks}}
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)}
    finally:
        try:
            if cymatix is not None and getattr(cymatix, "genome", None) is not None:
                cl = getattr(cymatix.genome, "close", None)
                if callable(cl):
                    cl()
        except Exception:  # noqa: BLE001
            pass
        gc.collect()
        shutil.rmtree(gdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Level runner
# ---------------------------------------------------------------------------

def run_level(level, config_name, cymatix_config, genome_root, limit, workers):
    """Score one difficulty level; returns stats dict or None on missing dump."""
    pattern = str(RESULTS_DIR / f"repobench_r_{config_name}_{level}_n*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        print(
            f"[{level}] no dump found at {pattern}\n"
            f"  -> Run repobench_r.py first: "
            f"python benchmarks/repobench_r.py --config {config_name}",
            file=sys.stderr,
        )
        return None

    rows = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
    if limit:
        rows = rows[:limit]

    # Tag each row with level so process_example knows which ks to use.
    for r in rows:
        r["level"] = level

    ks = _ks_for_level(level)
    payloads = [(ex, f"{level}_{i}") for i, ex in enumerate(rows)]
    acc = {k: 0.0 for k in ks}
    n = err = 0

    if workers > 1:
        # NOTE: run DIRECTLY (not via uv) to avoid ProcessPoolExecutor trampoline
        # deadlock. Pass config_path and genome_root explicitly.
        from concurrent.futures import ProcessPoolExecutor, as_completed
        import functools

        _fn = functools.partial(process_example, config_path=cymatix_config,
                                genome_root=genome_root)
        with ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=20) as pool:
            futs = {pool.submit(_fn, p): p for p in payloads}
            for fut in as_completed(futs):
                r = fut.result()
                if r.get("error"):
                    err += 1
                    continue
                for k in ks:
                    acc[k] += r["a"].get(k, 0.0)
                n += 1
    else:
        for p in payloads:
            r = process_example(p, config_path=cymatix_config, genome_root=genome_root)
            if r.get("error"):
                err += 1
                continue
            for k in ks:
                acc[k] += r["a"].get(k, 0.0)
            n += 1

    result = {"n": n, "err": err}
    for k in ks:
        result[f"cymatix_acc@{k}"] = round(acc[k] / n, 3) if n else 0.0
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="RepoBench-R Cymatix per-example arm -- acc@k retrieval benchmark"
    )
    ap.add_argument(
        "--config",
        default="python_cff",
        choices=["python_cff", "python_cfr", "java_cff", "java_cfr"],
        help="Dataset config (must match dump written by repobench_r.py)",
    )
    ap.add_argument(
        "--levels",
        default="easy,hard",
        help="Comma-separated difficulty levels",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap examples/level from dump (0 = all)",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel workers (default 1; set >1 only when running DIRECTLY, not via uv)",
    )
    ap.add_argument(
        "--cymatix-config",
        default=None,
        help="Path to lexical-probe cymatix.toml (dense/splade/ribosome OFF). "
             "Falls back to CYMATIX_CONFIG env var, then repo default.",
    )
    ap.add_argument(
        "--genome-root",
        default=None,
        help="Scratch dir for per-example genome DBs (default: auto temp dir)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Override output path for the summary JSON",
    )
    args = ap.parse_args()

    cymatix_config = (
        args.cymatix_config
        or os.environ.get("CYMATIX_CONFIG")
        or _find_default_config()
    )
    if not cymatix_config or not Path(cymatix_config).exists():
        print(
            "ERROR: No cymatix config found. Provide --cymatix-config or set CYMATIX_CONFIG.\n"
            "  Expected a lexical-probe cymatix.toml (dense/splade/ribosome OFF).\n"
            "  See docs/benchmarks/cymatix_probe_lexical.toml for a template.",
            file=sys.stderr,
        )
        sys.exit(1)

    genome_root = args.genome_root or os.path.join(
        os.environ.get("TEMP", "/tmp"), "repobench_r_genomes"
    )
    os.makedirs(genome_root, exist_ok=True)

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    levels = [x.strip() for x in args.levels.split(",") if x.strip()]

    summary = {
        "config": args.config,
        "cymatix_config": cymatix_config,
        "timestamp": ts,
        "arm": "cymatix_per_example",
        "levels": {},
    }

    for level in levels:
        print(f"[{level}] running...", flush=True)
        r = run_level(
            level=level,
            config_name=args.config,
            cymatix_config=cymatix_config,
            genome_root=genome_root,
            limit=args.limit,
            workers=args.workers,
        )
        if r is None:
            continue
        summary["levels"][level] = r
        ks = _ks_for_level(level)
        acc_str = "  ".join(f"acc@{k}={r[f'cymatix_acc@{k}']}" for k in ks)
        print(f"[{level}] n={r['n']} err={r['err']}  {acc_str}", flush=True)

    if args.out:
        out_path = Path(args.out)
    else:
        out_path = RESULTS_DIR / f"repobench_r_{args.config}_cymatix_{ts}.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
