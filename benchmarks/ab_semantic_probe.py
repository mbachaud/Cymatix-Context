r"""Offline A/B probe: per-ERB-question-type gold_delivered + gold RANK under
lexical / dense / fused retrieval arms (Issue #260, scope item 1).

In-process, retrieval-only, LLM-free, ~$0. For each (arm x optional
rerank-combinator) cell it runs ``build_context(read_only=True,
ignore_delivered=True)`` on a fresh ``HelixContextManager`` against a READ-ONLY
bed, and measures -- per question -- whether the gold gene(s) were delivered,
at what rank they sit in the full scored pool, and whether they were in the
candidate pool at all. That last split distinguishes a **recall miss** (gold
never surfaces) from a **rank miss** (gold surfaces but ranks below the
delivery budget) -- the #93 truncation finding (gold at rank 9 @ 829k).

The 125 ``semantic`` (paraphrastic) ERB questions are the target: #93 measured
20% gold_delivered on them vs 78-100% on structured types
(``benchmarks/results/erb_blob93_verdict.md``). This probe isolates, per arm,
whether that ceiling is recall or ranking.

ARMS (all forced onto ``fusion_mode = "rrf"``, layered on the LLM-free lexical
probe base so no decoder / model-splice runs):

  lexical : ``dense_embedding_enabled = False``, ``splade_enabled = False``
            -> pure FTS5 + tag + filename-anchor algorithmic stack.
  dense   : ``dense_embedding_enabled = True``, ``splade_enabled = False`` with
            the lexical tiers zeroed (``fts5_weight`` / ``tag_exact_weight`` /
            ``tag_prefix_weight`` / ``filename_anchor_weight`` /
            ``lex_anchor_weight`` = 0, ``bm25_shortlist_enabled = False``)
            -> a BGE-M3-dominated ranking. A tier with ``weight == 0`` is a
            no-op under RRF (see ``helix_context.retrieval.fusion.Fuser``).
  fused   : ``dense_embedding_enabled = True``, ``splade_enabled = True`` at
            shipped weights -> the full retrieval stack (dense + SPLADE + FTS +
            tag + filename anchor), i.e. shipped RETRIEVAL defaults minus the
            LLM decoder.

QUESTIONS (``--questions``): a sweep-queries JSON list of
``{"query": str, "gold_ids": [bed_gene_id]}`` -- built by
``scripts/bench_chain/erb_to_sweep_queries.py`` against the target bed, so the
gold ids are already resolved to gene ids IN THAT BED. Type labels + gold
answer text are joined in from ``--types-jsonl`` (the #93 scored run,
``erb500k_blob_additive_scored.jsonl``) by normalized question text. Questions
whose text does not join keep type ``"unknown"``.

Usage (smoke):
    python benchmarks/ab_semantic_probe.py \
        --bed-db genomes/bench/matrix/enterprise_rag_10k_batched.db \
        --questions benchmarks/results/erb_sweep_queries_erb10k.json \
        --types semantic --arms lexical,fused --limit 5 \
        --json-out benchmarks/results/semantic_probe_smoke.json

Design record: Issue #260 (spun out of #93). Template:
``benchmarks/ab_rerank_combinator.py``.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from helix_context.config import load_config  # noqa: E402
from helix_context.context_manager import HelixContextManager  # noqa: E402


DEFAULT_TYPES_JSONL = "benchmarks/results/erb500k_blob_additive_scored.jsonl"

# Arms recognised by ``apply_arm``. Kept as a module constant so the CLI help
# and the unit tests can enumerate them.
ARMS = ("lexical", "dense", "fused")

# Lightweight stopword set for the (soft, secondary) gold-answer text-overlap
# signal. The PRIMARY metric is id-based (gold gene delivered / ranked); the
# text signal is a resolution-free recall proxy that also works when gold gene
# ids are hard to resolve (e.g. the blob bed).
_STOP = frozenset(
    "the a an and or of to in on for with is are was were be been being this "
    "that these those it its as at by from into than then so such not no yes "
    "will would can could should may might must have has had do does did their "
    "they them we you your our his her he she what which who whom whose how "
    "when where why all any both each few more most other some only own same "
    "via about over under between during if but".split()
)
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*")


# ── text helpers ──────────────────────────────────────────────────────
def normalize_text(s: Optional[str]) -> str:
    """Lower-case, collapse whitespace -- the join key between the sweep
    queries and the scored jsonl."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def content_tokens(s: Optional[str]) -> set:
    """Distinctive content tokens (length > 2, non-stopword)."""
    return {w for w in _WORD.findall((s or "").lower())
            if len(w) > 2 and w not in _STOP}


