"""Stage 4: calibrate ANN threshold + per-classifier confidence floors.

Spec: ``docs/specs/2026-05-08-stage-4-threshold-calibration.md`` §3-§5.

This is the operator artifact that turns a genome snapshot + a located
bench JSON (``located_n1000.json``) into:

  1. A ``mu + sigma_mult * sigma`` ANN cosine cutoff (margin-over-random),
     computed by sampling ``--random-pairs`` random gene pairs from the
     genome's ``embedding_dense_v2`` blobs.
  2. Per-classifier ``abstain_top`` / ``focused_top`` / ``tight_top`` floors,
     derived from ``p85_miss`` / ``p25_hit`` / ``p60_hit`` of the bench's
     ``agent.score_top`` distributions, segmented by re-running each row's
     ``query`` through ``classify_query``.

Outputs:

  - TOML snippet on stdout (or ``--output-toml``) suitable for paste-in to
    ``helix.toml`` under ``[retrieval]`` and ``[abstain.<cls>]``.
  - ``calibration_report.json`` (``--output-report``) — provenance + stats,
    validates against ``$schema = calibration_report.v1.json``.
  - UPSERT into ``genome_calibration`` when ``--write-db`` (default).

LLM-free, deterministic, reproducible from the same ``--seed``.

Usage:
    python scripts/calibrate_thresholds.py \\
        --genome genome.db \\
        --bench benchmarks/located_n1000.json \\
        --output-report calibration_report.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import math
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("calibrate_thresholds")


# Classes the rule-based classifier emits (see helix_context.query_classifier).
# ``default`` is the catch-all + fallback for missing per-class blocks at runtime.
KNOWN_CLASSES = ("factual", "multi_hop", "arithmetic", "procedural", "default")

# Spec §5: when a class has fewer than this many total bench rows, the
# percentile estimates are degenerate. Fall back to the global default and
# emit a WARNING so the operator knows.
MIN_ROWS_PER_CLASS = 30


# ─── Margin-over-random ANN threshold ────────────────────────────────────


@dataclass
class AnnCalibrationResult:
    threshold: float
    mu: float
    sigma: float
    n_pairs: int
    dim: int
    sigma_mult: float
    seed: int
    n_genes: int


def _load_dense_vectors(genome_path: Path, dim: int) -> Tuple[List[str], "Any"]:
    """Load all dense vectors from ``genes.embedding_dense_v2``.

    Returns ``(gene_ids, fp32_matrix)``. Skips genes whose blob length does
    not match ``dim * 4`` bytes (legacy or partial-coverage rows).

    Raises ``RuntimeError`` if numpy is unavailable.
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("numpy required for calibration") from exc

    conn = sqlite3.connect(str(genome_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT gene_id, embedding_dense_v2 FROM genes "
            "WHERE embedding_dense_v2 IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    expected_bytes = dim * 4  # fp32 little-endian
    ids: List[str] = []
    vecs: List["np.ndarray"] = []
    for r in rows:
        blob = r["embedding_dense_v2"]
        if blob is None or len(blob) != expected_bytes:
            continue
        v = np.frombuffer(blob, dtype="<f4")
        if v.shape[0] != dim:
            continue
        ids.append(r["gene_id"])
        vecs.append(v)

    if not vecs:
        return [], np.zeros((0, dim), dtype=np.float32)
    matrix = np.stack(vecs).astype(np.float32, copy=False)
    return ids, matrix


def calibrate_ann_threshold(
    genome_path: Path,
    *,
    dim: int,
    n_pairs: int = 10000,
    sigma_mult: float = 3.0,
    seed: int = 42,
) -> AnnCalibrationResult:
    """Compute ``mu + sigma_mult * sigma`` over ``n_pairs`` random gene pairs.

    Spec §3 algorithm. Cosine of two L2-normalized vectors == dot product.
    BGE-M3 vectors arrive normalized from the codec, but we re-normalize
    defensively so a partial-coverage genome with mixed codec versions still
    produces a valid threshold.
    """
    import numpy as np

    ids, matrix = _load_dense_vectors(genome_path, dim)
    n_genes = len(ids)
    if n_genes < 2:
        raise ValueError(
            f"need at least 2 dense vectors to calibrate, found {n_genes} "
            f"in {genome_path}"
        )

    # L2-normalize each row (idempotent if already normalized).
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms < 1e-12, 1.0, norms)
    unit = matrix / norms

    rng = random.Random(seed)
    # If n_genes is small, n_pairs may exceed C(n_genes, 2) — cap to all
    # unique unordered pairs.
    max_pairs = (n_genes * (n_genes - 1)) // 2
    target_pairs = min(n_pairs, max_pairs)

    seen: set = set()
    cosines: List[float] = []
    attempts = 0
    max_attempts = target_pairs * 20 + 1000  # very loose backoff

    while len(cosines) < target_pairs and attempts < max_attempts:
        attempts += 1
        i = rng.randrange(n_genes)
        j = rng.randrange(n_genes)
        if i == j:
            continue
        key = (i, j) if i < j else (j, i)
        if key in seen:
            continue
        seen.add(key)
        cos = float(np.dot(unit[i], unit[j]))
        cosines.append(cos)

    if not cosines:
        raise RuntimeError("failed to sample any random pairs")

    cos_arr = np.asarray(cosines, dtype=np.float64)
    mu = float(cos_arr.mean())
    # ddof=1 — sample standard deviation (Bessel's correction).
    sigma = float(cos_arr.std(ddof=1)) if cos_arr.size >= 2 else 0.0
    threshold = mu + sigma_mult * sigma

    return AnnCalibrationResult(
        threshold=threshold,
        mu=mu,
        sigma=sigma,
        n_pairs=len(cosines),
        dim=dim,
        sigma_mult=sigma_mult,
        seed=seed,
        n_genes=n_genes,
    )


# ─── Per-classifier confidence floors ─────────────────────────────────────


@dataclass
class ClassFloors:
    abstain_top: float
    focused_top: float
    tight_top: float
    n_hits: int = 0
    n_misses: int = 0
    n_total: int = 0
    degenerate: bool = False  # n_total < MIN_ROWS_PER_CLASS


@dataclass
class FloorCalibrationResult:
    per_class: Dict[str, ClassFloors] = field(default_factory=dict)
    abstain_pct: float = 85.0
    focused_pct: float = 25.0
    tight_pct: float = 60.0
    bench_path: Optional[str] = None
    n_bench_rows: int = 0
    n_skipped: int = 0


def _percentile(values: List[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy ``method='linear'``).

    Returns ``0.0`` for empty input. ``pct`` ∈ [0, 100].
    """
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n == 1:
        return float(s[0])
    rank = (pct / 100.0) * (n - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(s[lo])
    frac = rank - lo
    return float(s[lo] * (1.0 - frac) + s[hi] * frac)


def _classify_bench_row(query: str) -> str:
    """Re-run the rule-based classifier against the bench query."""
    from helix_context.query_classifier import classify_query
    result = classify_query(query)
    return result.cls


def calibrate_floors(
    bench_path: Path,
    *,
    abstain_pct: float = 85.0,
    focused_pct: float = 25.0,
    tight_pct: float = 60.0,
    default_floors: Optional[ClassFloors] = None,
) -> FloorCalibrationResult:
    """Compute per-classifier ``abstain_top`` / ``focused_top`` / ``tight_top``
    from the bench's ``agent.score_top`` distributions.

    Spec §4. Each row is classified into ``(hit, miss)`` by whether
    ``agent.gene_id_top == row.gene_id`` (true) or not. We split scores by
    classifier class (re-derived from the row's ``query``) and compute:

        abstain_top = p_abstain over MISS scores  (default p85)
        focused_top = p_focused over HIT scores   (default p25)
        tight_top   = p_tight over HIT scores     (default p60)

    Falls back to ``default_floors`` for any class with ``n_total < 30``
    (spec §5).
    """
    with open(bench_path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        raise ValueError(f"{bench_path} is not a JSON list of rows")

    # Group: cls -> {"hits": [score], "misses": [score]}
    groups: Dict[str, Dict[str, List[float]]] = {
        cls: {"hits": [], "misses": []} for cls in KNOWN_CLASSES
    }
    n_skipped = 0
    n_bench_rows = 0

    for row in rows:
        if not isinstance(row, dict):
            n_skipped += 1
            continue
        query = row.get("query")
        agent = row.get("agent") or {}
        score_top = agent.get("score_top")
        gene_id_top = agent.get("gene_id_top")
        gene_id_truth = row.get("gene_id")
        if query is None or score_top is None:
            n_skipped += 1
            continue
        try:
            score_f = float(score_top)
        except (TypeError, ValueError):
            n_skipped += 1
            continue

        n_bench_rows += 1
        cls = _classify_bench_row(query)
        if cls not in groups:
            cls = "default"

        is_hit = (
            gene_id_top is not None
            and gene_id_truth is not None
            and gene_id_top == gene_id_truth
        )
        bucket = "hits" if is_hit else "misses"
        groups[cls][bucket].append(score_f)

    # Per-class floors.
    per_class: Dict[str, ClassFloors] = {}
    for cls, buckets in groups.items():
        hits = buckets["hits"]
        misses = buckets["misses"]
        n_total = len(hits) + len(misses)
        degenerate = n_total < MIN_ROWS_PER_CLASS
        if degenerate and default_floors is not None:
            floors = ClassFloors(
                abstain_top=default_floors.abstain_top,
                focused_top=default_floors.focused_top,
                tight_top=default_floors.tight_top,
                n_hits=len(hits),
                n_misses=len(misses),
                n_total=n_total,
                degenerate=True,
            )
        else:
            floors = ClassFloors(
                abstain_top=_percentile(misses, abstain_pct) if misses else 0.0,
                focused_top=_percentile(hits, focused_pct) if hits else 0.0,
                tight_top=_percentile(hits, tight_pct) if hits else 0.0,
                n_hits=len(hits),
                n_misses=len(misses),
                n_total=n_total,
                degenerate=degenerate,
            )
        per_class[cls] = floors

    return FloorCalibrationResult(
        per_class=per_class,
        abstain_pct=abstain_pct,
        focused_pct=focused_pct,
        tight_pct=tight_pct,
        bench_path=str(bench_path),
        n_bench_rows=n_bench_rows,
        n_skipped=n_skipped,
    )


# ─── Output emission (TOML snippet + JSON report) ────────────────────────


_CALIBRATION_REPORT_SCHEMA_URI = (
    "https://helix-context.dev/schemas/calibration_report.v1.json"
)


def emit_toml_snippet(
    ann: AnnCalibrationResult,
    floors: FloorCalibrationResult,
) -> str:
    """Render the TOML snippet per spec §5."""
    lines: List[str] = []
    lines.append("# Generated by scripts/calibrate_thresholds.py — DO NOT HAND-EDIT")
    lines.append(f"# Computed at {_now_iso()}")
    lines.append("")
    lines.append("[retrieval]")
    lines.append('ann_threshold_mode = "margin_over_random"')
    lines.append(f"ann_threshold_sigma_multiplier = {ann.sigma_mult:g}")
    lines.append(
        f"# mu={ann.mu:.4f} sigma={ann.sigma:.4f} N={ann.n_pairs} dim={ann.dim} "
        f"-> {ann.threshold:.4f} (n_genes={ann.n_genes}, seed={ann.seed})"
    )
    lines.append("")
    lines.append("[abstain]")
    lines.append('mode = "per_classifier"')
    for cls in KNOWN_CLASSES:
        f = floors.per_class.get(cls)
        if f is None:
            continue
        lines.append("")
        lines.append(f"[abstain.{cls}]")
        lines.append(f"abstain_top = {f.abstain_top:.4f}")
        lines.append(f"focused_top = {f.focused_top:.4f}")
        lines.append(f"tight_top   = {f.tight_top:.4f}")
        if f.degenerate:
            lines.append(
                f"# WARNING: only {f.n_total} rows for cls={cls!r} (n_hits={f.n_hits},"
                f" n_misses={f.n_misses}); using default floors."
            )
        else:
            lines.append(
                f"# n_hits={f.n_hits} n_misses={f.n_misses} (total={f.n_total})"
            )
    return "\n".join(lines) + "\n"


def emit_report(
    ann: AnnCalibrationResult,
    floors: FloorCalibrationResult,
    *,
    genome_path: Path,
) -> Dict[str, Any]:
    """Render the calibration_report.v1 JSON document per spec §5."""
    return {
        "$schema": _CALIBRATION_REPORT_SCHEMA_URI,
        "version": 1,
        "computed_at": _now_iso(),
        "genome": {
            "path": str(genome_path),
            "gene_count": ann.n_genes,
            "dim": ann.dim,
        },
        "ann_threshold": {
            "mode": "margin_over_random",
            "value": ann.threshold,
            "mu": ann.mu,
            "sigma": ann.sigma,
            "sigma_mult": ann.sigma_mult,
            "n_pairs": ann.n_pairs,
            "seed": ann.seed,
        },
        "floors": {
            "abstain_pct": floors.abstain_pct,
            "focused_pct": floors.focused_pct,
            "tight_pct": floors.tight_pct,
            "bench_path": floors.bench_path,
            "n_bench_rows": floors.n_bench_rows,
            "n_skipped": floors.n_skipped,
            "per_class": {
                cls: {
                    "abstain_top": f.abstain_top,
                    "focused_top": f.focused_top,
                    "tight_top": f.tight_top,
                    "n_hits": f.n_hits,
                    "n_misses": f.n_misses,
                    "n_total": f.n_total,
                    "degenerate": f.degenerate,
                }
                for cls, f in floors.per_class.items()
            },
        },
    }


def _now_iso() -> str:
    """ISO-8601 UTC timestamp (seconds precision)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── CLI ─────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="calibrate_thresholds",
        description="Calibrate ANN threshold + per-classifier confidence floors.",
    )
    p.add_argument("--genome", required=True, type=Path)
    p.add_argument("--bench", required=True, type=Path)
    p.add_argument("--output-toml", type=Path, default=None)
    p.add_argument(
        "--output-report",
        type=Path,
        default=Path("calibration_report.json"),
    )
    p.add_argument("--sigma-mult", type=float, default=3.0)
    p.add_argument("--random-pairs", type=int, default=10000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--abstain-pct", type=float, default=85.0)
    p.add_argument("--focused-pct", type=float, default=25.0)
    p.add_argument("--tight-pct", type=float, default=60.0)
    p.add_argument("--dim", type=int, default=1024,
                   help="Dense embedding dim (default 1024 = BGE-M3 full)")
    write_db = p.add_mutually_exclusive_group()
    write_db.add_argument(
        "--write-db",
        dest="write_db",
        action="store_true",
        help="UPSERT into genome_calibration (default)",
    )
    write_db.add_argument(
        "--no-write-db",
        dest="write_db",
        action="store_false",
        help="Do not write to genome_calibration",
    )
    p.set_defaults(write_db=True)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="No DB writes; emit TOML + report only",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _write_calibration_to_db(
    genome_path: Path,
    ann: AnnCalibrationResult,
) -> None:
    """UPSERT ``ann_threshold`` row into ``genome_calibration``.

    Direct sqlite3 write — does not go through ``Genome.upsert_calibration``
    because the script must work against pre-Stage-4 databases that do not
    yet have the table (we CREATE IF NOT EXISTS first).
    """
    conn = sqlite3.connect(str(genome_path))
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS genome_calibration (
            key          TEXT PRIMARY KEY,
            value_json   TEXT NOT NULL,
            computed_at  REAL NOT NULL
        )
        """)
        payload = json.dumps({
            "value": ann.threshold,
            "mu": ann.mu,
            "sigma": ann.sigma,
            "N": ann.n_pairs,
            "dim": ann.dim,
            "sigma_mult": ann.sigma_mult,
            "seed": ann.seed,
        })
        import time
        conn.execute(
            "INSERT INTO genome_calibration (key, value_json, computed_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value_json = excluded.value_json, "
            "  computed_at = excluded.computed_at",
            ("ann_threshold", payload, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.genome.exists():
        log.error("genome path does not exist: %s", args.genome)
        return 2
    if not args.bench.exists():
        log.error("bench path does not exist: %s", args.bench)
        return 2

    log.info("Calibrating ANN threshold (margin-over-random)")
    ann = calibrate_ann_threshold(
        args.genome,
        dim=args.dim,
        n_pairs=args.random_pairs,
        sigma_mult=args.sigma_mult,
        seed=args.seed,
    )
    log.info(
        "ann_threshold: mu=%.4f sigma=%.4f N=%d dim=%d -> %.4f",
        ann.mu, ann.sigma, ann.n_pairs, ann.dim, ann.threshold,
    )

    log.info("Calibrating per-classifier floors")
    floors = calibrate_floors(
        args.bench,
        abstain_pct=args.abstain_pct,
        focused_pct=args.focused_pct,
        tight_pct=args.tight_pct,
        default_floors=ClassFloors(
            abstain_top=2.5, focused_top=2.5, tight_top=5.0,
        ),
    )
    for cls, f in floors.per_class.items():
        flag = " [DEGENERATE]" if f.degenerate else ""
        log.info(
            "  cls=%s n_hits=%d n_misses=%d -> abstain=%.4f focused=%.4f tight=%.4f%s",
            cls, f.n_hits, f.n_misses,
            f.abstain_top, f.focused_top, f.tight_top, flag,
        )

    snippet = emit_toml_snippet(ann, floors)
    if args.output_toml is not None:
        args.output_toml.parent.mkdir(parents=True, exist_ok=True)
        args.output_toml.write_text(snippet, encoding="utf-8")
        log.info("Wrote TOML snippet -> %s", args.output_toml)
    else:
        sys.stdout.write(snippet)

    report = emit_report(ann, floors, genome_path=args.genome)
    if args.output_report is not None:
        args.output_report.parent.mkdir(parents=True, exist_ok=True)
        args.output_report.write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("Wrote report -> %s", args.output_report)

    if args.write_db and not args.dry_run:
        _write_calibration_to_db(args.genome, ann)
        log.info("UPSERT genome_calibration <- ann_threshold=%.4f", ann.threshold)
    elif args.dry_run:
        log.info("--dry-run: skipped genome_calibration write")
    else:
        log.info("--no-write-db: skipped genome_calibration write")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
