r"""Flip-default gate probe: /context delivery at multi-gene depth, no LLM.

Hits helix /context for each EnterpriseRAG needle and records what the SERVER
actually delivered -- the set of <GENE src="..."> blocks, the gold-delivery
flag, char count, and the /context wall-clock. No answerer is invoked, so this
is free and fast; it measures only the delivery path where the per_gene_budget
splice + _assemble trim live.

Run once per arm against a daemon booted with the matching helix.toml
``per_gene_budget`` and a forced depth (``ann_threshold_min_genes`` high), then
diff the two output files with gate_depth_analyze.py.

Unlike bench_enterprise_rag.helix_context, this does NOT truncate the response
to MAX_CTX_CHARS before counting genes -- that bench-side cap would undercount
the tail gene under dynamic and contaminate the dropped-gene measurement. Gold
matching reuses bench_enterprise_rag._rel_after_sources verbatim.

Usage:
  python benchmarks/gate_depth_probe.py --label fixed   --max-questions 100
  python benchmarks/gate_depth_probe.py --label dynamic --max-questions 100
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_enterprise_rag import load_needles, HELIX_URL
from gate_analysis import canon

GENE_SRC_RE = re.compile(r'<GENE\s+src="([^"]+)"')
RESULTS_ROOT = Path(__file__).resolve().parent / "results"


def probe_one(client: httpx.Client, url: str, question: str,
              gold_paths: list[str], max_genes: int, qid: str) -> dict:
    gold_canon = {c for c in (canon(p) for p in gold_paths) if c}
    body = {"query": question, "max_genes": max_genes,
            "session_id": f"gate-{qid}-{int(time.time() * 1e9)}"}
    t0 = time.perf_counter()
    try:
        resp = client.post(f"{url}/context", json=body, timeout=30)
        elapsed = time.perf_counter() - t0
    except Exception as exc:
        return {"id": qid, "status": "error", "error": str(exc),
                "elapsed_s": time.perf_counter() - t0,
                "delivered_rels": [], "gold_delivered": False, "n_genes": 0,
                "chars": 0}
    if resp.status_code != 200:
        return {"id": qid, "status": "http_error", "http": resp.status_code,
                "elapsed_s": elapsed, "delivered_rels": [],
                "gold_delivered": False, "n_genes": 0, "chars": 0}
    raw = resp.json()
    data = raw[0] if isinstance(raw, list) and raw else raw
    content = (data.get("content") or data.get("context") or "") if isinstance(data, dict) else ""
    # FULL content -- no MAX_CTX_CHARS truncation -- so the tail gene is counted.
    # The raw src string is the stable per-gene key for the cross-arm set diff;
    # canon() only normalizes for gold matching (src appears with or without a
    # leading "sources/").
    delivered_src = sorted({m.replace("\\", "/") for m in GENE_SRC_RE.findall(content)})
    delivered_canon = {canon(s) for s in delivered_src}
    return {
        "id": qid,
        "status": "ok",
        "elapsed_s": elapsed,
        "chars": len(content),
        "n_genes": len(delivered_src),
        "delivered_rels": delivered_src,  # raw src = stable per-gene key
        "gold_delivered": bool(gold_canon & delivered_canon),
        "n_gold": len(gold_canon),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="arm label, e.g. fixed | dynamic")
    ap.add_argument("--max-questions", type=int, default=100)
    ap.add_argument("--types", default="basic,semantic,intra_document_reasoning")
    ap.add_argument("--max-genes", type=int, default=8)
    ap.add_argument("--helix-url", default=HELIX_URL)
    args = ap.parse_args()

    # Confirm the daemon's config so the run is self-documenting.
    try:
        health = httpx.get(f"{args.helix_url}/health", timeout=5).json()
        print(f"helix /health: genes={health.get('genes')} pid={health.get('pid')}")
    except Exception as exc:
        print(f"ERROR: helix /health unreachable at {args.helix_url}: {exc}",
              file=sys.stderr)
        return 2

    types = [t.strip() for t in args.types.split(",")] if args.types else None
    needles = load_needles(max_questions=args.max_questions, question_types=types)
    print(f"loaded {len(needles)} needles; probing /context "
          f"max_genes={args.max_genes} label={args.label}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_ROOT / f"gate_depth_{args.label}_{stamp}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    with httpx.Client() as client, out_path.open("w", encoding="utf-8") as fh:
        for i, n in enumerate(needles, 1):
            row = probe_one(client, args.helix_url, n["question"],
                            n["gold_paths"], args.max_genes, n["id"])
            row["type"] = n["type"]
            fh.write(json.dumps(row) + "\n"); fh.flush()
            rows.append(row)
            if i % 25 == 0:
                gold = sum(1 for r in rows if r.get("gold_delivered"))
                print(f"  [{i}/{len(needles)}] gold_delivered={gold}")

    ok = [r for r in rows if r.get("status") == "ok"]
    gold = sum(1 for r in ok if r["gold_delivered"])
    ngenes = [r["n_genes"] for r in ok]
    print(f"\n=== {args.label}: {len(ok)}/{len(rows)} ok ===")
    print(f"gold_delivered: {gold}/{len(ok)}")
    if ngenes:
        print(f"n_genes: min={min(ngenes)} median={sorted(ngenes)[len(ngenes)//2]} "
              f"max={max(ngenes)} total={sum(ngenes)}")
    print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
