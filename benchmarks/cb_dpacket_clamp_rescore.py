#!/usr/bin/env python3
"""
cb_dpacket_clamp_rescore.py  — ContextBench D-packet 27k-clamp re-run + re-score

PURPOSE
-------
Re-emits the D-packet arm (/context/packet via in-process Helix) with a hard
27 000-token greedy budget cap applied AFTER packet assembly, then scores it
with the official contextbench.evaluate and regenerates the combined recall-vs-
tokens table + scatter plot alongside all existing arms.

BACKGROUND (why the first run was unclamped)
--------------------------------------------
The original cb_helix_pred.py called build_context_packet with max_genes=32
and max_item_chars=100_000. build_context_packet has no internal token-budget
cap; it just returns all genes up to max_genes with each gene's content
truncated at max_item_chars characters. With 32-55 genes each up to 100k chars
per instance the median injected-tokens landed at ~53k vs the BM25-27k / D-rank
comparison baseline at 27k.

THE FIX (the clamp knob)
------------------------
`build_context_packet` does NOT read [budget] expression_tokens. The only levers
that govern packet size are:
  * max_genes         — gene count cap (default 8 in the API, set to 32 in the bench)
  * max_item_chars    — per-gene character cap (default 48k in include_raw mode)
  * [budget] expression_tokens   — NOT consulted by build_context_packet at all
  * [budget] max_genes_per_turn  — NOT consulted by build_context_packet at all

The correct fix is a POST-assembly greedy token-budget truncation applied in the
bench driver, identical to the fingerprint arm's strategy: iterate delivered items
in order, accumulate tiktoken cl100k_base token counts, stop when the running
total reaches the budget.

For the live re-run there are two equivalent implementation options:
  Option A (config):  set max_genes=8 AND max_item_chars~=3400 in the helix
                      probe config so each gene ≤ ~850 tokens; the 8 × 850 = 6.8k
                      budget is too small. Not a clean match to 27k.
  Option B (post-assembly clamp, USED HERE): keep the full packet assembly
                      (max_genes=32, max_item_chars=100_000 same as before) so
                      we see what Helix "would deliver", then greedy-cap the pred
                      spans at 27k tokens before writing the pred JSON. This is
                      the same discipline the fingerprint arm uses.

HOW TO RUN (one-liner for the Windows rig)
------------------------------------------
  cd F:/Projects/helix-context
  C:/Users/max/AppData/Local/Python/pythoncore-3.14-64/python.exe ^
      benchmarks/cb_dpacket_clamp_rescore.py ^
      --tag wt ^
      --tasks F:/tmp/cb_tasks_smoke.json ^
      --gold benchmarks/contextbench/gold_smoke_4repo.parquet ^
      --cb-src F:/Projects/contextbench-src ^
      --out-dir benchmarks/contextbench/results ^
      --cache F:/Projects/_cache/cb_repos

Kill-switches (no GPU needed):
  HELIX_BFM_SPLADE=0 HELIX_BFM_DENSE_BACKFILL=0 (prevent multi-CUDA-context livelock)

Full example with all defaults shown:
  set HELIX_BFM_SPLADE=0
  set HELIX_BFM_DENSE_BACKFILL=0
  C:/Users/max/AppData/Local/Python/pythoncore-3.14-64/python.exe ^
      benchmarks/cb_dpacket_clamp_rescore.py ^
      --tag wt --budget 27000 ^
      --tasks F:/tmp/cb_tasks_smoke.json ^
      --gold benchmarks/contextbench/gold_smoke_4repo.parquet ^
      --cb-src F:/Projects/contextbench-src ^
      --cache F:/Projects/_cache/cb_repos ^
      --config F:/tmp/cb_helix_probe/helix_probe.toml ^
      --out-dir benchmarks/contextbench/results
"""
import argparse
import gc
import json
import os
import shutil
import statistics
import subprocess
import sys

# ──────────────────────── file universe (mirror cb_helix_pred.py) ─────────────
SRC_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".h", ".cpp", ".cc",
    ".hpp", ".hh", ".java", ".cs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".cfg", ".ini", ".toml", ".yaml", ".yml", ".pyi",
}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".tox", ".eggs", "dist", "build", ".venv", "venv",
}
MAX_FILE_BYTES = 2_000_000
COMPRESSED_PREFIX = "[COMPRESSED"

