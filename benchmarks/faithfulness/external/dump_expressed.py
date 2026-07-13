"""Stage 1 of local real-helix faithfulness (runs in the HELIX env).

Ingest the 6 synthetic facts into a fresh bed, run build_context(read_only) per
needle, and dump the FULL expressed_context to JSON for the graph env (np-graph
venv) to consume. Separates the helix models (BGE-M3/SPLADE) from circuit-tracer
so the two dependency stacks never mix.
"""
import os, sys, json, tempfile
os.environ.setdefault("HELIX_DISABLE_LEARN", "1")
from pathlib import Path

_REPO = Path("f:/Projects/helix-context")
sys.path.insert(0, str(_REPO / "benchmarks" / "faithfulness"))
sys.path.insert(0, str(_REPO))

from needle_faithfulness_experiment import NEEDLES
from helix_context.config import load_config
from helix_context.context_manager import HelixContextManager

BED = str(Path(tempfile.gettempdir()) / "faith_needle_bed.db")
OUT = "f:/Projects/np-graph/expressed_contexts.json"


def main():
    if os.path.exists(BED):
        os.remove(BED)
    cfg = load_config(str(_REPO / "helix.toml"))
    cfg.genome.path = BED
    mgr = HelixContextManager(cfg)
    print(f"ingesting {len(NEEDLES)} facts...", flush=True)
    for nd in NEEDLES:
        mgr.ingest(nd["ctx"], metadata={"source_id": nd["id"]})

    out = []
    for nd in NEEDLES:
        w = mgr.build_context(nd["q"], read_only=True, ignore_delivered=True)
        exp = w.expressed_context or ""
        survived = nd["ans"].strip().lower() in exp.lower()
        out.append({"id": nd["id"], "q": nd["q"], "ans": nd["ans"],
                    "expressed_context": exp, "expressed_len": len(exp),
                    "answer_survived": survived})
        print(f"  {nd['id']}: len={len(exp)} survived={survived}", flush=True)

    json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2)
    print(f"-> {OUT}  ({sum(r['answer_survived'] for r in out)}/{len(out)} survived)")


if __name__ == "__main__":
    main()
