"""Phase 2 of issue #493 -- pure eval checks over packet trace records.

No I/O, no LLM. Every check takes packet-event dicts (schema v0, see
docs/specs/packet-trace-v0.md) and returns plain values.
"""

from __future__ import annotations

import pytest

from cymatix_context.telemetry.trace_checks import (
    check_delivery,
    check_know_miss,
    classify_residue,
    cohens_kappa,
    over_delivery,
    raw_agreement,
    sample_for_judge,
)


def _packet(chunk_ids, verdict=None):
    return {
        "event": "packet",
        "packet_id": "01TESTPACKET0000000000000000",
        "chunks": [{"chunk_id": c, "doc_id": c, "rank": i + 1}
                   for i, c in enumerate(chunk_ids)],
        "verdict": verdict if verdict is not None else {"kind": "know"},
    }


# -- check_delivery -----------------------------------------------------


def test_delivery_hit_reports_first_gold_position():
    pkt = _packet(["g1", "g2", "g3", "g4"])
    assert check_delivery(pkt, {"g3", "g4"}) == {"hit": True, "rank": 3}


def test_delivery_rank_is_position_not_rank_field():
    pkt = _packet(["a", "b", "g"])
    pkt["chunks"][2]["rank"] = 99  # stored rank field must not be used
    assert check_delivery(pkt, {"g"}) == {"hit": True, "rank": 3}


def test_delivery_miss_when_gold_absent():
    pkt = _packet(["g1", "g2"])
    assert check_delivery(pkt, {"zzz"}) == {"hit": False, "rank": None}


def test_delivery_miss_on_empty_packet():
    assert check_delivery(_packet([]), {"g"}) == {"hit": False, "rank": None}


# -- check_know_miss ----------------------------------------------------


def test_miss_with_abstain_is_compliant():
    pkt = _packet([], verdict={"kind": "miss", "reason": "no_gene"})
    res = check_know_miss(pkt, {"abstained": True})
    assert res["compliant"] is True


def test_miss_with_confident_answer_is_noncompliant():
    pkt = _packet([], verdict={"kind": "miss", "reason": "no_gene"})
    res = check_know_miss(pkt, {"abstained": False})
    assert res["compliant"] is False
    assert res["reason"] == "answered_on_miss"


def test_know_verdict_is_compliant():
    pkt = _packet(["g1"], verdict={"kind": "know", "confidence": 0.8})
    assert check_know_miss(pkt, {"abstained": False})["compliant"] is True


# -- over_delivery ------------------------------------------------------


def test_over_delivery_ratio_from_referenced_chunks():
    pkt = _packet(["a", "b", "c", "d"])
    actions = [{"event": "action", "referenced_chunk_ids": ["a", "c"]}]
    assert over_delivery(pkt, actions) == pytest.approx(0.5)


def test_over_delivery_unions_across_actions():
    pkt = _packet(["a", "b", "c", "d"])
    actions = [
        {"referenced_chunk_ids": ["a"]},
        {"referenced_chunk_ids": ["b", "c", "d"]},
    ]
    assert over_delivery(pkt, actions) == 0.0


def test_over_delivery_none_without_actions():
    pkt = _packet(["a"])
    assert over_delivery(pkt, None) is None
    assert over_delivery(pkt, []) is None


# -- classify_residue ---------------------------------------------------


def _result(rid, hit, compliant):
    return {
        "id": rid,
        "delivery": {"hit": hit, "rank": 1 if hit else None},
        "know_miss": {"compliant": compliant, "reason": "x"},
    }


def test_residue_routes_clear_pass_fail_ambiguous():
    p = _result("p", True, True)
    f = _result("f", False, False)
    amb1 = _result("a1", True, False)   # delivered but answered on miss
    amb2 = _result("a2", False, True)   # missed but abstained correctly
    out = classify_residue([p, f, amb1, amb2])
    assert out["clear_pass"] == [p]
    assert out["clear_fail"] == [f]
    assert out["ambiguous"] == [amb1, amb2]


def test_residue_empty_input():
    assert classify_residue([]) == {
        "clear_pass": [], "clear_fail": [], "ambiguous": []}


# -- sample_for_judge ---------------------------------------------------


def test_sample_is_deterministic_for_seed():
    ids = [f"g{i}" for i in range(100)]
    a = sample_for_judge(ids, 0.2, seed=7)
    b = sample_for_judge(list(reversed(ids)), 0.2, seed=7)
    assert a == b
    assert len(a) == 20
    assert set(a) <= set(ids)


def test_sample_differs_across_seeds():
    ids = [f"g{i}" for i in range(100)]
    assert sample_for_judge(ids, 0.2, seed=1) != sample_for_judge(ids, 0.2, seed=2)


def test_sample_bounds():
    ids = ["a", "b", "c"]
    assert sample_for_judge(ids, 0.0, seed=0) == []
    assert sorted(sample_for_judge(ids, 1.0, seed=0)) == ids


# -- agreement ----------------------------------------------------------


def test_raw_agreement_fraction():
    assert raw_agreement(["y", "n", "y", "y"], ["y", "y", "y", "n"]) == 0.5


def test_kappa_perfect_agreement_is_one():
    a = ["y", "n", "y", "n", "n"]
    assert cohens_kappa(a, list(a)) == pytest.approx(1.0)


def test_kappa_textbook_2x2():
    # yes/yes=20, yes/no=5, no/yes=10, no/no=15 (N=50)
    a = ["y"] * 20 + ["y"] * 5 + ["n"] * 10 + ["n"] * 15
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert raw_agreement(a, b) == pytest.approx(0.7)
    assert cohens_kappa(a, b) == pytest.approx(0.4)


def test_agreement_rejects_length_mismatch():
    with pytest.raises(ValueError):
        raw_agreement(["y"], ["y", "n"])
    with pytest.raises(ValueError):
        cohens_kappa(["y"], ["y", "n"])