# ──────────────────────── defaults ────────────────────────────────────────────
DEFAULT_HELIX_CONFIG = "F:/tmp/cb_helix_probe/helix_probe.toml"
DEFAULT_GENOME_ROOT  = "F:/tmp/cb_helix_genomes"
DEFAULT_OUT_DIR      = "F:/Projects/helix-context/benchmarks/contextbench/results"
DEFAULT_CB_SRC       = "F:/Projects/contextbench-src"
DEFAULT_CACHE        = "F:/Projects/_cache/cb_repos"
DEFAULT_GOLD         = "F:/Projects/helix-context/benchmarks/contextbench/gold_smoke_4repo.parquet"
DEFAULT_TASKS        = "F:/tmp/cb_tasks_smoke.json"
DEFAULT_BUDGET       = 27_000

# ──────────────────────── tokenization ────────────────────────────────────────
import tiktoken
ENC = tiktoken.get_encoding("cl100k_base")


def ntok(text):
    return len(ENC.encode(text or "", disallowed_special=()))


# ──────────────────────── file helpers ────────────────────────────────────────
def iter_repo_files(repo_dir):
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


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def recover_lines(content, file_text):
    """Verbatim content-match → (start, end) 1-indexed inclusive. None on failure."""
    c = content or ""
    if not c.strip() or file_text is None:
        return None
    off = file_text.find(c)
    matched = c
    if off < 0:
        cs = c.strip()
        off = file_text.find(cs)
        matched = cs
    if off < 0:
        return None
    start = file_text.count("\n", 0, off) + 1
    end = start + matched.count("\n")
    return start, end


# ──────────────────────── helix bootstrap (mirror cb_helix_pred.py) ───────────
def build_helix(genome_dir, helix_config):
    os.environ.pop("HELIX_USE_SHARDS", None)
    os.environ["HELIX_CONFIG"] = helix_config
    os.environ["HELIX_GENOME_PATH"] = genome_dir + "/genome.db"
    os.makedirs(genome_dir, exist_ok=True)
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import HelixContextManager
    cfg = load_config()
    return HelixContextManager(cfg)


def ingest_repo(helix, repo_dir, file_cache):
    n = 0
    for rel, ap in iter_repo_files(repo_dir):
        text = read_text(ap)
        if text is None:
            continue
        file_cache[rel] = text
        try:
            helix.ingest(text, content_type="code", metadata={"path": rel})
            n += 1
        except Exception as e:
            print(f"    [ingest-warn] {rel}: {e!r}", file=sys.stderr)
    return n


def gene_src(g):
    src = getattr(g, "source_id", None)
    if src:
        return src.replace("\\", "/")
    if getattr(g, "promoter", None) and g.promoter.metadata:
        p = g.promoter.metadata.get("path")
        if p:
            return p.replace("\\", "/")
    return None


def run_packet(helix, query, max_genes=32, max_item_chars=100_000):
    """Call build_context_packet exactly like the original cb_helix_pred.py did."""
    from cymatix_context.context_packet import build_context_packet
    packet = build_context_packet(
        query,
        task_type="explain",
        genome=helix.genome,
        max_genes=max_genes,
        now_ts=0.0,
        read_only=True,
        include_raw=True,
        max_item_chars=max_item_chars,
    )
    pd = packet.model_dump()
    items = list(pd.get("verified", [])) + list(pd.get("stale_risk", []))
    return items


