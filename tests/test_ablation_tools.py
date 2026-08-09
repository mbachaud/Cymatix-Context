"""Unit tests for the ERB ablation/storage bench helpers.

Scope is deliberately narrow: the PURE helpers only — rank extraction, arm
identity/validation logic, and the storage layer mapping. No model loads, no
beds, no manager construction. The storage mapping test builds a tiny
in-memory SQLite database whose object NAMES mirror the real carve schema,
which is all ``classify_object`` and the estimator need.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ERB_DIR = REPO_ROOT / "benchmarks" / "dogfood" / "erb"


def _load(module_name: str):
    """Import a benchmarks/dogfood/erb script by path.

    The bench dir is not a package, so a normal import will not find it; the
    spec loader keeps these tests independent of sys.path shape.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        f"_erb_{module_name}", ERB_DIR / f"{module_name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ladder = _load("ablation_ladder")
audit = _load("storage_audit")


# ── rank extraction ─────────────────────────────────────────────────────


def test_rank_gold_orders_by_score_descending():
    scores = {"a": 1.0, "b": 9.0, "c": 5.0}
    first, ranks = ladder.rank_gold(scores, {"c"})
    assert first == 2
    assert ranks == [2]


def test_rank_gold_returns_every_gold_rank():
    scores = {"a": 9.0, "b": 8.0, "c": 7.0, "d": 6.0}
    first, ranks = ladder.rank_gold(scores, {"b", "d"})
    assert first == 2
    assert ranks == [2, 4]


def test_rank_gold_breaks_ties_by_gene_id_not_insertion_order():
    # Same scores, opposite insertion order — the rank must not move, or arms
    # would differ for reasons that have nothing to do with the ablation.
    forward = {"z": 5.0, "a": 5.0, "m": 5.0}
    backward = {"m": 5.0, "a": 5.0, "z": 5.0}
    assert ladder.rank_gold(forward, {"m"}) == ladder.rank_gold(backward, {"m"})
    assert ladder.rank_gold(forward, {"a"})[0] == 1


def test_rank_gold_missing_gold_is_none():
    first, ranks = ladder.rank_gold({"a": 1.0}, {"nope"})
    assert first is None
    assert ranks == []


def test_rank_gold_empty_scores_is_none():
    assert ladder.rank_gold({}, {"a"}) == (None, [])


def test_rank_gold_depth_truncates():
    scores = {"a": 9.0, "b": 8.0, "c": 7.0}
    assert ladder.rank_gold(scores, {"c"}, depth=2) == (None, [])
    assert ladder.rank_gold(scores, {"c"}, depth=3) == (3, [3])


def test_recall_at_k_counts_only_ranks_within_k():
    assert ladder.recall_at_k([1, 12, 13, None], 12) == 0.5


def test_recall_at_k_empty_is_zero():
    assert ladder.recall_at_k([], 12) == 0.0


def test_percentile_is_nearest_rank_not_interpolated():
    values = [10.0, 20.0, 30.0, 40.0]
    assert ladder.percentile(values, 50) in values
    assert ladder.percentile(values, 95) == 40.0
    assert ladder.percentile([], 50) is None


def test_layer_signal_medians_omits_queries_where_signal_did_not_fire():
    # 'splade' fired on one of two queries: its median must be its own value,
    # not diluted toward zero by the query where it never ran.
    per_query = [{"fts5": 4.0, "splade": 30.0}, {"fts5": 6.0}]
    out = ladder.layer_signal_medians(per_query)
    assert out["splade"] == 30.0
    assert out["fts5"] == 5.0


# ── config knob plumbing / identity detection ───────────────────────────


@dataclass
class _Section:
    enabled: bool = True
    flag: bool = False


@dataclass
class _Cfg:
    section: _Section
    synonym_map: dict


def _cfg(**kwargs) -> _Cfg:
    return _Cfg(section=_Section(**kwargs), synonym_map={"cache": ["redis"]})


def test_set_dotted_applies_and_reports_success():
    cfg = _cfg()
    assert ladder.set_dotted(cfg, "section.enabled", False) is True
    assert cfg.section.enabled is False


def test_set_dotted_supports_bare_top_level_path():
    cfg = _cfg()
    assert ladder.set_dotted(cfg, "synonym_map", {}) is True
    assert cfg.synonym_map == {}


def test_set_dotted_reports_failure_for_unknown_knob():
    cfg = _cfg()
    assert ladder.set_dotted(cfg, "section.no_such_knob", False) is False
    assert ladder.set_dotted(cfg, "no_such_section.enabled", False) is False
    assert ladder.set_dotted(cfg, "no_such_top", 1) is False


def test_arm_is_identity_flags_knob_already_at_ablated_value():
    cfg = _cfg(flag=False)
    arm = ladder.Arm(name="no_flag", knobs={"section.flag": False})
    reason = ladder.arm_is_identity(cfg, arm)
    assert reason is not None
    assert "already False" in reason


def test_arm_is_identity_returns_none_when_knob_actually_moves():
    cfg = _cfg(flag=True)
    arm = ladder.Arm(name="no_flag", knobs={"section.flag": False})
    assert ladder.arm_is_identity(cfg, arm) is None


def test_arm_is_identity_reports_unknown_path():
    arm = ladder.Arm(name="bogus", knobs={"section.ghost": False})
    assert "unknown config path" in ladder.arm_is_identity(_cfg(), arm)


def test_arm_is_identity_none_for_baseline():
    assert ladder.arm_is_identity(_cfg(), ladder.ARMS[0]) is None


# ── arm self-validation ─────────────────────────────────────────────────


def _arm(name: str) -> ladder.Arm:
    return next(a for a in ladder.ARMS if a.name == name)


def test_baseline_arm_is_always_valid():
    valid, detail = ladder.validate_arm(_arm("baseline"), {}, None)
    assert valid is True
    assert "control" in detail


def test_signal_arm_valid_when_signal_present_at_baseline_and_gone_in_arm():
    baseline = {"signal_keys": ["fts5", "splade"]}
    observed = {"signal_keys": ["fts5"]}
    valid, detail = ladder.validate_arm(_arm("no_splade"), observed, baseline)
    assert valid is True
    assert "splade" in detail


def test_signal_arm_invalid_when_ablation_did_not_take():
    baseline = {"signal_keys": ["fts5", "splade"]}
    observed = {"signal_keys": ["fts5", "splade"]}
    valid, detail = ladder.validate_arm(_arm("no_splade"), observed, baseline)
    assert valid is False
    assert "did not take" in detail


def test_signal_arm_invalid_when_layer_never_fired_at_baseline():
    # The whole point: a layer that never ran at baseline cannot be "ablated",
    # and reporting the arm as a clean null would be a lie.
    baseline = {"signal_keys": ["fts5"]}
    observed = {"signal_keys": ["fts5"]}
    valid, detail = ladder.validate_arm(_arm("no_splade"), observed, baseline)
    assert valid is False
    assert "unfalsifiable" in detail


def test_dense_arm_requires_only_one_of_its_signals_at_baseline():
    # 'dense' and 'ann_dense_leg' come from different code paths; either one
    # firing at baseline makes the arm falsifiable.
    baseline = {"signal_keys": ["dense"]}
    observed = {"signal_keys": ["fts5"]}
    valid, _ = ladder.validate_arm(_arm("no_dense"), observed, baseline)
    assert valid is True


