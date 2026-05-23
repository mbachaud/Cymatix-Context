"""Tests for bench_enterprise_rag path normalization.

The module-level ``_rel_after_sources`` is used by the recall sweep
(``bench_enterprise_rag_recall.py``) to match a /fingerprint ``source`` against
the gold paths. It historically returned ``None`` for any path lacking a
``sources/`` marker, which silently zeroed matches for docs whose stored
source_id is already relative (``linear/...``, ``slack/...``, ``eng-sre/...``).
A gold doc retrieved as ``linear/design/X.json`` would then read as a MISS,
contaminating any recall-failure taxonomy built on the sweep. These tests pin
the pass-through-not-None contract so gold and a bare-relative source for the
same doc canonicalize equal.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))
from bench_enterprise_rag import _rel_after_sources


def test_strips_absolute_sources_prefix():
    assert _rel_after_sources(
        r"F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\linear\design\DES-91235.json"
    ) == "linear/design/DES-91235.json"


def test_strips_leading_sources():
    assert _rel_after_sources("sources/github/pr-18421.json") == "github/pr-18421.json"


def test_passes_through_bare_relative():
    # The bug: a doc retrieved as "linear/..." (no sources/ prefix) returned
    # None and never matched its gold. Must pass through unchanged.
    assert _rel_after_sources("linear/design/DES-91235.json") == "linear/design/DES-91235.json"


def test_gold_and_bare_fingerprint_source_match():
    gold = _rel_after_sources(
        r"F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\linear\design\DES-91235.json"
    )
    fp_source = _rel_after_sources("linear/design/DES-91235.json")
    assert gold == fp_source
    assert gold is not None


def test_mixed_prefix_doc_types_all_resolve():
    # github stores with prefix, linear/slack/eng-sre often without; both forms
    # of each must reduce to the same rel key.
    for bare in ["linear/design/X.json", "slack/announcements/Y.json",
                 "eng-sre/incident-review/Z.json"]:
        absolute = "F:/Projects/corpus/generated_data/sources/" + bare
        assert _rel_after_sources(bare) == _rel_after_sources(absolute) == bare
