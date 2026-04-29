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
