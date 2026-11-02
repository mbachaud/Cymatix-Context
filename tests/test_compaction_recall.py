"""Tests for the compaction-recall bench (benchmarks/compaction_recall).

The bench mines real Claude Code transcripts for compaction events and asks,
per detail the agent needed again after compaction, whether each arm would
have put that detail in front of it. These tests pin the parts a wrong
answer would hide in: event reconstruction across rewinds, needle
extraction, query masking, and the scoring predicates.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.compaction_recall import arms, needles, transcript


SHA = "3f9dd86a1c"
WIN_PATH = "F:\\Projects\\demo\\cymatix_context\\foo_bar.py"


def _msg(uuid, parent, role, content, **extra):
    d = {"uuid": uuid, "parentUuid": parent, "type": role, "isSidechain": False,
         "message": {"role": role, "content": content}}
    d.update(extra)
    return d


def _fixture_lines():
    """One compaction, one rewound branch, one preserved message, attachments."""
    return [
        _msg("u1", None, "user", "please fix the ranking bug in foo_bar.py"),
        _msg("a1", "u1", "assistant", [{"type": "text", "text":
             f"Commit {SHA} is pending verification; the score was 0.668 on "
             "the bed. Next touch min_delivered_docs_floor."}]),
        _msg("a2", "a1", "assistant", [{"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": WIN_PATH}}]),
        _msg("r1", "a2", "user", [{"type": "tool_result", "tool_use_id": "t1",
             "content": "def rank():\n    return 0.4321\n"}]),
        # rewound branch: child of a1 that the main chain does not pass through
        _msg("x1", "a1", "assistant", [{"type": "text", "text": "ABANDONED 9e8d7c6b5a"}]),
        _msg("a3", "r1", "assistant", [{"type": "thinking", "thinking": "secret 0.9999"},
                                       {"type": "text", "text": "Looks fine."}]),
        {"uuid": "b1", "parentUuid": None, "logicalParentUuid": "a3", "type": "system",
         "subtype": "compact_boundary", "isSidechain": False,
         "timestamp": "2026-09-01T00:00:00Z",
         "compactMetadata": {"trigger": "auto", "preTokens": 1000, "postTokens": 100,
                             "preservedMessages": {"allUuids": ["a3"]}}},
        _msg("s1", "b1", "user", "Summary: worked on the ranking bug.", isCompactSummary=True),
        {"uuid": "at1", "parentUuid": "s1", "type": "attachment", "isSidechain": False,
         "attachment": {"type": "file", "filename": WIN_PATH,
                        "content": {"type": "text", "file": {"filePath": WIN_PATH,
                                                             "content": "def rank(): ..."}}}},
        {"uuid": "at2", "parentUuid": "at1", "type": "attachment", "isSidechain": False,
         "attachment": {"type": "plan_file_reference", "planFilePath": "plan.md",
                        "planContent": "PLAN: verify the floor knob"}},
        _msg("a4", "at2", "assistant", [{"type": "text", "text":
             f"Re-checking {SHA[:7]} and min_delivered_docs_floor now."}]),
        _msg("a5", "a4", "assistant", [{"type": "tool_use", "id": "t2", "name": "Bash",
             "input": {"command": f"git show {SHA}"}}]),
    ]


@pytest.fixture
def fixture_file(tmp_path: Path) -> Path:
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(d) for d in _fixture_lines()) + "\n", encoding="utf-8")
    return p


# ── transcript reconstruction ──────────────────────────────────────────────

def test_event_reconstruction_follows_the_parent_chain(fixture_file):
    events = transcript.load_events([fixture_file])
    assert len(events) == 1
    ev = events[0]
    assert ev.boundary_uuid == "b1"
    assert ev.trigger == "auto"
    pre_text = "\n".join(u.text for u in ev.pre)
    assert SHA in pre_text
    assert "ABANDONED" not in pre_text          # rewound branch excluded
    assert "secret" not in pre_text             # thinking blocks excluded
    assert ev.summary.startswith("Summary:")
    assert [u.uuid for u in ev.preserved] == ["a3"]
    att_text = "\n".join(u.text for u in ev.post_attachments)
    assert "def rank(): ..." in att_text
    assert ev.plan_text == "PLAN: verify the floor knob"
    assert [u.role for u in ev.post] == ["assistant", "tool_use"]


def test_post_window_continues_from_the_preserved_tail(tmp_path):
    """Partial compaction: the post lines hang off the preserved tail, not the boundary."""
    lines = _fixture_lines()
    for d in lines:
        if d["uuid"] == "at1":
            d["parentUuid"] = "a3"
    lines[6]["compactMetadata"]["preservedSegment"] = {"tailUuid": "a3"}
    p = tmp_path / "partial.jsonl"
    p.write_text("\n".join(json.dumps(d) for d in lines) + "\n", encoding="utf-8")
    ev = transcript.load_events([p])[0]
    assert ev.summary.startswith("Summary:")
    assert ev.plan_text == "PLAN: verify the floor knob"
    assert [u.role for u in ev.post] == ["assistant", "tool_use"]


def test_duplicate_boundaries_across_files_are_deduped(fixture_file, tmp_path):
    copy = tmp_path / "resumed.jsonl"
    copy.write_text(fixture_file.read_text(encoding="utf-8"), encoding="utf-8")
    events = transcript.load_events([fixture_file, copy])
    assert len(events) == 1


def test_today_context_is_summary_preserved_and_attachments(fixture_file):
    ev = transcript.load_events([fixture_file])[0]
    today = arms.today_text(ev)
    assert "Summary:" in today and "Looks fine." in today and "PLAN:" in today


# ── needle extraction ──────────────────────────────────────────────────────

def test_token_classes():
    text = (f"commit {SHA} fixes #412 (uuid 961fa48e-6859-4db3-b3a5-6a01f44a4f78) "
            f"in {WIN_PATH}; recall 0.668 vs 829k genes, 947,000 docs; knob "
            "CYMATIX_BFM_HUB_CUTOFF and min_delivered_docs_floor, ScaleRelativeBlender. "
            "color #a1b2c3 version 1.2.3")
    found = {(c, k) for c, k in needles.find_keys(text)}
    assert ("sha", SHA[:7]) in found
    assert ("ref", "#412") in found
    assert ("path", "cymatix_context/foo_bar.py") in found
    assert ("number", "0.668") in found
    assert ("number", "829k") in found
    assert ("number", "947,000") in found
    assert ("ident", "CYMATIX_BFM_HUB_CUTOFF") in found
    assert ("ident", "min_delivered_docs_floor") in found
    assert ("ident", "ScaleRelativeBlender") in found
    keys = {k for _, k in found}
    assert "961fa48e" not in keys                # uuid fragments are not shas
    assert "a1b2c3" not in keys                  # hex colour is not a sha
    assert "1.2" not in keys and "2.3" not in keys  # version strings are not numbers


def test_generic_numbers_and_tool_vocabulary_are_not_needles():
    keys = {k for _, k in needles.find_keys(
        "w = 0.32, cap 0.10, cost $0.05, ratio 0.80; run_in_background: True; "
        "kept 0.664 and 11.3 g/s")}
    assert {"0.32", "0.10", "0.05", "0.80", "run_in_background"}.isdisjoint(keys)
    assert {"0.664", "11.3"} <= keys


def test_contains_respects_boundaries():
    assert needles.contains("score 0.668 here", "number", "0.668")
    assert not needles.contains("score 0.6681 here", "number", "0.668")
    assert not needles.contains("score 10.668 here", "number", "0.668")
    assert needles.contains(f"see {SHA.upper()}", "sha", SHA[:7])
    assert needles.contains("C:\\x\\cymatix_context\\foo_bar.py", "path",
                            "cymatix_context/foo_bar.py")
    assert not needles.contains("#4123", "ref", "#412")


def test_needles_are_details_reused_after_compaction(fixture_file):
    ev = transcript.load_events([fixture_file])[0]
    ns = needles.extract_needles(ev)
    by_key = {n.key: n for n in ns}
    assert SHA[:7] in by_key                     # used post-compaction by the agent
    assert "min_delivered_docs_floor" in by_key
    assert "0.668" not in by_key                 # never needed again -> not a needle
    assert "0.4321" not in by_key
    sha = by_key[SHA[:7]]
    assert sha.cls == "sha"
    assert sha.use_role == "assistant"


def test_qualifier_is_taken_from_the_last_pre_mention(fixture_file):
    ev = transcript.load_events([fixture_file])[0]
    by_key = {n.key: n for n in needles.extract_needles(ev)}
    assert by_key[SHA[:7]].qualifier == "pending"
    assert by_key["min_delivered_docs_floor"].qualifier is None


def test_query_masks_the_needle(fixture_file):
    ev = transcript.load_events([fixture_file])[0]
    for n in needles.extract_needles(ev):
        assert not needles.contains(n.query, n.cls, n.key), n.key
        assert "[MASK]" in n.query


def test_hedge_preserved_needs_a_qualifier_near_the_key():
    assert needles.hedge_near("PR #419 still pending review", "ref", "#419")
    assert not needles.hedge_near("PR #419 merged cleanly" + " x" * 200 + " pending",
                                  "ref", "#419")


# ── chunking and the lexical arm ───────────────────────────────────────────

def test_chunking_never_splits_a_token():
    line = " ".join(f"tok{i}_{SHA}" for i in range(400))
    chunks = arms.chunk_text(line, max_chars=300)
    assert all(len(c) <= 300 for c in chunks)
    assert sum(c.count(SHA) for c in chunks) == 400


def test_bm25_arm_delivers_the_needle_chunk():
    arm = arms.BM25Arm()
    arm.reset()
    arm.add(["the weather is mild today", f"we pinned commit {SHA} for the release tag",
             "unrelated chatter about lunch"])
    delivered = arm.query("which commit did we pin for the release tag", k=1)
    assert SHA in delivered.text
    assert delivered.n_chunks == 1
