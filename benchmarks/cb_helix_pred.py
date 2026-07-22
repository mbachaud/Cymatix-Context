#!/usr/bin/env python3
"""ContextBench Step-0 arm-D: in-process Helix retrieval -> unified-format preds.

Run TWICE:
  helix062 venv  -> --tag v062   (shipped helix-context 0.6.2)
  bgem3   venv   -> --tag wt      (vibrant-easley perf worktree)

Per task: fresh isolated genome under F:/tmp/cb_helix_genomes/<tag>/<iid>/, ingest the
worktree's indexable files (same file-universe constants as the BM25 arm), run the
LLM-free fingerprint + packet recipe, recover 1-indexed line ranges by VERBATIM
content-match (no fabrication — failures are recorded and skipped), tally injected
tokens with cl100k_base (same tokenizer as BM25). rmtree the genome after each task.

Emits per (mode,budget) a unified-format pred list:
  helix_<tag>_fingerprint_8k_pred.json, ..._fingerprint_27k_pred.json, ..._packet_pred.json
plus a meta sidecar ..._meta.json = {iid: {injected_tokens, n_genes, n_match_fail, n_indexed_files}}.

Read-only on helix + contextbench source. No server / no ports touched (in-process).
HARMLESS: torch "hardware candidate cuda failed" tracebacks on helix062 are CPU-fallback noise.
"""
import argparse
import json
import os
import shutil
import sys

# ---- file universe (MUST match contextbench_step0.py exactly) ----------------
# Code + core docs only. High-volume non-code (.po/.html/.json/.css/.xml...) is dropped:
# Helix's per-file spaCy ingest chokes on django's thousands of .po files, and they are
# never code gold. Kept symmetric with the BM25 arm. The gold-file warn flags any drop.
SRC_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".h", ".cpp", ".cc",
    ".hpp", ".hh", ".java", ".cs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".cfg", ".ini", ".toml", ".yaml", ".yml", ".pyi",
}
# Helix compacts large genes -> content becomes "[COMPRESSED:euchromatin] source=...".
# Such genes cannot be line-mapped; they hit large DOCS prose (.txt/.rst/.md), never .py code
# gold, so docs are dropped above. This guard skips any residual compressed gene (never mis-mapped).
COMPRESSED_PREFIX = "[COMPRESSED"
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".tox", ".eggs", "dist", "build", ".venv", "venv",
}
MAX_FILE_BYTES = 2_000_000

HELIX_CONFIG = "F:/tmp/cb_helix_probe/helix_probe.toml"
GENOME_ROOT = "F:/tmp/cb_helix_genomes"
GOLD = "F:/Projects/helix-context/benchmarks/contextbench/gold_smoke_4repo.parquet"

import tiktoken  # noqa: E402
ENC = tiktoken.get_encoding("cl100k_base")


def ntok(text):
    return len(ENC.encode(text or "", disallowed_special=()))


def iter_repo_files(repo_dir):
    """Yield (rel_path forward-slashed, abs_path) for indexable files. Mirrors BM25 arm."""
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


def read_text(abs_path):
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def recover_lines(content, file_text):
    """Return (start, end) 1-indexed inclusive by verbatim content-match, or None on failure."""
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


def load_gold_files(tasks):
    """{iid: list(gold rel files)} from the tasks JSON (emitted by cb_dump_tasks.py).
    Used only for the 3-task retrieval sanity check."""
    return {t["instance_id"]: t.get("gold_files", []) for t in tasks}


def _patch_dense_gpu():
    """Move the BGE-M3 dense codec to CUDA. Results are IDENTICAL to CPU (same model,
    same vectors) — only faster. v0.6.2/worktree build BGEM3Codec(dim=...) with no device
    arg, defaulting to cpu; this subclass forces cuda. (SPLADE already uses get_hardware().device.)"""
    try:
        from cymatix_context.backends import bgem3_codec as _bc
    except Exception:  # noqa: BLE001
        return
    if getattr(_bc, "_cb_gpu_patched", False):
        return
    _Orig = _bc.BGEM3Codec

    class _GpuBGEM3(_Orig):  # noqa: N801
        def __init__(self, dim=1024, device="cpu", model_name="BAAI/bge-m3"):
            super().__init__(dim=dim, device="cuda", model_name=model_name)

    _bc.BGEM3Codec = _GpuBGEM3
    _bc._cb_gpu_patched = True


