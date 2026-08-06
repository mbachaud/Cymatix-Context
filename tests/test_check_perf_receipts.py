"""Perf regression gate (2026-08 campaign, A.1 item 9).

Synthetic receipts exercise check_perf_receipts.py: the ratio gates for
all three kinds, graceful skip on missing optional fields (older-harness
receipts), and the 0/1/2 exit-code contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make benchmarks/ and benchmarks/dogfood importable as flat module paths
# (repo test convention — see tests/test_perf_instrumentation.py).
_BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
for _p in (_BENCH, _BENCH / "dogfood"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import check_perf_receipts as cpr


# ── synthetic receipt builders ───────────────────────────────────────


def make_server_scaling(median=1000.0, recall=0.55, canary_p95=30.0,
                        qps=1.5, valid=True, concurrencies=(1, 2)):
    return {
        "measured_at": "2026-08-03T00:00:00-0700",
        "levels": [
            {
                "concurrency": c,
                "median_latency_ms": median * c,
                "recall@12": recall,
                "canary": {"p95_ms": canary_p95},
                "throughput_qps": qps,
                "valid": valid,
                "invalid_reasons": [] if valid else ["overlap_fraction below floor"],
            }
            for c in concurrencies
        ],
    }


def make_cold_start(first_median=14000.0, fraction=0.9, verdict="PASS"):
    return {
        "rollup": {
            "first_context_ms_median": first_median,
            "model_load_fraction_of_first": fraction,
        },
        "verdict": verdict,
    }


def make_write_contention(median_x=1.2, p95_x=1.25, busy=0, verdict="PASS"):
    return {
        "control_arm": "a",
        "arms": [
            {
                "arm": "a",
                "read_latency": {"median_latency_ms": 2600.0, "p95_latency_ms": 3100.0},
                "vs_control": None,
                "busy_error_count": 0,
                "verdict": "PASS",
            },
            {
                "arm": "b",
                "read_latency": {
                    "median_latency_ms": 2600.0 * median_x,
                    "p95_latency_ms": 3100.0 * p95_x,
                },
                "vs_control": {"median_latency_x": median_x, "p95_latency_x": p95_x},
                "busy_error_count": busy,
                "verdict": verdict,
            },
        ],
    }


def write_json(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def run_gate(tmp_path, kind, baseline, fresh, out_name="verdict.json"):
    b = write_json(tmp_path, f"{kind}_b.json", baseline)
    f = write_json(tmp_path, f"{kind}_f.json", fresh)
    out = tmp_path / out_name
    rc = cpr.main(["--kind", kind, "--fresh", str(f),
                   "--baseline", str(b), "--out", str(out)])
    return rc, json.loads(out.read_text(encoding="utf-8"))


def gates_named(result, name):
    return [g for g in result["gates"] if g["name"] == name]


# ── pass case ────────────────────────────────────────────────────────


def test_server_scaling_self_compare_passes(tmp_path):
    receipt = make_server_scaling()
    rc, result = run_gate(tmp_path, "server_scaling", receipt, receipt)
    assert rc == 0
    assert result["verdict"] == "PASS"
    # every ratio gate must sit at exactly 1.0 on a self-compare
    for g in result["gates"]:
        if g["metric"] == "ratio":
            assert g["ratio"] == 1.0
        assert g["pass"] is True
    # all five gate families ran per level
    for name in ("median_latency_ratio", "recall_delta", "canary_p95_ratio",
                 "valid", "throughput_qps_ratio"):
        assert len(gates_named(result, name)) == 2, name


def test_cold_start_and_write_contention_pass(tmp_path):
    rc, result = run_gate(tmp_path, "cold_start",
                          make_cold_start(), make_cold_start())
    assert rc == 0 and result["verdict"] == "PASS"
    rc, result = run_gate(tmp_path, "write_contention",
                          make_write_contention(), make_write_contention())
    assert rc == 0 and result["verdict"] == "PASS"
    assert gates_named(result, "arm_verdict")


# ── regression fails ─────────────────────────────────────────────────


def test_median_regression_fails(tmp_path):
    baseline = make_server_scaling(median=1000.0)
    fresh = make_server_scaling(median=1200.0)  # 1.2x > 1.15 ceiling
    rc, result = run_gate(tmp_path, "server_scaling", baseline, fresh)
    assert rc == 1
    assert result["verdict"] == "FAIL"
    bad = gates_named(result, "median_latency_ratio")
    assert bad and all(g["pass"] is False for g in bad)
    assert bad[0]["ratio"] == 1.2


def test_recall_drift_fails(tmp_path):
    baseline = make_server_scaling(recall=0.5556)
    fresh = make_server_scaling(recall=0.50)  # drift 0.0556 > 0.02
    rc, result = run_gate(tmp_path, "server_scaling", baseline, fresh)
    assert rc == 1
    bad = gates_named(result, "recall_delta")
    assert bad and all(g["pass"] is False for g in bad)


def test_invalid_level_fails(tmp_path):
    baseline = make_server_scaling(valid=True)
    fresh = make_server_scaling(valid=False)
    rc, result = run_gate(tmp_path, "server_scaling", baseline, fresh)
    assert rc == 1
    bad = gates_named(result, "valid")
    assert bad and all(g["pass"] is False for g in bad)


def test_cold_start_first_context_regression_fails(tmp_path):
    rc, result = run_gate(tmp_path, "cold_start",
                          make_cold_start(first_median=10000.0),
                          make_cold_start(first_median=13000.0))  # 1.3 > 1.25
    assert rc == 1
    assert gates_named(result, "first_context_ms_median_ratio")[0]["pass"] is False


def test_write_contention_vs_control_and_busy_fail(tmp_path):
    baseline = make_write_contention(median_x=1.2, busy=0)
    fresh = make_write_contention(median_x=1.8, busy=9)  # 1.5x growth; 9 > 3*0+2
    rc, result = run_gate(tmp_path, "write_contention", baseline, fresh)
    assert rc == 1
    assert gates_named(result, "vs_control_median_ratio")[0]["pass"] is False
    busy_b = [g for g in gates_named(result, "busy_error_count")
              if g["level_or_arm"] == "arm-b"]
    assert busy_b and busy_b[0]["pass"] is False


def test_write_contention_arm_verdict_flip_fails(tmp_path):
    rc, result = run_gate(tmp_path, "write_contention",
                          make_write_contention(),
                          make_write_contention(verdict="FAIL"))
    assert rc == 1
    flipped = [g for g in gates_named(result, "arm_verdict")
               if g["level_or_arm"] == "arm-b"]
    assert flipped and flipped[0]["pass"] is False


# ── graceful degradation (older-harness receipts) ────────────────────


def test_missing_optional_fields_skip_not_crash(tmp_path):
    baseline = make_server_scaling()
    fresh = make_server_scaling()
    for lv in fresh["levels"]:
        del lv["canary"]          # older harness: no canary sampler
        del lv["recall@12"]       # no recall column
    rc, result = run_gate(tmp_path, "server_scaling", baseline, fresh)
    assert rc == 0                # remaining gates all pass
    assert result["verdict"] == "PASS"
    assert not gates_named(result, "canary_p95_ratio")
    assert not gates_named(result, "recall_delta")
    assert any("canary_p95_ratio" in n for n in result["notes"])
    assert any("recall_delta" in n for n in result["notes"])


def test_cold_start_missing_rollup_skips(tmp_path):
    fresh = make_cold_start()
    del fresh["rollup"]
    rc, result = run_gate(tmp_path, "cold_start", make_cold_start(), fresh)
    assert rc == 0
    assert not gates_named(result, "first_context_ms_median_ratio")
    assert any("rollup" in n for n in result["notes"])


def test_write_contention_derives_vs_control_when_not_stored(tmp_path):
    baseline = make_write_contention(median_x=1.2, p95_x=1.25)
    fresh = make_write_contention(median_x=1.2, p95_x=1.25)
    for arm in fresh["arms"]:
        arm["vs_control"] = None  # older harness: no stored multipliers
    rc, result = run_gate(tmp_path, "write_contention", baseline, fresh)
    assert rc == 0
    derived = gates_named(result, "vs_control_median_ratio")
    assert derived and derived[0]["ratio"] == 1.0


# ── exit-code contract ───────────────────────────────────────────────


def test_exit_2_on_missing_file(tmp_path):
    rc = cpr.main(["--kind", "cold_start",
                   "--fresh", str(tmp_path / "nope.json"),
                   "--baseline", str(tmp_path / "also_nope.json")])
    assert rc == 2


def test_exit_2_on_malformed_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    good = write_json(tmp_path, "good.json", make_cold_start())
    rc = cpr.main(["--kind", "cold_start", "--fresh", str(bad),
                   "--baseline", str(good)])
    assert rc == 2


def test_exit_2_on_wrong_shape(tmp_path):
    empty = write_json(tmp_path, "empty.json", {"levels": []})
    good = write_json(tmp_path, "good.json", make_server_scaling())
    rc = cpr.main(["--kind", "server_scaling", "--fresh", str(empty),
                   "--baseline", str(good)])
    assert rc == 2


def test_exit_2_on_missing_args():
    assert cpr.main(["--kind", "server_scaling"]) == 2


# ── --ab mode ────────────────────────────────────────────────────────


def test_ab_mode_compares_dirs(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    write_json(dir_a, "server_scaling.json", make_server_scaling(median=1000.0))
    write_json(dir_b, "server_scaling.json", make_server_scaling(median=1300.0))
    write_json(dir_a, "cold_start.json", make_cold_start())
    write_json(dir_b, "cold_start.json", make_cold_start())
    out = tmp_path / "ab.json"
    rc = cpr.main(["--ab", str(dir_a), str(dir_b), "--out", str(out)])
    assert rc == 1  # server_scaling median 1.3x regression fails the set
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["kind"] == "ab"
    assert result["verdict"] == "FAIL"
    by_kind = {r["kind"]: r for r in result["results"]}
    assert by_kind["server_scaling"]["verdict"] == "FAIL"
    assert by_kind["cold_start"]["verdict"] == "PASS"
    # write_contention absent from both dirs: silently not compared
    assert "write_contention" not in by_kind


def test_ab_mode_exit_2_when_nothing_shared(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    write_json(dir_a, "cold_start.json", make_cold_start())
    rc = cpr.main(["--ab", str(dir_a), str(dir_b)])
    assert rc == 2
