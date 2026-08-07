"""Unit tests for #335 delivered-gold metrics in the ERB ablation ladder.

Scope is the pure helpers only — no beds, no manager construction. See
``tests/test_ablation_tools.py`` for the broader ablation-ladder helper
coverage; these are grouped separately because they land with #335 first.

#341 Task 11 extends this file with the rerank ladder arms
(``VALIDATION_SIGNAL_PRESENT``, final-order recall) and the pure helpers of
``benchmarks/dogfood/erb/rerank_ab.py``. The A/B runner's daemon-lifecycle
path is deliberately NOT unit-tested — it needs a real encoder daemon, and
the receipts run exercises it end to end.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_ladder():
    # ablation_ladder.py defines a frozen @dataclass (Arm); the dataclasses
    # module resolves ClassVar string annotations via
    # sys.modules.get(cls.__module__), so the module must be registered in
    # sys.modules BEFORE exec_module runs, or that lookup returns None and
    # dataclass construction blows up with an unrelated AttributeError. See
    # the working loader at tests/test_ablation_tools.py:24-42.
    spec = importlib.util.spec_from_file_location(
        "ablation_ladder", ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_rerank_ab():
    """Same spec loader, for the A/B runner (also a non-package script)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "rerank_ab", ROOT / "benchmarks" / "dogfood" / "erb" / "rerank_ab.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_delivered_gold_fields_hit():
    lad = _load_ladder()
    out = lad.delivered_gold_fields(["a", "b", "gold1", "c"], {"gold1", "gold9"})
    assert out == {"delivered_gold": 1, "delivered_count": 4, "delivered_gold_rank": 3}


def test_delivered_gold_fields_miss_and_empty():
    lad = _load_ladder()
    assert lad.delivered_gold_fields(["a", "b"], {"g"}) == {
        "delivered_gold": 0, "delivered_count": 2, "delivered_gold_rank": None}
    assert lad.delivered_gold_fields([], {"g"})["delivered_count"] == 0


def test_arm_level_delivered_gold_rate():
    lad = _load_ladder()
    recs = [{"delivered_gold": 1}, {"delivered_gold": 0}, {"delivered_gold": 1}]
    assert lad.delivered_gold_rate(recs) == 2 / 3
    assert lad.delivered_gold_rate([]) == 0.0


def test_newest_splice_count_returns_last_splice_entry():
    lad = _load_ladder()
    events = [
        {"stage": "express", "n_candidates": 99},
        {"stage": "splice", "n_candidates": 3},
        {"stage": "assemble"},
        {"stage": "splice", "n_candidates": 5},
    ]
    assert lad.newest_splice_count(events) == 5


def test_newest_splice_count_none_when_splice_never_ran():
    lad = _load_ladder()
    events = [{"stage": "express", "n_candidates": 99}, {"stage": "assemble"}]
    assert lad.newest_splice_count(events) is None
    assert lad.newest_splice_count([]) is None


# ── #341 review fix: per-request ring correlation (mark + since-mark) ────
#
# context_manager._pipeline_events is a process-lifetime global deque.
# Draining "the globally newest splice entry" with no correlation to the
# CURRENT build_context call leaks a prior query's (or the untimed
# warmup's) splice count into any query whose own build_context never
# rings a splice entry (no-match early return, abstain, pre-splice
# exception). pipeline_ring_mark/events_since_mark fix this: mark the ring
# immediately before a call, then only accept entries strictly newer than
# that mark.


def test_pipeline_ring_mark_of_empty_ring_is_zero():
    lad = _load_ladder()
    assert lad.pipeline_ring_mark([]) == 0.0


def test_pipeline_ring_mark_is_newest_ts():
    lad = _load_ladder()
    events = [{"stage": "express", "ts": 10.0}, {"stage": "splice", "ts": 12.5},
              {"stage": "assemble", "ts": 11.0}]
    assert lad.pipeline_ring_mark(events) == 12.5


def test_events_since_mark_excludes_entries_at_or_before_mark():
    lad = _load_ladder()
    events = [{"stage": "splice", "ts": 5.0, "n_candidates": 1},
              {"stage": "express", "ts": 8.0},
              {"stage": "splice", "ts": 9.0, "n_candidates": 2}]
    since = lad.events_since_mark(events, 8.0)
    assert since == [{"stage": "splice", "ts": 9.0, "n_candidates": 2}]


