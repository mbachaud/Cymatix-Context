"""Unit tests for helix_context.query_classifier."""

from helix_context.query_classifier import (
    ClassifierResult,
    classify_query,
)


def test_default_class_for_arbitrary_query():
    result = classify_query("Hello world.")
    assert isinstance(result, ClassifierResult)
    assert result.cls == "default"
    assert result.signals_matched == []
    assert result.signal_count == 0
    assert result.assembly_max_genes_cap is None
    assert result.decoder_mode is None
    assert result.reason is None


def test_empty_query_returns_default():
    assert classify_query("").cls == "default"
    assert classify_query(None).cls == "default"  # type: ignore[arg-type]


# --- arithmetic ---


def test_arithmetic_two_keywords_fires():
    r = classify_query("Calculate the total cost of the migration.")
    assert r.cls == "arithmetic"
    assert r.assembly_max_genes_cap == 2
    assert r.decoder_mode == "minimal"
    assert r.signal_count >= 2
    assert r.threshold_required == 2


def test_arithmetic_operator_plus_numeric_keyword_fires():
    # 1 strong: operator (`+`) + numeric keyword ("total") fires on the
    # strong-pair shortcut.
    r = classify_query("What is 5 + the total?")
    assert r.cls == "arithmetic"


def test_arithmetic_single_weak_signal_falls_through():
    # Single stray `%` in an otherwise factual query must NOT fire.
    r = classify_query("What is the cache hit rate at 95%?")
    assert r.cls != "arithmetic"


def test_arithmetic_critical_path_phrase_counts_as_one_keyword():
    # "critical path" is a single multi-word keyword. Alone it should NOT fire.
    r = classify_query("Tell me about the critical path.")
    assert r.cls != "arithmetic"


def test_arithmetic_critical_path_plus_calculate_fires():
    r = classify_query("Calculate the critical path.")
    assert r.cls == "arithmetic"
