"""BEIR-standard scoring over ladder ``--rank-dump`` receipts (issue #482)."""

import importlib.util
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "beir_ndcg", ROOT / "benchmarks" / "dogfood" / "beir" / "beir_ndcg.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


beir = _load()


def test_ndcg_uses_linear_gains_like_trec_eval():
    # ranked grades [2, 0, 1], qrels {2, 1}: DCG = 2/log2(2) + 1/log2(4)
    dcg = 2 / math.log2(2) + 1 / math.log2(4)
    idcg = 2 / math.log2(2) + 1 / math.log2(3)
    assert math.isclose(beir.ndcg_at_k([2, 0, 1], [2, 1], 10), dcg / idcg)


def test_ndcg_ideal_counts_gold_never_retrieved():
    # one of two binary golds retrieved at rank 1: idcg covers both
    expect = 1.0 / (1 + 1 / math.log2(3))
    assert math.isclose(beir.ndcg_at_k([1], [1, 1], 10), expect)


def test_ndcg_cuts_at_k_and_zero_when_no_gold():
    assert beir.ndcg_at_k([0] * 10 + [1], [1], 10) == 0.0
    assert beir.ndcg_at_k([1], [], 10) == 0.0


def test_collapse_dedups_genes_of_one_document_keeping_first_position():
    group_of = {"g1": "A", "g2": "B", "g3": "A", "g4": "C"}
    assert beir.collapse_to_docs(["g1", "g2", "g3", "g4"], group_of, exclude=set()) == ["A", "B", "C"]


def test_collapse_drops_self_document_and_unknown_genes():
    group_of = {"g1": "SELF", "g2": "B", "g4": "C"}
    out = beir.collapse_to_docs(["g1", "g2", "gX", "g4"], group_of, exclude={"SELF"})
    assert out == ["B", "C"]


def test_grades_for_docs_takes_max_grade_within_a_twin_group():
    members = {"G1": {"a.txt", "b.txt"}, "G2": {"c.txt"}}
    qrels = {"a.txt": 1, "b.txt": 2}
    assert beir.grades_for_docs(["G1", "G2"], members, qrels) == [2, 0]


def test_recall_at_k_counts_distinct_relevant_documents():
    members = {"G1": {"a.txt", "b.txt"}, "G2": {"c.txt"}}
    qrels = {"a.txt": 1, "b.txt": 1, "c.txt": 1, "d.txt": 1}
    # G1 retrieves two relevant paths (byte twins), G2 one; 3 of 4 relevant
    assert math.isclose(beir.recall_at_k(["G1", "G2"], members, qrels, 10), 3 / 4)
    assert math.isclose(beir.recall_at_k(["G1", "G2"], members, qrels, 1), 2 / 4)