def test_mark_and_since_mark_prevent_stale_splice_leak_across_needles():
    """The exact scenario the review flagged: needle 1 rings a splice
    entry; needle 2's build_context never reaches splice (simulated
    no-match/abstain). Draining the newest splice entry in the WHOLE ring
    after needle 2 would wrongly return needle 1's count. Marking the ring
    before each call and filtering to entries since that mark must return
    None for needle 2 instead."""
    lad = _load_ladder()
    ring: list = []

    # Needle 1: mark the (empty) ring, then its build_context rings splice.
    mark_1 = lad.pipeline_ring_mark(ring)
    ring.append({"stage": "express", "ts": 1.0})
    ring.append({"stage": "splice", "ts": 1.5, "n_candidates": 4})
    n1_count = lad.newest_splice_count(lad.events_since_mark(ring, mark_1))
    assert n1_count == 4

    # Needle 2: mark AFTER needle 1's entries, then its build_context rings
    # no splice entry at all (only express/assemble — abstain path).
    mark_2 = lad.pipeline_ring_mark(ring)
    ring.append({"stage": "express", "ts": 2.0})
    ring.append({"stage": "assemble", "ts": 2.2})
    n2_count = lad.newest_splice_count(lad.events_since_mark(ring, mark_2))
    assert n2_count is None, (
        "needle 2 never rang splice -- must not inherit needle 1's count"
    )

    # Sanity: draining the WHOLE ring (the pre-fix bug) would have leaked
    # needle 1's count into needle 2 -- confirms the scenario is real.
    assert lad.newest_splice_count(ring) == 4


# ── #341 Task 11: VALIDATION_SIGNAL_PRESENT (enable-arm validation) ──────
#
# Every pre-existing validation kind can only prove a signal's ABSENCE, so
# an ENABLE arm (rerank_on) is unfalsifiable with them: "xenc_rerank did not
# appear" and "the knob never applied" look identical. The new kind inverts
# both halves of the contract — present in the arm, absent at baseline.


def _arm(lad, name):
    return next(a for a in lad.ARMS if a.name == name)


def test_signal_present_valid_when_fired_in_arm_and_absent_at_baseline():
    lad = _load_ladder()
    valid, detail = lad.validate_arm(
        _arm(lad, "rerank_on"),
        {"signal_keys": ["fts5", "dense", "xenc_rerank"]},
        {"signal_keys": ["fts5", "dense"]},
    )
    assert valid is True
    assert "xenc_rerank" in detail


def test_signal_present_invalid_when_signal_never_fired_in_arm():
    # The enable did not take: the knob was set but the seam never ran.
    lad = _load_ladder()
    valid, detail = lad.validate_arm(
        _arm(lad, "rerank_on"),
        {"signal_keys": ["fts5", "dense"]},
        {"signal_keys": ["fts5", "dense"]},
    )
    assert valid is False
    assert "did not take" in detail


def test_signal_present_invalid_when_signal_already_fired_at_baseline():
    # Already on at baseline => the arm changes nothing observable, so its
    # presence is not evidence the knob did anything: unfalsifiable.
    lad = _load_ladder()
    valid, detail = lad.validate_arm(
        _arm(lad, "rerank_on"),
        {"signal_keys": ["xenc_rerank"]},
        {"signal_keys": ["xenc_rerank"]},
    )
    assert valid is False
    assert "unfalsifiable" in detail


def test_signal_present_requires_every_expected_key():
    lad = _load_ladder()
    arm = lad.Arm(
        name="two_signals", knobs={"x.y": True}, gate="test",
        validation=lad.VALIDATION_SIGNAL_PRESENT,
        expect_present=("alpha", "beta"),
    )
    valid, detail = lad.validate_arm(
        arm, {"signal_keys": ["alpha"]}, {"signal_keys": []},
    )
    assert valid is False
    assert "beta" in detail
    valid2, _ = lad.validate_arm(
        arm, {"signal_keys": ["alpha", "beta"]}, {"signal_keys": []},
    )
    assert valid2 is True