def build_helix(genome_dir):
    """Fresh in-process Helix on an isolated genome. Imports helix AFTER env is set."""
    os.environ.pop("HELIX_USE_SHARDS", None)
    os.environ.setdefault("HELIX_CONFIG", HELIX_CONFIG)  # main sets the real path; fallback only
    os.environ["HELIX_GENOME_PATH"] = genome_dir + "/genome.db"
    os.makedirs(genome_dir, exist_ok=True)
    if os.environ.get("CB_DENSE_DEVICE", "cpu").strip().lower() == "cuda":
        _patch_dense_gpu()
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import HelixContextManager
    cfg = load_config()
    return HelixContextManager(cfg)


# Release CUDA caching-allocator memory every N ingested files. helix.ingest() dense-encodes
# each file, and torch caches a distinct block size-class per input shape; over a large repo
# (~1k+ files of varying size) those size-classes accumulate and VRAM climbs to the card
# ceiling even on ONE worker (measured: 11.7GB/95% on a 12GB 3080 Ti; expandable_segments
# alone only got the *start* down to 7.7GB, it still climbed within a single task). A periodic
# empty_cache() inside the loop bounds the within-task peak. This is a STOP-GAP for the bench;
# the real fix belongs in helix's dense ingest path (see issue). Off unless dense on cuda.
VRAM_RELEASE_EVERY = int(os.environ.get("CB_VRAM_RELEASE_EVERY", "100"))


def _release_vram():
    if os.environ.get("CB_DENSE_DEVICE", "cpu").strip().lower() != "cuda":
        return
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def ingest_repo(helix, repo_dir, file_cache):
    """Ingest every indexable file as whole-file code blobs. Populates file_cache[rel]=text.
    Returns n_indexed_files."""
    n = 0
    for rel, ap in iter_repo_files(repo_dir):
        text = read_text(ap)
        if text is None:
            continue
        file_cache[rel] = text
        try:
            helix.ingest(text, content_type="code", metadata={"path": rel})
            n += 1
            if n % VRAM_RELEASE_EVERY == 0:
                _release_vram()
        except Exception as e:  # noqa: BLE001
            print(f"    [ingest-warn] {rel}: {e!r}", file=sys.stderr)
    return n


def gene_src(g):
    """rel path for a gene: source_id (== metadata path) preferred, fall back to promoter meta."""
    src = getattr(g, "source_id", None)
    if src:
        return src.replace("\\", "/")
    if getattr(g, "promoter", None) and g.promoter.metadata:
        p = g.promoter.metadata.get("path")
        if p:
            return p.replace("\\", "/")
    return None


def run_fingerprint(helix, q, max_results=400):
    """LLM-free fingerprint. Returns ranked list of genes (by last_query_scores desc)."""
    eq, dom, ent = helix._prepare_query_signals(q, session_context=None, expand_query=False)
    cands = helix._retrieve(dom, ent, max_results, query_text=q, include_cold=None,
                            party_id="default", use_harmonic=False, use_sr=False)
    cands, contrib = helix._apply_candidate_refiners(
        q, cands, max_results, use_cymatics=False, use_harmonic_bin=False,
        use_tcm=True, allow_rerank=False)
    scores = dict(helix.genome.last_query_scores or {})
    ranked = sorted(cands, key=lambda g: scores.get(g.gene_id, 0.0), reverse=True)
    return ranked


def run_packet(helix, q):
    """Delivered packet items (verified + stale_risk). Each dict has gene_id, content, source_id."""
    from cymatix_context.context_packet import build_context_packet
    packet = build_context_packet(q, task_type="explain", genome=helix.genome, max_genes=32,
                                  now_ts=0.0, read_only=True, include_raw=True,
                                  max_item_chars=100000)
    pd = packet.model_dump()
    return list(pd.get("verified", [])) + list(pd.get("stale_risk", []))


def genes_to_pred(iid, items, file_cache):
    """items: list of (src, content). Build unified pred + (injected_tokens, n_genes, n_match_fail).
    Lines recovered by verbatim content-match; failures recorded+skipped (never fabricated)."""
    from collections import defaultdict
    spans = defaultdict(list)
    files = set()
    injected = 0
    n_genes = 0
    n_fail = 0
    n_compressed = 0
    for src, content in items:
        if not src:
            n_fail += 1
            continue
        if (content or "").startswith(COMPRESSED_PREFIX):
            n_compressed += 1  # Helix-compacted gene: not line-mappable (separate from a real fail)
            continue
        ftext = file_cache.get(src)
        rng = recover_lines(content, ftext)
        if rng is None:
            n_fail += 1
            continue
        start, end = rng
        spans[src].append({"start": start, "end": end})
        files.add(src)
        injected += ntok(content)
        n_genes += 1
    pred = {
        "instance_id": iid,
        "traj_data": {"pred_steps": [], "pred_files": sorted(files),
                      "pred_spans": {k: v for k, v in spans.items()}},
        "model_patch": "",
    }
    meta = {"injected_tokens": injected, "n_genes": n_genes,
            "n_match_fail": n_fail, "n_compressed": n_compressed}
    return pred, meta