def test_dense_arm_invalid_if_either_dense_signal_survives():
    baseline = {"signal_keys": ["dense", "ann_dense_leg"]}
    observed = {"signal_keys": ["ann_dense_leg"]}
    valid, detail = ladder.validate_arm(_arm("no_dense"), observed, baseline)
    assert valid is False
    assert "ann_dense_leg" in detail


def test_tier_contrib_arm_reads_tier_keys_not_signal_keys():
    arm = _arm("no_sr")
    assert arm.validation == ladder.VALIDATION_TIER
    valid, _ = ladder.validate_arm(
        arm, {"tier_keys": ["tag_exact"]}, {"tier_keys": ["tag_exact", "sr"]},
    )
    assert valid is True
    invalid, detail = ladder.validate_arm(
        arm, {"tier_keys": ["sr"]}, {"tier_keys": ["sr"]},
    )
    assert invalid is False
    assert "did not take" in detail


def test_classifier_arm_validates_on_metadata_presence():
    arm = _arm("no_classifier")
    valid, _ = ladder.validate_arm(
        arm, {"classifier_metadata_queries": 0}, {"classifier_metadata_queries": 30},
    )
    assert valid is True
    invalid, _ = ladder.validate_arm(
        arm, {"classifier_metadata_queries": 3}, {"classifier_metadata_queries": 30},
    )
    assert invalid is False
    unfalsifiable, detail = ladder.validate_arm(
        arm, {"classifier_metadata_queries": 0}, {"classifier_metadata_queries": 0},
    )
    assert unfalsifiable is False
    assert "unfalsifiable" in detail


def test_synonym_arm_validates_on_expansion_count():
    arm = _arm("no_synonyms")
    valid, _ = ladder.validate_arm(
        arm, {"synonym_expansions": 0}, {"synonym_expansions": 7},
    )
    assert valid is True
    unfalsifiable, detail = ladder.validate_arm(
        arm, {"synonym_expansions": 0}, {"synonym_expansions": 0},
    )
    assert unfalsifiable is False
    assert "unfalsifiable" in detail


def test_cymatics_arm_validates_on_call_probe():
    arm = _arm("no_cymatics")
    valid, _ = ladder.validate_arm(arm, {"cymatics_calls": 0}, {"cymatics_calls": 30})
    assert valid is True
    invalid, detail = ladder.validate_arm(
        arm, {"cymatics_calls": 1}, {"cymatics_calls": 30},
    )
    assert invalid is False
    assert "did not take" in detail


def test_cold_tier_arm_validates_on_fired_flag():
    arm = _arm("no_cold_tier")
    valid, _ = ladder.validate_arm(arm, {"cold_tier_queries": 0}, {"cold_tier_queries": 4})
    assert valid is True


# ── VALIDATION_REWEIGHT (Wave 3b, PKI weight sweep) ─────────────────────
#
# The reweight kind exists because a weight arm cannot be graded by signal
# absence OR presence: the tier is on in the arm AND at baseline, which is
# "unfalsifiable" under both other kinds. These tests pin that it is not
# vacuous either — each of the four ways the treatment can silently fail to
# apply must be caught, because a reweight arm that never reweighted anything
# is exactly the #354 retraction.


def _rw_obs(*, weight=2.0, mode="rrf", tiers=("pki", "fts5")):
    return {
        "tier_keys": list(tiers),
        "store_tier_weights": {"pki": weight, "fts5": 3.0},
        "fusion_mode": mode,
    }


def test_reweight_arm_valid_when_the_weight_reached_the_live_store():
    valid, detail = ladder.validate_arm(
        _arm("pki_w2"), _rw_obs(weight=2.0), _rw_obs(weight=1.0))
    assert valid is True
    assert "_pki_weight 1.0 -> 2.0" in detail


def test_reweight_arm_invalid_when_the_knob_never_reached_the_store():
    # The knob loaded into the config object and was never plumbed to the
    # KnowledgeStore the fuser reads. Nothing else in the receipt would show
    # this — the arm would just look like a clean null.
    observed = _rw_obs(weight=2.0)
    observed["store_tier_weights"] = {"fts5": 3.0}
    valid, detail = ladder.validate_arm(_arm("pki_w2"), observed, _rw_obs(weight=1.0))
    assert valid is False
    assert "never reached the store" in detail


def test_reweight_arm_invalid_when_the_store_holds_a_different_weight():
    valid, detail = ladder.validate_arm(
        _arm("pki_w3"), _rw_obs(weight=1.0), _rw_obs(weight=1.0))
    assert valid is False
    assert "reweight did not take" in detail


def test_reweight_arm_invalid_when_it_matches_baseline_exactly():
    # #354 in miniature: an arm byte-identical to the control, reported as a
    # measured null.
    arm = ladder.Arm(
        name="pki_w1", knobs={"retrieval.pki_weight": 1.0}, gate="x",
        validation=ladder.VALIDATION_REWEIGHT, expect_present=("pki",),
    )
    valid, detail = ladder.validate_arm(arm, _rw_obs(weight=1.0), _rw_obs(weight=1.0))
    assert valid is False
    assert "identity vs baseline" in detail


def test_reweight_arm_invalid_under_additive_fusion_where_the_weight_is_inert():
    # add_tier still runs under "additive", so every other observable looks
    # healthy; the Fuser is simply never queried and the weight is dead.
    valid, detail = ladder.validate_arm(
        _arm("pki_w4"),
        _rw_obs(weight=4.0, mode="additive"),
        _rw_obs(weight=1.0, mode="additive"),
    )
    assert valid is False
    assert "inert treatment" in detail


def test_reweight_arm_unfalsifiable_when_the_tier_never_fired_at_baseline():
    valid, detail = ladder.validate_arm(
        _arm("pki_w2"), _rw_obs(weight=2.0, tiers=("pki",)),
        _rw_obs(weight=1.0, tiers=("fts5",)),
    )
    assert valid is False
    assert "unfalsifiable" in detail


def test_reweight_arm_invalid_when_the_tier_vanished_in_the_arm():
    valid, detail = ladder.validate_arm(
        _arm("pki_w2"), _rw_obs(weight=2.0, tiers=("fts5",)), _rw_obs(weight=1.0))
    assert valid is False
    assert "lost the tier" in detail


def test_reweight_arm_requires_a_baseline_to_contrast_against():
    valid, detail = ladder.validate_arm(_arm("pki_w2"), _rw_obs(weight=2.0), None)
    assert valid is False
    assert "unfalsifiable" in detail


def test_store_tier_weights_reads_the_instance_dict_only():
    class _Store:
        def __init__(self):
            self._pki_weight = 1.0
            self._tag_prefix_weight = 1.5
            self._splade_enabled = True     # not a weight
            self._rerank_weight = True      # bool: not a real weight
            self.pki_weight = 99.0          # not underscore-prefixed

        @property
        def _exploding_weight(self):        # must never be touched
            raise AssertionError("property getter fired")

    weights = ladder.store_tier_weights(_Store())
    assert weights == {"pki": 1.0, "tag_prefix": 1.5}


