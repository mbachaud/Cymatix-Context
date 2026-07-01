#!/usr/bin/env python3
"""ContextBench Step-0 arm-D: score Helix preds with the OFFICIAL evaluator + compare to BM25.

Run with the cb-step0 venv. For each Helix pred JSON: run `python -m contextbench.evaluate`
(same convention as the BM25 harness), parse the per-instance JSONL, micro-average file/symbol/
line EXACTLY like the evaluator (cov=inter/gold if gold else 1.0; prec=inter/pred if pred else 1.0),
join injected_tokens (median) from the meta sidecar. Load existing BM25 numbers from step0_summary.json.
Print ONE combined 8-row table and write a combined summary JSON.

Read-only on contextbench source; never mutates repos.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys

CONTEXTBENCH_SRC = os.environ.get("CONTEXTBENCH_SRC", "F:/Projects/contextbench-src")
GOLD = "F:/Projects/helix-context/benchmarks/contextbench/gold_smoke_4repo.parquet"
CACHE = "F:/Projects/_cache/cb_repos"
RESULTS = "F:/Projects/helix-context/benchmarks/contextbench/results"
BM25_SUMMARY = os.path.join(RESULTS, "step0_summary.json")


def micro(results, gran):
    """Micro-average exactly like the official evaluator aggregate (and the BM25 harness)."""
    valid = [r for r in results if "error" not in r]
    inter = sum(r.get("final", {}).get(gran, {}).get("intersection", 0) for r in valid)
    gold = sum(r.get("final", {}).get(gran, {}).get("gold_size", 0) for r in valid)
    pred = sum(r.get("final", {}).get(gran, {}).get("pred_size", 0) for r in valid)
    cov = inter / gold if gold else 1.0
    prec = inter / pred if pred else 1.0
    f1 = (2 * cov * prec / (cov + prec)) if (cov + prec) else 0.0
    return {"recall": cov, "precision": prec, "f1": f1,
            "inter": inter, "gold": gold, "pred": pred, "n_valid": len(valid)}


def run_evaluator(pred_path, out_jsonl, env):
    """Run the official evaluator; delete stale --out first (crash != real empty result)."""
    cmd = [sys.executable, "-m", "contextbench.evaluate",
           "--gold", GOLD, "--pred", pred_path, "--cache", CACHE, "--out", out_jsonl]
    print(f"  [eval] {' '.join(cmd)}", file=sys.stderr)
    if os.path.isfile(out_jsonl):
        os.remove(out_jsonl)
    r = subprocess.run(cmd, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rows = []
    if os.path.isfile(out_jsonl):
        with open(out_jsonl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    if r.returncode != 0 or not rows:
        print(f"  [eval] ERROR rc={r.returncode} rows={len(rows)} -- UNTRUSTWORTHY",
              file=sys.stderr)
    return rows, r.returncode


def score_helix_pred(label, pred_path, meta_path, env):
    """Returns a summary dict for one Helix arm (or not_measured)."""
    out_jsonl = os.path.join(RESULTS, os.path.basename(pred_path).replace("_pred.json", "_eval.jsonl"))
    rows, rc = run_evaluator(pred_path, out_jsonl, env)
    n_scored = sum(1 for r in rows if "error" not in r)
    if rc != 0 or n_scored == 0:
        return {"not_measured": True, "returncode": rc, "n_scored": n_scored,
                "pred_path": pred_path, "eval_jsonl": out_jsonl}, out_jsonl

    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    inj = [meta.get(r["instance_id"], {}).get("injected_tokens", 0)
           for r in rows if "error" not in r]
    match_fail = sum(meta.get(r["instance_id"], {}).get("n_match_fail", 0)
                     for r in rows if "error" not in r)
    gran = {g: micro(rows, g) for g in ("file", "symbol", "span", "line")}
    med_inj = statistics.median(inj) if inj else 0
    p90_inj = sorted(inj)[int(0.9 * (len(inj) - 1))] if inj else 0
    line_recall = gran["line"]["recall"]
    rec_per_1k = (line_recall / (med_inj / 1000.0)) if med_inj else 0.0
    return {
        "n_scored": n_scored, "granularity": gran,
        "median_injected_tokens": med_inj, "p90_injected_tokens": p90_inj,
        "recall_per_1k_tokens": rec_per_1k, "match_fail": match_fail,
        "pred_path": pred_path, "eval_jsonl": out_jsonl,
    }, out_jsonl


def bm25_row(bm, label_in):
    """Pull a BM25 arm from the existing step0_summary into our common row shape."""
    a = bm["arms"][label_in]
    g = a["granularity"]
    return {
        "n_scored": a["n_scored"], "granularity": g,
        "median_injected_tokens": a["median_injected_tokens"],
        "p90_injected_tokens": a["p90_injected_tokens"],
        "recall_per_1k_tokens": a["recall_per_1k_tokens"],
        "match_fail": 0,
        "pred_path": a.get("pred_path"), "eval_jsonl": a.get("eval_jsonl"),
    }


def main():
    ap = argparse.ArgumentParser(description="Score Helix arm-D + compare to BM25")
    ap.add_argument("--tags", default="v062,wt")
    ap.add_argument("--out", default=os.path.join(RESULTS, "step0_helix_compare_summary.json"))
    args = ap.parse_args()

    os.environ["CONTEXTBENCH_TMP_ROOT"] = "F:/Projects/_cache/cb_wt"
    os.environ["GIT_LFS_SKIP_SMUDGE"] = "1"
    env = os.environ.copy()
    env["PYTHONPATH"] = CONTEXTBENCH_SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CONTEXTBENCH_TMP_ROOT"] = "F:/Projects/_cache/cb_wt"
    env["GIT_LFS_SKIP_SMUDGE"] = "1"

    with open(BM25_SUMMARY, "r", encoding="utf-8") as f:
        bm = json.load(f)

    # row order: bm25:8k, bm25:27k, then helix arms per tag
    rows = {}
    rows["bm25:8k"] = bm25_row(bm, "bm25:8k")
    rows["bm25:27k"] = bm25_row(bm, "bm25:27k")

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    helix_specs = []  # (row_label, pred_basename, meta_basename)
    for tag in tags:
        helix_specs += [
            (f"helix_{tag}_fingerprint_8k", f"helix_{tag}_fingerprint_8k_pred.json",
             f"helix_{tag}_fingerprint_8k_meta.json"),
            (f"helix_{tag}_fingerprint_27k", f"helix_{tag}_fingerprint_27k_pred.json",
             f"helix_{tag}_fingerprint_27k_meta.json"),
            (f"helix_{tag}_packet", f"helix_{tag}_packet_pred.json",
             f"helix_{tag}_packet_meta.json"),
        ]

    for label, predb, metab in helix_specs:
        pred_path = os.path.join(RESULTS, predb)
        meta_path = os.path.join(RESULTS, metab)
        if not os.path.isfile(pred_path):
            print(f"  [skip] {label}: pred not found ({pred_path})", file=sys.stderr)
            rows[label] = {"not_measured": True, "reason": "pred_missing"}
            continue
        print(f"\n===== SCORE {label} =====", file=sys.stderr)
        row, _ = score_helix_pred(label, pred_path, meta_path, env)
        rows[label] = row

    # ---- combined table ----
    print("\n" + "=" * 104)
    hdr = ["arm", "scored", "file_R", "line_R", "line_P", "sym_R", "med_inj_tok", "R/1k", "match_fail"]
    print(("{:<26}" + "{:>9}" * (len(hdr) - 1)).format(*hdr))
    print("-" * 104)
    for label, a in rows.items():
        if "granularity" not in a:
            print(f"{label:<26}  (not_measured: {a.get('reason') or a.get('returncode')})")
            continue
        g = a["granularity"]
        print(("{:<26}" + "{:>9}" * 8).format(
            label, a["n_scored"],
            f"{g['file']['recall']:.3f}", f"{g['line']['recall']:.3f}",
            f"{g['line']['precision']:.3f}", f"{g['symbol']['recall']:.3f}",
            f"{a['median_injected_tokens']:.0f}", f"{a['recall_per_1k_tokens']:.4f}",
            a.get("match_fail", 0)))
    print("=" * 104)
    print("Headline: line_R (recall) vs med_inj_tok — high recall at low tokens wins. "
          "R/1k = line_R per 1k median injected tokens.")

    summary = {"gold": GOLD, "bm25_source": BM25_SUMMARY, "arms": rows}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nsummary -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