# ── gold ground-truth (pure -- unit tested) ───────────────────────────
def emitted_order(scored: Dict[str, float]) -> List[str]:
    """Reconstruct the store's ranked order from a ``{gene_id: score}`` map.

    Sort by ``(-score, gene_id)`` -- the ``Fuser`` tie-break -- so this
    reproduces ``ranked_ids`` for the additive / rrf paths where
    ``last_query_scores`` already carries the final score.
    """
    return sorted(scored, key=lambda g: (-float(scored[g]), str(g)))


def best_gold_rank(order: Sequence[str], gold_ids: Sequence[str]) -> Optional[int]:
    """1-based rank of the best (earliest) gold gene in ``order``; ``None`` if
    no gold gene appears in the ranked pool."""
    gold = set(gold_ids)
    for i, gid in enumerate(order, start=1):
        if gid in gold:
            return i
    return None


def gold_answer_overlap(gold_answer: Optional[str], assembled: str) -> Optional[float]:
    """Fraction of the gold-answer content tokens present in the assembled
    context. ``None`` when no gold-answer string is available. Soft signal --
    see module docstring."""
    gt = content_tokens(gold_answer)
    if not gt:
        return None
    ct = content_tokens(assembled)
    return len(gt & ct) / len(gt)


def score_question(
    gold_ids: Sequence[str],
    expressed_ids: Sequence[str],
    scored: Dict[str, float],
    gold_answer: Optional[str],
    assembled: str,
    text_overlap_threshold: float = 0.6,
) -> dict:
    """Per-question metric bundle (PURE -- the unit-tested core).

    * ``gold_delivered_id`` -- a gold gene is in the delivered (expressed) set.
    * ``pool_present``      -- a gold gene appears anywhere in the scored pool
      (recall). ``gold_delivered_id and not below`` distinguishes rank-miss.
    * ``best_gold_rank``    -- 1-based rank of the best gold gene in the pool.
    * ``gold_answer_overlap`` / ``gold_delivered_text`` -- soft text recall.
    """
    gold = set(gold_ids)
    order = emitted_order(scored)
    rank = best_gold_rank(order, gold_ids)
    overlap = gold_answer_overlap(gold_answer, assembled)
    return {
        "n_gold_ids": len(gold),
        "pool_size": len(scored),
        "n_expressed": len(expressed_ids),
        "gold_delivered_id": bool(gold & set(expressed_ids)),
        "pool_present": bool(gold & set(scored)),
        "best_gold_rank": rank,
        "gold_answer_overlap": overlap,
        "gold_delivered_text": (overlap is not None
                                and overlap >= text_overlap_threshold),
    }


# ── aggregation (pure -- unit tested) ─────────────────────────────────
def _rate(flags: Sequence[bool]) -> Optional[float]:
    flags = [1.0 if f else 0.0 for f in flags]
    return sum(flags) / len(flags) if flags else None


def aggregate(records: Sequence[dict]) -> dict:
    """Aggregate a list of per-question metric bundles into rates + rank stats.

    Rank stats are computed only over questions whose gold is present in the
    pool (``best_gold_rank is not None``) -- a rank is undefined otherwise.
    """
    n = len(records)
    ranks = [r["best_gold_rank"] for r in records if r["best_gold_rank"] is not None]
    overlaps = [r["gold_answer_overlap"] for r in records
                if r["gold_answer_overlap"] is not None]
    return {
        "n": n,
        "gold_delivered_id_rate": _rate([r["gold_delivered_id"] for r in records]),
        "pool_present_rate": _rate([r["pool_present"] for r in records]),
        "gold_delivered_text_rate": _rate([r["gold_delivered_text"] for r in records]),
        "n_ranked": len(ranks),
        "mean_best_gold_rank": (statistics.fmean(ranks) if ranks else None),
        "median_best_gold_rank": (statistics.median(ranks) if ranks else None),
        "mean_gold_answer_overlap": (statistics.fmean(overlaps) if overlaps else None),
    }


# ── question loading + type join (attach_types is pure) ───────────────
def attach_types(sweep: Sequence[dict], scored: Sequence[dict]) -> List[dict]:
    """Join type + gold_answer onto the sweep queries by normalized text.

    ``sweep``  : list of ``{"query", "gold_ids"}``.
    ``scored`` : list of records carrying ``question`` / ``type`` /
                 ``gold_answer`` (the #93 scored jsonl). Unmatched queries get
                 type ``"unknown"`` and ``gold_answer = None``.
    """
    by_text = {normalize_text(r.get("question")): r for r in scored}
    out: List[dict] = []
    for q in sweep:
        text = q.get("query") or q.get("question") or ""
        meta = by_text.get(normalize_text(text))
        out.append({
            "query": text,
            "gold_ids": list(q.get("gold_ids") or []),
            "type": (meta.get("type") if meta else None) or "unknown",
            "gold_answer": (meta.get("gold_answer") if meta else None),
        })
    return out


