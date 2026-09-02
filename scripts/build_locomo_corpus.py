"""Emit the LoCoMo + LoCoMo-Plus corpus as ingestible files + needle sets.

Semantic-portfolio lane (user-approved 2026-08-30: "run the smaller 2 first,
queue FinanceBench after" — Sol's benchmark reference doc). Source: the
LoCoMo-Plus repo (github.com/xjtuleeyf/Locomo-Plus, ARR 2026), which bundles
the original LoCoMo 10 multi-session conversations (``locomo10.json``,
1,986 QA rows across 5 categories with per-turn evidence ids) and the
Cognitive extension (``locomo_plus.json``, 401 cue-dialogue → trigger-query
pairs testing implicit recall — the "semantic cue-trigger disconnect").

One tiny bed serves both benchmarks:

  <corpus-root>/turns/<sample_id>/<dia_id>.txt   one file per dialogue turn,
                                                 header = conv + session +
                                                 session timestamp + speaker
                                                 (timestamps are load-bearing
                                                 for the temporal category);
                                                 image turns append the BLIP
                                                 caption as "[shared a photo:
                                                 ...]".
  <corpus-root>/cues/cue_<i>.txt                 one file per Cognitive cue
                                                 dialogue, text as-is (pure —
                                                 mirrors the paper's bm25
                                                 corpus, which ranks the cue
                                                 among all 401 cues).

  <bench-dir>/needles_locomo_pairsless.json      needle set: 1,986 LoCoMo QA
                                                 (question_type locomo_<cat>)
                                                 + 401 cognitive triggers
                                                 (locomo_cognitive).
  <bench-dir>/gold_paths_locomo.json             needle -> [corpus-relative
                                                 gold paths] (resolved to
                                                 gene_ids after ingest by
                                                 scripts/resolve_locomo_needles.py).
  <bench-dir>/needles_locomo_meta.json           per-needle extras: category,
                                                 adversarial_answer, and for
                                                 cognitive needles the paper's
                                                 own baseline ranks
                                                 (mpnet/bge/bm25/combined) so
                                                 our receipt can compare
                                                 rank-among-cues like-for-like.

Method notes (recorded here because they bound comparability):
  - The paper's published ranks rank the cue among the 401 cues ONLY
    (generation_pipeline/rank.py: bm25_corpus = cue texts). Our bed adds the
    ~3k conversation turns as distractor mass, so we report BOTH bases:
    rank-among-cues (like-for-like) and full-bed rank (harder, realistic).
  - 4 LoCoMo QA rows carry no evidence ids; they are skipped and listed in
    the meta. Category 5 (adversarial) rows keep their evidence turn — the
    contradicting turn is legitimate gold for a retrieval bench.
  - Turn headers make every file byte-unique (conv id + session + speaker),
    so content-hash dedup cannot merge repeated short turns across convs.

Determinism: no sampling — the full sets are emitted; re-runs are
byte-identical from the same inputs.

Usage:
  python scripts/build_locomo_corpus.py \
    --data-dir F:/Projects/locomo-plus/data \
    --corpus-root F:/Projects/locomo-plus/corpus \
    --bench-dir benchmarks/dogfood/locomo
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("build_locomo_corpus")

CATEGORY_NAMES = {
    1: "locomo_multihop",
    2: "locomo_temporal",
    3: "locomo_commonsense",
    4: "locomo_singlehop",
    5: "locomo_adversarial",
}

MIN_BYTES = 50  # build_fixture_matrix ingest size gate (lower bound)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def emit_turns(locomo: list, corpus_root: Path) -> dict:
    """Write one file per turn; return dia-key -> corpus-relative path."""
    paths: dict[str, str] = {}
    n_files = 0
    for conv in locomo:
        sample_id = conv["sample_id"]
        c = conv["conversation"]
        sess_idx = 1
        while f"session_{sess_idx}" in c:
            turns = c.get(f"session_{sess_idx}") or []
            stamp = c.get(f"session_{sess_idx}_date_time") or ""
            for t in turns:
                dia = t.get("dia_id") or ""
                if not dia:
                    continue
                text = (t.get("text") or "").strip()
                cap = (t.get("blip_caption") or "").strip()
                if cap:
                    text = (text + " " if text else "") + f"[shared a photo: {cap}]"
                body = (
                    f"[{sample_id} session_{sess_idx} | {stamp}] "
                    f"{t.get('speaker', '?')}: {text}\n"
                )
                rel = f"turns/{_safe(sample_id)}/{_safe(dia)}.txt"
                p = corpus_root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                data = body.encode("utf-8")
                assert len(data) >= MIN_BYTES, (rel, len(data), body)
                p.write_bytes(data)
                paths[f"{sample_id}|{dia}"] = rel
                n_files += 1
            sess_idx += 1
    log.info("turn files: %d", n_files)
    return paths


def emit_cues(plus: list, corpus_root: Path) -> list[str]:
    rels = []
    for i, row in enumerate(plus):
        rel = f"cues/cue_{i:03d}.txt"
        p = corpus_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        body = (row.get("cue_dialogue") or "").strip() + "\n"
        data = body.encode("utf-8")
        assert len(data) >= MIN_BYTES, (rel, len(data))
        p.write_bytes(data)
        rels.append(rel)
    log.info("cue files: %d", len(rels))
    return rels


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=r"F:/Projects/locomo-plus/data")
    ap.add_argument("--corpus-root", default=r"F:/Projects/locomo-plus/corpus")
    ap.add_argument("--bench-dir", default="benchmarks/dogfood/locomo")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    corpus_root = Path(args.corpus_root)
    bench_dir = Path(args.bench_dir)
    bench_dir.mkdir(parents=True, exist_ok=True)

    locomo = json.loads((data_dir / "locomo10.json").read_text(encoding="utf-8"))
    plus = json.loads((data_dir / "locomo_plus.json").read_text(encoding="utf-8"))

    turn_paths = emit_turns(locomo, corpus_root)
    cue_rels = emit_cues(plus, corpus_root)

    needles: list[dict] = []
    gold: dict[str, list[str]] = {}
    meta: dict[str, dict] = {}
    skipped_no_evidence: list[str] = []
    skipped_bad_evidence: list[str] = []

    for conv in locomo:
        sample_id = conv["sample_id"]
        for qi, q in enumerate(conv["qa"]):
            name = f"{_safe(sample_id)}_q{qi:03d}"
            ev = q.get("evidence") or []
            if not ev:
                skipped_no_evidence.append(name)
                continue
            rels = [turn_paths.get(f"{sample_id}|{d}") for d in ev]
            rels = [r for r in rels if r]
            if not rels:
                skipped_bad_evidence.append(name)
                continue
            cat = q.get("category")
            needles.append({
                "name": name,
                "query": q["question"],
                "question_type": CATEGORY_NAMES.get(cat, f"locomo_cat{cat}"),
            })
            gold[name] = rels
            meta[name] = {
                "category": cat,
                "answer": q.get("answer"),
                "adversarial_answer": q.get("adversarial_answer"),
                "evidence": ev,
            }

    for i, row in enumerate(plus):
        name = f"cog_{i:03d}"
        needles.append({
            "name": name,
            "query": (row.get("trigger_query") or "").strip(),
            "question_type": "locomo_cognitive",
        })
        gold[name] = [cue_rels[i]]
        meta[name] = {
            "relation_type": row.get("relation_type"),
            "time_gap": row.get("time_gap"),
            "paper_ranks_among_401_cues": row.get("ranks"),
            "paper_scores": row.get("scores"),
        }

    (bench_dir / "needles_locomo_pairsless.json").write_text(
        json.dumps({"needles": needles}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    (bench_dir / "gold_paths_locomo.json").write_text(
        json.dumps(gold, indent=1), encoding="utf-8")
    (bench_dir / "needles_locomo_meta.json").write_text(
        json.dumps({
            "source": "github.com/xjtuleeyf/Locomo-Plus @ HEAD 2026-08-30",
            "skipped_no_evidence": skipped_no_evidence,
            "skipped_bad_evidence": skipped_bad_evidence,
            "paper_rank_basis": "ranks in cognitive needles are the paper's, computed among the 401 cues only (generation_pipeline/rank.py)",
            "per_needle": meta,
        }, indent=1, ensure_ascii=False), encoding="utf-8")

    log.info("needles: %d (%d locomo QA + %d cognitive); skipped: %d no-evidence, %d bad-evidence",
             len(needles), len(needles) - len(plus), len(plus),
             len(skipped_no_evidence), len(skipped_bad_evidence))
    return 0


if __name__ == "__main__":
    sys.exit(main())
