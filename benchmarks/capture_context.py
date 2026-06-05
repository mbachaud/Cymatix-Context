r"""Capture helix /context responses ONCE for offline multi-model testing.

Harness-side (dev tooling; nothing in the shipped product). Runs the slow
~100s/q retrieval over the bench set a single time and saves the assembled
context per question, so you can replay it against any number of gen models
(haiku/sonnet/opus/gpt-5.4/5.5) via gen_from_context.py WITHOUT re-retrieving.

Captures whatever pipeline the daemon at --helix-url is serving (point it at the
fixed-pipeline daemon to capture fixed-pipeline context).

Usage:
  python benchmarks/capture_context.py --types semantic --max-questions 200 \
      --helix-url http://127.0.0.1:11439 --out benchmarks/results/ctx_fixed_sem125.jsonl
"""
from __future__ import annotations
import argparse, json, logging, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_enterprise_rag import (   # noqa: E402  -- reuse the exact daemon-fetch + dsid logic
    load_needles, helix_context, extract_dsids,
    make_uuid_reverse_with_stripped, UUID_INDEX_PATH, HELIX_URL,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="context.jsonl path (the reusable artifact)")
    ap.add_argument("--max-questions", type=int, default=None)
    ap.add_argument("--per-type", type=int, default=None)
    ap.add_argument("--types", help="Comma-separated question types (omit for all)")
    ap.add_argument("--helix-url", default=HELIX_URL)
    ap.add_argument("--resume", action="store_true",
                    help="Skip ids already present in --out and append the rest")
    args = ap.parse_args()

    # bench_enterprise_rag reads HELIX_URL at import; honor --helix-url override.
    import bench_enterprise_rag as B
    B.HELIX_URL = args.helix_url

    log = logging.getLogger("capture_context")
    log.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(h)

    types = [t.strip() for t in args.types.split(",")] if args.types else None
    needles = load_needles(max_questions=args.max_questions, question_types=types, per_type=args.per_type)
    uuid_reverse = make_uuid_reverse_with_stripped(json.loads(UUID_INDEX_PATH.read_text(encoding="utf-8")))

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if args.resume and out.exists():
        for ln in out.read_text(encoding="utf-8").splitlines():
            try: done.add(json.loads(ln)["id"])
            except Exception: pass
    needles = [n for n in needles if n["id"] not in done]
    log.info("capturing %d contexts -> %s (helix=%s)%s",
             len(needles), out, args.helix_url, f"  (resume: {len(done)} done)" if done else "")

    t0 = time.time(); n_gold = 0
    with out.open("a" if args.resume else "w", encoding="utf-8") as fh:
        for i, n in enumerate(needles, 1):
            sid = f"capture-{n['id']}-{int(time.time()*1e9)}"
            # Send the needle's ground-truth type so the fixed-pipeline daemon
            # (HELIX_SEMANTIC_ARM=1) applies broaden + semantic dense weight on
            # /context. On a stock daemon the field is parsed-and-ignored.
            ctx_text, ctx_meta = helix_context(
                n["question"], n["gold_paths"], sid, log, query_type=n.get("type"),
            )
            dsids = extract_dsids(ctx_text, ctx_meta.get("delivered_paths_sample", []), uuid_reverse)
            if ctx_meta.get("gold_delivered"): n_gold += 1
            fh.write(json.dumps({
                "id": n["id"], "type": n["type"], "question": n["question"],
                "gold_answer": n["gold_answer"], "answer_facts": n["answer_facts"],
                "expected_doc_ids": n["expected_doc_ids"], "gold_paths": n["gold_paths"],
                "ctx_text": ctx_text, "ctx": ctx_meta, "predicted_doc_ids": dsids,
            }) + "\n"); fh.flush()
            if i % 25 == 0:
                log.info("  [%d/%d] gold_delivered=%d  %.0f min elapsed",
                         i, len(needles), n_gold, (time.time()-t0)/60)
    log.info("DONE %d contexts, gold_delivered=%d, %.0f min", len(needles), n_gold, (time.time()-t0)/60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
