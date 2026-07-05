"""Tests for the SNOW oracle consumer — string matching per data tier."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure benchmarks package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.snow.oracle import oracle_cascade


def _make_fp(**kw):
    """Build a fingerprint dict with defaults."""
    return {
        "entities": kw.get("entities", []),
        "key_values": kw.get("key_values", "{}"),
        "complement": kw.get("complement", ""),
        "content": kw.get("content", ""),
    }


# ── cascade surfaces: entities / key_values / complement / content /
#    neighbor walk, plus the terminal MISS ──────────────────────────

@pytest.mark.parametrize(
    (
        "fingerprints",
        "gene_ids",
        "neighbors",
        "expected_answer",
        "accept",
        "expected_tier",
        "expected_gene_id",
    ),
    [
        pytest.param(
            {"g1": _make_fp(entities=["port", "11437", "helix"])},
            ["g1"],
            {},
            "11437",
            ["11437"],
            0,
            "g1",
            id="entities",
        ),
        pytest.param(
            {"g1": _make_fp(entities=["port", "server"], key_values='{"port": "11437"}')},
            ["g1"],
            {},
            "11437",
            ["11437"],
            1,
            "g1",
            id="key_values",
        ),
        pytest.param(
            {"g1": _make_fp(complement="Use Decimal type for monetary values")},
            ["g1"],
            {},
            "Decimal",
            ["decimal", "Decimal"],
            2,
            "g1",
            id="complement",
        ),
        pytest.param(
            {"g1": _make_fp(content="creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)")},
            ["g1"],
            {},
            "CREATE_NO_WINDOW",
            ["CREATE_NO_WINDOW"],
            3,
            "g1",
            id="content",
        ),
        pytest.param(
            {
                "g1": _make_fp(),  # empty — forces walk
                "nb1": _make_fp(content="timeout is set to 30 seconds"),
            },
            ["g1"],
            {"g1": [("nb1", 0.9)]},
            "30",
            ["30"],
            4,
            "nb1",
            id="neighbor",
        ),
        pytest.param(
            {
                "g1": _make_fp(
                    entities=["alpha"],
                    key_values='{"k": "v"}',
                    complement="some text",
                    content="more text",
                )
            },
            ["g1"],
            {},
            "nonexistent_value",
            ["nonexistent_value"],
            -1,
            None,
            id="miss",
        ),
    ],
)
def test_oracle_cascade(
    fingerprints, gene_ids, neighbors, expected_answer, accept, expected_tier, expected_gene_id
):
    """Cascade surfaces (entities, key_values, complement, content, neighbor
    walk) each answer at their own tier; an answer present nowhere falls
    through to the terminal MISS (tier -1, gene_id None)."""
    result = oracle_cascade(
        expected_answer=expected_answer,
        accept=accept,
        gene_ids=gene_ids,
        fingerprints=fingerprints,
        neighbors=neighbors,
    )
    assert result["tier"] == expected_tier
    assert result["gene_id"] == expected_gene_id
    assert result["tokens"] > 0
