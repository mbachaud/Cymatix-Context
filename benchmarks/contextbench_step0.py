#!/usr/bin/env python3
"""
ContextBench Step-0 offline retrieval scorer for Helix.

Measures CODE-context retrieval on its designed terrain (path/symbol/structure),
LLM-free, scored by the OFFICIAL ContextBench evaluator (tree-sitter alignment).

Arms:
  none              no-retrieval floor (empty pred -> recall 0)
  bm25:<budget>     BM25-dump foil, fill to a token budget (e.g. bm25:8k, bm25:27k)
  helix_fingerprint Helix /fingerprint ranked retrieval (recall ceiling)  [needs --helix-url]
  helix_packet      Helix /context/packet delivered evidence              [needs --helix-url]

Per task:
  checkout repo@base_commit (contextbench.core.checkout, cached + shared with evaluator)
   -> retriever -> emit unified-format pred JSON
   -> score via `python -m contextbench.evaluate` (Coverage=recall, Precision @ file/symbol/span/line)
   -> join evaluator per-instance line/file metrics with our injected-token tallies
   -> table + summary JSON (recall vs injected-tokens is the headline).

Discipline: retrieval is LLM-free; repo is never mutated; gold/patch/test_patch
are NEVER indexed (only base_commit tree is read). Failures are recorded, not swallowed.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict

CONTEXTBENCH_SRC = os.environ.get("CONTEXTBENCH_SRC", "F:/Projects/contextbench-src")
sys.path.insert(0, CONTEXTBENCH_SRC)

from contextbench.core import checkout  # noqa: E402
from contextbench.extractors import available as ts_available  # noqa: E402

import tiktoken  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402

ENC = tiktoken.get_encoding("cl100k_base")

# Index any source/text file; gold is always real source. Exclude only generated/binary trees.
# Code + core docs only. Kept symmetric with the Helix arm (cb_helix_pred.py): high-volume
# non-code (.po/.html/.json/.css/.xml...) is excluded because Helix's spaCy ingest chokes on
# django's thousands of .po files and they are never code gold. The gold-file warn flags any drop.
SRC_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".h", ".cpp", ".cc",
    ".hpp", ".hh", ".java", ".cs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".cfg", ".ini", ".toml", ".yaml", ".yml", ".pyi",
}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".tox", ".eggs", "dist", "build", ".venv", "venv",
}
MAX_FILE_BYTES = 2_000_000  # skip pathological/generated files; gold chunks are small
CHUNK_LINES = 50            # BM25 chunk window (lines)


# ----------------------------- tokenization -----------------------------------
_WORD = re.compile(r"[A-Za-z0-9_]+")
_SUB = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def code_tokens(text):
    """Lexical tokens for BM25: whole identifiers + camelCase/snake_case subtokens."""
    out = []
    for m in _WORD.findall(text):
        ml = m.lower()
        out.append(ml)
        for p in _SUB.findall(m):
            pl = p.lower()
            if pl and pl != ml:
                out.append(pl)
    return out


def ntok(text):
    return len(ENC.encode(text, disallowed_special=()))


# ----------------------------- repo walking -----------------------------------
def iter_repo_files(repo_dir):
    """Yield (rel_path, abs_path) for indexable files under repo_dir."""
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SRC_EXT:
                continue
            ap = os.path.join(root, fn)
            try:
                if os.path.getsize(ap) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(ap, repo_dir).replace("\\", "/")
            yield rel, ap


def read_lines(abs_path):
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().split("\n")
    except OSError:
        return None


def chunk_file(rel, lines, window=CHUNK_LINES):
    """Yield (rel, start_line, end_line, text) line-window chunks (1-indexed inclusive)."""
    n = len(lines)
    i = 0
    while i < n:
        seg = lines[i:i + window]
        text = "\n".join(seg)
        if text.strip():
            yield rel, i + 1, min(i + len(seg), n), text
        i += window


# ----------------------------- arms -------------------------------------------
def arm_none(task, repo_dir):
    return {"pred_files": [], "pred_spans": {}}, {"injected_tokens": 0, "n_chunks": 0, "n_indexed_files": 0}


def arm_bm25(task, repo_dir, budget_tokens, gold_files=None):
    """BM25 over fixed line-window chunks; greedily fill ranked chunks to a token budget."""
    chunks = []          # (rel, s, e, text)
    corpus = []          # token lists
    indexed = set()
    n_files = 0
    for rel, ap in iter_repo_files(repo_dir):
        lines = read_lines(ap)
        if lines is None:
            continue
        n_files += 1
        indexed.add(rel)
        for c_rel, s, e, text in chunk_file(rel, lines):
            chunks.append((c_rel, s, e, text))
            corpus.append(code_tokens(text))

    # Fairness guard: a gold file unindexable by extension/size hard-caps recall for EVERY arm.
    missing_gold = [g for g in (gold_files or []) if g not in indexed]
    if missing_gold:
        print(f"  [WARN] {task['instance_id']}: {len(missing_gold)} gold file(s) NOT indexed "
              f"(extension/size excluded -> recall capped): {missing_gold[:5]}", file=sys.stderr)

    if not chunks:
        return {"pred_files": [], "pred_spans": {}}, {
            "injected_tokens": 0, "n_chunks": 0, "n_indexed_files": n_files,
            "n_candidate_chunks": 0, "gold_files_missing": len(missing_gold)}

    bm25 = BM25Okapi(corpus)
    q = code_tokens(task["problem_statement"] or "")
    scores = bm25.get_scores(q)
    order = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)

    spans = defaultdict(list)
    files = set()
    injected = 0
    picked = 0
    for i in order:
        rel, s, e, text = chunks[i]
        t = ntok(text)
        if picked >= 1 and injected + t > budget_tokens:
            continue  # skip oversized chunk, keep packing smaller high-ranked chunks (fairer fill)
        spans[rel].append({"start": s, "end": e})
        files.add(rel)
        injected += t
        picked += 1
        if injected >= budget_tokens:
            break

    pred = {"pred_files": sorted(files), "pred_spans": {k: v for k, v in spans.items()}}
    meta = {"injected_tokens": injected, "n_chunks": picked, "n_indexed_files": n_files,
            "n_candidate_chunks": len(chunks), "gold_files_missing": len(missing_gold)}
    return pred, meta


def arm_helix(task, repo_dir, mode, helix_url):
    """Placeholder for Helix arms (wired once a code-genome daemon is up).

    mode in {"fingerprint","packet"}. Will POST the problem_statement to the
    helix endpoint and translate ranked spans -> unified pred. Raises until wired.
    """
    raise NotImplementedError(
        f"helix arm '{mode}' not yet wired (needs --helix-url + code-genome daemon). "
        "Build/validate the BM25 + scoring pipeline first.")


def parse_arm(spec):
    """'bm25:27k' -> ('bm25', 27000); 'none' -> ('none', None); 'helix_packet' -> ('helix_packet', None)."""
    if spec.startswith("bm25:"):
        b = spec.split(":", 1)[1].lower().replace("k", "000")
        return "bm25", int(b)
    return spec, None


# ----------------------------- scoring ----------------------------------------
def run_evaluator(gold_pq, pred_path, cache_dir, out_jsonl, env):
    """Run the official evaluator. Returns (rows, returncode). Deletes any stale --out first so a
    crash cannot be mistaken for a real empty result (the evaluator writes --out only at the end)."""
    cmd = [sys.executable, "-m", "contextbench.evaluate",
           "--gold", gold_pq, "--pred", pred_path, "--cache", cache_dir, "--out", out_jsonl]
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
        print(f"  [eval] ERROR rc={r.returncode} rows={len(rows)} -- arm result UNTRUSTWORTHY "
              f"(evaluator crashed?); NOT reported as a real 0", file=sys.stderr)
    return rows, r.returncode


def micro(results, gran):
    """Micro-average matching the official aggregate_results/coverage_precision convention exactly."""
    valid = [r for r in results if "error" not in r]
    inter = sum(r.get("final", {}).get(gran, {}).get("intersection", 0) for r in valid)
    gold = sum(r.get("final", {}).get(gran, {}).get("gold_size", 0) for r in valid)
    pred = sum(r.get("final", {}).get(gran, {}).get("pred_size", 0) for r in valid)
    cov = inter / gold if gold else 1.0     # evaluator: gold==0 -> coverage 1.0
    prec = inter / pred if pred else 1.0    # evaluator: pred==0 -> precision 1.0
    f1 = (2 * cov * prec / (cov + prec)) if (cov + prec) else 0.0
    return {"recall": cov, "precision": prec, "f1": f1,
            "inter": inter, "gold": gold, "pred": pred, "n_valid": len(valid)}


# ----------------------------- main -------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="ContextBench Step-0 offline retrieval scorer")
    ap.add_argument("--gold", required=True, help="Gold parquet (e.g. gold_smoke_4repo.parquet)")
    ap.add_argument("--arms", default="bm25:8k,bm25:27k",
                    help="comma list: bm25:8k,bm25:27k,helix_fingerprint,helix_packet,none "
                         "(note: 'none' yields all-error rows, reported as not-measured)")
    ap.add_argument("--limit", type=int, default=0, help="cap number of tasks (0=all in gold)")
    ap.add_argument("--cache", default="F:/Projects/_cache/cb_repos", help="repo base-clone cache")
    ap.add_argument("--worktree-root", default="F:/Projects/_cache/cb_wt", help="CONTEXTBENCH_TMP_ROOT (worktrees)")
    ap.add_argument("--helix-url", default="", help="Helix bench-lane base URL, e.g. http://127.0.0.1:11439")
    ap.add_argument("--out", default="", help="summary JSON output path")
    args = ap.parse_args()

    if not ts_available():
        print("FATAL: tree-sitter not available (pip install tree-sitter==0.20.4 tree-sitter-languages==1.10.2)",
              file=sys.stderr)
        sys.exit(2)

    os.makedirs(args.cache, exist_ok=True)
    os.makedirs(args.worktree_root, exist_ok=True)
    os.environ["CONTEXTBENCH_TMP_ROOT"] = args.worktree_root
    os.environ["GIT_LFS_SKIP_SMUDGE"] = "1"

    env = os.environ.copy()
    env["PYTHONPATH"] = CONTEXTBENCH_SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CONTEXTBENCH_TMP_ROOT"] = args.worktree_root
    env["GIT_LFS_SKIP_SMUDGE"] = "1"

    # Load gold tasks (we need problem_statement + repo_url + base_commit; gold metrics come from the parquet).
    import pyarrow.dataset as ds
    table = ds.dataset(args.gold, format="parquet").to_table().to_pylist()
    if args.limit and args.limit > 0:
        table = table[:args.limit]
    tasks = []
    for r in table:
        try:
            gc = json.loads(r["gold_context"]) if r.get("gold_context") else []
        except Exception:  # noqa: BLE001
            gc = []
        gold_files = sorted({(e.get("file") or "").replace("\\", "/").lstrip("/")
                             for e in gc if e.get("file")})
        tasks.append({
            "instance_id": r["instance_id"],
            "repo": r.get("repo"),
            "repo_url": r.get("repo_url"),
            "base_commit": r.get("base_commit"),
            "problem_statement": r.get("problem_statement") or "",
            "gold_files": gold_files,
        })
    print(f"loaded {len(tasks)} tasks from {args.gold}", file=sys.stderr)

    arms = [parse_arm(a.strip()) for a in args.arms.split(",") if a.strip()]
    out_dir = os.path.join(os.path.dirname(os.path.abspath(args.gold)), "results")
    os.makedirs(out_dir, exist_ok=True)

    # 1) checkout every task once (shared cache; evaluator reuses these worktrees).
    repo_dirs = {}
    checkout_errors = {}
    for i, t in enumerate(tasks):
        print(f"[checkout {i+1}/{len(tasks)}] {t['instance_id']} {t['repo']}@{(t['base_commit'] or '')[:10]}",
              file=sys.stderr)
        t0 = time.time()
        try:
            rd = checkout(t["repo_url"], t["base_commit"], args.cache)
        except Exception as e:  # noqa: BLE001
            rd = None
            checkout_errors[t["instance_id"]] = repr(e)
        if not rd or not os.path.isdir(rd):
            checkout_errors[t["instance_id"]] = checkout_errors.get(t["instance_id"], "checkout_returned_none")
            print(f"  CHECKOUT FAILED: {checkout_errors[t['instance_id']]}", file=sys.stderr)
        else:
            repo_dirs[t["instance_id"]] = rd
            print(f"  ok in {time.time()-t0:.1f}s -> {rd}", file=sys.stderr)

    # 2) per arm: retrieve -> pred JSON -> evaluate -> metrics.
    summary = {"gold": args.gold, "n_tasks": len(tasks), "checkout_errors": checkout_errors, "arms": {}}
    for arm_name, budget in arms:
        label = arm_name if budget is None else f"{arm_name}:{budget//1000}k"
        print(f"\n===== ARM {label} =====", file=sys.stderr)
        preds = []
        per_inst_meta = {}
        for i, t in enumerate(tasks):
            iid = t["instance_id"]
            rd = repo_dirs.get(iid)
            if not rd:
                continue  # checkout failed; recorded above
            try:
                if arm_name == "none":
                    pred, meta = arm_none(t, rd)
                elif arm_name == "bm25":
                    pred, meta = arm_bm25(t, rd, budget, t["gold_files"])
                elif arm_name in ("helix_fingerprint", "helix_packet"):
                    mode = "fingerprint" if arm_name.endswith("fingerprint") else "packet"
                    pred, meta = arm_helix(t, rd, mode, args.helix_url)
                else:
                    raise ValueError(f"unknown arm {arm_name}")
            except NotImplementedError as e:
                print(f"  {label}: {e}", file=sys.stderr)
                preds = None
                break
            preds.append({
                "instance_id": iid,
                "traj_data": {"pred_steps": [], "pred_files": pred["pred_files"], "pred_spans": pred["pred_spans"]},
                "model_patch": "",
            })
            per_inst_meta[iid] = meta
            if (i + 1) % 5 == 0 or i + 1 == len(tasks):
                print(f"  retrieved {i+1}/{len(tasks)} (last injected_tokens={meta.get('injected_tokens')})",
                      file=sys.stderr)
        if preds is None:
            summary["arms"][label] = {"skipped": "not_implemented"}
            continue

        pred_path = os.path.join(out_dir, f"{label.replace(':','_')}_pred.json")
        with open(pred_path, "w", encoding="utf-8") as f:
            json.dump(preds, f)
        out_jsonl = os.path.join(out_dir, f"{label.replace(':','_')}_eval.jsonl")
        results, rc = run_evaluator(args.gold, pred_path, args.cache, out_jsonl, env)

        eval_errors = Counter(r.get("error") for r in results if r.get("error"))
        n_scored = sum(1 for r in results if "error" not in r)
        # An evaluator crash or an all-error arm (e.g. 'none') is NOT a real 0 -> mark not-measured.
        if rc != 0 or n_scored == 0:
            summary["arms"][label] = {
                "not_measured": True, "eval_crashed": rc != 0, "returncode": rc,
                "n_pred": len(preds), "n_scored": n_scored, "eval_errors": dict(eval_errors),
                "pred_path": pred_path, "eval_jsonl": out_jsonl,
            }
            print(f"  {label}: NOT MEASURED (rc={rc}, scored={n_scored}, errors={dict(eval_errors)})",
                  file=sys.stderr)
            continue

        # join evaluator metrics with our token tallies
        inj = [per_inst_meta.get(r["instance_id"], {}).get("injected_tokens", 0)
               for r in results if "error" not in r]
        gran = {g: micro(results, g) for g in ("file", "symbol", "span", "line")}
        med_inj = statistics.median(inj) if inj else 0
        p90_inj = sorted(inj)[int(0.9 * (len(inj) - 1))] if inj else 0
        line_recall = gran["line"]["recall"]
        rec_per_1k = (line_recall / (med_inj / 1000.0)) if med_inj else 0.0
        n_gold_missing = sum(per_inst_meta.get(r["instance_id"], {}).get("gold_files_missing", 0)
                             for r in results if "error" not in r)

        summary["arms"][label] = {
            "n_pred": len(preds), "n_scored": n_scored, "eval_errors": dict(eval_errors),
            "granularity": gran,
            "median_injected_tokens": med_inj, "p90_injected_tokens": p90_inj,
            "recall_per_1k_tokens": rec_per_1k, "gold_files_unindexable": n_gold_missing,
            "pred_path": pred_path, "eval_jsonl": out_jsonl,
        }
        print(f"  {label}: line recall={line_recall:.3f} prec={gran['line']['precision']:.3f} "
              f"file recall={gran['file']['recall']:.3f} sym recall={gran['symbol']['recall']:.3f} "
              f"median_inj={med_inj} rec/1k={rec_per_1k:.3f} scored={n_scored} "
              f"gold_unindexable={n_gold_missing}", file=sys.stderr)

    # 3) report
    print("\n" + "=" * 96)
    hdr = ["arm", "scored", "file_R", "line_R", "line_P", "line_F1", "sym_R", "med_inj", "p90_inj", "R/1k"]
    print(("{:<16}" + "{:>8}" * (len(hdr) - 1)).format(*hdr))
    for label, a in summary["arms"].items():
        if "granularity" not in a:
            reason = "eval_crashed" if a.get("eval_crashed") else (
                "not_measured" if a.get("not_measured") else "skipped(impl)")
            print(f"{label:<16}  ({reason}; scored={a.get('n_scored','?')})")
            continue
        g = a["granularity"]
        print(("{:<16}" + "{:>8}" * 9).format(
            label, a["n_scored"],
            f"{g['file']['recall']:.3f}", f"{g['line']['recall']:.3f}", f"{g['line']['precision']:.3f}",
            f"{g['line']['f1']:.3f}", f"{g['symbol']['recall']:.3f}",
            a["median_injected_tokens"], a["p90_injected_tokens"], f"{a['recall_per_1k_tokens']:.3f}"))
    print("=" * 96)
    print("Headline: line_R (recall) vs med_inj (median injected tokens) — high recall at low tokens wins.")

    out_path = args.out or os.path.join(out_dir, "step0_summary.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nsummary -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
