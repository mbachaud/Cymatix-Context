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


# --- factual ---


def test_factual_short_wh_query_fires():
    r = classify_query("What port does helix use?")
    assert r.cls == "factual"
    assert r.assembly_max_genes_cap == 5
    assert r.decoder_mode == "condensed"


def test_factual_long_wh_query_does_not_fire():
    # 16+ words — over the < 15 word threshold; must fall through.
    long_q = (
        "What is the precise mechanism by which the helix promoter index "
        "interacts with the synonym map and the co-activation graph during retrieval?"
    )
    assert len(long_q.split()) >= 16
    r = classify_query(long_q)
    assert r.cls != "factual"


def test_factual_at_14_words_fires():
    q = "What does the helix promoter index do during retrieval for very small simple queries?"
    assert len(q.split()) == 14
    r = classify_query(q)
    assert r.cls == "factual"


def test_factual_no_wh_word_does_not_fire():
    # Short, but no leading wh-word.
    r = classify_query("Helix port number.")
    assert r.cls != "factual"


# --- procedural ---


def test_procedural_how_to_fires():
    r = classify_query("How do I configure the ribosome timeout?")
    assert r.cls == "procedural"
    assert r.assembly_max_genes_cap == 6
    assert r.decoder_mode == "full"


def test_procedural_steps_keyword_fires():
    r = classify_query("Walk me through the ingest steps.")
    assert r.cls == "procedural"


# --- multi_hop ---


def test_multi_hop_connective_fires():
    r = classify_query("Compare the cold tier and the hot tier.")
    assert r.cls == "multi_hop"
    assert r.assembly_max_genes_cap == 8
    assert r.decoder_mode == "full"


def test_multi_hop_long_query_fires():
    # > 25 words, no other markers — length alone qualifies.
    q = " ".join(["token"] * 26)
    r = classify_query(q)
    assert r.cls == "multi_hop"


def test_multi_hop_and_then_connective():
    r = classify_query("Run ingest and then verify the gene count.")
    assert r.cls == "multi_hop"