def test_pki_weight_sweep_arms_are_all_above_the_shipped_value():
    # The sweep only answers "is 1.0 leaving recall on the table" if every
    # arm actually moves off 1.0.
    sweep = {a.name: a.knobs["retrieval.pki_weight"]
             for a in ladder.ARMS if a.name.startswith("pki_w")}
    assert sweep == {"pki_w2": 2.0, "pki_w3": 3.0, "pki_w4": 4.0}
    for arm in ladder.ARMS:
        if arm.name.startswith("pki_w"):
            assert arm.validation == ladder.VALIDATION_REWEIGHT
            # expect_absent=("pki",) here would be FALSE by construction: PKI
            # still fires in a reweight arm.
            assert not arm.expect_absent


def test_pki_and_tags_gate_arms_declare_their_signal_observables():
    assert _arm("no_pki").expect_absent == ("pki",)
    assert _arm("no_tags").expect_absent == ("tag_exact", "tag_prefix")
    assert _arm("no_pki").knobs == {"retrieval.pki_enabled": False}
    assert _arm("no_tags").knobs == {"retrieval.tags_enabled": False}


def test_tag_arm_is_no_longer_listed_as_dropped():
    # Un-dropped in Wave 3b: [retrieval] tags_enabled is a real gate, so the
    # "only mute is weight-zeroing" rationale no longer holds.
    assert not [d for d in ladder.DROPPED_ARMS if "tag" in d["arm"]]


def test_unknown_validation_kind_is_not_silently_valid():
    arm = ladder.Arm(name="x", knobs={}, validation="none")
    valid, detail = ladder.validate_arm(arm, {}, {})
    assert valid is False
    assert "no observable" in detail


def test_every_arm_declares_a_gate_and_an_implemented_validation():
    implemented = {
        ladder.VALIDATION_CONTROL, ladder.VALIDATION_SIGNAL, ladder.VALIDATION_TIER,
        ladder.VALIDATION_METADATA, ladder.VALIDATION_EXPANSION,
        ladder.VALIDATION_CALL_PROBE, ladder.VALIDATION_COLD,
        ladder.VALIDATION_SIGNAL_PRESENT, ladder.VALIDATION_REWEIGHT,
    }
    for arm in ladder.ARMS:
        assert arm.gate, f"{arm.name} has no recorded gate file:line"
        assert arm.validation in implemented, f"{arm.name} has no validation method"
        if arm.name != "baseline":
            assert arm.knobs, f"{arm.name} changes nothing"
            # #341: an arm declares an ABSENCE observable (ablation arms) or a
            # PRESENCE one (enable arms) — never neither, and never both.
            assert arm.expect_absent or arm.expect_present, (
                f"{arm.name} declares no expected observable")
            assert not (arm.expect_absent and arm.expect_present), (
                f"{arm.name} declares both directions — pick one")
        if arm.validation == ladder.VALIDATION_SIGNAL_PRESENT:
            assert arm.expect_present, f"{arm.name} is an enable arm with nothing to expect"


def test_per_query_record_shape_and_recall_contribution():
    rec = ladder.per_query_record(
        "erb_000", gold_ranks=[3, 9], first_rank=3, k=12, n_queries=4,
        wall_ms=21.4567, signal_ms={"splade": 4.2, "fts5": 1.0},
    )
    assert set(rec) == {
        "needle", "gold_ranks", "rank_of_first_gold", "recall_hit",
        "recall_contribution", "wall_ms", "signal_ms",
    }
    assert rec["gold_ranks"] == [3, 9]
    assert rec["recall_hit"] == 1
    assert rec["recall_contribution"] == 0.25
    assert rec["rank_of_first_gold"] == 3
    assert rec["wall_ms"] == 21.457
    assert rec["signal_ms"] == {"fts5": 1.0, "splade": 4.2}
    assert "error" not in rec


def test_per_query_record_misses_and_errors_contribute_nothing():
    miss = ladder.per_query_record(
        "erb_001", gold_ranks=[40], first_rank=40, k=12, n_queries=4,
        wall_ms=1.0, signal_ms={},
    )
    assert miss["recall_hit"] == 0 and miss["recall_contribution"] == 0.0
    failed = ladder.per_query_record(
        "erb_002", gold_ranks=[], first_rank=None, k=12, n_queries=4,
        wall_ms=1.0, signal_ms={}, error="RuntimeError: boom",
    )
    assert failed["recall_hit"] == 0
    assert failed["error"] == "RuntimeError: boom"


def test_per_query_basis_reconstructs_recall_at_k():
    # The point of the column: a receipt reader can check the aggregate
    # against its own parts. 3 hits of 5 = 0.6, which is exact in neither
    # thirds nor 4-dp rounding by accident — use a case that isn't.
    first_ranks = [1, 5, None, 40, 12]
    n = len(first_ranks)
    records = [
        ladder.per_query_record(
            f"n{i}", gold_ranks=[r] if r else [], first_rank=r, k=12,
            n_queries=n, wall_ms=1.0, signal_ms={},
        )
        for i, r in enumerate(first_ranks)
    ]
    aggregate = ladder.recall_at_k(first_ranks, 12)
    # recall_hit is the exact integer basis — this identity holds to the bit.
    assert sum(r["recall_hit"] for r in records) / n == aggregate
    # recall_contribution agrees up to the aggregate's 4-decimal rounding.
    total = sum(r["recall_contribution"] for r in records)
    assert total == pytest.approx(aggregate, abs=5e-5)


def test_per_query_contribution_is_unrounded_so_thirds_still_sum():
    # 1/3 rounded to 6 dp would drift the column off recall@k; full precision
    # keeps it inside the aggregate's own rounding.
    first_ranks = [1, 2, None]
    records = [
        ladder.per_query_record(
            f"n{i}", gold_ranks=[r] if r else [], first_rank=r, k=12,
            n_queries=3, wall_ms=1.0, signal_ms={},
        )
        for i, r in enumerate(first_ranks)
    ]
    total = sum(r["recall_contribution"] for r in records)
    assert total == pytest.approx(ladder.recall_at_k(first_ranks, 12), abs=5e-5)


# ── #335 delivered gold ─────────────────────────────────────────────────
#
# The metric these tests guard exists because rank-based recall is
# structurally blind to two real effects (2026-08-06 retrieval ledger, claim
# 1): an admission gate that cuts candidates before the window, and a
# post-rank pull-forward that reorders inside it. The fixtures below are
# synthetic assembled windows — a gold that IS in the window, one that is
# NOT despite ranking well in the pool, and one that arrives as a session
# elision stub (id present, content absent).


def _window(ids, *, elided=()):
    """A synthetic assembled window: the id slice plus the text a real
    ``_assemble`` would have produced for it. Elided ids get the production
    stub, everything else gets a legibility header + body — so the stub
    probe is exercised against the SAME two shapes it must tell apart."""
    from cymatix_context.encoding.legibility import format_gene_header
    from cymatix_context.identity.session_delivery import format_elision_stub

    parts = []
    for gid in ids:
        if gid in elided:
            parts.append(format_elision_stub(
                gene_id=gid, delivered_at=1000.0, now=1045.0, queries_ago=3))
        else:
            header = format_gene_header(
                gid, raw_chars=900, compressed_chars=300, combined_score=1.0,
                tier_contrib={"fts5": 1.0}, score_stats=(0.5, 0.2))
            parts.append(f"{header}\nbody text for {gid}")
    return list(ids), "\n---\n".join(parts)


