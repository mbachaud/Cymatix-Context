"""ERB crater: can a model rephrase its way to gold with extra cymatix calls?

For each crater needle (c4000 classcap missed delivery; question_type
semantic or basic):
  round 0  the original question -> cymatix packet (re-verified miss)
  round r  Sonnet sees the question, its prior queries and the latest packet,
           and replies either ``ANSWER: ...`` or ``SEARCH: <new query>``;
           a SEARCH runs a fresh cymatix call (up to --max-rephrase).
Gold is checked by an ORACLE after every search (delivered gene ids vs
gold_by_needle) and is never shown to the model. Recorded per round:
query, packet chars, delivered ids count, gold hit, model input/output
tokens, cache tokens, cost, wall. Resumable: one JSON line per needle.

Model calls go through ``claude -p`` with no tools, no MCP servers and a
short custom system prompt (no --bare: it skips the keychain login).
Budget: one request at a time, >= 300 ms apart, 180 s timeout each.

Usage:
  python -P crater_rephrase.py --limit 10 --out F:/tmp/crater_rephrase_2026-10-19/pilot.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
ERB = ROOT / "benchmarks" / "dogfood" / "erb"
BED = Path(r"F:\tmp\grain_erb_full_c4000_2026-10-18\erb_full_c4000_lean.db")
RECEIPT = Path(r"F:\tmp\grain_erb_full_c4000_2026-10-18\ladder_erb_full_c4000_classcap_2026-10-17.json")
CONFIG = HERE / "configs" / "grain_c4000_classcap.toml"
EPOCH = "1792397919"   # the receipt's pinned scoring clock
K = 12
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TYPES = ("semantic", "basic")

SYSTEM = (
    "You answer questions about a company's internal documents using a search tool "
    "that returns retrieved context. Each turn you see the question, the searches "
    "already made, and the context returned by the latest search. If the context "
    "contains the answer, reply with exactly one line: ANSWER: <short answer>. "
    "Otherwise reply with exactly one line: SEARCH: <a new search query>, phrased "
    "differently from earlier searches (other words, likely document titles, "
    "names, or terms the answer would contain). No other text."
)


def crater_needles():
    meta = json.loads((ERB / "needles_grain4000_meta.json").read_text(encoding="utf-8"))["needles"]
    pq = json.loads(RECEIPT.read_text(encoding="utf-8"))["arms"][0]["per_query"]
    q = {n["name"]: n["query"] for n in
         json.loads((ERB / "needles_resolved_grain4000_full.json").read_text(encoding="utf-8"))["needles"]}
    gold = json.loads((ERB / "gold_by_needle_grain4000.json").read_text(encoding="utf-8"))
    out = []
    for r in pq:
        t = meta[r["needle"]]["question_type"]
        if t in TYPES and not r.get("delivered_gold"):
            out.append({"needle": r["needle"], "type": t, "query": q[r["needle"]],
                        "gold": set(gold[r["needle"]]),
                        "receipt_rank": r.get("final_rank_of_first_gold")})
    return out


def stratified(needles, per_type: int, seed: int):
    """N per question type, split across miss kind (never ranked vs ranked
    but cut) in proportion to the crater, seeded and order-stable."""
    import random
    rng = random.Random(seed)
    out = []
    for t in TYPES:
        pool = [n for n in needles if n["type"] == t]
        never = sorted((n for n in pool if n["receipt_rank"] is None), key=lambda n: n["needle"])
        cut = sorted((n for n in pool if n["receipt_rank"] is not None), key=lambda n: n["needle"])
        k_cut = round(per_type * len(cut) / max(1, len(pool)))
        out += rng.sample(never, min(len(never), per_type - k_cut)) + rng.sample(cut, min(len(cut), k_cut))
    return sorted(out, key=lambda n: n["needle"])


def ask_model(prompt: str, model: str) -> dict:
    # Prompt on stdin: packets reach ~19k chars, near Windows' 32k argv cap.
    cmd = ["claude", "-p", "--tools", "", "--strict-mcp-config",
           "--system-prompt", SYSTEM, "--model", model, "--output-format", "json"]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           timeout=180, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "wall_s": 180.0}
    wall = round(time.perf_counter() - t0, 1)
    try:
        j = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"error": f"rc={p.returncode} non-json: {p.stdout[:200]!r} {p.stderr[:200]!r}", "wall_s": wall}
    u = j.get("usage") or {}
    return {"text": (j.get("result") or "").strip(), "is_error": j.get("is_error"),
            "cost_usd": j.get("total_cost_usd"), "wall_s": wall,
            "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
            "cache_read": u.get("cache_read_input_tokens"),
            "cache_write": u.get("cache_creation_input_tokens"),
            "model": ",".join((j.get("modelUsage") or {}).keys())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-rephrase", type=int, default=3)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-type", type=int, default=0,
                    help="stratified sample: N per question type, split by miss kind")
    ap.add_argument("--seed", type=int, default=20261019)
    a = ap.parse_args()

    os.environ["CYMATIX_SCORING_EPOCH"] = EPOCH
    os.environ["CYMATIX_LEAN_INDEXES"] = "1"
    os.environ["CYMATIX_DISABLE_LEARN"] = "1"
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import CymatixContextManager

    cfg = load_config(str(CONFIG))
    cfg.genome.path = str(BED)
    mgr = CymatixContextManager(cfg)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        done = {json.loads(l)["needle"] for l in out.read_text(encoding="utf-8").splitlines() if l.strip()}
    needles = crater_needles()
    if a.per_type:
        needles = stratified(needles, a.per_type, a.seed)
    if a.limit:
        needles = needles[:a.limit]
    print(f"crater needles: {len(needles)} (done {len(done)})", flush=True)

    def search(query: str):
        t0 = time.perf_counter()
        w = mgr.build_context(query, read_only=True, ignore_delivered=True, max_genes=K)
        ids = list(w.expressed_gene_ids or ())
        return w.expressed_context or "", ids, round(time.perf_counter() - t0, 1)

    try:
        for nd in needles:
            if nd["needle"] in done:
                continue
            rec = {"needle": nd["needle"], "type": nd["type"], "question": nd["query"],
                   "receipt_rank": nd["receipt_rank"], "rounds": []}
            queries = [nd["query"]]
            packet, ids, s_wall = search(nd["query"])
            rec["rounds"].append({"r": 0, "query": nd["query"], "packet_chars": len(packet),
                                  "delivered": len(ids), "gold_hit": bool(nd["gold"] & set(ids)),
                                  "search_s": s_wall})
            final = None
            for r in range(1, a.max_rephrase + 2):
                prompt = (f"Question: {nd['query']}\n\nSearches so far:\n"
                          + "\n".join(f"- {x}" for x in queries)
                          + f"\n\nContext returned by the latest search:\n<context>\n{packet}\n</context>")
                if r > a.max_rephrase:
                    prompt += "\n\nNo searches left: reply ANSWER: <best answer or 'not found'>."
                time.sleep(0.3)
                m = ask_model(prompt, a.model)
                step = {"r": r, "prompt_chars": len(prompt), "model": m}
                text = m.get("text", "")
                if text.upper().startswith("SEARCH:") and r <= a.max_rephrase:
                    nq = text.split(":", 1)[1].strip()
                    queries.append(nq)
                    packet, ids, s_wall = search(nq)
                    step.update({"query": nq, "packet_chars": len(packet), "delivered": len(ids),
                                 "gold_hit": bool(nd["gold"] & set(ids)), "search_s": s_wall})
                    rec["rounds"].append(step)
                    continue
                final = text
                rec["rounds"].append(step)
                break
            hits = [x.get("gold_hit") for x in rec["rounds"] if "gold_hit" in x]
            rec.update({
                "final": final,
                "gold_recovered": any(hits[1:]),
                "first_gold_round": next((x["r"] for x in rec["rounds"] if x.get("gold_hit")), None),
                "round0_gold": hits[0] if hits else None,
                "searches": len(queries),
                "model_calls": sum(1 for x in rec["rounds"] if "model" in x),
                "cost_usd": round(sum((x.get("model") or {}).get("cost_usd") or 0 for x in rec["rounds"]), 4),
                "model_in_tokens": sum((x.get("model") or {}).get("input_tokens") or 0 for x in rec["rounds"]),
                "model_out_tokens": sum((x.get("model") or {}).get("output_tokens") or 0 for x in rec["rounds"]),
                "packet_chars_total": sum(x.get("packet_chars") or 0 for x in rec["rounds"]),
            })
            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            print(json.dumps({k: rec[k] for k in ("needle", "type", "gold_recovered", "first_gold_round",
                                                   "searches", "cost_usd", "packet_chars_total")}), flush=True)
    finally:
        mgr.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