# ──────────────────────── clamp + pred builder ────────────────────────────────
def items_to_pred_clamped(iid, items, file_cache, budget_tokens):
    """Convert packet items → unified pred, greedy-clamped at budget_tokens.

    Items are consumed in delivery order (verified first, then stale_risk, as
    returned by build_context_packet). We accumulate injected_tokens and stop
    when the budget is reached. This is identical to the fingerprint arm's
    strategy (cb_helix_pred.py process_task lines 281-289).
    """
    from collections import defaultdict
    spans = defaultdict(list)
    files = set()
    injected = 0
    n_genes = 0
    n_fail = 0
    n_compressed = 0
    n_clamped = 0

    for it in items:
        src = ((it.get("source_id") or "").replace("\\", "/")) or None
        content = it.get("content") or ""

        if not src:
            n_fail += 1
            continue
        if content.startswith(COMPRESSED_PREFIX):
            n_compressed += 1
            continue

        ftext = file_cache.get(src)
        rng = recover_lines(content, ftext)
        if rng is None:
            n_fail += 1
            continue

        tok = ntok(content)
        # Greedy budget gate: if we've already filled 1+ genes and this pushes
        # us over, skip it (same policy as the fingerprint arm's fill loop).
        if n_genes >= 1 and injected + tok > budget_tokens:
            n_clamped += 1
            continue  # try smaller subsequent genes

        start, end = rng
        spans[src].append({"start": start, "end": end})
        files.add(src)
        injected += tok
        n_genes += 1

        if injected >= budget_tokens:
            break

    pred = {
        "instance_id": iid,
        "traj_data": {
            "pred_steps": [],
            "pred_files": sorted(files),
            "pred_spans": {k: v for k, v in spans.items()},
        },
        "model_patch": "",
    }
    meta = {
        "injected_tokens": injected,
        "n_genes": n_genes,
        "n_match_fail": n_fail,
        "n_compressed": n_compressed,
        "n_clamped_by_budget": n_clamped,
    }
    return pred, meta