def test_delivered_gold_counts_a_gold_that_reached_the_window():
    ids, text = _window(["a", "gold1", "c"])
    fields = ladder.delivered_gold_fields(ids, {"gold1"},
                                          ladder.elided_gene_ids(text, ids))
    assert fields["delivered_gold"] == 1
    assert fields["delivered_gold_full"] == 1
    assert fields["delivered_gold_elided"] == 0
    assert fields["delivered_gold_rank"] == 2
    assert fields["delivered_gold_full_rank"] == 2
    assert fields["delivered_count"] == 3


def test_delivered_gold_misses_a_gold_the_pool_ranked_but_the_window_dropped():
    """The whole reason the metric exists. Gold sits at pool rank 3 — a clean
    recall@12 hit — and never reaches the assembled window. Rank-based recall
    reports a hit; the delivery basis must report a miss."""
    ids, text = _window(["a", "b", "c"])
    pool_first, pool_ranks = ladder.rank_gold(
        {"a": 9.0, "b": 8.0, "gold1": 7.0}, {"gold1"})
    assert (pool_first, pool_ranks) == (3, [3])          # recall@12 says HIT
    fields = ladder.delivered_gold_fields(ids, {"gold1"},
                                          ladder.elided_gene_ids(text, ids))
    assert fields["delivered_gold"] == 0                  # delivery says MISS
    assert fields["delivered_gold_full"] == 0
    assert fields["delivered_gold_rank"] is None
    assert fields["delivered_gold_full_rank"] is None


def test_delivered_gold_treats_an_elision_stub_as_delivered_but_not_full():
    """A session-elided document keeps its id in expressed_gene_ids while its
    CONTENT never ships. delivered_gold (id basis, unchanged meaning) still
    counts it; delivered_gold_full (content basis) must not."""
    ids, text = _window(["a", "gold1", "c"], elided={"gold1"})
    elided = ladder.elided_gene_ids(text, ids)
    assert elided == {"gold1"}
    fields = ladder.delivered_gold_fields(ids, {"gold1"}, elided)
    assert fields["delivered_gold"] == 1
    assert fields["delivered_gold_full"] == 0
    assert fields["delivered_gold_elided"] == 1
    assert fields["delivered_gold_rank"] == 2
    assert fields["delivered_gold_full_rank"] is None


def test_delivered_gold_full_prefers_a_content_bearing_gold_over_a_stub():
    """Two gold documents, the earlier one elided: the query DID receive gold
    content, so it is a full delivery — at the later rank."""
    ids, text = _window(["gold1", "b", "gold2"], elided={"gold1"})
    fields = ladder.delivered_gold_fields(ids, {"gold1", "gold2"},
                                          ladder.elided_gene_ids(text, ids))
    assert fields["delivered_gold"] == 1
    assert fields["delivered_gold_rank"] == 1        # first gold, a stub
    assert fields["delivered_gold_full"] == 1
    assert fields["delivered_gold_full_rank"] == 3   # first gold with content
    assert fields["delivered_gold_elided"] == 0


def test_full_and_elided_partition_delivered_gold_exactly():
    """The invariant the aggregate is read through: a delivered gold is either
    content or stub, never both, never neither."""
    cases = [
        (["gold1"], set()),           # content
        (["gold1"], {"gold1"}),       # stub
        (["x"], set()),               # no gold delivered
        ([], set()),                  # empty window
    ]
    for ids, elided in cases:
        _ids, text = _window(ids, elided=elided)
        f = ladder.delivered_gold_fields(_ids, {"gold1"},
                                         ladder.elided_gene_ids(text, _ids))
        assert f["delivered_gold_full"] + f["delivered_gold_elided"] == f["delivered_gold"]


def test_elision_probe_is_pinned_to_the_production_stub_format():
    """Coupling guard. A false NEGATIVE here (stub read as content) would
    silently inflate delivered gold, and the only way it happens is
    format_elision_stub changing shape — so the probe is fed that function's
    real output rather than a hand-written lookalike."""
    from cymatix_context.identity.session_delivery import format_elision_stub

    stub = format_elision_stub(
        gene_id="deadbeefcafebabe0123", delivered_at=0.0, now=90.0, queries_ago=2)
    assert ladder.elided_gene_ids(stub, ["deadbeefcafebabe0123"]) == {
        "deadbeefcafebabe0123"}


def test_elision_probe_does_not_fire_on_a_legibility_header():
    """The false-POSITIVE guard. A full delivery's legibility header is the
    same ``[gene=<id12> ...]`` shape as a stub and differs only by the glyph
    (◆/◇/⬦ vs ↻) — if the probe keyed on the prefix alone every delivered
    document would read as elided and delivered_gold_full would collapse to
    zero on every arm."""
    from cymatix_context.encoding.legibility import format_gene_header

    header = format_gene_header(
        "deadbeefcafebabe0123", raw_chars=900, compressed_chars=300,
        combined_score=2.0, tier_contrib={"fts5": 1.0, "dense": 0.5},
        score_stats=(0.5, 0.2))
    assert header.startswith("[gene=deadbeefcafe ")
    assert ladder.elided_gene_ids(header, ["deadbeefcafebabe0123"]) == set()


def test_elision_probe_is_empty_without_window_text():
    # A caller with no expressed_context (or a window that failed) must get
    # "nothing known to be elided", not a crash and not a phantom stub.
    assert ladder.elided_gene_ids(None, ["a", "b"]) == set()
    assert ladder.elided_gene_ids("", ["a", "b"]) == set()


def test_delivered_gold_fields_default_reproduces_the_pre_wave1_numbers():
    """Backward compatibility: omitting ``elided`` is the no-session case and
    must give exactly what the committed 100k receipt's delivered_gold_queries
    counted — id membership, nothing subtracted."""
    fields = ladder.delivered_gold_fields(["a", "gold1"], {"gold1"})
    assert fields["delivered_gold"] == 1
    assert fields["delivered_gold_rank"] == 2
    assert fields["delivered_gold_full"] == 1
    assert fields["delivered_gold_elided"] == 0


def test_delivered_gold_summary_is_derived_from_the_per_needle_rows():
    rows = [
        {"needle": "n0", "delivered_gold": 1, "delivered_gold_full": 1,
         "delivered_gold_elided": 0, "delivered_gold_rank": 1, "delivered_count": 4},
        {"needle": "n1", "delivered_gold": 1, "delivered_gold_full": 0,
         "delivered_gold_elided": 1, "delivered_gold_rank": 3, "delivered_count": 4},
        {"needle": "n2", "delivered_gold": 0, "delivered_gold_full": 0,
         "delivered_gold_elided": 0, "delivered_gold_rank": None, "delivered_count": 2},
        {"needle": "n3", "delivered_gold": 0, "delivered_gold_full": 0,
         "delivered_gold_elided": 0, "delivered_gold_rank": None, "delivered_count": 0},
    ]
    s = ladder.delivered_gold_summary(rows)
    assert s["delivered_gold_queries"] == 2
    assert s["delivered_gold_rate"] == 0.5
    assert s["delivered_gold_full_queries"] == 1
    assert s["delivered_gold_full_rate"] == 0.25
    assert s["delivered_gold_elided_queries"] == 1
    # median over the queries that DELIVERED gold (1 and 3), not over misses
    # padded with a sentinel rank.
    assert s["delivered_gold_median_rank"] == 2.0
    assert s["delivered_count_median"] == 3.0
    assert ladder.delivered_gold_rate(rows) == s["delivered_gold_rate"]