def process_task(payload):
    """Run one task end-to-end in an isolated process (fresh genome). Returns a result dict.
    Top-level + picklable so it works under ProcessPoolExecutor (spawn) on Windows."""
    task, tag, budgets, modes, gold_files = payload
    iid = task["instance_id"]
    wt = task["worktree_dir"]
    gdir = f"{GENOME_ROOT}/{tag}/{iid}"
    shutil.rmtree(gdir, ignore_errors=True)
    res = {"iid": iid, "repo": task.get("repo", ""), "fp": {}, "packet": None, "error": None,
           "n_indexed": 0, "n_ranked": 0, "gold_in_ranked": 0, "n_gold": len(gold_files or [])}
    do_fp = "fingerprint" in modes
    do_pk = "packet" in modes
    helix = None
    file_cache = {}
    try:
        helix = build_helix(gdir)
        n_indexed = ingest_repo(helix, wt, file_cache)
        res["n_indexed"] = n_indexed
        q = task["problem_statement"]
        ranked = run_fingerprint(helix, q, max_results=400) if do_fp else []
        ranked_items = [(gene_src(g), (g.content or "")) for g in ranked]
        res["n_ranked"] = len(ranked_items)
        gset = set(gold_files or [])
        res["gold_in_ranked"] = len(gset & {s for s, _ in ranked_items if s})
        if do_fp:
            for b in budgets:
                sel = []
                inj = 0
                for s, c in ranked_items:
                    if s is None or not (c or "").strip() or c.startswith(COMPRESSED_PREFIX):
                        continue
                    sel.append((s, c))
                    inj += ntok(c)
                    if inj >= b:
                        break
                pred, meta = genes_to_pred(iid, sel, file_cache)
                meta["n_indexed_files"] = n_indexed
                res["fp"][b] = (pred, meta)
        if do_pk:
            items = [(((it.get("source_id") or "").replace("\\", "/")) or None, it.get("content") or "")
                     for it in run_packet(helix, q)]
            pred, meta = genes_to_pred(iid, items, file_cache)
            meta["n_indexed_files"] = n_indexed
            res["packet"] = (pred, meta)
    except Exception as e:  # noqa: BLE001
        import traceback
        res["error"] = f"{e!r} | {traceback.format_exc().splitlines()[-1]}"
    finally:
        try:
            if helix is not None and getattr(helix, "genome", None) is not None:
                close = getattr(helix.genome, "close", None)
                if callable(close):
                    close()
        except Exception:  # noqa: BLE001
            pass
        helix = None
        file_cache = None
        import gc as _gc
        _gc.collect()
        # Serial (--workers 1) reuses one process across all tasks, so torch's CUDA caching
        # allocator accumulates to the largest-task peak and holds it (medium repos -> ~12GB,
        # 95% of a 12GB card). Release cached VRAM back to the OS after each task so the next
        # task starts near baseline instead of stacking. (Pool path already gets a fresh proc.)
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(gdir, ignore_errors=True)
    return res


