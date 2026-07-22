"""Retrieval + know-confidence eval harness (#239, council move #3).

Consumes the JSONL emitted by ``benchmarks/located_n1000.py`` (one row
per labeled query: raw know-features + ``label`` + ``planted_rank``)
and reports:

  * retrieval@1 / retrieval@5 / retrieval@10 (planted_rank based)
  * MRR (mean reciprocal rank over the post-fusion ranking)
  * expressed@k (planted document made it into the assembled window)
  * know-confidence quality under a given calibration:
      - AUC (rank-based Mann-Whitney; sklearn not required)
      - ECE (10-bin, equal-width)
      - risk-coverage table (selective risk at 10%..100% coverage,
        confidence-ordered)

The confidence column is recomputed from the raw features via
``cymatix_context.scoring.know_calibration.compute_confidence`` so the
same JSONL can be scored under DEFAULT_BETAS (--calibration default),
the shipped helix.toml ([know] table), or any candidate TOML — which is
exactly the before/after comparison the #239 recalibration needs.

Usage:

    python benchmarks/eval_retrieval.py \
        --input benchmarks/results/located_n1000.jsonl \
        --calibration helix.toml \
        --out benchmarks/results/eval_retrieval_report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BENCH_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cymatix_context.scoring.know_calibration import (  # noqa: E402
    DEFAULT_BETAS,
    DEFAULT_EMIT_FLOOR,
    DEFAULT_G_REF,
    DEFAULT_S_REF,
    KnowCalibration,
    compute_confidence,
)


# ── metric primitives (pure, unit-tested) ────────────────────────────


def auc_mann_whitney(scores: list[float], labels: list[int]) -> float:
    """Rank-based AUC: P(score_pos > score_neg) with tie correction.

    Pure-python Mann-Whitney U / (n_pos · n_neg). Returns 0.5 when a
    class is missing (degenerate — no discrimination measurable).
    """
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return 0.5
    combined = sorted((s, y) for s, y in zip(scores, labels))
    # average ranks with tie handling
    ranks: dict[int, float] = {}
    i = 0
    rank_sum_pos = 0.0
    n = len(combined)
    while i < n:
        j = i
        while j + 1 < n and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-indexed average rank of the tie block
        for k in range(i, j + 1):
            if combined[k][1] == 1:
                rank_sum_pos += avg_rank
        i = j + 1
    n_pos, n_neg = len(pos), len(neg)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2
    return u / (n_pos * n_neg)


def ece(confidences: list[float], labels: list[int], n_bins: int = 10) -> float:
    """Expected Calibration Error, equal-width bins over [0, 1]."""
    if not confidences:
        return 0.0
    n = len(confidences)
    total = 0.0
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        idx = [
            i
            for i, c in enumerate(confidences)
            if (lo <= c < hi) or (b == n_bins - 1 and c == hi)
        ]
        if not idx:
            continue
        avg_conf = sum(confidences[i] for i in idx) / len(idx)
        acc = sum(labels[i] for i in idx) / len(idx)
        total += (len(idx) / n) * abs(acc - avg_conf)
    return total


def risk_coverage(
    confidences: list[float],
    labels: list[int],
    points: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
) -> list[dict]:
    """Selective risk at fixed coverage levels, confidence-ordered desc.

    At coverage c, answer the top c-fraction most-confident queries;
    risk = error rate on that covered subset.
    """
    if not confidences:
        return []
    order = sorted(range(len(confidences)), key=lambda i: -confidences[i])
    out = []
    n = len(order)
    for p in points:
        k = max(1, int(round(p * n)))
        covered = order[:k]
        errors = sum(1 - labels[i] for i in covered)
        out.append(
            {
                "coverage": p,
                "n": k,
                "risk": errors / k,
                "min_confidence": confidences[covered[-1]],
            }
        )
    return out


def mrr(planted_ranks: list[int]) -> float:
    """Mean reciprocal rank; rank -1 (not retrieved) contributes 0."""
    if not planted_ranks:
        return 0.0
    return sum(1.0 / r for r in planted_ranks if r > 0) / len(planted_ranks)


def retrieval_at(planted_ranks: list[int], k: int) -> float:
    if not planted_ranks:
        return 0.0
    return sum(1 for r in planted_ranks if 0 < r <= k) / len(planted_ranks)


# ── calibration loading ──────────────────────────────────────────────


def load_calibration(spec: str) -> KnowCalibration:
    """"default" → DEFAULT_BETAS; otherwise a TOML path with a [know] table."""
    if spec == "default":
        return KnowCalibration(
            betas=list(DEFAULT_BETAS),
            s_ref=DEFAULT_S_REF,
            g_ref=DEFAULT_G_REF,
            emit_floor=DEFAULT_EMIT_FLOOR,
        )
    try:
        import tomllib
    except ImportError:  # py<3.11
        import tomli as tomllib  # type: ignore
    with open(spec, "rb") as fh:
        raw = tomllib.load(fh)
    know = raw.get("know", {})
    return KnowCalibration(
        betas=list(know.get("betas", DEFAULT_BETAS)),
        s_ref=float(know.get("s_ref", DEFAULT_S_REF)),
        g_ref=float(know.get("g_ref", DEFAULT_G_REF)),
        emit_floor=float(know.get("emit_floor", DEFAULT_EMIT_FLOOR)),
    )


def score_rows(rows: list[dict], cal: KnowCalibration) -> list[float]:
    return [
        compute_confidence(
            top_score=float(r.get("top_score", 0.0)),
            score_gap=float(r.get("score_gap", 0.0)),
            lexical_dense_agree=bool(r.get("lexical_dense_agree", False)),
            coordinate_confidence=float(r.get("coordinate_confidence", 0.0)),
            calibration=cal,
            freshness_min=r.get("freshness_min"),
        )
        for r in rows
    ]


def build_report(rows: list[dict], cal: KnowCalibration, cal_name: str) -> dict:
    labels = [int(r.get("label", 0)) for r in rows]
    ranks = [int(r.get("planted_rank", -1)) for r in rows]
    confs = score_rows(rows, cal)
    return {
        "n": len(rows),
        "calibration": cal_name,
        "betas": list(cal.betas),
        "s_ref": cal.s_ref,
        "g_ref": cal.g_ref,
        "emit_floor": cal.emit_floor,
        "retrieval_at_1": retrieval_at(ranks, 1),
        "retrieval_at_5": retrieval_at(ranks, 5),
        "retrieval_at_10": retrieval_at(ranks, 10),
        "mrr": mrr(ranks),
        "expressed_rate": (
            sum(1 for r in rows if int(r.get("expressed_rank", -1)) > 0) / len(rows)
            if rows
            else 0.0
        ),
        "auc": auc_mann_whitney(confs, labels),
        "ece_10bin": ece(confs, labels, n_bins=10),
        "risk_coverage": risk_coverage(confs, labels),
        "emit_rate_at_floor": (
            sum(1 for c in confs if c >= cal.emit_floor) / len(confs)
            if confs
            else 0.0
        ),
        "precision_at_floor": (
            (
                sum(1 for c, y in zip(confs, labels) if c >= cal.emit_floor and y)
                / max(1, sum(1 for c in confs if c >= cal.emit_floor))
            )
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", required=True, help="located_n1000 JSONL")
    ap.add_argument(
        "--calibration",
        default="default",
        help='"default" (DEFAULT_BETAS) or a TOML path with a [know] table; repeatable via comma-separation for side-by-side reports',
    )
    ap.add_argument("--out", default="", help="optional JSON report path")
    args = ap.parse_args()

    rows = []
    with open(args.input, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        raise SystemExit(f"no rows in {args.input}")

    reports = []
    for spec in args.calibration.split(","):
        spec = spec.strip()
        cal = load_calibration(spec)
        reports.append(build_report(rows, cal, spec))

    payload = {"input": args.input, "reports": reports}
    text = json.dumps(payload, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"-> {args.out}", file=sys.stderr)
    print(text)


if __name__ == "__main__":
    main()
