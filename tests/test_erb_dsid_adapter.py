"""Tests for the ERB dsid adapter (scripts/bench_chain/erb_dsid_adapter.py).

Covers:
  * the output row SHAPE and that it drops into the existing
    ``load_document_ids_hook`` in erb_official_answers_export.py unchanged;
  * a worked-example trace against a VERIFIED real triple
    (gene -> source_id -> dsid) that doubles as executable documentation.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.bench_chain.erb500k_scored import _rel_after_sources
from scripts.bench_chain.erb_dsid_adapter import (
    build_inverted_index,
    source_id_to_dsid,
    transform_rows,
)
from scripts.bench_chain.erb_official_answers_export import load_document_ids_hook


def _write_uuid_index(tmp: Path, forward: dict[str, str]) -> Path:
    """Write a {dsid: rel} stub as ERB's uuid_index.json shape."""
    p = tmp / "uuid_index.json"
    p.write_text(json.dumps(forward), encoding="utf-8")
    return p


# ── Test 3: adapter shape + hook compatibility ───────────────────────────

def test_transform_row_shape_and_hook_roundtrip(tmp_path):
    # 3-entry uuid_index stub: {dsid: rel}
    forward = {
        "dsid_aaa": "confluence/team-a/design.json",
        "dsid_bbb": "confluence/team-b/runbook.json",
        "dsid_ccc": "github/repo/readme.json",
    }
    uuid_index_path = _write_uuid_index(tmp_path, forward)
    inverted = build_inverted_index(uuid_index_path)
    assert inverted == {
        "confluence/team-a/design.json": "dsid_aaa",
        "confluence/team-b/runbook.json": "dsid_bbb",
        "github/repo/readme.json": "dsid_ccc",
    }

    # 1-question delivered map -> two gene_ids, one resolvable + one dupe path.
    delivered_rows = [{"question_id": "q1", "gene_ids": ["g_design", "g_runbook"]}]
    gene_source_map = {
        "g_design": "/corpus/generated_data/sources/confluence/team-a/design.json",
        "g_runbook": "/corpus/generated_data/sources/confluence/team-b/runbook.json",
    }

    out_rows, stats = transform_rows(delivered_rows, gene_source_map, inverted)
    assert out_rows == [{"question_id": "q1", "document_ids": ["dsid_aaa", "dsid_bbb"]}]
    assert stats["gene_ids_total"] == 2
    assert stats["gene_ids_resolved"] == 2
    assert stats["gene_ids_unresolved"] == 0

    # The emitted JSONL must drop into the EXISTING export hook unchanged.
    out_path = tmp_path / "docids.jsonl"
    with open(out_path, "w", encoding="utf-8") as fh:
        for row in out_rows:
            fh.write(json.dumps(row) + "\n")

    merged = load_document_ids_hook(out_path)
    assert merged == {"q1": ["dsid_aaa", "dsid_bbb"]}


def test_dedup_preserves_delivery_order():
    inverted = {"a/x.json": "dsid_x", "b/x.json": "dsid_x"}  # two rels -> same dsid
    rows = [{"question_id": "q", "gene_ids": ["g1", "g2", "g3"]}]
    gene_source_map = {
        "g1": "/s/sources/a/x.json",
        "g2": "/s/sources/b/x.json",  # resolves to same dsid_x -> deduped
        "g3": "/s/sources/a/x.json",
    }
    out_rows, stats = transform_rows(rows, gene_source_map, inverted)
    assert out_rows[0]["document_ids"] == ["dsid_x"]
    assert stats["gene_ids_resolved"] == 3  # all three resolved; dedup at emit


def test_unresolved_source_id_is_dropped_and_counted():
    inverted = {"a/x.json": "dsid_x"}
    rows = [{"question_id": "q", "gene_ids": ["hit", "miss", "null"]}]
    gene_source_map = {
        "hit": "/s/sources/a/x.json",
        "miss": "/s/sources/z/unknown.json",
        "null": None,
    }
    out_rows, stats = transform_rows(rows, gene_source_map, inverted)
    assert out_rows[0]["document_ids"] == ["dsid_x"]
    assert stats["gene_ids_total"] == 3
    assert stats["gene_ids_resolved"] == 1
    assert stats["gene_ids_unresolved"] == 2


def test_first_component_strip_retry():
    """On a primary miss, retry once with the first path component stripped
    (mirrors erb500k_scored.make_gold_index:152-154)."""
    inverted = {"applied-ml/eval-harness/x.json": "dsid_strip"}
    # Delivered rel has an extra leading component vs the index key.
    sid = "/corpus/sources/confluence/applied-ml/eval-harness/x.json"
    # primary rel = confluence/applied-ml/eval-harness/x.json (miss)
    # stripped    = applied-ml/eval-harness/x.json (hit)
    assert source_id_to_dsid(sid, inverted) == "dsid_strip"


# ── Test 4: worked-example trace (VERIFIED real triple) ──────────────────

def test_worked_example_real_triple():
    """gene 0ce4a7bd3b68aaa1
       -> source_id .../sources/confluence/applied-ml-and-evals/eval-harness/
                     adaptive-eval-mixer-scheduling-2026.json
       -> dsid_57f5a3b6e4424f7cac7b7ee37baa1750

    Executable documentation of the resolution chain, independent of the blob.
    """
    source_id = (
        "/data/erb/generated_data/sources/confluence/applied-ml-and-evals/"
        "eval-harness/adaptive-eval-mixer-scheduling-2026.json"
    )
    expected_rel = (
        "confluence/applied-ml-and-evals/eval-harness/"
        "adaptive-eval-mixer-scheduling-2026.json"
    )
    expected_dsid = "dsid_57f5a3b6e4424f7cac7b7ee37baa1750"

    # 1. source_id -> rel via the reused normalizer.
    assert _rel_after_sources(source_id) == expected_rel

    # 2. rel -> dsid via the inverted index (stub with the real mapping).
    inverted = {expected_rel: expected_dsid}
    assert source_id_to_dsid(source_id, inverted) == expected_dsid