def load_questions(questions_path: str, types_jsonl_path: Optional[str],
                   types_filter: Optional[Sequence[str]],
                   limit: int) -> List[dict]:
    sweep = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    if isinstance(sweep, dict):  # tolerate {"queries": [...]} wrappers
        sweep = sweep.get("queries") or sweep.get("questions") or []
    scored: List[dict] = []
    if types_jsonl_path and Path(types_jsonl_path).exists():
        for line in Path(types_jsonl_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    scored.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    recs = attach_types(sweep, scored)
    if types_filter:
        want = {t.strip().lower() for t in types_filter}
        recs = [r for r in recs if r["type"].lower() in want]
    # drop questions with no resolvable gold in the bed -- a rank is undefined
    recs = [r for r in recs if r["gold_ids"]]
    if limit and limit > 0:
        recs = recs[:limit]
    return recs


# ── cell model + config ───────────────────────────────────────────────
@dataclass
class Cell:
    name: str
    arm: str
    combinator: Optional[str] = None
    delta: float = 0.05


def apply_arm(cfg, arm: str) -> None:
    """Mutate ``cfg`` in place to realise a retrieval arm. All arms run under
    RRF so the weight-zeroing in the ``dense`` arm takes effect."""
    r = cfg.retrieval
    r.fusion_mode = "rrf"
    if arm == "lexical":
        r.dense_embedding_enabled = False
        cfg.ingestion.splade_enabled = False
    elif arm == "dense":
        r.dense_embedding_enabled = True
        cfg.ingestion.splade_enabled = False
        r.fts5_weight = 0.0
        r.tag_exact_weight = 0.0
        r.tag_prefix_weight = 0.0
        r.filename_anchor_weight = 0.0
        r.lex_anchor_weight = 0.0
        r.bm25_shortlist_enabled = False
    elif arm == "fused":
        r.dense_embedding_enabled = True
        cfg.ingestion.splade_enabled = True
    else:
        raise ValueError(f"unknown arm: {arm!r} (expected one of {ARMS})")


def build_cells(arms: Sequence[str], combinators: Sequence[str],
                delta: float) -> List[Cell]:
    cells: List[Cell] = []
    for arm in arms:
        if combinators:
            for comb in combinators:
                cells.append(Cell(f"{arm}/{comb}", arm, combinator=comb,
                                  delta=delta))
        else:
            cells.append(Cell(arm, arm))
    return cells


def _make_manager(base_config: str, bed_path: str, cell: Cell):
    cfg = load_config(base_config)
    cfg.genome.path = bed_path
    apply_arm(cfg, cell.arm)
    if cell.combinator:
        cfg.retrieval.rerank_combinator = cell.combinator
        cfg.retrieval.rerank_band_delta = cell.delta
    return HelixContextManager(cfg)


# ── per-cell run ──────────────────────────────────────────────────────
def run_cell(base_config: str, bed_path: str, cell: Cell,
             questions: Sequence[dict], progress_every: int = 25) -> List[dict]:
    mgr = _make_manager(base_config, bed_path, cell)
    out: List[dict] = []
    try:
        for i, q in enumerate(questions, start=1):
            try:
                win = mgr.build_context(q["query"], read_only=True,
                                        ignore_delivered=True)
                scores = dict(getattr(mgr.genome, "last_query_scores", None) or {})
                expressed = list(getattr(win, "expressed_gene_ids", None) or [])
                assembled = getattr(win, "expressed_context", "") or ""
                m = score_question(q["gold_ids"], expressed, scores,
                                   q.get("gold_answer"), assembled)
            except Exception as exc:  # keep the sweep alive on a bad query
                print(f"  [warn] {cell.name} q{i}: {exc}", file=sys.stderr)
                m = {
                    "n_gold_ids": len(q["gold_ids"]), "pool_size": 0,
                    "n_expressed": 0, "gold_delivered_id": False,
                    "pool_present": False, "best_gold_rank": None,
                    "gold_answer_overlap": None, "gold_delivered_text": False,
                    "error": str(exc),
                }
            m["type"] = q["type"]
            out.append(m)
            if progress_every and i % progress_every == 0:
                print(f"  {cell.name}: {i}/{len(questions)}", file=sys.stderr)
    finally:
        mgr.close()
    return out


def group_by_type(records: Sequence[dict]) -> Dict[str, List[dict]]:
    groups: Dict[str, List[dict]] = {}
    for r in records:
        groups.setdefault(r.get("type", "unknown"), []).append(r)
    return groups


# ── reporting ─────────────────────────────────────────────────────────
def _fmt(x: Optional[float], nd: int = 2) -> str:
    return "   n/a" if x is None else f"{x:.{nd}f}"


def _print_table(cell_name: str, per_type: Dict[str, dict], overall: dict) -> None:
    print(f"\n{'-' * 86}\nARM/CELL: {cell_name}\n{'-' * 86}")
    print(f"{'type':<26}{'n':>5}{'gold_deliv':>12}{'pool':>7}"
          f"{'mRank':>8}{'medRank':>9}{'txtOv':>8}")
    for t in sorted(per_type):
        a = per_type[t]
        print(f"{t:<26}{a['n']:>5}{_fmt(a['gold_delivered_id_rate']):>12}"
              f"{_fmt(a['pool_present_rate']):>7}"
              f"{_fmt(a['mean_best_gold_rank'], 1):>8}"
              f"{_fmt(a['median_best_gold_rank'], 1):>9}"
              f"{_fmt(a['mean_gold_answer_overlap']):>8}")
    print(f"{'ALL':<26}{overall['n']:>5}{_fmt(overall['gold_delivered_id_rate']):>12}"
          f"{_fmt(overall['pool_present_rate']):>7}"
          f"{_fmt(overall['mean_best_gold_rank'], 1):>8}"
          f"{_fmt(overall['median_best_gold_rank'], 1):>9}"
          f"{_fmt(overall['mean_gold_answer_overlap']):>8}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bed-db", required=True,
                    help="bed genome DB path (opened read-only by the manager)")
    ap.add_argument("--questions", required=True,
                    help="sweep-queries JSON: [{'query','gold_ids'}] for THIS bed")
    ap.add_argument("--types-jsonl", default=DEFAULT_TYPES_JSONL,
                    help="scored jsonl carrying question/type/gold_answer "
                         "(joined by normalized text)")
    ap.add_argument("--types", default="",
                    help="comma list of question types to keep (default: all)")
    ap.add_argument("--arms", default="lexical,dense,fused",
                    help=f"comma list of {ARMS}")
    ap.add_argument("--combinators", default="",
                    help="optional rerank-combinator rider cells "
                         "(e.g. additive,eps_band,off); crossed with each arm")
    ap.add_argument("--delta", type=float, default=0.05,
                    help="eps_band delta for combinator riders")
    ap.add_argument("--base-config",
                    default="docs/benchmarks/helix_probe_lexical.toml",
                    help="LLM-free base config (decoder off); arms layer on top")
    ap.add_argument("--limit", type=int, default=0,
                    help="probe only the first N questions (0 = all)")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    bed_path = str(Path(args.bed_db).resolve())
    types_filter = [t for t in args.types.split(",") if t.strip()] or None
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    combinators = [c.strip() for c in args.combinators.split(",") if c.strip()]
    cells = build_cells(arms, combinators, args.delta)

    questions = load_questions(args.questions, args.types_jsonl,
                               types_filter, args.limit)
    if not questions:
        print("ERROR: no questions after filtering", file=sys.stderr)
        raise SystemExit(1)

    import collections
    type_hist = collections.Counter(q["type"] for q in questions)
    print(f"bed        : {bed_path}")
    print(f"questions  : {len(questions)}  types={dict(type_hist)}")
    print(f"cells      : {[c.name for c in cells]}")

    report: dict = {
        "config": {
            "bed": bed_path, "questions_path": args.questions,
            "types_jsonl": args.types_jsonl, "base_config": args.base_config,
            "types_filter": types_filter, "n_questions": len(questions),
            "type_hist": dict(type_hist), "cells": [c.name for c in cells],
        },
        "cells": {},
    }

    for cell in cells:
        print(f"\n=== running cell: {cell.name} ===", file=sys.stderr)
        records = run_cell(args.base_config, bed_path, cell, questions)
        groups = group_by_type(records)
        per_type = {t: aggregate(rs) for t, rs in groups.items()}
        overall = aggregate(records)
        report["cells"][cell.name] = {
            "arm": cell.arm, "combinator": cell.combinator,
            "overall": overall, "per_type": per_type,
            "records": records,
        }
        _print_table(cell.name, per_type, overall)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nJSON written: {out.resolve()}")


if __name__ == "__main__":
    main()
