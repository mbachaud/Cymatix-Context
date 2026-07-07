"""located_n1000 — calibration-data generator for the KnowBlock logistic (#239).

The long-referenced-but-never-committed generator that
``scripts/calibrate_know_confidence.py`` documents as its input source
(its docstring, step 1). Referenced across SETUP.md, the operator
runbooks and the Stage-1/6 specs; flagged ABSENT by the 2026-07-06
J-space roadmap council (verified-premises ledger). This file closes
that gap.

For each of N needles harvested from a bench bed (the Stage-1
``harvest_needles`` sampler + ``--axis located|blind`` query templates
from ``bench_needle_1000.py``), it runs the retrieval pipeline
IN-PROCESS (``build_context(read_only=True)`` — no server, no knowledge
store writes, learn disabled) and emits one JSONL row carrying exactly
the raw signals ``calibrate_know_confidence._row_to_features`` consumes:

    top_score              post-fusion rank-1 score
    score_gap              rank-1 minus rank-2 (rank-1 when singleton)
    lexical_dense_agree    top-k lexical/dense tier agreement
    coordinate_confidence  folder/file grain coverage in [0, 1]
    freshness_min          Stage-7 min freshness over expressed docs
    label                  1 iff planted_gene_id == retrieved_top1

plus audit fields (query, planted_gene_id, retrieved_top1, planted_rank,
expressed_rank, axis, degraded) so ``benchmarks/eval_retrieval.py`` can
compute retrieval@k / MRR / risk-coverage / ECE from the same file.

Feature derivation mirrors ``server/helpers._compute_know_or_miss_block``
(the runtime path that feeds ``compute_confidence``) so training rows and
inference see the same distributions.

Usage:

    python benchmarks/located_n1000.py \
        --bed-db genomes/bench/matrix/xl_clean.db \
        --base-config docs/benchmarks/helix_probe_lexical.toml \
        --n 1000 --seed 42 --axis located \
        --set retrieval.fusion_mode=rrf \
        --out benchmarks/results/located_n1000.jsonl

``--set section.key=value`` applies config overrides AFTER the base
config loads — use it to pin the calibration cell to the shipped
defaults (e.g. fusion_mode) without editing the probe profile.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Read-only contract, twice over: build_context(read_only=True) below AND
# the process-wide learn kill-switch (#221) — an echo-gene write into a
# bench bed poisons every later run against it (the SIKE Run-1 lesson).
os.environ.setdefault("HELIX_DISABLE_LEARN", "1")

_BENCH_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_DIR.parent
for _p in (str(_REPO_ROOT), str(_BENCH_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bench_needle_1000 import (  # noqa: E402
    build_query_blind,
    build_query_located,
    harvest_needles,
)

from helix_context.config import load_config  # noqa: E402
from helix_context.context_manager import HelixContextManager  # noqa: E402
from helix_context.context_packet import _coordinate_confidence  # noqa: E402
from helix_context.scoring.know_decision import (  # noqa: E402
    _agree_from_tier_contributions,
)


def _apply_overrides(cfg, overrides: list[str]) -> None:
    """Apply ``section.key=value`` strings onto the loaded config.

    Values parse as JSON when possible (numbers, booleans) and fall back
    to raw strings ("rrf" etc. work unquoted).
    """
    for item in overrides:
        path, _, raw = item.partition("=")
        if not _:
            raise SystemExit(f"--set expects section.key=value, got {item!r}")
        section, _, key = path.strip().partition(".")
        if not key:
            raise SystemExit(f"--set expects section.key=value, got {item!r}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw.strip()
        target = getattr(cfg, section, None)
        if target is None or not hasattr(target, key):
            raise SystemExit(f"unknown config field {section}.{key}")
        setattr(target, key, value)


def _gene_proxies(genome, gene_ids: list[str]):
    """source_id proxies for _coordinate_confidence, mirroring
    server/helpers (polymorphic citation lookup, response order)."""

    class _GeneProxy:
        __slots__ = ("gene_id", "source_id")

        def __init__(self, gid, sid):
            self.gene_id = gid
            self.source_id = sid

    proxies = []
    try:
        row_map = genome.get_citation_rows(gene_ids)
    except Exception:
        return proxies
    for gid in gene_ids:
        r = row_map.get(gid)
        if r is not None:
            proxies.append(_GeneProxy(gid, r["source_id"]))
    return proxies


def features_for_query(manager: HelixContextManager, query: str) -> dict:
    """Run one read-only retrieval turn and extract the raw know-features.

    Mirrors server/helpers._compute_know_or_miss_block field-for-field.
    """
    window = manager.build_context(query, read_only=True, ignore_delivered=True)

    raw_scores = manager.genome.last_query_scores or {}
    ranked = sorted(raw_scores.items(), key=lambda kv: kv[1], reverse=True)
    if ranked:
        top1_id, top_score = ranked[0][0], float(ranked[0][1])
        score_gap = (
            float(ranked[0][1] - ranked[1][1]) if len(ranked) > 1 else top_score
        )
    else:
        top1_id, top_score, score_gap = None, 0.0, 0.0

    tier_contrib = getattr(manager.genome, "last_tier_contributions", {}) or {}
    agree = _agree_from_tier_contributions(tier_contrib, k=3)

    expressed_ids = list(window.expressed_gene_ids or [])
    proxies = _gene_proxies(manager.genome, expressed_ids)
    coord = _coordinate_confidence(query, proxies) if proxies else 0.0

    freshness_min = getattr(window.context_health, "freshness_min", None)

    return {
        "retrieved_top1": top1_id,
        "top_score": top_score,
        "score_gap": score_gap,
        "lexical_dense_agree": bool(agree),
        "coordinate_confidence": float(coord),
        "freshness_min": freshness_min,
        "ranked_ids": [gid for gid, _ in ranked],
        "expressed_ids": expressed_ids,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bed-db", default="genomes/bench/matrix/xl_clean.db")
    ap.add_argument(
        "--base-config", default="docs/benchmarks/helix_probe_lexical.toml"
    )
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--axis", choices=("located", "blind"), default="located")
    ap.add_argument(
        "--set", dest="overrides", action="append", default=[],
        metavar="SECTION.KEY=VALUE",
        help="config override applied after --base-config (repeatable)",
    )
    ap.add_argument("--out", default="benchmarks/results/located_n1000.jsonl")
    args = ap.parse_args()

    if not Path(args.bed_db).exists():
        raise SystemExit(f"bed db not found: {args.bed_db}")

    cfg = load_config(args.base_config)
    cfg.genome.path = args.bed_db
    _apply_overrides(cfg, args.overrides)

    print(
        f"harvesting {args.n} needles (seed={args.seed}) from {args.bed_db}...",
        file=sys.stderr,
    )
    needles = harvest_needles(args.bed_db, args.n, args.seed)
    if not needles:
        raise SystemExit("no needles harvested — is this a KV-bearing bed?")

    manager = HelixContextManager(cfg)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_written = 0
    n_label1 = 0
    t0 = time.time()
    with out_path.open("w", encoding="utf-8") as fh:
        for i, needle in enumerate(needles):
            if args.axis == "located":
                query = build_query_located(needle)
                degraded = query == build_query_blind(needle)
            else:
                query = build_query_blind(needle)
                degraded = False
            try:
                feats = features_for_query(manager, query)
            except Exception as exc:  # keep the run alive; row is droppable
                print(f"  [{i}] ERROR {needle['gene_id']}: {exc}", file=sys.stderr)
                continue

            planted = needle["gene_id"]
            ranked_ids = feats.pop("ranked_ids")
            expressed_ids = feats.pop("expressed_ids")
            planted_rank = (
                ranked_ids.index(planted) + 1 if planted in ranked_ids else -1
            )
            expressed_rank = (
                expressed_ids.index(planted) + 1 if planted in expressed_ids else -1
            )
            label = int(feats["retrieved_top1"] == planted)
            n_label1 += label

            row = {
                "query": query,
                "axis": args.axis,
                "degraded": degraded,
                "planted_gene_id": planted,
                "planted_source": needle.get("source", ""),
                "key": needle.get("key"),
                "value": needle.get("value"),
                "planted_rank": planted_rank,
                "expressed_rank": expressed_rank,
                "label": label,
                **feats,
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            n_written += 1
            if n_written % 100 == 0:
                fh.flush()
                print(
                    f"  {n_written}/{len(needles)} rows, retrieval@1 so far "
                    f"{n_label1 / n_written:.3f} ({time.time() - t0:.0f}s)",
                    file=sys.stderr,
                )

    dt = time.time() - t0
    print(
        json.dumps(
            {
                "out": str(out_path),
                "rows": n_written,
                "retrieval_at_1": round(n_label1 / max(1, n_written), 4),
                "axis": args.axis,
                "bed_db": args.bed_db,
                "overrides": args.overrides,
                "seconds": round(dt, 1),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
