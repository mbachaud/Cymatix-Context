"""Encoder daemon receipt gate (Fork 1 slice 1 receipts tooling).

Companion to ``benchmarks/dogfood/erb/encoder_daemon_capacity.py`` (the
capacity probe) and ``benchmarks/dogfood/run_server_scaling.py`` (the
server-level A/B harness the daemon-vs-off receipts come from). Same house
style as ``check_perf_receipts.py``: a gates list, a PASS/FAIL verdict, and
an exit-code contract of 0/1/2. RATIO (or absolute-floor) gates only — never
an absolute millisecond assertion baked into a test; the bars themselves are
CLI-supplied data, per the kickoff doc's "Discipline" section.

Kinds:
  encoder_capacity   ``encoder_daemon_capacity.py`` output. The kickoff bar
                       (>=3.5 CPU / >=30 GPU bundles/s) is discounted from
                       measured CONCURRENT-BATCHED ceilings (9.3 / 62
                       bundles/s) — a single synchronous client is
                       latency-bound by construction (measured GPU c=1: 19.5
                       bundles/s at 47ms/bundle) and cannot express that
                       ceiling, so the bar is only meaningful against the
                       SATURATED throughput, not every level individually.
                       BINDING gates: ``capacity_bundles_per_s`` =
                       max(``bundles_per_s`` across levels) >=
                       ``--bar-bundles-per-s`` (default 3.5; GPU runs pass
                       30) — the one absolute floor in this file, the fork's
                       bar being stated as a raw bundles/s number, not a
                       ratio; ``errors == 0`` at every level. INFORMATIONAL
                       (``informational: true``, never flips the verdict):
                       one ``level_c{n}_bundles_per_s`` entry per level
                       (same bar as limit) so the per-level shape — e.g. a
                       latency-bound c=1 sitting below the bar while c=8
                       saturates well above it — is still visible in the
                       verdict JSON. Rerank family (#341): a level's
                       ``rerank_pairs_per_s``, when present, is surfaced as a
                       plain NOTE (``notes[]``), never a gate entry — rerank
                       errors already fold into the level's ``errors`` field
                       (``encoder_daemon_capacity.aggregate_level``), so the
                       binding ``errors_zero`` gate covers rerank failures
                       with no checker change.
  server_scaling_ab  Two ``run_server_scaling.py`` receipts (fresh = daemon
                       on, baseline = daemon off), levels keyed by
                       ``concurrency``. Gates: c=1 ``median_latency_ms``
                       ratio <= 1.15; ``recall@k`` delta within +/-0.02
                       absolute at every common level (``recall_equal_exact``
                       is reported per level as an informational note, not a
                       gate); every common level c>=2 needs
                       ``throughput_qps`` ratio strictly > 1.0 (that is the
                       point of the daemon); every common level needs
                       ``valid == true`` and ``errors == 0`` in BOTH
                       receipts.
  erb_sanity         Same receipt shape, concurrency=1 only: median ratio
                       <= 1.15, ``errors == 0`` and ``valid == true`` in
                       both receipts.

Missing optional fields degrade to a note + skipped gate — an older
harness's receipt must never crash the gate. Exit codes: 0 PASS, 1 FAIL,
2 schema/read error. Stdlib only, by bench convention.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

KINDS = ("encoder_capacity", "server_scaling_ab", "erb_sanity")

DEFAULT_BUNDLES_PER_S_BAR = 3.5
MEDIAN_RATIO_LIMIT = 1.15
RECALL_DELTA_LIMIT = 0.02
QPS_RATIO_FLOOR = 1.0


class ReceiptError(Exception):
    """Schema/read problem that makes a comparison impossible (exit 2)."""


# ── plumbing ─────────────────────────────────────────────────────────


def load_receipt(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ReceiptError(f"receipt not found: {p}")
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot read receipt {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReceiptError(f"receipt {p} is not a JSON object")
    return data


def _num(container: Any, key: str) -> Optional[float]:
    """Return container[key] as float, or None when absent/non-numeric."""
    if not isinstance(container, dict):
        return None
    v = container.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _gate(name: str, where: str, value: Any, limit: Any, ok: bool, **extra: Any) -> dict:
    g = {"name": name, "level_or_arm": where, "value": value, "limit": limit, "pass": bool(ok)}
    g.update(extra)
    return g


def _ratio_gate(
    gates: List[dict], notes: List[str], name: str, where: str,
    b: Optional[float], f: Optional[float], limit: float, direction: str = "max",
) -> None:
    """Append a ratio gate f/b vs limit; skip with a note when impossible.

    ``direction="max"`` is a ceiling: pass iff ``ratio <= limit`` (regression
    budgets like the c=1 median). ``direction="gt"`` is a strict floor: pass
    iff ``ratio > limit`` — used for the c>=2 qps ratio, where the fork's
    whole point is that the daemon must beat the baseline, not merely tie it.
    """
    if b is None or f is None:
        notes.append(f"skip {name} at {where}: field missing in "
                     f"{'baseline' if b is None else 'fresh'}")
        return
    if b <= 0:
        notes.append(f"skip {name} at {where}: baseline value {b} not ratio-able")
        return
    ratio = round(f / b, 4)
    ok = ratio <= limit if direction == "max" else ratio > limit
    gates.append(_gate(name, where, ratio, limit, ok, baseline=b, fresh=f, metric="ratio"))


def _levels_by_concurrency(receipt: dict, label: str) -> Dict[Any, dict]:
    levels = receipt.get("levels")
    if not isinstance(levels, list) or not levels:
        raise ReceiptError(f"{label} receipt has no levels[]")
    out = {}
    for lv in levels:
        if isinstance(lv, dict) and lv.get("concurrency") is not None:
            out[lv["concurrency"]] = lv
    if not out:
        raise ReceiptError(f"{label} levels[] carry no concurrency keys")
    return out


# ── encoder_capacity ─────────────────────────────────────────────────


def check_encoder_capacity(receipt: dict, bar: float) -> Tuple[List[dict], List[str]]:
    gates: List[dict] = []
    notes: List[str] = []
    levels = receipt.get("levels")
    if not isinstance(levels, list) or not levels:
        raise ReceiptError("encoder_capacity receipt has no levels[]")
    if all(not isinstance(lv, dict) for lv in levels):
        raise ReceiptError("encoder_capacity receipt levels[] carry no usable entries")

    bps_by_level: List[Tuple[Any, float]] = []
    for lv in levels:
        if not isinstance(lv, dict):
            notes.append("skip malformed level entry (not an object)")
            continue
        n = lv.get("n_clients")
        where = f"n{n}" if n is not None else "n?"

        bps = _num(lv, "bundles_per_s")
        if bps is None:
            notes.append(f"skip level_c{n}_bundles_per_s at {where}: field missing")
        else:
            bps_by_level.append((n, bps))
            # Informational only: a single synchronous client is
            # latency-bound and cannot express the concurrent-batched
            # ceiling the bar is discounted from (module docstring) — this
            # entry shows the per-level shape but never flips the verdict.
            gates.append(_gate(f"level_c{n}_bundles_per_s", where, bps, bar,
                               bps >= bar, informational=True))

        errors = lv.get("errors")
        if isinstance(errors, bool) or not isinstance(errors, (int, float)):
            notes.append(f"skip errors_zero at {where}: field missing")
        else:
            gates.append(_gate("errors_zero", where, int(errors), 0,
                               int(errors) == 0, metric="count"))

        # Rerank family (#341): informational NOTE only, never a gate — the
        # fork's binding bar covers bundle throughput. Rerank errors already
        # folded into `errors` above by encoder_daemon_capacity.aggregate_
        # level, so errors_zero covers rerank failures with no gate here.
        rerank_pairs_per_s = _num(lv, "rerank_pairs_per_s")
        if rerank_pairs_per_s is not None:
            notes.append(f"info: rerank_pairs_per_s at {where} = {rerank_pairs_per_s}")

    # The BINDING throughput gate: saturated (max-across-levels) bundles/s
    # vs the bar. A latency-bound c=1 sitting below the bar is expected and
    # must not fail the run on its own — only informational above.
    if not bps_by_level:
        notes.append("skip capacity_bundles_per_s: no level reported bundles_per_s")
    else:
        peak_n, peak_bps = max(bps_by_level, key=lambda pair: pair[1])
        where = f"n{peak_n}" if peak_n is not None else "n?"
        gates.append(_gate("capacity_bundles_per_s", where, peak_bps, bar, peak_bps >= bar))

    return gates, notes


# ── shared server_scaling / erb_sanity level gate ────────────────────


def _level_gates(
    b: dict, f: dict, where: str, *,
    want_median: bool, want_qps: bool, want_recall: bool,
) -> Tuple[List[dict], List[str]]:
    gates: List[dict] = []
    notes: List[str] = []

    if want_median:
        _ratio_gate(gates, notes, "median_latency_ratio", where,
                    _num(b, "median_latency_ms"), _num(f, "median_latency_ms"),
                    MEDIAN_RATIO_LIMIT, "max")

    if want_recall:
        recall_key = next((k for k in b if isinstance(k, str) and k.startswith("recall@")), None)
        if recall_key is None:
            notes.append(f"skip recall_delta at {where}: baseline has no recall@k")
        else:
            br, fr = _num(b, recall_key), _num(f, recall_key)
            if fr is None:
                notes.append(f"skip recall_delta at {where}: fresh has no {recall_key}")
            else:
                delta = round(fr - br, 4)
                gates.append(_gate("recall_delta", where, delta, RECALL_DELTA_LIMIT,
                                   abs(delta) <= RECALL_DELTA_LIMIT,
                                   baseline=br, fresh=fr, metric="delta"))
                # Informational only — NOT a gate. Reported so a reviewer can
                # see at a glance whether the daemon reproduced the exact
                # same recall (the byte-parity claim) or merely a close one.
                notes.append(f"info: recall_equal_exact at {where} = {fr == br}")

    if want_qps:
        _ratio_gate(gates, notes, "qps_ratio", where,
                    _num(b, "throughput_qps"), _num(f, "throughput_qps"),
                    QPS_RATIO_FLOOR, "gt")

    b_valid, f_valid = b.get("valid"), f.get("valid")
    gates.append(_gate("valid_both", where, {"baseline": b_valid, "fresh": f_valid}, True,
                       b_valid is True and f_valid is True, metric="bool"))

    b_err, f_err = _num(b, "errors"), _num(f, "errors")
    if b_err is None or f_err is None:
        notes.append(f"skip errors_both_zero at {where}: field missing in "
                     f"{'baseline' if b_err is None else 'fresh'}")
    else:
        gates.append(_gate("errors_both_zero", where, {"baseline": b_err, "fresh": f_err}, 0,
                           b_err == 0 and f_err == 0, metric="count"))

    return gates, notes


# ── server_scaling_ab ────────────────────────────────────────────────


def check_server_scaling_ab(baseline: dict, fresh: dict) -> Tuple[List[dict], List[str]]:
    gates: List[dict] = []
    notes: List[str] = []
    b_levels = _levels_by_concurrency(baseline, "baseline")
    f_levels = _levels_by_concurrency(fresh, "fresh")

    only_b = sorted(set(b_levels) - set(f_levels))
    only_f = sorted(set(f_levels) - set(b_levels))
    if only_b:
        notes.append(f"levels only in baseline (ungated): {only_b}")
    if only_f:
        notes.append(f"levels only in fresh (ungated): {only_f}")

    common = sorted(set(b_levels) & set(f_levels))
    if 1 not in common:
        notes.append("c=1 not present in both receipts: median_latency_ratio gate skipped")

    for conc in common:
        b, f = b_levels[conc], f_levels[conc]
        where = f"c{conc}"
        g, n = _level_gates(
            b, f, where,
            want_median=(conc == 1), want_qps=(conc >= 2), want_recall=True,
        )
        gates.extend(g)
        notes.extend(n)

    return gates, notes


# ── erb_sanity ────────────────────────────────────────────────────────


def check_erb_sanity(baseline: dict, fresh: dict) -> Tuple[List[dict], List[str]]:
    b_levels = _levels_by_concurrency(baseline, "baseline")
    f_levels = _levels_by_concurrency(fresh, "fresh")
    if 1 not in b_levels or 1 not in f_levels:
        raise ReceiptError(
            "erb_sanity requires concurrency=1 in both baseline and fresh receipts"
        )
    return _level_gates(
        b_levels[1], f_levels[1], "c1",
        want_median=True, want_qps=False, want_recall=False,
    )


# ── orchestration ────────────────────────────────────────────────────

_AB_CHECKERS = {
    "server_scaling_ab": check_server_scaling_ab,
    "erb_sanity": check_erb_sanity,
}


def compare(kind: str, fresh: dict, baseline: Optional[dict], bar_bundles_per_s: float) -> dict:
    if kind not in KINDS:
        raise ReceiptError(f"unknown receipt kind {kind!r} (want one of {KINDS})")

    if kind == "encoder_capacity":
        gates, notes = check_encoder_capacity(fresh, bar_bundles_per_s)
    else:
        if baseline is None:
            raise ReceiptError(f"--kind {kind} requires --baseline")
        gates, notes = _AB_CHECKERS[kind](baseline, fresh)

    binding = [g for g in gates if not g.get("informational")]
    if not binding:
        notes.append("no binding gates evaluated — verdict PASS by absence of evidence")
    verdict = "PASS" if all(g["pass"] for g in binding) else "FAIL"
    return {"kind": kind, "gates": gates, "verdict": verdict, "notes": notes}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kind", choices=list(KINDS), help="Receipt kind to gate.")
    ap.add_argument("--fresh", help="Fresh receipt JSON to gate.")
    ap.add_argument(
        "--baseline",
        help="Baseline (daemon-off) receipt JSON — required for "
             "server_scaling_ab / erb_sanity.",
    )
    ap.add_argument(
        "--bar-bundles-per-s", type=float, default=DEFAULT_BUNDLES_PER_S_BAR,
        help="encoder_capacity throughput floor (default %(default)s; "
             "GPU profile runs pass 30).",
    )
    ap.add_argument("--out", help="Also write the verdict JSON to this path.")
    args = ap.parse_args(argv)

    if not args.kind or not args.fresh:
        print("error: --kind and --fresh are required", file=sys.stderr)
        return 2

    try:
        fresh = load_receipt(args.fresh)
        baseline = load_receipt(args.baseline) if args.baseline else None
        result = compare(args.kind, fresh, baseline, args.bar_bundles_per_s)
    except ReceiptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result["fresh_path"] = str(args.fresh)
    if args.baseline:
        result["baseline_path"] = str(args.baseline)

    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