def test_signal_present_arm_with_no_baseline_is_not_silently_valid():
    # No control ran (baseline skipped/crashed) -> nothing to contrast
    # against. "Absent at baseline" must not be inferred from a missing
    # baseline, or a signal that was on all along reads as a clean enable.
    lad = _load_ladder()
    valid, detail = lad.validate_arm(
        _arm(lad, "rerank_on"), {"signal_keys": ["xenc_rerank"]}, None,
    )
    assert valid is False
    assert "unfalsifiable" in detail


# ── #341 Task 11: the rerank arms themselves ────────────────────────────


def test_rerank_arms_are_registered_and_resolve_on_a_fresh_config():
    lad = _load_ladder()
    from cymatix_context.config import CymatixConfig

    on_arm = _arm(lad, "rerank_on")
    off_arm = _arm(lad, "rerank_off")
    assert on_arm.knobs == {"retrieval.rerank_enabled": True}
    assert off_arm.knobs == {"retrieval.rerank_enabled": False}
    assert on_arm.validation == lad.VALIDATION_SIGNAL_PRESENT
    assert on_arm.expect_present == ("xenc_rerank",)
    assert off_arm.validation == lad.VALIDATION_SIGNAL
    assert off_arm.expect_absent == ("xenc_rerank",)
    assert "#341" in on_arm.gate

    cfg = CymatixConfig()
    assert lad.set_dotted(cfg, "retrieval.rerank_enabled", True) is True
    assert cfg.retrieval.rerank_enabled is True


def test_rerank_off_arm_is_identity_dropped_when_base_config_is_off():
    lad = _load_ladder()
    from cymatix_context.config import CymatixConfig

    cfg = CymatixConfig()          # ships rerank_enabled=False
    assert lad.arm_is_identity(cfg, _arm(lad, "rerank_off")) is not None
    assert lad.arm_is_identity(cfg, _arm(lad, "rerank_on")) is None
    cfg.retrieval.rerank_enabled = True
    assert lad.arm_is_identity(cfg, _arm(lad, "rerank_on")) is not None
    assert lad.arm_is_identity(cfg, _arm(lad, "rerank_off")) is None


# ── #341 Task 11: final-order (post-rerank) rank basis ──────────────────


def test_final_rank_of_first_gold_is_one_based_position():
    lad = _load_ladder()
    assert lad.final_rank_of_first_gold(["a", "b", "g1", "g2"], {"g1", "g2"}) == 3
    assert lad.final_rank_of_first_gold(["g1"], {"g1"}) == 1


def test_final_rank_of_first_gold_none_when_gold_absent_or_order_empty():
    lad = _load_ladder()
    assert lad.final_rank_of_first_gold(["a", "b"], {"g1"}) is None
    assert lad.final_rank_of_first_gold([], {"g1"}) is None
    assert lad.final_rank_of_first_gold(["a"], set()) is None


def test_final_order_fields_carry_hit_and_rerank_diag():
    lad = _load_ladder()
    out = lad.final_order_fields(["a", "g1"], {"g1"}, k=12,
                                 diag={"gate_kept": 8, "floor_appended": 2})
    assert out == {
        "final_rank_of_first_gold": 2, "final_recall_hit": 1,
        "gate_kept": 8, "floor_appended": 2,
    }


def test_final_order_fields_miss_beyond_k_and_absent_diag():
    lad = _load_ladder()
    out = lad.final_order_fields(["x"] * 20 + ["g1"], {"g1"}, k=12, diag=None)
    assert out["final_rank_of_first_gold"] == 21
    assert out["final_recall_hit"] == 0
    assert out["gate_kept"] is None and out["floor_appended"] is None


# ── #341 Task 11: rerank_ab.py pure helpers ─────────────────────────────


def _daemon_log(tmp_path: Path) -> Path:
    p = tmp_path / "run1_daemon.log"
    p.write_text(
        "INFO:     Started server process [1234]\n"
        'INFO:     127.0.0.1:5001 - "GET /ready HTTP/1.1" 200 OK\n'
        'INFO:     127.0.0.1:5001 - "POST /encode/dense HTTP/1.1" 200 OK\n'
        'INFO:     127.0.0.1:5001 - "POST /encode/rerank HTTP/1.1" 200 OK\n'
        'INFO:     127.0.0.1:5002 - "POST /encode/bundle HTTP/1.1" 200 OK\n'
        'INFO:     127.0.0.1:5002 - "POST /encode/rerank HTTP/1.1" 200 OK\n'
        "some unrelated traceback line mentioning rerank\n",
        encoding="utf-8",
    )
    return p


