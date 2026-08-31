"""Observation-enrichment variant of the LoCoMo corpus (write-time abstraction A/B).

Emits the SAME turns+cues as ``build_locomo_corpus.py`` plus LoCoMo's own
LLM-derived observation layer as standalone bridge documents — one file per
observation sentence, headered with conv/session/date like the turns. Gold
for a QA needle becomes: evidence turn docs ∪ observation docs whose
provenance dia_id is in the needle's evidence (an observation is a compressed
paraphrase of its source turn — delivering it answers the question; this
grading basis is the point of the cell and is recorded in the meta).
Cognitive needles are unchanged (no observation layer exists for the 401
synthetic cues — enriching those requires *generating* abstractions, i.e.
the LLM-ribosome lane).

Outputs mirror the base emitter under --bench-dir (same filenames, so
``resolve_locomo_needles.py --bench-dir benchmarks/dogfood/locomo_obs
--corpus-root F:/Projects/locomo-plus/corpus_obs`` works unchanged).

Usage:
  python scripts/build_locomo_obs_variant.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_locomo_corpus import (  # noqa: E402
    CATEGORY_NAMES, MIN_BYTES, _safe, emit_cues, emit_turns)

log = logging.getLogger("build_locomo_obs_variant")

DATA_DIR = Path(r"F:/Projects/locomo-plus/data")
CORPUS = Path(r"F:/Projects/locomo-plus/corpus_obs")
BENCH = Path("benchmarks/dogfood/locomo_obs")


def emit_observations(locomo: list, corpus_root: Path) -> dict[str, list[str]]:
    """Write one file per observation sentence; return dia-key -> [rel paths]."""
    by_dia: dict[str, list[str]] = {}
    n = 0
    for conv in locomo:
        sample_id = conv["sample_id"]
        c = conv["conversation"]
        obs = conv.get("observation") or {}
        for sess_key, speakers in obs.items():
            # session_N_observation -> N
            sess_n = sess_key.split("_")[1]
            stamp = c.get(f"session_{sess_n}_date_time") or ""
            if not isinstance(speakers, dict):
                continue
            for speaker, rows in speakers.items():
                if not isinstance(rows, list):
                    continue
                for i, row in enumerate(rows):
                    if not (isinstance(row, list) and row):
                        continue
                    sentence = str(row[0]).strip()
                    dias = [str(d) for d in row[1:] if d]
                    if not sentence:
                        continue
                    rel = (f"observations/{_safe(sample_id)}/"
                           f"s{sess_n}_{_safe(speaker)}_{i:03d}.txt")
                    p = corpus_root / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    body = f"[{sample_id} session_{sess_n} | {stamp}] {sentence}\n"
                    data = body.encode("utf-8")
                    if len(data) < MIN_BYTES:
                        body = body.rstrip("\n") + " (observed)\n"
                        data = body.encode("utf-8")
                    p.write_bytes(data)
                    n += 1
                    for d in dias:
                        by_dia.setdefault(f"{sample_id}|{d}", []).append(rel)
    log.info("observation files: %d (linked dia keys: %d)", n, len(by_dia))
    return by_dia


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    BENCH.mkdir(parents=True, exist_ok=True)
    locomo = json.loads((DATA_DIR / "locomo10.json").read_text(encoding="utf-8"))
    plus = json.loads((DATA_DIR / "locomo_plus.json").read_text(encoding="utf-8"))

    turn_paths = emit_turns(locomo, CORPUS)
    cue_rels = emit_cues(plus, CORPUS)
    obs_by_dia = emit_observations(locomo, CORPUS)

    needles, gold, meta = [], {}, {}
    skipped = []
    obs_gold_needles = 0
    for conv in locomo:
        sample_id = conv["sample_id"]
        for qi, q in enumerate(conv["qa"]):
            name = f"{_safe(sample_id)}_q{qi:03d}"
            ev = q.get("evidence") or []
            rels = [turn_paths.get(f"{sample_id}|{d}") for d in ev]
            rels = [r for r in rels if r]
            if not rels:
                skipped.append(name)
                continue
            obs_rels = sorted({r for d in ev
                               for r in obs_by_dia.get(f"{sample_id}|{d}", [])})
            if obs_rels:
                obs_gold_needles += 1
            cat = q.get("category")
            needles.append({"name": name, "query": q["question"],
                            "question_type": CATEGORY_NAMES.get(cat, f"locomo_cat{cat}")})
            gold[name] = rels + obs_rels
            meta[name] = {"category": cat, "evidence": ev,
                          "n_turn_gold": len(rels), "n_obs_gold": len(obs_rels)}

    for i, row in enumerate(plus):
        name = f"cog_{i:03d}"
        needles.append({"name": name,
                        "query": (row.get("trigger_query") or "").strip(),
                        "question_type": "locomo_cognitive"})
        gold[name] = [cue_rels[i]]
        meta[name] = {"relation_type": row.get("relation_type")}

    (BENCH / "needles_locomo_pairsless.json").write_text(
        json.dumps({"needles": needles}, indent=1, ensure_ascii=False), encoding="utf-8")
    (BENCH / "gold_paths_locomo.json").write_text(
        json.dumps(gold, indent=1), encoding="utf-8")
    (BENCH / "needles_locomo_meta.json").write_text(
        json.dumps({
            "variant": "observation-enrichment (write-time abstraction A/B vs the base locomo bed)",
            "grading_basis": "gold = evidence turns UNION observations derived from those turns (an observation is a compressed paraphrase of its source turn); cognitive gold unchanged",
            "qa_needles_with_obs_gold": obs_gold_needles,
            "skipped": skipped,
            "per_needle": meta,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("needles: %d; QA needles with observation gold: %d",
             len(needles), obs_gold_needles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