def test_delivered_gold_summary_on_an_empty_arm_is_zero_not_a_crash():
    s = ladder.delivered_gold_summary([])
    assert s["delivered_gold_queries"] == 0
    assert s["delivered_gold_rate"] == 0.0
    assert s["delivered_gold_median_rank"] is None


def test_delivered_gold_by_needle_carries_both_bases_per_needle():
    """The cross-tab basis a later wave needs: pool_rank next to delivered.
    Their DISAGREEMENT is the finding — ranked-but-undelivered is the
    admission-gate class, never-ranked is the #340 static-miss class."""
    rows = [
        {"needle": "erb_000", "delivered_gold": 1, "delivered_gold_full": 1,
         "delivered_gold_rank": 2, "delivered_count": 5, "rank_of_first_gold": 1},
        {"needle": "erb_001", "delivered_gold": 0, "delivered_gold_full": 0,
         "delivered_gold_rank": None, "delivered_count": 5, "rank_of_first_gold": 3},
        {"needle": "erb_002", "delivered_gold": 0, "delivered_gold_full": 0,
         "delivered_gold_rank": None, "delivered_count": 5, "rank_of_first_gold": None},
    ]
    by_needle = ladder.delivered_gold_by_needle(rows)
    assert list(by_needle) == ["erb_000", "erb_001", "erb_002"]
    # ranked well, never delivered — invisible to recall@12 by construction
    assert by_needle["erb_001"] == {
        "delivered": 0, "full": 0, "delivered_rank": None,
        "delivered_count": 5, "pool_rank": 3,
    }
    # never scored anywhere — the static-miss class
    assert by_needle["erb_002"]["pool_rank"] is None


@pytest.mark.parametrize("per_query", [True, False])
def test_run_arm_emits_per_query_only_when_flag_is_set(monkeypatch, per_query):
    """Fake-arm path: stub every heavy import so no model or bed is touched."""
    needles = [
        {"name": "n0", "query": "q0"},
        {"name": "n1", "query": "q1"},
        {"name": "n2", "query": "q2"},
    ]
    gold = {"n0": {"g1"}, "n1": {"g9"}, "n2": {"gX"}}

    class _FakeGenome:
        def __init__(self):
            self.last_query_scores = {"g1": 9.0, "g2": 8.0, "g9": 7.0}
            self.last_signal_timings = {"fts5": 2.0, "splade": 5.0}
            self.last_tier_contributions = {"g1": {"fts5": 2.0}}
            self.synonym_map = {}

        def _expand_terms(self, terms):
            return sorted(t.lower() for t in terms)

    class _FakeWindow:
        metadata = {"classifier": {"class": "default"}}
        expressed_gene_ids = ["g1"]

    class _FakeManager:
        def __init__(self, cfg):
            self.config = cfg
            self.genome = _FakeGenome()
            self._last_cold_tier_used = False

        def build_context(self, *a, **kw):
            return _FakeWindow()

        def close(self):
            pass

    class _FakeCfg:
        class _Genome:
            path = ""
        def __init__(self):
            self.genome = _FakeCfg._Genome()
            self.synonym_map = {}

    fake_modules = {
        "cymatix_context.backends.encoder_client": type(
            "M", (), {"active_url": staticmethod(lambda: "")},
        ),
        "cymatix_context.config": type(
            "M", (), {"load_config": staticmethod(lambda *a, **kw: _FakeCfg())},
        ),
        "cymatix_context.context_manager": type(
            "M", (), {
                "CymatixContextManager": _FakeManager,
                # #341: run_arm also drains the pipeline ring for the splice
                # candidate count — an empty ring is a legitimate "splice
                # never rang" result (newest_splice_count -> None).
                "get_pipeline_ring_max": staticmethod(lambda: 128),
                "get_recent_pipeline_events": staticmethod(lambda *a, **kw: []),
            },
        ),
        "cymatix_context.scoring.cymatics": type(
            "M", (), {"query_spectrum": staticmethod(lambda *a, **kw: None)},
        ),
    }
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        # run_arm imports these lazily inside the function body.
        if name == "cymatix_context.backends" and "encoder_client" in (fromlist or ()):
            return type("P", (), {
                "encoder_client": fake_modules["cymatix_context.backends.encoder_client"],
            })
        if name == "cymatix_context.scoring" and "cymatics" in (fromlist or ()):
            return type("P", (), {
                "cymatics": fake_modules["cymatix_context.scoring.cymatics"],
            })
        if name in fake_modules:
            return fake_modules[name]
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _fake_import)

    row = ladder.run_arm(
        ladder.Arm(name="fake", knobs={}, gate="test", validation=ladder.VALIDATION_CONTROL),
        genome_path="unused.db", config_path=None, needles=needles,
        gold_by_needle=gold, k=12, per_query=per_query,
    )

    if not per_query:
        assert "per_query" not in row, "flag off must leave the receipt shape unchanged"
        return

    assert len(row["per_query"]) == len(needles)
    assert [r["needle"] for r in row["per_query"]] == ["n0", "n1", "n2"]
    for rec in row["per_query"]:
        assert set(rec) >= {
            "needle", "gold_ranks", "rank_of_first_gold", "recall_hit",
            "recall_contribution", "wall_ms", "signal_ms",
        }
    # n0 gold ranks 1st, n1 gold ranks 3rd, n2's gold is not in the score map.
    assert row["per_query"][0]["gold_ranks"] == [1]
    assert row["per_query"][1]["gold_ranks"] == [3]
    assert row["per_query"][2]["gold_ranks"] == []
    assert row["per_query"][2]["rank_of_first_gold"] is None
    # The basis must reconstruct the aggregate it sits next to.
    assert sum(r["recall_hit"] for r in row["per_query"]) / len(needles) == pytest.approx(
        row["recall_at_k"], abs=5e-5
    )
    assert sum(r["recall_contribution"] for r in row["per_query"]) == pytest.approx(
        row["recall_at_k"], abs=5e-5
    )
    assert row["per_query"][0]["signal_ms"] == {"fts5": 2.0, "splade": 5.0}