def test_count_daemon_posts_counts_only_matching_access_lines(tmp_path):
    ab = _load_rerank_ab()
    log = _daemon_log(tmp_path)
    assert ab.count_daemon_posts(log, "POST /encode/rerank") == 2
    assert ab.count_daemon_posts(log, "POST /encode/dense") == 1
    # The total marker spans every encode family (dense/bundle/rerank).
    assert ab.count_daemon_posts(log, ab.MARK_POST_ANY) == 4


def test_count_daemon_posts_ignores_non_post_and_prose_lines(tmp_path):
    ab = _load_rerank_ab()
    log = tmp_path / "empty_daemon.log"
    log.write_text(
        'INFO:     127.0.0.1:5001 - "GET /health HTTP/1.1" 200 OK\n'
        "rerank model loaded: BAAI/bge-reranker-base\n",
        encoding="utf-8",
    )
    assert ab.count_daemon_posts(log, "POST /encode/rerank") == 0
    assert ab.count_daemon_posts(log, ab.MARK_POST_ANY) == 0


def test_count_daemon_posts_missing_log_is_zero_not_a_crash(tmp_path):
    # A missing log means the daemon never wrote one; the caller's refusal
    # gate turns that 0 into a loud failure, so returning 0 is honest here.
    ab = _load_rerank_ab()
    assert ab.count_daemon_posts(tmp_path / "nope.log", "POST /encode/rerank") == 0


def test_run_plans_encode_the_order_reversal_and_direction_inversion():
    ab = _load_rerank_ab()
    run1, run2 = ab.RUN_PLANS
    assert (run1.run, run2.run) == (1, 2)
    # Run 1: shipped-OFF config, ON is the ablation arm.
    assert run1.arms == "rerank_on" and run1.on_side == "arm"
    assert run1.config_role == "off" and run1.delta_sign == 1
    # Run 2: ON is the ladder's baseline control; the paired delta inverts.
    assert run2.arms == "rerank_off" and run2.on_side == "baseline"
    assert run2.config_role == "on" and run2.delta_sign == -1


def test_expected_min_rerank_posts_includes_the_warmup_query():
    ab = _load_rerank_ab()
    assert ab.expected_min_rerank_posts(30) == 31
    assert ab.expected_min_rerank_posts(0) == 1


def _wrapper(ab, plan, **over):
    kwargs = dict(
        ladder_receipt_path="receipts/rerank_ab_run1_ladder.json",
        daemon_log="receipts/rerank_ab_run1_daemon.log",
        posts_rerank=31, posts_total=95, spawn_token_verified=True,
        n_queries=30, ladder_returncode=0, config="cymatix.toml", port=11439,
    )
    kwargs.update(over)
    return ab.build_wrapper(plan, **kwargs)


def _records(n, *, gate_kept=8):
    """n per-needle records, all rerank-eligible unless overridden."""
    return [{"needle": f"n{i}", "gate_kept": gate_kept, "floor_appended": 0}
            for i in range(n)]


def test_build_wrapper_passes_when_the_daemon_carried_every_query():
    ab = _load_rerank_ab()
    w = _wrapper(ab, ab.RUN_PLANS[0])
    assert w["daemon_log_posts_rerank"] == 31
    assert w["daemon_log_posts_total"] == 95
    assert w["daemon_log_posts_rerank_expected_min"] == 31
    assert w["daemon_proof_ok"] is True
    assert w["refusals"] == []
    assert w["spawn_token_verified"] is True
    assert w["on_side"] == "arm" and w["arm_order"] == ab.RUN_PLANS[0].arm_order


def test_build_wrapper_refuses_a_shortfall_as_silent_in_process_fallback():
    ab = _load_rerank_ab()
    w = _wrapper(ab, ab.RUN_PLANS[0], posts_rerank=30)
    assert w["daemon_proof_ok"] is False
    assert w["refusals"] and any("fallback" in r for r in w["refusals"])