def _print_result(n, total, r):
    if r["error"]:
        print(f"[{n}/{total}] {r['iid'][-8:]} {r['repo']} ERROR: {r['error']}", file=sys.stderr, flush=True)
        return
    fp = " ".join(f"fp{b//1000}k:g{m['n_genes']}/t{m['injected_tokens']}/cz{m['n_compressed']}"
                  for b, (p, m) in sorted(r["fp"].items()))
    pk = r["packet"][1] if r["packet"] else None
    pkinfo = f"pk:g{pk['n_genes']}/t{pk['injected_tokens']}/cz{pk['n_compressed']}" if pk else ""
    print(f"[{n}/{total}] {r['iid'][-8:]} {r['repo']:<22} idx={r['n_indexed']} "
          f"gold_in_rank={r['gold_in_ranked']}/{r['n_gold']} {fp} {pkinfo}", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description="ContextBench Step-0 Helix arm-D predictor")
    ap.add_argument("--tasks", default="F:/tmp/cb_tasks_smoke.json")
    ap.add_argument("--tag", required=True, help="v062 | wt")
    ap.add_argument("--out-dir", default="F:/Projects/helix-context/benchmarks/contextbench/results")
    ap.add_argument("--budgets", default="8000,27000")
    ap.add_argument("--modes", default="fingerprint,packet")
    ap.add_argument("--validate-only", action="store_true", help="only first 3 tasks; don't write preds")
    ap.add_argument("--workers", type=int, default=1, help="parallel task workers (one process+genome each)")
    ap.add_argument("--config", default=HELIX_CONFIG, help="HELIX_CONFIG toml (v1=lexical, v2=dense+splade)")
    ap.add_argument("--dense-device", default="cpu", choices=["cpu", "cuda"],
                    help="device for BGE-M3 dense codec (cuda = results-identical speedup)")
    args = ap.parse_args()

    # Set before spawning workers so they inherit (Windows spawn copies os.environ).
    os.environ["HELIX_CONFIG"] = args.config
    os.environ["CB_DENSE_DEVICE"] = args.dense_device

    budgets = [int(b) for b in args.budgets.split(",") if b.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    with open(args.tasks, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    gold_map = load_gold_files(tasks)
    os.makedirs(args.out_dir, exist_ok=True)

    # accumulators: (mode,budget) -> list of preds; meta sidecars per output
    fp_preds = {b: [] for b in budgets}          # fingerprint @ budget
    fp_meta = {b: {} for b in budgets}
    pk_preds = []                                # packet
    pk_meta = {}
    do_fp = "fingerprint" in modes
    do_pk = "packet" in modes

    if args.validate_only:
        tasks = tasks[:3]
    payloads = [(t, args.tag, budgets, modes, gold_map.get(t["instance_id"], [])) for t in tasks]
    total = len(payloads)
    results = []
    if args.workers and args.workers > 1:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        print(f"running {total} tasks with {args.workers} workers (tag={args.tag})",
              file=sys.stderr, flush=True)
        with ProcessPoolExecutor(max_workers=args.workers, max_tasks_per_child=1) as ex:
            futs = {ex.submit(process_task, p): p[0]["instance_id"] for p in payloads}
            done = 0
            for fut in as_completed(futs):
                done += 1
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001
                    r = {"iid": futs[fut], "repo": "", "fp": {}, "packet": None,
                         "error": repr(e), "n_indexed": 0, "n_ranked": 0,
                         "gold_in_ranked": 0, "n_gold": 0}
                results.append(r)
                _print_result(done, total, r)
    else:
        for i, p in enumerate(payloads):
            r = process_task(p)
            results.append(r)
            _print_result(i + 1, total, r)

    # aggregate into output accumulators
    n_err = 0
    for r in results:
        if r["error"]:
            n_err += 1
            continue
        for b, (pred, meta) in r["fp"].items():
            fp_preds[b].append(pred)
            fp_meta[b][r["iid"]] = meta
        if r["packet"]:
            pred, meta = r["packet"]
            pk_preds.append(pred)
            pk_meta[r["iid"]] = meta
    print(f"\naggregated {total - n_err}/{total} tasks ({n_err} errored)", file=sys.stderr, flush=True)

    # ---- write outputs (skip when validate-only so we don't ship partial preds) ----
    if args.validate_only:
        print("[validate-only] not writing pred files.", file=sys.stderr, flush=True)
        return

    written = []

    def dump(name_preds, name_meta, preds, meta):
        p1 = os.path.join(args.out_dir, name_preds)
        p2 = os.path.join(args.out_dir, name_meta)
        with open(p1, "w", encoding="utf-8") as f:
            json.dump(preds, f)
        with open(p2, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        written.extend([p1, p2])

    if do_fp:
        for b in budgets:
            bk = f"{b // 1000}k"
            dump(f"helix_{args.tag}_fingerprint_{bk}_pred.json",
                 f"helix_{args.tag}_fingerprint_{bk}_meta.json", fp_preds[b], fp_meta[b])
    if do_pk:
        dump(f"helix_{args.tag}_packet_pred.json",
             f"helix_{args.tag}_packet_meta.json", pk_preds, pk_meta)

    print(f"\nwrote {len(written)} files:", file=sys.stderr)
    for w in written:
        print(f"  {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