def test_run_arm_delivery_receipt_is_always_on_and_records_its_posture(monkeypatch):
    """#335 wave 1, at the run_arm seam.

    Three needles whose POOL rank is identical (gold ranks 1st in the score
    map on all three) but whose assembled WINDOW differs: n0 receives gold
    content, n1's window drops it despite the rank, n2 receives it as a
    session elision stub. recall@12 is 1.000 for the arm; delivered gold must
    not be. Also pins the two contract promises later waves depend on: the
    per-needle map ships WITHOUT ``--per-query``, and the arm records which
    delivery posture produced the numbers."""
    from cymatix_context.identity.session_delivery import format_elision_stub

    needles = [
        {"name": "n0", "query": "q0"},
        {"name": "n1", "query": "q1"},
        {"name": "n2", "query": "q2"},
    ]
    gold = {"n0": {"g1"}, "n1": {"g1"}, "n2": {"g1"}}
    stub = format_elision_stub(
        gene_id="g1", delivered_at=0.0, now=45.0, queries_ago=3)
    # (expressed_gene_ids, expressed_context) per call, warmup first.
    windows = [
        (["g1", "g2"], "[gene=g1 ◆ fired=fts5 900→300c]\nwarmup"),
        (["g1", "g2"], "[gene=g1 ◆ fired=fts5 900→300c]\ncontent for g1"),
        (["g2", "g3"], "[gene=g2 ◇ fired=fts5 900→300c]\ncontent for g2"),
        (["g1", "g2"], f"{stub}\n---\n[gene=g2 ◇ fired=fts5 900→300c]\nc"),
    ]

    class _FakeGenome:
        def __init__(self):
            # gold "g1" is rank 1 in the pool on EVERY needle.
            self.last_query_scores = {"g1": 9.0, "g2": 8.0, "g3": 7.0}
            self.last_signal_timings = {"fts5": 2.0}
            self.last_tier_contributions = {}
            self.synonym_map = {}

        def _expand_terms(self, terms):
            return sorted(t.lower() for t in terms)

    class _FakeWindow:
        def __init__(self, ids, text):
            self.expressed_gene_ids = ids
            self.expressed_context = text
            self.metadata = {}

    class _FakeManager:
        def __init__(self, cfg):
            self.config = cfg
            self.genome = _FakeGenome()
            self._last_cold_tier_used = False
            self._n = 0

        def build_context(self, *a, **kw):
            # The posture the delivery_basis block claims — asserted here so
            # the receipt cannot advertise a flag run_arm stopped passing.
            assert kw.get("ignore_delivered") is True
            assert kw.get("read_only") is True
            ids, text = windows[min(self._n, len(windows) - 1)]
            self._n += 1
            return _FakeWindow(list(ids), text)

        def close(self):
            pass

    class _FakeCfg:
        class _Genome:
            path = ""

        class _Budget:
            session_delivery_enabled = True

        def __init__(self):
            self.genome = _FakeCfg._Genome()
            self.budget = _FakeCfg._Budget()
            self.synonym_map = {}

    fake_modules = {
        "cymatix_context.config": type(
            "M", (), {"load_config": staticmethod(lambda *a, **kw: _FakeCfg())},
        ),
        "cymatix_context.context_manager": type(
            "M", (), {
                "CymatixContextManager": _FakeManager,
                "get_pipeline_ring_max": staticmethod(lambda: 128),
                "get_recent_pipeline_events": staticmethod(lambda *a, **kw: []),
            },
        ),
    }
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "cymatix_context.backends" and "encoder_client" in (fromlist or ()):
            return type("P", (), {"encoder_client": type(
                "M", (), {"active_url": staticmethod(lambda: "")})})
        if name == "cymatix_context.scoring" and "cymatics" in (fromlist or ()):
            return type("P", (), {"cymatics": type(
                "M", (), {"query_spectrum": staticmethod(lambda *a, **kw: None)})})
        if name in fake_modules:
            return fake_modules[name]
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _fake_import)

    row = ladder.run_arm(
        ladder.Arm(name="fake", knobs={}, gate="test",
                   validation=ladder.VALIDATION_CONTROL),
        genome_path="unused.db", config_path=None, needles=needles,
        gold_by_needle=gold, k=12, per_query=False,   # <- flag deliberately OFF
    )

    # Rank basis: perfect. Delivery basis: not. That divergence IS the metric.
    assert row["recall_at_k"] == 1.0
    assert row["delivered_gold_queries"] == 2          # n0 (content) + n2 (stub)
    assert row["delivered_gold_full_queries"] == 1     # n0 only
    assert row["delivered_gold_elided_queries"] == 1   # n2
    assert row["pool_delivery_gap"] == pytest.approx(1.0 - 2 / 3, abs=5e-5)

    # Per-needle map ships even with --per-query off.
    assert "per_query" not in row
    by_needle = row["delivered_gold_by_needle"]
    assert list(by_needle) == ["n0", "n1", "n2"]
    assert by_needle["n0"] == {"delivered": 1, "full": 1, "delivered_rank": 1,
                               "delivered_count": 2, "pool_rank": 1}
    # ranked 1st, delivered never — the effect recall@12 cannot express
    assert by_needle["n1"] == {"delivered": 0, "full": 0, "delivered_rank": None,
                               "delivered_count": 2, "pool_rank": 1}
    assert by_needle["n2"] == {"delivered": 1, "full": 0, "delivered_rank": 1,
                               "delivered_count": 2, "pool_rank": 1}

    basis = row["delivery_basis"]
    assert basis["ignore_delivered"] is True
    assert basis["elision_possible"] is False
    assert basis["session_delivery_enabled_in_config"] is True
    # The audit field: this fixture forces a stub through, so it is non-zero
    # here. On a real ladder run it must be 0 — ignore_delivered=True makes
    # elision unreachable, and a non-zero count means that broke.
    assert basis["elision_observed_queries"] == 1


def test_run_arm_splice_n_candidates_does_not_leak_across_needles(monkeypatch):
    """#341 review regression: ``context_manager._pipeline_events`` is a
    process-lifetime global ring, never cleared between queries/arms.
    Before the mark-correlation fix, draining "the newest splice entry" on
    a needle whose own ``build_context`` never rings one (no-match early
    return, abstain, pre-splice exception) silently inherited the
    PREVIOUS needle's (or the untimed warmup's) splice count instead of
    None. n0 here rings splice; n1 does not (simulated abstain) — n1 must
    read None, not n0's count."""
    needles = [
        {"name": "n0", "query": "q0"},
        {"name": "n1", "query": "q1"},
    ]
    gold = {"n0": {"g1"}, "n1": {"g9"}}

    fake_ring: list = []
    _clock = {"t": 0.0}

    def _ring_append(stage, **extra):
        _clock["t"] += 1.0
        fake_ring.append({"stage": stage, "ts": _clock["t"], **extra})

    class _FakeGenome:
        def __init__(self):
            self.last_query_scores = {"g1": 9.0}
            self.last_signal_timings = {}
            self.last_tier_contributions = {}
            self.synonym_map = {}

        def _expand_terms(self, terms):
            return sorted(t.lower() for t in terms)

    class _FakeWindow:
        metadata = {}
        expressed_gene_ids = ["g1"]

    class _FakeManager:
        def __init__(self, cfg):
            self.config = cfg
            self.genome = _FakeGenome()
            self._last_cold_tier_used = False

        def build_context(self, query, *a, **kw):
            _ring_append("express")
            if query == "q0":
                _ring_append("splice", n_candidates=7, splice_target=100)
            # q1 rings no splice entry — simulates a no-match/abstain early
            # return that never reaches the splice stage.
            return _FakeWindow()

        def close(self):
            pass

    class _FakeCfg:
        class _Genome:
            path = ""
        def __init__(self):
            self.genome = _FakeCfg._Genome()
            self.synonym_map = {}

    fake_modules = {
        "cymatix_context.backends.encoder_client": type(
            "M", (), {"active_url": staticmethod(lambda: "")},
        ),
        "cymatix_context.config": type(
            "M", (), {"load_config": staticmethod(lambda *a, **kw: _FakeCfg())},
        ),
        "cymatix_context.context_manager": type(
            "M", (), {
                "CymatixContextManager": _FakeManager,
                "get_pipeline_ring_max": staticmethod(lambda: 128),
                "get_recent_pipeline_events": staticmethod(
                    lambda limit=32: list(fake_ring)[-limit:] if limit else list(fake_ring)
                ),
            },
        ),
        "cymatix_context.scoring.cymatics": type(
            "M", (), {"query_spectrum": staticmethod(lambda *a, **kw: None)},
        ),
    }
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "cymatix_context.backends" and "encoder_client" in (fromlist or ()):
            return type("P", (), {
                "encoder_client": fake_modules["cymatix_context.backends.encoder_client"],
            })
        if name == "cymatix_context.scoring" and "cymatics" in (fromlist or ()):
            return type("P", (), {
                "cymatics": fake_modules["cymatix_context.scoring.cymatics"],
            })
        if name in fake_modules:
            return fake_modules[name]
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _fake_import)

    row = ladder.run_arm(
        ladder.Arm(name="fake", knobs={}, gate="test", validation=ladder.VALIDATION_CONTROL),
        genome_path="unused.db", config_path=None, needles=needles,
        gold_by_needle=gold, k=12, per_query=True,
    )

    recs = row["per_query"]
    assert recs[0]["splice_n_candidates"] == 7
    assert recs[1]["splice_n_candidates"] is None, (
        "n1 never rang splice — must not inherit n0's stale ring count"
    )


