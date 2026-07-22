"""Complement REACH diagnostic (thread 2) — the cheap falsification BEFORE any
80k re-embed.

Scout finding: the ~2% semantic ceiling is a REACH problem — semantic gold is
absent from top-200 ~78% of the time (BGE-M3 dense doesn't surface it), not a
ranking/fusion problem. Dense embeds `content` ONLY. Inspection of the ERB beds
shows the chunker often put boilerplate METADATA in `content` and the
substantive/answer-bearing text in `complement` (e.g. fireflies: content=meeting
header, complement=topics/action_items). So dense may be embedding the wrong half.

This diagnostic tests, per SEMANTIC gold doc, which encoding best aligns with the
query (BGE-M3 cosine):
    content-only  (what ships today)   vs   complement-only   vs   content+complement

If complement (or both) systematically beats content, re-embedding dense on that
strand is the ceiling lever — and we've proven it for ~$0 of compute before the
80k re-embed. If not, the thesis is dead and we save the re-embed.

Local, no egress. Read-only on the bed.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

BED = str(_REPO / "genomes/bench/matrix/enterprise_rag_50k_batched.db")
DSID_MAP = str(_REPO / "benchmarks" / "results" / "dsid_map_enterprise_rag_50k.json")
QUESTIONS = "F:/tmp/ext_ct_helixbench/questions/onyx_500.jsonl"
CAP = 2000  # PASSAGE_CHAR_CAP


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def cos(a, b):
    d = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return d / (na * nb)


def main():
    from cymatix_context.backends.bgem3_codec import BGEM3Codec

    dsid_map = json.load(open(DSID_MAP, encoding="utf-8"))          # _norm(path)->dsid
    dsid_to_paths = defaultdict(set)
    for p, d in dsid_map.items():
        dsid_to_paths[d].add(p)

    # semantic questions
    sem = [json.loads(l) for l in open(QUESTIONS, encoding="utf-8")]
    sem = [q for q in sem if q.get("question_type") == "semantic" and q.get("expected_doc_ids")]
    print(f"semantic scoreable: {len(sem)}", file=sys.stderr)

    # bed: reverse {_norm(source_id)->original}, then fetch content/complement for gold sources
    c = sqlite3.connect(f"file:{BED}?mode=ro", uri=True)
    try:
        norm2orig = {}
        for (src,) in c.execute("SELECT DISTINCT source_id FROM genes"):
            norm2orig[_norm(src)] = src

        # gold original source_ids across all semantic Qs
        gold_srcs = set()
        for q in sem:
            for dsid in q["expected_doc_ids"]:
                for np_ in dsid_to_paths.get(dsid, ()):
                    o = norm2orig.get(np_)
                    if o:
                        gold_srcs.add(o)

        src_rows = defaultdict(list)  # source_id -> [(content, complement)]
        gold_list = list(gold_srcs)
        for i in range(0, len(gold_list), 400):
            chunk = gold_list[i:i + 400]
            ph = ",".join("?" * len(chunk))
            for src, content, comp in c.execute(
                f"SELECT source_id, content, complement FROM genes WHERE source_id IN ({ph})",
                chunk,
            ):
                src_rows[src].append((content or "", comp or ""))
    finally:
        c.close()

    codec = BGEM3Codec()
    print("codec loaded; scoring...", file=sys.stderr)

    wins = {"content": 0, "complement": 0, "both": 0}
    sums = {"content": 0.0, "complement": 0.0, "both": 0.0}
    n_scored = 0
    comp_gt_content = 0
    rows = []
    for qi, q in enumerate(sem):
        qv = codec.encode(q["question"], task="query")
        # gather this q's gold genes
        genes = []
        for dsid in q["expected_doc_ids"]:
            for np_ in dsid_to_paths.get(dsid, ()):
                o = norm2orig.get(np_)
                if o:
                    genes.extend(src_rows.get(o, []))
        if not genes:
            continue
        best = {"content": -1.0, "complement": -1.0, "both": -1.0}
        for content, comp in genes:
            cv = codec.encode(content[:CAP], task="passage")
            best["content"] = max(best["content"], cos(qv, cv))
            if comp.strip():
                mv = codec.encode(comp[:CAP], task="passage")
                best["complement"] = max(best["complement"], cos(qv, mv))
                bv = codec.encode((content + " " + comp)[:CAP], task="passage")
                best["both"] = max(best["both"], cos(qv, bv))
        n_scored += 1
        for k in sums:
            sums[k] += best[k]
        winner = max(best, key=best.get)
        wins[winner] += 1
        if best["complement"] > best["content"]:
            comp_gt_content += 1
        rows.append({"qid": q["question_id"],
                     **{k: round(best[k], 4) for k in best}})
        if (qi + 1) % 25 == 0:
            print(f"  {qi+1}/{len(sem)}", file=sys.stderr)

    print("\n=== COMPLEMENT REACH DIAGNOSTIC (semantic gold, BGE-M3 cosine) ===")
    print(f"N scored: {n_scored}")
    print(f"mean best-cosine  content={sums['content']/n_scored:.4f}  "
          f"complement={sums['complement']/n_scored:.4f}  "
          f"both={sums['both']/n_scored:.4f}")
    print(f"argmax winner counts: {wins}")
    print(f"complement > content: {comp_gt_content}/{n_scored} = {comp_gt_content/n_scored:.2f}")
    out = str((_REPO / "benchmarks" / "results" / "complement_reach_diag.json"))
    json.dump({"n": n_scored, "mean": {k: sums[k]/n_scored for k in sums},
               "wins": wins, "comp_gt_content": comp_gt_content, "rows": rows},
              open(out, "w"), indent=2)
    print(f"-> {out}")


if __name__ == "__main__":
    main()