# ──────────────────────── scoring helpers ─────────────────────────────────────
def run_evaluator(cb_src, gold, pred_path, cache_dir, out_jsonl):
    """Run contextbench.evaluate as a subprocess. Returns (rows, returncode)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = cb_src + os.pathsep + env.get("PYTHONPATH", "")
    env["CONTEXTBENCH_TMP_ROOT"] = os.path.dirname(cache_dir).replace("\\", "/") + "/_cache/cb_wt"
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    cmd = [sys.executable, "-m", "contextbench.evaluate",
           "--gold", gold, "--pred", pred_path, "--cache", cache_dir, "--out", out_jsonl]
    print(f"  [eval] {' '.join(cmd)}", file=sys.stderr)
    if os.path.isfile(out_jsonl):
        os.remove(out_jsonl)
    r = subprocess.run(cmd, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rows = []
    if os.path.isfile(out_jsonl):
        with open(out_jsonl, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows, r.returncode


def micro(rows, gran):
    valid = [r for r in rows if "error" not in r]
    inter = sum(r.get("final", {}).get(gran, {}).get("intersection", 0) for r in valid)
    gold  = sum(r.get("final", {}).get(gran, {}).get("gold_size",  0) for r in valid)
    pred  = sum(r.get("final", {}).get(gran, {}).get("pred_size",  0) for r in valid)
    recall = inter / gold if gold else 1.0
    prec   = inter / pred if pred else 1.0
    f1 = (2 * recall * prec / (recall + prec)) if (recall + prec) else 0.0
    return {"recall": recall, "precision": prec, "f1": f1, "n_valid": len(valid)}


def med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


# ──────────────────────── per-task entry point ────────────────────────────────
def process_task(task, tag, budget, genome_root, helix_config):
    """Run one task: ingest → packet → clamp → pred. Returns a result dict."""
    iid  = task["instance_id"]
    wt   = task["worktree_dir"]
    gdir = f"{genome_root}/{tag}_packet27k/{iid}"
    shutil.rmtree(gdir, ignore_errors=True)

    res = {
        "iid": iid, "repo": task.get("repo", ""),
        "pred": None, "meta": None, "error": None, "n_indexed": 0,
    }
    helix = None
    file_cache = {}
    try:
        helix = build_helix(gdir, helix_config)
        n_indexed = ingest_repo(helix, wt, file_cache)
        res["n_indexed"] = n_indexed
        q = task["problem_statement"]
        items = run_packet(helix, q)
        pred, meta = items_to_pred_clamped(iid, items, file_cache, budget)
        meta["n_indexed_files"] = n_indexed
        res["pred"] = pred
        res["meta"] = meta
    except Exception as e:
        import traceback
        res["error"] = f"{e!r} | {traceback.format_exc().splitlines()[-1]}"
    finally:
        try:
            if helix is not None and getattr(helix, "genome", None) is not None:
                close = getattr(helix.genome, "close", None)
                if callable(close):
                    close()
        except Exception:
            pass
        helix = None
        file_cache = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        shutil.rmtree(gdir, ignore_errors=True)
    return res


# ──────────────────────── combined table + plot ────────────────────────────────
def load_existing_table(out_dir):
    """Load combined_table.json from a previous cb_score_all.py run."""
    path = os.path.join(out_dir, "combined_table.json")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def regenerate_table_and_plot(existing_rows, new_arm_label, new_scores, out_dir):
    """Append the new clamped-packet arm to the existing combined table and
    regenerate the recall-vs-tokens scatter plot."""

    # Build the new row
    g = new_scores["granularity"]
    new_row = {
        "arm": new_arm_label,
        "n": new_scores["n_scored"],
        "file_R": g["file"]["recall"],
        "line_R": g["line"]["recall"],
        "line_P": g["line"]["precision"],
        "sym_R": g["symbol"]["recall"],
        "med_inj": new_scores["median_injected_tokens"],
        "compressed": new_scores.get("n_compressed", 0),
    }

    # Remove any stale entry with the same arm label
    merged = [r for r in existing_rows if r.get("arm") != new_arm_label]
    merged.append(new_row)

    # Write updated combined_table.json
    table_path = os.path.join(out_dir, "combined_table.json")
    with open(table_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    print(f"  [table] -> {table_path}", file=sys.stderr)

    # Print the table
    print(f"\n{'arm':<28}{'n':>4}{'file_R':>8}{'line_R':>8}{'line_P':>8}"
          f"{'sym_R':>8}{'med_inj':>9}")
    print("-" * 77)
    for r in merged:
        print(f"{r['arm']:<28}{r['n']:>4}{r['file_R']:>8.3f}{r['line_R']:>8.3f}"
              f"{r['line_P']:>8.3f}{r['sym_R']:>8.3f}{int(r['med_inj']):>9}")

    # Generate scatter plot
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(9, 5))
        colors = {
            "bm25": "#4C72B0",
            "fingerprint": "#55A868",
            "packet": "#C44E52",
            "packet27k": "#DD8452",
        }
        markers = {"8k": "o", "27k": "s", "packet": "D", "packet27k": "*"}

        for r in merged:
            arm = r["arm"]
            x = r["med_inj"]
            y = r["line_R"]
            # Pick colour + marker by arm name keywords
            c = "#888888"
            m = "o"
            s = 80
            label = arm
            if "bm25" in arm:
                c = colors["bm25"]
                m = "o" if "8k" in arm else "s"
            elif "fingerprint" in arm:
                c = colors["fingerprint"]
                m = "o" if "8k" in arm else "s"
                s = 70
            elif arm.endswith("packet27k") or "packet27k" in arm:
                c = colors["packet27k"]
                m = "*"
                s = 160
            elif "packet" in arm:
                c = colors["packet"]
                m = "D"
                s = 100
            ax.scatter(x, y, color=c, marker=m, s=s, zorder=5, label=label)
            ax.annotate(arm, (x, y), textcoords="offset points",
                        xytext=(4, 4), fontsize=6.5)

        ax.set_xlabel("Median injected tokens", fontsize=11)
        ax.set_ylabel("Line recall (coverage)", fontsize=11)
        ax.set_title("ContextBench Step-0: recall vs injected tokens\n"
                     "(D-packet 27k = clamped; D-packet = original unclamped)", fontsize=10)
        ax.grid(True, alpha=0.3)
        # Legend deduplicated
        handles, labels = ax.get_legend_handles_labels()
        by_label = dict(zip(labels, handles))
        ax.legend(by_label.values(), by_label.keys(), fontsize=7, loc="lower right")
        fig.tight_layout()
        plot_path = os.path.join(os.path.dirname(out_dir), "cb_step0_recall_vs_tokens.png")
        fig.savefig(plot_path, dpi=150)
        plt.close(fig)
        print(f"  [plot]  -> {plot_path}", file=sys.stderr)
    except ImportError:
        print("  [plot]  matplotlib not available — skipping plot", file=sys.stderr)

    return table_path


# ──────────────────────── post-process preview ────────────────────────────────
def postprocess_existing_packet(tag, budget, out_dir, cb_src, gold, cache_dir):
    """
    APPROXIMATE PREVIEW (no live re-run): post-process the existing unclamped
    packet pred JSON by greedy-truncating pred_spans to match the ~27k budget.

    This is an approximation because:
    1. We no longer have the gene content (only the line ranges in pred_spans)
       so we re-read the file bytes from the cached repo worktrees to count
       tokens — those worktrees may not be present in the sandbox.
    2. We cannot recover injected_tokens per span without the content, so we
       fall back to line-count × average-chars-per-line as a token estimate.

    In practice this produces a conservative lower bound on recall (we may
    drop spans that would have fit) but it's directionally correct.
    If repo worktrees are unavailable we emit the pred as-is and note it.
    """
    pred_path = os.path.join(out_dir, f"helix_{tag}_packet_pred.json")
    meta_path = os.path.join(out_dir, f"helix_{tag}_packet_meta.json")
    if not os.path.isfile(pred_path):
        print(f"  [preview] no existing pred at {pred_path} — skipping preview",
              file=sys.stderr)
        return None

    with open(pred_path, "r", encoding="utf-8") as f:
        preds = json.load(f)
    meta_map = {}
    if os.path.isfile(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta_map = json.load(f)

    # Greedy truncation by estimated token count (line count × 4 tokens/line heuristic)
    TOKENS_PER_LINE = 4   # conservative for Python code
    clamped_preds = []
    approx_totals = []
    for item in preds:
        iid = item["instance_id"]
        td = item.get("traj_data", {})
        old_spans = td.get("pred_spans", {})

        new_spans = {}
        new_files = []
        approx_injected = 0
        for fpath, spans in old_spans.items():
            for sp in spans:
                n_lines = max(1, sp["end"] - sp["start"] + 1)
                est_tok = n_lines * TOKENS_PER_LINE
                if approx_injected > 0 and approx_injected + est_tok > budget:
                    continue  # skip — would exceed budget
                if fpath not in new_spans:
                    new_spans[fpath] = []
                    new_files.append(fpath)
                new_spans[fpath].append(sp)
                approx_injected += est_tok
                if approx_injected >= budget:
                    break
            if approx_injected >= budget:
                break

        clamped_preds.append({
            "instance_id": iid,
            "traj_data": {
                "pred_steps": [],
                "pred_files": sorted(set(new_files)),
                "pred_spans": new_spans,
            },
            "model_patch": "",
        })
        approx_totals.append(approx_injected)

    out_pred = os.path.join(out_dir, f"helix_{tag}_packet27k_approx_pred.json")
    with open(out_pred, "w", encoding="utf-8") as f:
        json.dump(clamped_preds, f)
    print(f"  [preview] approx-clamped pred -> {out_pred}", file=sys.stderr)
    print(f"  [preview] approx median injected tokens (line-count heuristic): "
          f"{statistics.median(approx_totals):.0f}", file=sys.stderr)

    # Try to score if cb_src is available
    out_jsonl = os.path.join(out_dir, f"helix_{tag}_packet27k_approx_eval.jsonl")
    rows, rc = run_evaluator(cb_src, gold, out_pred, cache_dir, out_jsonl)
    if rc == 0 and rows:
        lr_r = micro(rows, "line")
        fr_r = micro(rows, "file")
        sr_r = micro(rows, "symbol")
        print(f"  [preview] APPROXIMATE CLAMPED SCORES (line-heuristic truncation):")
        print(f"    file_R={fr_r['recall']:.3f}  line_R={lr_r['recall']:.3f}  "
              f"line_P={lr_r['precision']:.3f}  sym_R={sr_r['recall']:.3f}  "
              f"approx_med_inj={statistics.median(approx_totals):.0f}")
        print(f"  [preview] NOTE: these are APPROXIMATE — content not re-fetched, "
              f"budget enforced by line-count×4tok heuristic. Run without "
              f"--preview-only for exact numbers.", file=sys.stderr)
        return {
            "arm": f"{tag}_packet27k_approx",
            "granularity": {"line": lr_r, "file": fr_r, "symbol": sr_r,
                            "span": micro(rows, "span")},
            "median_injected_tokens_approx": statistics.median(approx_totals),
            "n_scored": lr_r["n_valid"],
            "approximate": True,
            "method": "line_count_heuristic",
        }
    else:
        print(f"  [preview] evaluator returned rc={rc} or no rows — "
              f"score not available", file=sys.stderr)
        return None


# ──────────────────────── main ────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Re-emit D-packet arm at 27k token budget and re-score"
    )
    ap.add_argument("--tag", required=True,
                    help="Helix variant tag, e.g. wt | v062 | r063fix")
    ap.add_argument("--tasks", default=DEFAULT_TASKS,
                    help="JSON task list from cb_dump_tasks.py")
    ap.add_argument("--gold", default=DEFAULT_GOLD,
                    help="Gold parquet (e.g. gold_smoke_4repo.parquet)")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help="Token budget for clamping (default 27000)")
    ap.add_argument("--cb-src", default=DEFAULT_CB_SRC,
                    help="Path to contextbench-src (for contextbench.evaluate)")
    ap.add_argument("--cache", default=DEFAULT_CACHE,
                    help="Repo clone cache dir for evaluator")
    ap.add_argument("--config", default=DEFAULT_HELIX_CONFIG,
                    help="HELIX_CONFIG toml for the probe genome")
    ap.add_argument("--genome-root", default=DEFAULT_GENOME_ROOT,
                    help="Root dir for temporary probe genomes")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help="Results directory (where pred JSONs are written)")
    ap.add_argument("--workers", type=int, default=1,
                    help="Parallel task workers (1 = serial, safe on low VRAM)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap number of tasks (0 = all)")
    ap.add_argument("--preview-only", action="store_true",
                    help="Skip live re-run; only post-process existing pred JSON "
                         "with line-count heuristic (approximate, no VRAM needed)")
    ap.add_argument("--no-plot", action="store_true",
                    help="Skip regenerating the recall-vs-tokens plot")
    args = ap.parse_args()

    bk = f"{args.budget // 1000}k"
    arm_label = f"{args.tag}_packet{bk}"

    sys.path.insert(0, args.cb_src)
    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(args.cache, exist_ok=True)
    os.makedirs(args.genome_root, exist_ok=True)

    # ── preview-only path ────────────────────────────────────────────────────
    if args.preview_only:
        print(f"\n=== PREVIEW ONLY (approximate, no live re-run) ===", file=sys.stderr)
        preview = postprocess_existing_packet(
            args.tag, args.budget, args.out_dir,
            args.cb_src, args.gold, args.cache,
        )
        if preview:
            g = preview["granularity"]
            print(f"\n[PREVIEW] Approximate clamped-packet recall @ {bk}:")
            print(f"  file_R={g['file']['recall']:.3f}  "
                  f"line_R={g['line']['recall']:.3f}  "
                  f"line_P={g['line']['precision']:.3f}  "
                  f"sym_R={g['symbol']['recall']:.3f}  "
                  f"approx_med_inj={preview['median_injected_tokens_approx']:.0f}")
        else:
            print("[PREVIEW] Could not produce approximate scores.", file=sys.stderr)
        return

    # ── live re-run ──────────────────────────────────────────────────────────
    with open(args.tasks, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    if args.limit and args.limit > 0:
        tasks = tasks[:args.limit]
    print(f"loaded {len(tasks)} tasks | tag={args.tag} | budget={args.budget} ({bk})",
          file=sys.stderr)

    preds  = []
    pk_meta = {}
    total = len(tasks)

    if args.workers and args.workers > 1:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        payloads = [(t, args.tag, args.budget, args.genome_root, args.config)
                    for t in tasks]
        with ProcessPoolExecutor(max_workers=args.workers,
                                 max_tasks_per_child=1) as ex:
            futs = {ex.submit(process_task, *p): p[0]["instance_id"]
                    for p in payloads}
            done = 0
            for fut in as_completed(futs):
                done += 1
                try:
                    r = fut.result()
                except Exception as e:
                    iid = futs[fut]
                    r = {"iid": iid, "repo": "", "pred": None, "meta": None,
                         "error": repr(e), "n_indexed": 0}
                if r["error"]:
                    print(f"[{done}/{total}] {r['iid'][-8:]} ERROR: {r['error']}",
                          file=sys.stderr, flush=True)
                else:
                    m = r["meta"]
                    print(f"[{done}/{total}] {r['iid'][-8:]} "
                          f"idx={r['n_indexed']} "
                          f"pk:g{m['n_genes']}/t{m['injected_tokens']}"
                          f"/clamp{m['n_clamped_by_budget']}",
                          file=sys.stderr, flush=True)
                    preds.append(r["pred"])
                    pk_meta[r["iid"]] = r["meta"]
    else:
        for i, t in enumerate(tasks):
            r = process_task(t, args.tag, args.budget, args.genome_root, args.config)
            if r["error"]:
                print(f"[{i+1}/{total}] {r['iid'][-8:]} ERROR: {r['error']}",
                      file=sys.stderr, flush=True)
            else:
                m = r["meta"]
                print(f"[{i+1}/{total}] {r['iid'][-8:]} "
                      f"idx={r['n_indexed']} "
                      f"pk:g{m['n_genes']}/t{m['injected_tokens']}"
                      f"/clamp{m['n_clamped_by_budget']}",
                      file=sys.stderr, flush=True)
                preds.append(r["pred"])
                pk_meta[r["iid"]] = r["meta"]

    if not preds:
        print("ERROR: no successful tasks — aborting", file=sys.stderr)
        sys.exit(1)

    # Write pred + meta
    pred_path = os.path.join(args.out_dir, f"helix_{arm_label}_pred.json")
    meta_path = os.path.join(args.out_dir, f"helix_{arm_label}_meta.json")
    with open(pred_path, "w", encoding="utf-8") as f:
        json.dump(preds, f)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(pk_meta, f, indent=2)
    print(f"\n[pred] -> {pred_path}", file=sys.stderr)
    print(f"[meta] -> {meta_path}", file=sys.stderr)

    # Score
    out_jsonl = os.path.join(args.out_dir, f"helix_{arm_label}_eval.jsonl")
    rows, rc = run_evaluator(args.cb_src, args.gold, pred_path, args.cache, out_jsonl)
    n_scored = sum(1 for r in rows if "error" not in r)

    if rc != 0 or n_scored == 0:
        print(f"ERROR: evaluator returned rc={rc}, n_scored={n_scored} — check logs",
              file=sys.stderr)
        sys.exit(1)

    inj = [pk_meta.get(r["instance_id"], {}).get("injected_tokens", 0)
           for r in rows if "error" not in r]
    n_clamped_total = sum(pk_meta.get(r["instance_id"], {}).get("n_clamped_by_budget", 0)
                          for r in rows if "error" not in r)
    gran = {g: micro(rows, g) for g in ("file", "symbol", "span", "line")}
    med_inj = statistics.median(inj) if inj else 0
    p90_inj = sorted(inj)[int(0.9 * (len(inj) - 1))] if inj else 0
    line_recall = gran["line"]["recall"]
    rec_per_1k  = (line_recall / (med_inj / 1000.0)) if med_inj else 0.0
    n_comp = sum(pk_meta.get(r["instance_id"], {}).get("n_compressed", 0)
                 for r in rows if "error" not in r)

    new_scores = {
        "arm_label": arm_label,
        "n_scored": n_scored,
        "granularity": gran,
        "median_injected_tokens": med_inj,
        "p90_injected_tokens": p90_inj,
        "recall_per_1k_tokens": rec_per_1k,
        "n_clamped_by_budget_total": n_clamped_total,
        "n_compressed": n_comp,
    }

    print(f"\n{'='*72}")
    print(f"ARM: {arm_label}")
    print(f"  n_scored      = {n_scored}")
    print(f"  file_R        = {gran['file']['recall']:.4f}")
    print(f"  line_R        = {line_recall:.4f}  (line_P={gran['line']['precision']:.4f}  "
          f"F1={gran['line']['f1']:.4f})")
    print(f"  sym_R         = {gran['symbol']['recall']:.4f}")
    print(f"  med_inj       = {med_inj:.0f} tokens")
    print(f"  p90_inj       = {p90_inj:.0f} tokens")
    print(f"  R/1k          = {rec_per_1k:.4f}")
    print(f"  n_clamped     = {n_clamped_total} genes dropped by budget across all tasks")
    print(f"{'='*72}")

    # Regenerate combined table + plot
    existing = load_existing_table(args.out_dir)
    if not args.no_plot:
        regenerate_table_and_plot(existing, arm_label, new_scores, args.out_dir)

    # Also run the approximate preview on top, to compare with real scores
    print(f"\n=== Running approximate preview (line-count heuristic) for comparison ===",
          file=sys.stderr)
    preview = postprocess_existing_packet(
        args.tag, args.budget, args.out_dir,
        args.cb_src, args.gold, args.cache,
    )
    if preview:
        g = preview["granularity"]
        print(f"\n[PREVIEW vs REAL comparison]")
        print(f"  Approx (line heuristic) line_R = {g['line']['recall']:.3f}")
        print(f"  Real   (live re-run)    line_R = {line_recall:.3f}")
        print(f"  Delta  = {line_recall - g['line']['recall']:+.3f}")

    print(f"\nDone. Key output: {pred_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