def test_run_arm_final_order_fields_do_not_leak_across_needles(monkeypatch):
    """#341: last_ranked_ids / last_rerank_diag are PROCESS-lifetime store
    attributes, rewritten only when a query reaches its publication point.
    n0 publishes an order; n1's query returns without publishing (no-match /
    abstain). Without the clear-before-call discipline n1 would report n0's
    rank as its own — a fabricated hit."""
    needles = [{"name": "n0", "query": "q0"}, {"name": "n1", "query": "q1"}]
    gold = {"n0": {"g1"}, "n1": {"g1"}}

    class _FakeGenome:
        def __init__(self):
            self.last_query_scores = {"g1": 9.0}
            self.last_signal_timings = {"xenc_rerank": 12.0}
            self.last_tier_contributions = {}
            self.synonym_map = {}
            self.last_ranked_ids = []
            self.last_rerank_diag = {}

        def _expand_terms(self, terms):
            return sorted(t.lower() for t in terms)

    class _FakeWindow:
        metadata = {}
        expressed_gene_ids = ["g1"]

    class _FakeManager:
        def __init__(self, cfg):
            self.config = cfg
            self.genome = _FakeGenome()
            self._last_cold_tier_used = False

        def build_context(self, query, *a, **kw):
            if query == "q0":
                self.genome.last_ranked_ids = ["zz", "g1", "yy"]
                self.genome.last_rerank_diag = {"gate_kept": 12, "floor_appended": 3}
            # q1 publishes nothing — simulates a query that never reaches the
            # ANN/lex publication point.
            return _FakeWindow()

        def close(self):
            pass

    class _FakeCfg:
        class _Genome:
            path = ""

        def __init__(self):
            self.genome = _FakeCfg._Genome()
            self.synonym_map = {}

    fake_modules = {
        "cymatix_context.backends.encoder_client": type(
            "M", (), {"active_url": staticmethod(lambda: "http://127.0.0.1:11439")},
        ),
        "cymatix_context.config": type(
            "M", (), {"load_config": staticmethod(lambda *a, **kw: _FakeCfg())},
        ),
        "cymatix_context.context_manager": type(
            "M", (), {
                "CymatixContextManager": _FakeManager,
                "get_pipeline_ring_max": staticmethod(lambda: 128),
                "get_recent_pipeline_events": staticmethod(lambda *a, **kw: []),
            },
        ),
        "cymatix_context.scoring.cymatics": type(
            "M", (), {"query_spectrum": staticmethod(lambda *a, **kw: None)},
        ),
    }
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "cymatix_context.backends" and "encoder_client" in (fromlist or ()):
            return type("P", (), {
                "encoder_client": fake_modules["cymatix_context.backends.encoder_client"],
            })
        if name == "cymatix_context.scoring" and "cymatics" in (fromlist or ()):
            return type("P", (), {
                "cymatics": fake_modules["cymatix_context.scoring.cymatics"],
            })
        if name in fake_modules:
            return fake_modules[name]
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _fake_import)

    row = ladder.run_arm(
        ladder.Arm(name="rerank_on", knobs={}, gate="test",
                   validation=ladder.VALIDATION_CONTROL),
        genome_path="unused.db", config_path=None, needles=needles,
        gold_by_needle=gold, k=12, per_query=True,
    )

    recs = row["per_query"]
    assert recs[0]["final_rank_of_first_gold"] == 2
    assert recs[0]["final_recall_hit"] == 1
    assert recs[0]["gate_kept"] == 12 and recs[0]["floor_appended"] == 3
    assert recs[1]["final_rank_of_first_gold"] is None, (
        "n1 published no order — must not inherit n0's ranked ids")
    assert recs[1]["final_recall_hit"] == 0
    assert recs[1]["gate_kept"] is None and recs[1]["floor_appended"] is None
    # Arm aggregate: 1 of 2 needles hit on the final-order basis.
    assert row["final_recall_at_k"] == 0.5
    # ...and the score-map basis still reports both as hits, which is exactly
    # why both bases ship: they are not interchangeable.
    assert row["recall_at_k"] == 1.0
    assert row["observables"]["signal_keys"] == ["xenc_rerank"]


def test_call_probe_counts_and_restores_the_original():
    class _Mod:
        @staticmethod
        def target(value):
            return value * 2

    original = _Mod.target
    with ladder._CallProbe(_Mod, "target") as probe:
        assert _Mod.target(3) == 6
        assert _Mod.target(4) == 8
    assert probe.count == 2
    assert _Mod.target is original


def test_call_probe_restores_on_exception():
    class _Mod:
        @staticmethod
        def target():
            return 1

    original = _Mod.target
    with pytest.raises(RuntimeError):
        with ladder._CallProbe(_Mod, "target"):
            raise RuntimeError("boom")
    assert _Mod.target is original


# ── storage audit: layer mapping ────────────────────────────────────────


@pytest.mark.parametrize(
    "name,layer",
    [
        ("genes_fts", "fts5"),
        ("genes_fts_idx", "fts5"),
        ("genes_fts_docsize", "fts5"),
        ("splade_terms", "splade"),
        ("splade_term_df", "splade"),
        ("idx_splade_term_cover", "splade"),
        ("sqlite_autoindex_splade_term_df_1", "splade"),
        ("promoter_index", "tags"),
        ("idx_promoter_cover", "tags"),
        ("idx_genes_dense_v2_hot", "dense"),
        ("harmonic_links", "coact"),
        ("idx_harmonic_source", "coact"),
        ("gene_relations", "coact"),
        ("path_key_index", "pki"),
        ("idx_pki_lookup", "pki"),
        ("filename_index", "filename_anchor"),
        ("entity_graph", "entity_graph"),
        ("symbol_defs", "symbol_graph"),
        ("participants", "identity"),
        ("gene_attribution", "identity"),
        ("cwola_log", "ops_not_retrieval"),
        ("session_delivery_log", "ops_not_retrieval"),
        ("idx_sdl_session_gene", "ops_not_retrieval"),
        ("health_log", "ops_not_retrieval"),
        ("okf_links", "okf"),
        ("genes", "core"),
        ("idx_genes_last_seen", "core"),
        ("sqlite_stat1", "core"),
    ],
)
def test_classify_object_maps_real_carve_schema_names(name, layer):
    assert audit.classify_object(name)[0] == layer