def test_build_wrapper_refuses_on_unverified_token_bad_rc_or_unknown_n():
    ab = _load_rerank_ab()
    assert _wrapper(ab, ab.RUN_PLANS[0], spawn_token_verified=False)["daemon_proof_ok"] is False
    assert _wrapper(ab, ab.RUN_PLANS[0], ladder_returncode=2)["daemon_proof_ok"] is False
    unknown = _wrapper(ab, ab.RUN_PLANS[0], n_queries=None)
    assert unknown["daemon_proof_ok"] is False
    assert unknown["daemon_log_posts_rerank_expected_min"] is None


def test_build_wrapper_run2_carries_the_inverted_direction():
    ab = _load_rerank_ab()
    w = _wrapper(ab, ab.RUN_PLANS[1], config="cymatix_rerank_on.toml")
    assert w["run"] == 2
    assert w["on_side"] == "baseline"
    assert w["on_arm"] == "baseline" and w["off_arm"] == "rerank_off"
    assert w["delta_sign"] == -1
    assert w["daemon_proof_ok"] is True


# ── review fix 1: the ladder's own arm_valid verdict gates the proof ─────
#
# ablation_ladder exits 0 even when an arm fails self-validation, and a
# contaminated baseline that fires xenc_rerank makes rerank_on
# "unfalsifiable" while pushing the POST count UP — so the daemon floor
# holds and, without this, the wrapper would publish daemon_proof_ok on a
# measurement the ladder itself flagged as proving nothing.


def test_build_wrapper_refuses_when_the_ladder_invalidated_an_arm():
    ab = _load_rerank_ab()
    w = _wrapper(ab, ab.RUN_PLANS[0], invalid_arms=[
        {"arm": "rerank_on",
         "validation_detail": "unfalsifiable: ['xenc_rerank'] already present "
                              "in the baseline signal_keys"},
    ])
    assert w["daemon_proof_ok"] is False
    joined = " ".join(w["refusals"])
    assert "rerank_on" in joined and "unfalsifiable" in joined
    assert w["invalid_arms"][0]["arm"] == "rerank_on"


def test_invalid_arms_from_receipt_picks_only_false_verdicts():
    ab = _load_rerank_ab()
    payload = {"arms": [
        {"arm": "baseline", "arm_valid": True, "validation_detail": "control"},
        {"arm": "rerank_on", "arm_valid": False, "validation_detail": "unfalsifiable: x"},
        {"arm": "skipped_one", "arm_valid": None, "validation_detail": ""},
    ]}
    out = ab.invalid_arms_from_receipt(payload)
    assert [a["arm"] for a in out] == ["rerank_on"]
    assert out[0]["validation_detail"].startswith("unfalsifiable")
    assert ab.invalid_arms_from_receipt({}) == []
    assert ab.invalid_arms_from_receipt(None) == []


def test_arm_records_from_receipt_reads_the_on_side_arm():
    ab = _load_rerank_ab()
    payload = {"arms": [
        {"arm": "baseline", "per_query": [{"needle": "n0", "gate_kept": None}]},
        {"arm": "rerank_on", "per_query": [{"needle": "n0", "gate_kept": 9}]},
    ]}
    assert ab.arm_records_from_receipt(payload, "rerank_on")[0]["gate_kept"] == 9
    assert ab.arm_records_from_receipt(payload, "baseline")[0]["gate_kept"] is None
    # Absent arm / absent per_query / absent receipt -> None (fall back to n+1).
    assert ab.arm_records_from_receipt(payload, "nope") is None
    assert ab.arm_records_from_receipt({"arms": [{"arm": "x"}]}, "x") is None
    assert ab.arm_records_from_receipt(None, "x") is None


# ── review fix 3: the POST floor counts only rerank-ELIGIBLE needles ─────
#
# n_queries + 1 has zero margin and is ambiguous for a needle that
# legitimately never reaches the seam (no-match early return, abstain,
# empty pool). gate_kept is the discriminator the ladder receipt already
# carries: non-null iff query_docs_ann completed, 0 iff the pool was empty.


def test_rerank_eligible_needles_excludes_unreached_and_empty_pool():
    ab = _load_rerank_ab()
    records = [
        {"gate_kept": 12},      # eligible
        {"gate_kept": None},    # query never reached the ANN publication point
        {"gate_kept": 0},       # empty pool: score_pairs posts nothing
        {"gate_kept": 3},       # eligible
        {},                     # field absent entirely -> not eligible
    ]
    assert ab.rerank_eligible_needles(records) == 2


