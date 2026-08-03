"""Perf regression gate for the 2026-08 latency campaign (A.1 item 9).

Compares a FRESH perf receipt against a committed BASELINE receipt of the
same kind and emits a verdict JSON in the bench_plr_smoke house pattern:
``{kind, gates: [{name, level_or_arm, baseline, fresh, ratio, limit,
pass}], verdict: PASS|FAIL, notes: []}``.

RATIO gates only — never absolute milliseconds. Every receipt in this
repo is a dev-box receipt, so the only defensible comparison is
fresh-vs-baseline on the same box.

Kinds and gates:
  server_scaling   per concurrency level present in BOTH receipts:
                     median_latency ratio <= 1.15, recall delta within
                     +/-0.02 absolute, canary p95 ratio <= 1.5, fresh
                     valid=true wherever baseline was, qps ratio >= 0.85
  cold_start       rollup first_context_ms_median ratio <= 1.25,
                     model_load_fraction_of_first >= baseline - 0.10,
                     fresh verdict must not be FAIL
  write_contention per arm present in BOTH receipts: fresh verdict PASS
                     wherever baseline PASSed, read-latency-vs-control
                     ratio <= baseline ratio * 1.25 (stored vs_control or
                     derived from arm read_latency vs the control arm),
                     busy_error_count regression (fresh > 3*baseline + 2)

Modes:
  default  --kind K --fresh F [--baseline B]   B defaults to the
           committed ``<kind>_baseline.json`` next to this script.
  --ab A B two receipt directories; every kind found in both is compared
           with A as the baseline (run_perf_ab flows).

Missing optional fields degrade to a note + skipped gate — an older
harness's receipt must never crash the gate. Exit codes: 0 PASS, 1 FAIL,
2 schema/read error.

Stdlib only, by bench convention.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

KINDS = ("server_scaling", "cold_start", "write_contention")

LIMITS = {
    "server_scaling": {
        "median_ratio": 1.15,
        "recall_delta": 0.02,
        "canary_p95_ratio": 1.5,
        "qps_ratio": 0.85,
    },
    "cold_start": {
        "first_context_ratio": 1.25,
        "model_load_fraction_drop": 0.10,
    },
    "write_contention": {
        "vs_control_growth": 1.25,
        "busy_mult": 3,
        "busy_slack": 2,
    },
}


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


def _num(container, key):
    """Return container[key] as float, or None when absent/non-numeric."""
    if not isinstance(container, dict):
        return None
    v = container.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _gate(name, where, baseline, fresh, ratio, limit, ok, metric="ratio"):
    return {
        "name": name,
        "level_or_arm": where,
        "baseline": baseline,
        "fresh": fresh,
        "ratio": ratio,
        "limit": limit,
        "metric": metric,
        "pass": bool(ok),
    }


def _ratio_gate(gates, notes, name, where, b, f, limit, direction="max"):
    """Append a ratio gate f/b vs limit; skip with a note when impossible."""
    if b is None or f is None:
        notes.append(f"skip {name} at {where}: field missing in "
                     f"{'baseline' if b is None else 'fresh'}")
        return
    if b <= 0:
        notes.append(f"skip {name} at {where}: baseline value {b} not ratio-able")
        return
    ratio = round(f / b, 4)
    ok = ratio <= limit if direction == "max" else ratio >= limit
    gates.append(_gate(name, where, b, f, ratio, limit, ok))


# ── server_scaling ───────────────────────────────────────────────────


def _levels_by_concurrency(receipt: dict, label: str) -> dict:
    levels = receipt.get("levels")
    if not isinstance(levels, list) or not levels:
        raise ReceiptError(f"{label} server_scaling receipt has no levels[]")
    out = {}
    for lv in levels:
        if isinstance(lv, dict) and lv.get("concurrency") is not None:
            out[lv["concurrency"]] = lv
    if not out:
        raise ReceiptError(f"{label} server_scaling levels[] carry no concurrency keys")
    return out


def check_server_scaling(baseline: dict, fresh: dict):
    lim = LIMITS["server_scaling"]
    gates, notes = [], []
    b_levels = _levels_by_concurrency(baseline, "baseline")
    f_levels = _levels_by_concurrency(fresh, "fresh")

    only_b = sorted(set(b_levels) - set(f_levels))
    only_f = sorted(set(f_levels) - set(b_levels))
    if only_b:
        notes.append(f"levels only in baseline (ungated): {only_b}")
    if only_f:
        notes.append(f"levels only in fresh (ungated): {only_f}")

    for conc in sorted(set(b_levels) & set(f_levels)):
        b, f = b_levels[conc], f_levels[conc]
        where = f"c{conc}"

        _ratio_gate(gates, notes, "median_latency_ratio", where,
                    _num(b, "median_latency_ms"), _num(f, "median_latency_ms"),
                    lim["median_ratio"], "max")

        recall_key = next((k for k in b if k.startswith("recall@")), None)
        if recall_key is None:
            notes.append(f"skip recall_delta at {where}: baseline has no recall@k")
        else:
            br, fr = _num(b, recall_key), _num(f, recall_key)
            if fr is None:
                notes.append(f"skip recall_delta at {where}: fresh has no {recall_key}")
            else:
                delta = round(fr - br, 4)
                gates.append(_gate("recall_delta", where, br, fr, delta,
                                   lim["recall_delta"],
                                   abs(delta) <= lim["recall_delta"],
                                   metric="delta"))

        _ratio_gate(gates, notes, "canary_p95_ratio", where,
                    _num(b.get("canary"), "p95_ms"), _num(f.get("canary"), "p95_ms"),
                    lim["canary_p95_ratio"], "max")

        if b.get("valid") is True:
            f_valid = f.get("valid")
            gates.append(_gate("valid", where, True, f_valid, None, None,
                               f_valid is True, metric="bool"))
        else:
            notes.append(f"skip valid gate at {where}: baseline level not valid=true")

        _ratio_gate(gates, notes, "throughput_qps_ratio", where,
                    _num(b, "throughput_qps"), _num(f, "throughput_qps"),
                    lim["qps_ratio"], "min")

    return gates, notes


# ── cold_start ───────────────────────────────────────────────────────


def check_cold_start(baseline: dict, fresh: dict):
    lim = LIMITS["cold_start"]
    gates, notes = [], []
    b_roll = baseline.get("rollup")
    f_roll = fresh.get("rollup")
    if not isinstance(b_roll, dict) or not isinstance(f_roll, dict):
        notes.append("skip rollup gates: rollup{} missing in "
                     + ("baseline" if not isinstance(b_roll, dict) else "fresh"))
        b_roll = b_roll if isinstance(b_roll, dict) else {}
        f_roll = f_roll if isinstance(f_roll, dict) else {}

    _ratio_gate(gates, notes, "first_context_ms_median_ratio", "rollup",
                _num(b_roll, "first_context_ms_median"),
                _num(f_roll, "first_context_ms_median"),
                lim["first_context_ratio"], "max")

    b_frac = _num(b_roll, "model_load_fraction_of_first")
    f_frac = _num(f_roll, "model_load_fraction_of_first")
    if b_frac is None or f_frac is None:
        notes.append("skip model_load_fraction_of_first: field missing in "
                     + ("baseline" if b_frac is None else "fresh"))
    else:
        floor = round(b_frac - lim["model_load_fraction_drop"], 4)
        gates.append(_gate("model_load_fraction_of_first", "rollup",
                           b_frac, f_frac, round(f_frac - b_frac, 4), floor,
                           f_frac >= floor, metric="delta"))

    f_verdict = fresh.get("verdict")
    if f_verdict is None:
        notes.append("skip verdict_not_fail: fresh receipt has no verdict")
    else:
        gates.append(_gate("verdict_not_fail", "receipt",
                           baseline.get("verdict"), f_verdict, None, None,
                           f_verdict != "FAIL", metric="bool"))

    return gates, notes


# ── write_contention ─────────────────────────────────────────────────


def _arms_by_id(receipt: dict, label: str) -> dict:
    arms = receipt.get("arms")
    if not isinstance(arms, list) or not arms:
        raise ReceiptError(f"{label} write_contention receipt has no arms[]")
    out = {}
    for a in arms:
        if isinstance(a, dict) and a.get("arm") is not None:
            out[a["arm"]] = a
    if not out:
        raise ReceiptError(f"{label} write_contention arms[] carry no arm ids")
    return out


def _vs_control_x(receipt: dict, arm_obj: dict, metric: str):
    """Read-latency-vs-control multiplier: stored vs_control, else derived
    from this arm's read_latency over the control arm's. Returns
    (value|None, how)."""
    x_key = {"median": "median_latency_x", "p95": "p95_latency_x"}[metric]
    ms_key = {"median": "median_latency_ms", "p95": "p95_latency_ms"}[metric]
    stored = _num(arm_obj.get("vs_control"), x_key)
    if stored is not None:
        return stored, "stored"
    ctrl_id = receipt.get("control_arm", "a")
    if arm_obj.get("arm") == ctrl_id:
        return None, "control-arm"
    arms = receipt.get("arms") or []
    ctrl = next((a for a in arms if isinstance(a, dict) and a.get("arm") == ctrl_id), None)
    c_ms = _num((ctrl or {}).get("read_latency"), ms_key)
    f_ms = _num(arm_obj.get("read_latency"), ms_key)
    if c_ms is None or f_ms is None or c_ms <= 0:
        return None, "unavailable"
    return round(f_ms / c_ms, 4), "derived"


def check_write_contention(baseline: dict, fresh: dict):
    lim = LIMITS["write_contention"]
    gates, notes = [], []
    b_arms = _arms_by_id(baseline, "baseline")
    f_arms = _arms_by_id(fresh, "fresh")

    only_b = sorted(set(b_arms) - set(f_arms))
    only_f = sorted(set(f_arms) - set(b_arms))
    if only_b:
        notes.append(f"arms only in baseline (ungated): {only_b}")
    if only_f:
        notes.append(f"arms only in fresh (ungated): {only_f}")

    for arm_id in sorted(set(b_arms) & set(f_arms)):
        b, f = b_arms[arm_id], f_arms[arm_id]
        where = f"arm-{arm_id}"

        if b.get("verdict") == "PASS":
            gates.append(_gate("arm_verdict", where, "PASS", f.get("verdict"),
                               None, None, f.get("verdict") == "PASS",
                               metric="bool"))
        else:
            notes.append(f"skip arm_verdict at {where}: baseline verdict was "
                         f"{b.get('verdict')!r}, not PASS")

        for metric in ("median", "p95"):
            b_x, b_how = _vs_control_x(baseline, b, metric)
            f_x, f_how = _vs_control_x(fresh, f, metric)
            name = f"vs_control_{metric}_ratio"
            if b_how == "control-arm" or f_how == "control-arm":
                continue  # control arm has no vs-control multiplier by design
            if b_x is None or f_x is None:
                notes.append(f"skip {name} at {where}: multiplier "
                             f"{'baseline' if b_x is None else 'fresh'} unavailable")
                continue
            if b_x <= 0:
                notes.append(f"skip {name} at {where}: baseline multiplier {b_x}")
                continue
            ratio = round(f_x / b_x, 4)
            gates.append(_gate(name, where, b_x, f_x, ratio,
                               lim["vs_control_growth"],
                               ratio <= lim["vs_control_growth"]))

        b_busy = _num(b, "busy_error_count")
        f_busy = _num(f, "busy_error_count")
        if b_busy is None or f_busy is None:
            notes.append(f"skip busy_error_count at {where}: field missing in "
                         f"{'baseline' if b_busy is None else 'fresh'}")
        else:
            ceiling = lim["busy_mult"] * b_busy + lim["busy_slack"]
            gates.append(_gate("busy_error_count", where, b_busy, f_busy,
                               None, ceiling, f_busy <= ceiling,
                               metric="count"))

    return gates, notes


# ── orchestration ────────────────────────────────────────────────────

_CHECKERS = {
    "server_scaling": check_server_scaling,
    "cold_start": check_cold_start,
    "write_contention": check_write_contention,
}


def compare(kind: str, baseline: dict, fresh: dict) -> dict:
    if kind not in _CHECKERS:
        raise ReceiptError(f"unknown receipt kind {kind!r} (want one of {KINDS})")
    gates, notes = _CHECKERS[kind](baseline, fresh)
    if not gates:
        notes.append("no gates evaluated — verdict PASS by absence of evidence")
    verdict = "PASS" if all(g["pass"] for g in gates) else "FAIL"
    return {"kind": kind, "gates": gates, "verdict": verdict, "notes": notes}


def _default_baseline(kind: str) -> Path:
    return Path(__file__).resolve().parent / f"{kind}_baseline.json"


def _find_kind_receipt(directory: Path, kind: str) -> Path | None:
    hits = sorted(directory.glob(f"{kind}*.json"))
    return hits[0] if hits else None


def run_ab(dir_a: str | Path, dir_b: str | Path) -> dict:
    da, db = Path(dir_a), Path(dir_b)
    for d in (da, db):
        if not d.is_dir():
            raise ReceiptError(f"--ab directory not found: {d}")
    results, notes, all_gates = [], [], []
    for kind in KINDS:
        pa, pb = _find_kind_receipt(da, kind), _find_kind_receipt(db, kind)
        if pa is None and pb is None:
            continue
        if pa is None or pb is None:
            notes.append(f"{kind}: receipt present only in "
                         f"{'B' if pa is None else 'A'} — skipped")
            continue
        res = compare(kind, load_receipt(pa), load_receipt(pb))
        res["baseline_path"] = str(pa)
        res["fresh_path"] = str(pb)
        results.append(res)
        for g in res["gates"]:
            all_gates.append(dict(g, name=f"{kind}/{g['name']}"))
    if not results:
        raise ReceiptError(f"--ab found no receipt kind present in both {da} and {db}")
    verdict = "PASS" if all(r["verdict"] == "PASS" for r in results) else "FAIL"
    return {
        "kind": "ab",
        "dir_a": str(da),
        "dir_b": str(db),
        "results": results,
        "gates": all_gates,
        "verdict": verdict,
        "notes": notes,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kind", choices=list(KINDS),
                    help="Receipt kind for the default fresh-vs-baseline mode.")
    ap.add_argument("--fresh", help="Fresh receipt JSON to gate.")
    ap.add_argument("--baseline",
                    help="Baseline receipt JSON (default: committed "
                         "<kind>_baseline.json next to this script).")
    ap.add_argument("--ab", nargs=2, metavar=("DIR_A", "DIR_B"),
                    help="Compare two receipt directories, A as baseline.")
    ap.add_argument("--out", help="Also write the verdict JSON to this path.")
    args = ap.parse_args(argv)

    try:
        if args.ab:
            result = run_ab(args.ab[0], args.ab[1])
        else:
            if not args.kind or not args.fresh:
                print("error: default mode needs --kind and --fresh "
                      "(or use --ab DIR_A DIR_B)", file=sys.stderr)
                return 2
            baseline_path = args.baseline or _default_baseline(args.kind)
            result = compare(args.kind,
                             load_receipt(baseline_path),
                             load_receipt(args.fresh))
            result["baseline_path"] = str(baseline_path)
            result["fresh_path"] = str(args.fresh)
    except ReceiptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
