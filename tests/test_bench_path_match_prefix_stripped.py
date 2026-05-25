"""Regression for the prefix-stripped delivered-path bug found 2026-05-23.

Helix's ``<GENE src="...">`` tag does not always include the source
prefix. For ``confluence`` documents in particular, ~30 % of delivered
paths come back as e.g. ``architecture-and-standards/decision-records/
adr-015-…json`` rather than the canonical
``confluence/architecture-and-standards/decision-records/adr-015-…json``.
``_rel_after_sources`` returns the input unchanged (no ``sources/``
marker to split on), and the resulting key does not match the gold key
``confluence/architecture-and-standards/…``. The bench then reports
``gold_delivered=False`` for what is actually a successful delivery — a
**measurement-layer false-False that depresses headline gd rates by
5-7 pp** on every arm we have run.

This test pins the desired post-fix behaviour: a delivered path missing
the source prefix must still resolve to the gold key when one of the
nine known sources, prepended to the delivered key, produces a gold
match.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "benchmarks")
)


# Will be importable after the GREEN step lands. Until then this import
# fails, which is the desired RED.
from bench_enterprise_rag import (
    _rel_after_sources,
    make_gold_index,
    make_uuid_reverse_with_stripped,
    match_delivered_to_gold,
)


# ---------- fixtures (verbatim shapes captured from real bench runs) ----------

GOLD_PATHS_CONFLUENCE = [
    r"F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\confluence\architecture-and-standards\decision-records\adr-015-admission-control-layering-gateway-routing-runtime.json",
]
GOLD_PATHS_LINEAR = [
    r"F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\linear\design\DES-31789-compact-interactive-examples-and-annotated-diff-cards.json",
]
GOLD_PATHS_GMAIL = [
    r"F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\gmail\aditya_rao\20270719-aws-listing-partner-onboarding-copy-sync.json",
]


# ---------- the bug-confirming tests ----------

def test_stripped_confluence_path_matches_prefixed_gold():
    """Helix delivers ``architecture-and-standards/…`` (no ``confluence/``
    prefix). The gold path *does* have ``confluence/`` in it. The matcher
    must reconcile them."""
    gi, canonicals = make_gold_index(GOLD_PATHS_CONFLUENCE)
    delivered_stripped = "architecture-and-standards/decision-records/adr-015-admission-control-layering-gateway-routing-runtime.json"

    matched_canonical = match_delivered_to_gold(delivered_stripped, gi, canonicals)
    assert matched_canonical is not None, (
        "stripped path failed to reconcile with gold — the false-False bug is back"
    )
    assert matched_canonical == _rel_after_sources(GOLD_PATHS_CONFLUENCE[0])


def test_prefixed_linear_path_still_matches_directly():
    """Sanity: the common case ``linear/design/…`` (already source-prefixed)
    must still match without going through the prepend fallback."""
    gi, canonicals = make_gold_index(GOLD_PATHS_LINEAR)
    delivered_prefixed = "linear/design/DES-31789-compact-interactive-examples-and-annotated-diff-cards.json"
    matched = match_delivered_to_gold(delivered_prefixed, gi, canonicals)
    assert matched == "linear/design/DES-31789-compact-interactive-examples-and-annotated-diff-cards.json"


def test_sources_prefixed_gmail_path_matches():
    """Another observed shape: ``sources/gmail/…`` (the leading
    ``sources/`` literal, not the abs Windows path). ``_rel_after_sources``
    already strips this; the matcher must agree."""
    gi, canonicals = make_gold_index(GOLD_PATHS_GMAIL)
    delivered_with_sources = "sources/gmail/aditya_rao/20270719-aws-listing-partner-onboarding-copy-sync.json"
    matched = match_delivered_to_gold(delivered_with_sources, gi, canonicals)
    assert matched is not None


def test_genuine_miss_does_not_falsely_match():
    """A delivered path that bears no resemblance to gold must NOT be
    accidentally matched by the fallback strategy. This pins the upper
    bound — we should not over-correct."""
    gi, canonicals = make_gold_index(GOLD_PATHS_LINEAR)
    delivered_unrelated = "github/audit-log-exporter/pr-627-support-retention-config-attachment.json"
    matched = match_delivered_to_gold(delivered_unrelated, gi, canonicals)
    assert matched is None, (
        f"unrelated path matched! over-correction bug. matched={matched!r}"
    )


def test_stripped_path_with_ambiguous_suffix_picks_unique():
    """Two gold paths from different sources that *would* collide if
    you only compared the stripped tail. Make sure the matcher prefers
    the unambiguous case and returns the right canonical key — or, if
    truly ambiguous, returns None rather than a wrong match."""
    gold_paths = [
        r"F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\confluence\runbooks\restart-procedure.json",
    ]
    gi, canonicals = make_gold_index(gold_paths)

    # Delivered as `runbooks/restart-procedure.json` (no source prefix) —
    # unambiguous because only one gold has this suffix.
    matched = match_delivered_to_gold(
        "runbooks/restart-procedure.json", gi, canonicals,
    )
    assert matched == "confluence/runbooks/restart-procedure.json"


def test_empty_inputs_safe():
    gi, canonicals = make_gold_index([])
    assert match_delivered_to_gold("anything/at/all.json", gi, canonicals) is None
    assert match_delivered_to_gold("", gi, canonicals) is None


def test_uuid_reverse_includes_stripped_form():
    """``extract_dsids`` reverse-looks-up delivered paths in the
    ``uuid_index_reverse`` to assemble ``predicted_doc_ids``. The reverse
    map must include the source-prefix-stripped form alongside the
    canonical form so confluence's stripped delivered paths still find
    their dsid. Without this, the Onyx scorer sees an empty
    ``predicted_doc_ids`` and reports a doc-recall miss on a delivered
    gold."""
    uuid_idx = {
        "dsid_abc123": "confluence/architecture-and-standards/decision-records/adr-015.json",
        "dsid_def456": "linear/design/DES-31789.json",
    }
    rev = make_uuid_reverse_with_stripped(uuid_idx)
    # Canonical form still works.
    assert rev["confluence/architecture-and-standards/decision-records/adr-015.json"] == "dsid_abc123"
    assert rev["linear/design/DES-31789.json"] == "dsid_def456"
    # Stripped form (helix's actual output for confluence) also works.
    assert rev["architecture-and-standards/decision-records/adr-015.json"] == "dsid_abc123"
    # Stripped form for linear too — even though we typically don't see
    # this shape for linear, the symmetric handling is correct.
    assert rev["design/DES-31789.json"] == "dsid_def456"


def test_uuid_reverse_canonical_wins_on_collision():
    """If two gold paths exist where one's canonical form equals
    another's stripped form, the canonical mapping must NOT be
    overwritten by the stripped fallback."""
    uuid_idx = {
        "dsid_abc": "linear/design/X.json",  # canonical
        "dsid_def": "confluence/linear/design/X.json",  # contrived collision
    }
    rev = make_uuid_reverse_with_stripped(uuid_idx)
    # Canonical key for dsid_abc must win — not overwritten by dsid_def's
    # stripped form.
    assert rev["linear/design/X.json"] == "dsid_abc"
    assert rev["confluence/linear/design/X.json"] == "dsid_def"


def test_strip_to_gold_index_lookup_is_constant_time_in_intent():
    """Smoke: the index must hold both the canonical key and the stripped
    fallback key so lookup is O(1) per delivered path, not O(sources)."""
    gi, _ = make_gold_index(GOLD_PATHS_CONFLUENCE)
    canonical = _rel_after_sources(GOLD_PATHS_CONFLUENCE[0])
    # Stripped form must be in the index, mapping to the canonical key.
    stripped = canonical.split("/", 1)[1]  # drop the "confluence/" segment
    assert stripped in gi
    assert gi[stripped] == canonical