def test_expected_min_uses_eligible_count_when_records_are_present():
    ab = _load_rerank_ab()
    w = _wrapper(ab, ab.RUN_PLANS[0], posts_rerank=31,
                 on_arm_records=_records(30))
    assert w["daemon_log_posts_rerank_expected_min"] == 31
    assert w["daemon_proof_ok"] is True
    assert "gate_kept" in w["rerank_expected_basis"]


def test_a_needle_that_never_reached_the_seam_lowers_the_floor():
    # 30 needles, one of which never published a diag: 29 eligible + warmup.
    ab = _load_rerank_ab()
    recs = _records(29) + [{"needle": "n29", "gate_kept": None}]
    w = _wrapper(ab, ab.RUN_PLANS[0], posts_rerank=30, on_arm_records=recs)
    assert w["daemon_log_posts_rerank_expected_min"] == 30
    assert w["daemon_proof_ok"] is True, "a legitimate no-match must not refuse"


def test_an_empty_pool_needle_lowers_the_floor_too():
    ab = _load_rerank_ab()
    recs = _records(29) + [{"needle": "n29", "gate_kept": 0}]
    w = _wrapper(ab, ab.RUN_PLANS[0], posts_rerank=30, on_arm_records=recs)
    assert w["daemon_log_posts_rerank_expected_min"] == 30
    assert w["daemon_proof_ok"] is True


def test_a_genuine_shortfall_still_trips_the_eligible_floor():
    # 30 eligible needles, only 25 POSTs served: five encoded in-process.
    ab = _load_rerank_ab()
    w = _wrapper(ab, ab.RUN_PLANS[0], posts_rerank=25, on_arm_records=_records(30))
    assert w["daemon_log_posts_rerank_expected_min"] == 31
    assert w["daemon_proof_ok"] is False
    assert any("fallback" in r for r in w["refusals"])


def test_missing_per_query_records_fall_back_to_n_plus_one_with_a_note():
    ab = _load_rerank_ab()
    w = _wrapper(ab, ab.RUN_PLANS[0], posts_rerank=31, on_arm_records=None)
    assert w["daemon_log_posts_rerank_expected_min"] == 31
    assert "per-query records absent" in w["rerank_expected_basis"]


def test_wrapper_note_says_the_post_count_is_a_run_total():
    ab = _load_rerank_ab()
    note = _wrapper(ab, ab.RUN_PLANS[0])["note"]
    assert "run total" in note.lower()


def test_config_diff_paths_reports_every_difference_as_a_dotted_path():
    # The confound this guards: a hand-written "rerank on" toml that omits
    # [synonyms] does not inherit them from the OFF config — load_config
    # falls back to DATACLASS defaults, so run 2 would silently measure a
    # different retrieval system than run 1.
    ab = _load_rerank_ab()
    a = {"retrieval": {"rerank_enabled": False, "rerank_depth": 50},
         "synonym_map": {"cache": ["redis"]}}
    b = {"retrieval": {"rerank_enabled": True, "rerank_depth": 50},
         "synonym_map": {}}
    assert ab.config_diff_paths(a, b) == ["retrieval.rerank_enabled", "synonym_map.cache"]


def test_config_diff_paths_is_empty_for_identical_configs():
    ab = _load_rerank_ab()
    cfg = {"retrieval": {"rerank_enabled": False}, "budget": {"expression_tokens": 7000}}
    assert ab.config_diff_paths(cfg, dict(cfg)) == []


# ── review fix 2: a per-class override map defeats the global knob ──────
#
# [retrieval] rerank_enabled_by_class is live (context_manager ~:1939 ->
# _rerank_effective). Inherited IDENTICALLY by both cell configs it produces
# no config_delta_other entry at all, yet it makes the OFF side not-actually-
# off for the classes it names — an A/B whose control silently reranks.