def test_classify_object_never_silently_buckets_an_unknown():
    layer, rationale = audit.classify_object("some_future_table")
    assert layer == "unknown"
    assert "no layer mapping" in rationale


def test_every_layer_pattern_carries_a_rationale():
    for pattern, layer, rationale in audit.LAYER_PATTERNS:
        assert pattern and layer and rationale


# ── storage audit: estimator on a tiny in-memory db ─────────────────────


def _tiny_db(tmp_path: Path) -> Path:
    """A miniature bed: real object names, three rows, no models involved."""
    db = tmp_path / "tiny.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE genes (
            gene_id TEXT PRIMARY KEY,
            content TEXT,
            embedding BLOB,
            embedding_dense_v2 BLOB
        );
        CREATE TABLE promoter_index (gene_id TEXT, tag_value TEXT);
        CREATE INDEX idx_promoter_value ON promoter_index(tag_value);
        CREATE TABLE splade_terms (term TEXT, gene_id TEXT, weight REAL);
        CREATE TABLE cwola_log (retrieval_id TEXT, session_id TEXT);
        CREATE TABLE mystery_table (x TEXT);
        """
    )
    for i in range(3):
        conn.execute(
            "INSERT INTO genes VALUES (?,?,?,?)",
            (f"g{i}", "body" * 20, b"\x01" * 80, b"\x02" * 4096),
        )
        conn.execute("INSERT INTO promoter_index VALUES (?,?)", (f"g{i}", "cymatix"))
        conn.execute("INSERT INTO splade_terms VALUES (?,?,?)", ("tok", f"g{i}", 1.0))
        conn.execute("INSERT INTO mystery_table VALUES (?)", ("x",))
    conn.commit()
    conn.close()
    return db


def test_audit_estimate_mode_buckets_by_layer(tmp_path):
    out = audit.audit(_tiny_db(tmp_path), want_exact=False)
    assert out["mode"] == "estimate"
    assert out["exact_available"] is False
    assert set(out["layers"]) >= {"dense", "sema", "tags", "splade", "ops_not_retrieval"}
    # The 4 KB/row dense blob must dominate the 80 B/row sema blob.
    assert out["layers"]["dense"]["bytes"] > out["layers"]["sema"]["bytes"]


def test_audit_lists_unknown_objects_by_name(tmp_path):
    out = audit.audit(_tiny_db(tmp_path), want_exact=False)
    assert "mystery_table" in out["unknown_objects"]
    assert out["layers"]["unknown"]["bytes"] >= 0


def test_audit_file_total_is_exact_page_math_not_an_estimate(tmp_path):
    db = _tiny_db(tmp_path)
    out = audit.audit(db, want_exact=False)
    assert out["file"]["file_bytes"] == (
        out["file"]["page_size"] * out["file"]["page_count"]
    )
    assert out["file"]["on_disk_bytes"] == db.stat().st_size
    assert out["unaccounted_bytes"] == out["file"]["file_bytes"] - out["accounted_bytes"]


def test_audit_carves_embedding_columns_out_of_the_genes_bucket(tmp_path):
    out = audit.audit(_tiny_db(tmp_path), want_exact=False)
    genes = next(o for o in out["objects"] if o["name"] == "genes")
    assert genes["column_split"] == "estimated"
    assert "bytes_after_column_carveout" in genes
    assert genes["bytes_after_column_carveout"] <= genes["bytes"]
    names = {o["name"] for o in out["objects"]}
    assert {"genes.embedding", "genes.embedding_dense_v2"} <= names


def test_audit_exact_mode_degrades_honestly_when_dbstat_is_missing(tmp_path):
    out = audit.audit(_tiny_db(tmp_path), want_exact=True)
    assert out["exact_requested"] is True
    if not out["exact_available"]:
        # Stock CPython lacks SQLITE_ENABLE_DBSTAT_VTAB: the run must say so
        # rather than presenting estimates as exact.
        assert "dbstat unavailable" in out["mode"]
        assert out["exact_error"]
    else:
        assert out["mode"] == "exact(dbstat)"


def test_audit_charges_virtual_tables_nothing(tmp_path):
    # genes_fts / genes_fts_vocab answer COUNT(*) and expose columns, so a
    # naive estimator invents storage for them (2.86 GB of a 5.99 GB bed, in
    # the case that produced this test). They own no b-tree.
    db = tmp_path / "fts.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE genes (gene_id TEXT PRIMARY KEY, content TEXT);
        CREATE VIRTUAL TABLE genes_fts USING fts5(gene_id, content);
        """
    )
    for i in range(5):
        conn.execute("INSERT INTO genes VALUES (?,?)", (f"g{i}", "lorem ipsum " * 50))
        conn.execute("INSERT INTO genes_fts VALUES (?,?)", (f"g{i}", "lorem ipsum " * 50))
    conn.commit()
    conn.close()

    out = audit.audit(db, want_exact=False)
    by_name = {o["name"]: o for o in out["objects"]}
    assert by_name["genes_fts"]["bytes"] == 0
    assert "virtual" in by_name["genes_fts"]["measured"]
    # The shadow tables carry the real bytes and are still counted.
    assert by_name["genes_fts_data"]["bytes"] > 0
    assert out["layers"]["fts5"]["bytes"] > 0


def test_virtual_tables_detects_fts5_shells(tmp_path):
    db = tmp_path / "vt.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE genes (gene_id TEXT, content TEXT);"
        "CREATE VIRTUAL TABLE genes_fts USING fts5(gene_id, content);"
        "CREATE VIRTUAL TABLE genes_fts_vocab USING fts5vocab(genes_fts, instance);"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db)
    try:
        names = audit.virtual_tables(conn)
        assert names == {"genes_fts", "genes_fts_vocab"}
    finally:
        conn.close()


def test_column_bytes_handles_all_null_column(tmp_path):
    db = tmp_path / "nulls.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE genes (gene_id TEXT, embedding BLOB)")
    conn.executemany("INSERT INTO genes VALUES (?,?)", [("a", None), ("b", None)])
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db)
    try:
        assert audit.column_bytes(conn, "genes", "embedding") == (0, 0)
    finally:
        conn.close()


def test_sampled_mean_row_bytes_counts_utf8_bytes_not_codepoints(tmp_path):
    db = tmp_path / "utf8.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (s TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", ("é" * 10,))  # 10 chars, 20 bytes
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db)
    try:
        assert audit.sampled_mean_row_bytes(conn, "t", ["s"]) == pytest.approx(20.0)
    finally:
        conn.close()


def test_to_gb_is_binary_gigabytes():
    assert audit.to_gb(1024 ** 3) == 1.0
