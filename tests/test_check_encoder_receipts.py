"""Fork 1 slice 1 receipts tooling: encoder daemon capacity + ratio gates.

Two things are exercised here, both stdlib-only and non-live:

1. ``check_encoder_receipts.py`` — synthetic receipt dicts drive every gate
   family (pass case + each gate's failure) for all three kinds, plus the
   0/1/2 exit-code contract and graceful degradation on missing optional
   fields (repo convention — see ``test_check_perf_receipts.py``).
2. ``encoder_daemon_capacity.py``'s ``aggregate_level`` — the pure
   level-stats-from-raw-timings function factored out of the probe so it can
   be unit-tested without spawning a daemon. The probe script itself gets no
   non-live test (it spawns a subprocess and hits real HTTP).

No absolute-ms assertions on the checker's own limits — those are CLI data
(``MEDIAN_RATIO_LIMIT`` etc.), not something this suite re-derives; it only
asserts the gate's pass/fail behavior against them.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

# Make benchmarks/, benchmarks/dogfood, and benchmarks/dogfood/erb importable
# as flat module paths (repo test convention — see test_check_perf_receipts.py
# / test_perf_instrumentation.py).
_BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
for _p in (_BENCH, _BENCH / "dogfood", _BENCH / "dogfood" / "erb"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import check_encoder_receipts as cer
import encoder_daemon_capacity as edc


# ── synthetic receipt builders ───────────────────────────────────────


def make_capacity_receipt(levels):
    """*levels* is a list of (n_clients, bundles_per_s, errors) tuples."""
    return {
        "kind": "encoder_capacity",
        "profile_label": "cpu",
        "levels": [
            {
                "n_clients": n,
                "bundles_total": 50,
                "errors": errors,
                "wall_s": 10.0,
                "bundles_per_s": bps,
                "latency_p50_ms": 100.0,
                "latency_p95_ms": 150.0,
                "latency_max_ms": 200.0,
            }
            for n, bps, errors in levels
        ],
    }


def make_scaling_receipt(median=1000.0, recall=0.5556, qps=1.5, valid=True,
                         errors=0, concurrencies=(1, 2, 4)):
    return {
        "measured_at": "2026-08-05T00:00:00+00:00",
        "levels": [
            {
                "concurrency": c,
                "median_latency_ms": median,
                "recall@12": recall,
                "throughput_qps": qps,
                "valid": valid,
                "errors": errors,
            }
            for c in concurrencies
        ],
    }


def write_json(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def run_checker(tmp_path, kind, fresh, baseline=None, out_name="verdict.json", extra_args=()):
    f = write_json(tmp_path, f"{kind}_fresh.json", fresh)
    out = tmp_path / out_name
    argv = ["--kind", kind, "--fresh", str(f), "--out", str(out), *extra_args]
    if baseline is not None:
        b = write_json(tmp_path, f"{kind}_baseline.json", baseline)
        argv += ["--baseline", str(b)]
    rc = cer.main(argv)
    return rc, json.loads(out.read_text(encoding="utf-8"))


def gates_named(result, name):
    return [g for g in result["gates"] if g["name"] == name]


# ── encoder_capacity ─────────────────────────────────────────────────
#
# Calibrated from the first real receipts: CPU 4.26/5.60/6.86/7.02
# bundles/s at c=1/2/4/8 (bar 3.5), GPU 19.46/31.34/45.09/52.70 (bar 30).
# The kickoff bar is discounted from measured CONCURRENT-BATCHED ceilings
# (9.3/62 bundles/s); a single synchronous client is latency-bound by
# construction (measured GPU c=1: 19.5 bundles/s at 47ms/bundle) and cannot
# express that ceiling, so the binding gate is the SATURATED (max-across-
# levels) throughput, not every level individually — per-level entries are
# informational only.


def test_encoder_capacity_passes_default_bar(tmp_path):
    fresh = make_capacity_receipt([(1, 4.26, 0), (2, 5.60, 0), (4, 6.86, 0), (8, 7.02, 0)])
    rc, result = run_checker(tmp_path, "encoder_capacity", fresh)
    assert rc == 0
    assert result["verdict"] == "PASS"
    level_gates = gates_named(result, "level_c1_bundles_per_s") \
        + gates_named(result, "level_c2_bundles_per_s") \
        + gates_named(result, "level_c4_bundles_per_s") \
        + gates_named(result, "level_c8_bundles_per_s")
    assert len(level_gates) == 4
    assert all(g["informational"] is True for g in level_gates)
    assert len(gates_named(result, "errors_zero")) == 4
    cap = gates_named(result, "capacity_bundles_per_s")
    assert len(cap) == 1
    assert cap[0]["value"] == 7.02  # the saturated (c=8) reading
    assert cap[0]["pass"] is True
    assert not cap[0].get("informational")
    assert all(g["pass"] for g in result["gates"])  # every level clears the bar here too


def test_encoder_capacity_c1_below_bar_but_saturated_above_passes(tmp_path):
    """Case (a): c=1 is latency-bound below the bar; c=8 saturates above it."""
    fresh = make_capacity_receipt([(1, 2.0, 0), (2, 4.26, 0), (4, 6.86, 0), (8, 7.02, 0)])
    rc, result = run_checker(tmp_path, "encoder_capacity", fresh)
    assert rc == 0
    assert result["verdict"] == "PASS"

    c1 = gates_named(result, "level_c1_bundles_per_s")
    assert c1 and c1[0]["pass"] is False and c1[0]["informational"] is True

    cap = gates_named(result, "capacity_bundles_per_s")[0]
    assert cap["value"] == 7.02
    assert cap["pass"] is True


def test_encoder_capacity_saturated_max_below_bar_fails(tmp_path):
    """Case (b): every level (including the saturated one) is below the bar."""
    fresh = make_capacity_receipt([(1, 1.0, 0), (2, 1.5, 0), (4, 2.0, 0), (8, 2.5, 0)])
    rc, result = run_checker(tmp_path, "encoder_capacity", fresh)
    assert rc == 1
    assert result["verdict"] == "FAIL"
    cap = gates_named(result, "capacity_bundles_per_s")[0]
    assert cap["value"] == 2.5
    assert cap["pass"] is False


def test_encoder_capacity_errors_fail_even_when_saturated_above_bar(tmp_path):
    """Case (c): errors_zero is binding regardless of throughput."""
    fresh = make_capacity_receipt([(1, 4.26, 0), (2, 5.60, 0), (4, 6.86, 0), (8, 7.02, 2)])
    rc, result = run_checker(tmp_path, "encoder_capacity", fresh)
    assert rc == 1
    assert result["verdict"] == "FAIL"
    cap = gates_named(result, "capacity_bundles_per_s")[0]
    assert cap["pass"] is True  # throughput itself is fine
    bad = [g for g in gates_named(result, "errors_zero") if g["level_or_arm"] == "n8"]
    assert bad and bad[0]["pass"] is False


def test_encoder_capacity_gpu_bar_via_cli(tmp_path):
    fresh = make_capacity_receipt([(1, 19.46, 0), (2, 31.34, 0), (4, 45.09, 0), (8, 52.70, 0)])
    rc, result = run_checker(tmp_path, "encoder_capacity", fresh,
                             extra_args=["--bar-bundles-per-s", "30"])
    assert rc == 0
    assert result["verdict"] == "PASS"
    # c=1 (19.46) is latency-bound below a 30 GPU bar — informational only.
    c1 = gates_named(result, "level_c1_bundles_per_s")
    assert c1 and c1[0]["pass"] is False and c1[0]["informational"] is True

    fresh_low = make_capacity_receipt([(1, 20.0, 0)])  # only level, below a 30 GPU bar
    rc2, result2 = run_checker(tmp_path, "encoder_capacity", fresh_low,
                               out_name="verdict2.json",
                               extra_args=["--bar-bundles-per-s", "30"])
    assert rc2 == 1
    assert result2["verdict"] == "FAIL"


def test_checker_keeps_bundle_gate_binding_and_rerank_informational(tmp_path):
    """Rerank fields (#341) on a level must surface as a NOTE, never a gate —
    the binding bundle gates (capacity_bundles_per_s, errors_zero) are
    untouched by rerank load."""
    fresh = make_capacity_receipt([(1, 4.26, 0), (2, 5.60, 0), (4, 6.86, 0), (8, 7.02, 0)])
    for lv in fresh["levels"]:
        lv["rerank_scores_total"] = 100
        lv["rerank_pairs_per_s"] = 227.27
        lv["rerank_latency_p50_ms"] = 200.0
        lv["rerank_latency_p95_ms"] = 240.0
    rc, result = run_checker(tmp_path, "encoder_capacity", fresh)
    assert rc == 0
    assert result["verdict"] == "PASS"
    assert any(g["name"] == "capacity_bundles_per_s" for g in result["gates"])
    assert not any(g["name"].startswith("rerank") for g in result["gates"])
    # The note must label the sequential-latency-proxy methodology inline —
    # rerank_pairs_per_s sits right beside bundles_per_s in the receipt and
    # must not be mistaken for a comparable concurrent-phase rate.
    assert (
        "info: rerank_pairs_per_s (sequential-latency proxy, not phase "
        "wall-clock) at n1 = 227.27" in result["notes"]
    )


def test_encoder_capacity_empty_levels_exit_2(tmp_path):
    fresh = write_json(tmp_path, "empty.json", {"kind": "encoder_capacity", "levels": []})
    rc = cer.main(["--kind", "encoder_capacity", "--fresh", str(fresh)])
    assert rc == 2


# ── server_scaling_ab ─────────────────────────────────────────────────


def test_server_scaling_ab_daemon_improves_passes(tmp_path):
    baseline = make_scaling_receipt(median=1000.0, recall=0.5556, qps=1.5)
    fresh = make_scaling_receipt(median=1000.0, recall=0.5556, qps=2.0)  # qps up at every level
    rc, result = run_checker(tmp_path, "server_scaling_ab", fresh, baseline)
    assert rc == 0
    assert result["verdict"] == "PASS"
    # median gated only at c=1, qps gated only at c>=2
    assert len(gates_named(result, "median_latency_ratio")) == 1
    assert len(gates_named(result, "qps_ratio")) == 2
    assert len(gates_named(result, "recall_delta")) == 3
    assert len(gates_named(result, "valid_both")) == 3
    assert len(gates_named(result, "errors_both_zero")) == 3
    assert any("recall_equal_exact" in n and "True" in n for n in result["notes"])


def test_server_scaling_ab_median_regression_fails(tmp_path):
    baseline = make_scaling_receipt(median=1000.0, qps=1.5)
    fresh = make_scaling_receipt(median=1200.0, qps=2.0)  # 1.2x > 1.15 ceiling at c=1
    rc, result = run_checker(tmp_path, "server_scaling_ab", fresh, baseline)
    assert rc == 1
    bad = gates_named(result, "median_latency_ratio")
    assert bad and bad[0]["pass"] is False
    assert bad[0]["value"] == 1.2
    assert bad[0]["level_or_arm"] == "c1"


def test_server_scaling_ab_recall_drift_fails(tmp_path):
    baseline = make_scaling_receipt(recall=0.5556, qps=1.5)
    fresh = make_scaling_receipt(recall=0.50, qps=2.0)  # drift 0.0556 > 0.02
    rc, result = run_checker(tmp_path, "server_scaling_ab", fresh, baseline)
    assert rc == 1
    bad = gates_named(result, "recall_delta")
    assert bad and all(g["pass"] is False for g in bad)
    assert any("recall_equal_exact" in n and "False" in n for n in result["notes"])


def test_server_scaling_ab_qps_not_improved_fails(tmp_path):
    baseline = make_scaling_receipt(qps=1.5)
    fresh = make_scaling_receipt(qps=1.5)  # ratio exactly 1.0 — strict > required
    rc, result = run_checker(tmp_path, "server_scaling_ab", fresh, baseline)
    assert rc == 1
    bad = gates_named(result, "qps_ratio")
    assert bad and all(g["pass"] is False for g in bad)
    assert all(g["value"] == 1.0 for g in bad)
    # c=1 has no qps gate at all (only c>=2)
    assert not any(g["level_or_arm"] == "c1" for g in bad)


def test_server_scaling_ab_invalid_level_fails(tmp_path):
    baseline = make_scaling_receipt(qps=1.5, valid=True)
    fresh = make_scaling_receipt(qps=2.0, valid=False)
    rc, result = run_checker(tmp_path, "server_scaling_ab", fresh, baseline)
    assert rc == 1
    bad = gates_named(result, "valid_both")
    assert bad and all(g["pass"] is False for g in bad)


def test_server_scaling_ab_errors_fails(tmp_path):
    baseline = make_scaling_receipt(qps=1.5, errors=0)
    fresh = make_scaling_receipt(qps=2.0, errors=3)
    rc, result = run_checker(tmp_path, "server_scaling_ab", fresh, baseline)
    assert rc == 1
    bad = gates_named(result, "errors_both_zero")
    assert bad and all(g["pass"] is False for g in bad)


def test_server_scaling_ab_missing_optional_fields_skip_not_crash(tmp_path):
    baseline = make_scaling_receipt(qps=1.5)
    fresh = make_scaling_receipt(qps=2.0)
    for lv in fresh["levels"]:
        del lv["recall@12"]
    rc, result = run_checker(tmp_path, "server_scaling_ab", fresh, baseline)
    assert rc == 0
    assert result["verdict"] == "PASS"
    assert not gates_named(result, "recall_delta")
    assert any("recall_delta" in n for n in result["notes"])


def test_server_scaling_ab_requires_baseline_exit_2(tmp_path):
    fresh = write_json(tmp_path, "fresh.json", make_scaling_receipt())
    rc = cer.main(["--kind", "server_scaling_ab", "--fresh", str(fresh)])
    assert rc == 2


# ── erb_sanity ────────────────────────────────────────────────────────


def test_erb_sanity_passes(tmp_path):
    baseline = make_scaling_receipt(median=2600.0, concurrencies=(1,))
    fresh = make_scaling_receipt(median=2600.0, concurrencies=(1,))
    rc, result = run_checker(tmp_path, "erb_sanity", fresh, baseline)
    assert rc == 0
    assert result["verdict"] == "PASS"
    assert gates_named(result, "median_latency_ratio")
    assert gates_named(result, "valid_both")
    assert gates_named(result, "errors_both_zero")
    # erb_sanity gates neither recall nor qps
    assert not gates_named(result, "recall_delta")
    assert not gates_named(result, "qps_ratio")


def test_erb_sanity_median_regression_fails(tmp_path):
    baseline = make_scaling_receipt(median=2600.0, concurrencies=(1,))
    fresh = make_scaling_receipt(median=5100.0, concurrencies=(1,))  # ~2x
    rc, result = run_checker(tmp_path, "erb_sanity", fresh, baseline)
    assert rc == 1
    bad = gates_named(result, "median_latency_ratio")
    assert bad and bad[0]["pass"] is False


def test_erb_sanity_errors_or_invalid_fails(tmp_path):
    baseline = make_scaling_receipt(median=2600.0, concurrencies=(1,), errors=0, valid=True)
    fresh = make_scaling_receipt(median=2600.0, concurrencies=(1,), errors=1, valid=False)
    rc, result = run_checker(tmp_path, "erb_sanity", fresh, baseline)
    assert rc == 1
    assert gates_named(result, "valid_both")[0]["pass"] is False
    assert gates_named(result, "errors_both_zero")[0]["pass"] is False


def test_erb_sanity_missing_c1_exit_2(tmp_path):
    baseline = make_scaling_receipt(concurrencies=(2,))
    fresh = make_scaling_receipt(concurrencies=(2,))
    rc = cer.main([
        "--kind", "erb_sanity",
        "--fresh", str(write_json(tmp_path, "fresh.json", fresh)),
        "--baseline", str(write_json(tmp_path, "baseline.json", baseline)),
    ])
    assert rc == 2


# ── exit-code contract ───────────────────────────────────────────────


def test_exit_2_on_missing_file(tmp_path):
    rc = cer.main(["--kind", "encoder_capacity", "--fresh", str(tmp_path / "nope.json")])
    assert rc == 2


def test_exit_2_on_malformed_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    rc = cer.main(["--kind", "encoder_capacity", "--fresh", str(bad)])
    assert rc == 2


def test_exit_2_on_missing_args():
    assert cer.main([]) == 2
    assert cer.main(["--kind", "encoder_capacity"]) == 2


def test_unknown_kind_rejected_by_argparse():
    import pytest

    with pytest.raises(SystemExit):
        cer.main(["--kind", "not_a_kind", "--fresh", "whatever.json"])


# ── aggregate_level (pure function, no subprocess/network) ───────────


def test_aggregate_level_basic_percentiles():
    stats = edc.aggregate_level(
        n_clients=2, per_request_ms=[100.0, 200.0, 300.0, 400.0, 500.0],
        errors=0, wall_s=2.0,
    )
    assert stats["n_clients"] == 2
    assert stats["bundles_total"] == 5
    assert stats["errors"] == 0
    assert stats["bundles_per_s"] == 2.5
    assert stats["latency_p50_ms"] == 300.0
    assert stats["latency_p95_ms"] == 500.0
    assert stats["latency_max_ms"] == 500.0


def test_aggregate_level_counts_errors_into_bundles_total():
    stats = edc.aggregate_level(
        n_clients=4, per_request_ms=[100.0, 200.0], errors=3, wall_s=1.0,
    )
    assert stats["bundles_total"] == 5  # 2 successful + 3 errors
    assert stats["errors"] == 3
    assert stats["bundles_per_s"] == 5.0
    # percentiles computed only over the 2 successful timings
    assert stats["latency_p50_ms"] == 100.0
    assert stats["latency_p95_ms"] == 200.0
    assert stats["latency_max_ms"] == 200.0


def test_aggregate_level_zero_wall_s_guards_div_by_zero():
    stats = edc.aggregate_level(n_clients=1, per_request_ms=[100.0], errors=0, wall_s=0.0)
    assert stats["bundles_per_s"] == 0.0
    assert math.isfinite(stats["bundles_per_s"])


def test_aggregate_level_no_successful_requests_latency_is_none():
    stats = edc.aggregate_level(n_clients=1, per_request_ms=[], errors=5, wall_s=2.0)
    assert stats["bundles_total"] == 5
    assert stats["bundles_per_s"] == 2.5
    assert stats["latency_p50_ms"] is None
    assert stats["latency_p95_ms"] is None
    assert stats["latency_max_ms"] is None


def test_aggregate_level_with_rerank_vectors():
    """rerank_ms/errors/pairs_per_request (#341) fold into the pure row.

    n=2 successful rerank calls; nearest-rank percentile() on sorted
    [200.0, 240.0] at p=0.50 -> idx = round(0.5 * 1) = round(0.5) = 0
    (Python banker's rounding) -> sorted[0] == 200.0.
    """
    row = edc.aggregate_level(
        2, [100.0, 120.0], 0, 1.0,
        rerank_ms=[200.0, 240.0], rerank_errors=0, rerank_pairs_per_request=50,
    )
    assert row["rerank_scores_total"] == 100  # 2 successful calls * 50 pairs/call
    assert row["rerank_pairs_per_s"] > 0
    assert row["rerank_latency_p50_ms"] == 200.0
    assert row["rerank_latency_p95_ms"] == 240.0
    assert row["errors"] == 0


def test_aggregate_level_without_rerank_omits_keys():
    row = edc.aggregate_level(1, [100.0], 0, 1.0)
    assert not any(k.startswith("rerank") for k in row)


def test_aggregate_level_rerank_errors_fold_into_errors_not_bundles_total():
    """Rerank errors count into the level's existing `errors` field (so the
    binding errors_zero gate covers rerank with no checker change) but must
    NOT pollute bundles_total/bundles_per_s — those stay pure bundle-phase
    metrics per the bar-preserving design."""
    row = edc.aggregate_level(
        1, [100.0], 2, 1.0,
        rerank_ms=[200.0], rerank_errors=3, rerank_pairs_per_request=10,
    )
    assert row["errors"] == 5  # 2 bundle errors + 3 rerank errors
    assert row["bundles_total"] == 3  # 1 success + 2 bundle errors only
    assert row["bundles_per_s"] == 3.0


def test_client_text_is_deterministic_and_distinct():
    a = edc._client_text(0, 0)
    b = edc._client_text(0, 0)
    c = edc._client_text(1, 0)
    d = edc._client_text(0, 1)
    assert a == b  # deterministic
    assert a != c  # distinct per client
    assert a != d  # distinct per request
    assert len(a) <= 200