def _write_toml(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_preflight_refuses_a_non_empty_per_class_rerank_map(tmp_path):
    ab = _load_rerank_ab()
    off = _write_toml(tmp_path / "off.toml",
                      "[retrieval]\nrerank_enabled = false\n"
                      "rerank_enabled_by_class = { factual = true }\n")
    on = _write_toml(tmp_path / "on.toml", "[retrieval]\nrerank_enabled = true\n")
    fatal, _ = ab.preflight_configs(off, on)
    joined = " ".join(fatal)
    assert "rerank_enabled_by_class" in joined
    assert "--config-off" in joined


def test_preflight_refuses_the_per_class_map_on_the_on_side_too(tmp_path):
    ab = _load_rerank_ab()
    off = _write_toml(tmp_path / "off.toml", "[retrieval]\nrerank_enabled = false\n")
    on = _write_toml(tmp_path / "on.toml",
                     "[retrieval]\nrerank_enabled = true\n"
                     "rerank_enabled_by_class = { default = false }\n")
    fatal, _ = ab.preflight_configs(off, on)
    assert any("rerank_enabled_by_class" in f and "--config-on" in f for f in fatal)


def test_preflight_accepts_empty_per_class_maps(tmp_path):
    ab = _load_rerank_ab()
    off = _write_toml(tmp_path / "off.toml", "[retrieval]\nrerank_enabled = false\n")
    on = _write_toml(tmp_path / "on.toml", "[retrieval]\nrerank_enabled = true\n")
    fatal, deltas = ab.preflight_configs(off, on)
    assert fatal == []
    assert deltas == []   # identical files apart from the one knob


def test_build_wrapper_records_non_rerank_config_deltas():
    ab = _load_rerank_ab()
    w = _wrapper(ab, ab.RUN_PLANS[0], config_delta_other=["synonym_map.cache"])
    assert w["config_delta_other"] == ["synonym_map.cache"]
    # A delta is a disclosure, not a refusal: only the operator can say
    # whether it is legitimate.
    assert w["daemon_proof_ok"] is True


# ── review fix 4: pin the ladder subprocess to THIS worktree ────────────
#
# Campaign trap #1. The editable install is a plain-path .pth pointing at
# the MASTER checkout, so sys.path ORDER decides which cymatix_context a
# child process imports. ablation_ladder.py's own prologue happens to
# insert its REPO_ROOT at sys.path[0] (verified: it resolves to the
# worktree even with PYTHONPATH unset), but the RUNNER must not depend on
# the callee's hygiene — a refactor that drops that prologue would silently
# run the whole A/B against master, which has no rerank seams at all.


def test_ladder_env_pins_pythonpath_to_the_worktree_when_absent():
    ab = _load_rerank_ab()
    out = ab.ladder_env({}, 11439)
    assert out["PYTHONPATH"] == str(ab.REPO_ROOT)


def test_ladder_env_prepends_the_worktree_to_an_existing_pythonpath():
    import os

    ab = _load_rerank_ab()
    out = ab.ladder_env({"PYTHONPATH": "C:/other/lib"}, 11439)
    assert out["PYTHONPATH"] == str(ab.REPO_ROOT) + os.pathsep + "C:/other/lib"


def test_ladder_env_does_not_double_prepend_an_already_pinned_path():
    import os

    ab = _load_rerank_ab()
    existing = str(ab.REPO_ROOT) + os.pathsep + "C:/other/lib"
    assert ab.ladder_env({"PYTHONPATH": existing}, 11439)["PYTHONPATH"] == existing


def test_ladder_env_sets_the_encoder_url_and_leaves_the_base_env_alone():
    ab = _load_rerank_ab()
    base = {"EXISTING": "keep"}
    out = ab.ladder_env(base, 11439)
    assert out["CYMATIX_ENCODER_URL"] == "http://127.0.0.1:11439"
    assert out["EXISTING"] == "keep"
    assert base == {"EXISTING": "keep"}, "the caller's env must not be mutated"


def test_rerank_ab_arg_parser_exposes_the_documented_flags():
    ab = _load_rerank_ab()
    parser = ab.build_arg_parser()
    flags = {opt for action in parser._actions for opt in action.option_strings}
    assert {
        "--genome", "--resolved", "--gold", "--limit", "--k",
        "--config-off", "--config-on", "--port", "--daemon-python",
        "--out-prefix",
    } <= flags
    args = parser.parse_args([])
    assert args.port == 11439
    assert args.config_off and args.config_on
