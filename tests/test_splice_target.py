"""Splice-floor fix (J-space roadmap council kill-switch #1).

The Step-4 splice loop capped every candidate at a query-agnostic
``target = 1000`` chars (context_manager.py) and the headroom-unavailable
fallback truncated with a blind prefix cut ``content[:target].strip()``
(encoding/headroom_bridge.py). Any answer past char 1000 of its document
was silently cut — 6/50 SIKE xl needles (helix_port, scorerift_threshold,
bookkeeper_1099_threshold, bookkeeper_test_count,
bookkeeper_backup_interval, cosmictasha_auth_library) had gold delivered
but the answer truncated away.

Fix under test:
1. ``[budget] splice_target_chars`` knob — 0 (default) sizes the per-doc
   target from the expression budget (budget-proportional); an explicit
   positive value pins a fixed target (1000 == the exact legacy floor).
2. Query-aware trimming in the fallback truncation: when a cut is
   required, lines containing query terms that would be lost to the
   prefix cut are retained within the same char budget.
"""

from __future__ import annotations

import pytest

from cymatix_context.config import BudgetConfig, load_config
from cymatix_context.context_manager import _compute_splice_target
from cymatix_context.encoding.headroom_bridge import (
    _query_aware_trim,
    compress_text,
)


# ── config knob ──────────────────────────────────────────────────────


def test_budget_config_default_is_auto():
    """0 = budget-proportional auto (the new default behavior)."""
    assert BudgetConfig().splice_target_chars == 0


def test_toml_loader_plumbs_splice_target_chars(tmp_path):
    cfg_file = tmp_path / "helix.toml"
    cfg_file.write_text("[budget]\nsplice_target_chars = 1500\n")
    cfg = load_config(str(cfg_file))
    assert cfg.budget.splice_target_chars == 1500


# ── target sizing ────────────────────────────────────────────────────


def test_explicit_target_pins_fixed_behavior():
    """splice_target_chars = 1000 restores the exact legacy floor."""
    assert _compute_splice_target(1000, expression_tokens=7000, n_candidates=12) == 1000
    assert _compute_splice_target(2500, expression_tokens=7000, n_candidates=1) == 2500


def test_auto_target_is_budget_proportional():
    """0 → distribute the expression char budget across candidates:
    int(expression_tokens · 4 chars/token · 0.9 safety) // n, floored at
    the legacy 1000 so no candidate ever gets LESS than before."""
    # 7000 tokens → 25200 chars; 12 candidates → 2100 each.
    assert _compute_splice_target(0, expression_tokens=7000, n_candidates=12) == 2100
    # Single candidate gets the whole budget.
    assert _compute_splice_target(0, expression_tokens=7000, n_candidates=1) == 25200
    # Many candidates → floor at the legacy 1000 (never worse than today).
    assert _compute_splice_target(0, expression_tokens=7000, n_candidates=100) == 1000
    # Degenerate n=0 guarded.
    assert _compute_splice_target(0, expression_tokens=7000, n_candidates=0) == 25200


# ── query-aware trim ─────────────────────────────────────────────────


FILLER_LINE = "unrelated configuration commentary line with no signal\n"


def _doc_with_deep_answer(prefix_lines: int = 40) -> str:
    """A document whose answer line sits well past char 1000."""
    return (FILLER_LINE * prefix_lines) + "server_port = 11437\n" + (FILLER_LINE * 10)


def test_trim_without_terms_is_legacy_prefix_cut():
    content = _doc_with_deep_answer()
    assert _query_aware_trim(content, 1000, []) == content[:1000].strip()


def test_trim_with_terms_already_in_prefix_is_legacy_prefix_cut():
    content = "server_port = 11437\n" + FILLER_LINE * 60
    out = _query_aware_trim(content, 1000, ["server_port"])
    assert out == content[:1000].strip()


def test_trim_retains_query_term_line_beyond_cut():
    content = _doc_with_deep_answer()
    assert "11437" not in content[:1000]  # the cut would lose the answer
    out = _query_aware_trim(content, 1000, ["server_port"])
    assert "server_port = 11437" in out
    assert len(out) <= 1000


def test_trim_with_absent_terms_is_legacy_prefix_cut():
    content = _doc_with_deep_answer()
    out = _query_aware_trim(content, 1000, ["kubernetes"])
    assert out == content[:1000].strip()


def test_trim_keeps_multiple_matching_lines_in_order():
    content = (
        FILLER_LINE * 40
        + "alpha_threshold = 0.15\n"
        + FILLER_LINE * 5
        + "beta_threshold = 0.30\n"
    )
    out = _query_aware_trim(content, 1200, ["threshold"])
    assert "alpha_threshold = 0.15" in out
    assert "beta_threshold = 0.30" in out
    assert out.index("alpha_threshold") < out.index("beta_threshold")
    assert len(out) <= 1200


def test_compress_text_fallback_is_query_aware():
    """compress_text (headroom unavailable here) threads query_terms
    through to the fallback trim."""
    content = _doc_with_deep_answer()
    out = compress_text(content, target_chars=1000, query_terms=["server_port"])
    assert "11437" in out
    # And stays backward-compatible when no terms are supplied.
    legacy = compress_text(content, target_chars=1000)
    assert legacy == content[:1000].strip()


# ── end-to-end through the splice loop ───────────────────────────────


def _stub_express(manager, *, candidates, scores):
    def fake_express(domains, entities, max_genes, **_kwargs):
        manager.genome.last_query_scores = dict(scores)
        return list(candidates)

    manager._retrieve = fake_express
    manager._express = fake_express

    def fake_refiners(query, candidates, max_genes, **_kwargs):
        return list(candidates), {}

    manager._apply_candidate_refiners = fake_refiners


def _make_manager(gene_contents, scores, budget_kwargs=None):
    from cymatix_context.config import (
        BudgetConfig,
        ClassifierConfig,
        GenomeConfig,
        HelixConfig,
        RibosomeConfig,
    )
    from cymatix_context.context_manager import HelixContextManager
    from tests.conftest import make_gene

    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(
            max_genes_per_turn=len(gene_contents) + 2,
            abstain_enabled=True,
            **(budget_kwargs or {}),
        ),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=False),
    )
    mgr = HelixContextManager(cfg)
    candidates = [
        make_gene(content, gene_id=f"gene_{i:02d}")
        for i, content in enumerate(gene_contents)
    ]
    score_map = {c.gene_id: scores[i] for i, c in enumerate(candidates)}
    _stub_express(mgr, candidates=candidates, scores=score_map)
    return mgr


def test_build_context_fixed_target_keeps_answer_via_query_terms():
    """Legacy-size target (1000) + query terms → the deep answer line
    survives the splice truncation end-to-end."""
    deep_doc = _doc_with_deep_answer()
    mgr = _make_manager(
        [deep_doc, "short unrelated doc"],
        scores=[10.0, 9.0],  # BROAD under both fusion gate scales
        budget_kwargs={"splice_target_chars": 1000},
    )
    try:
        window = mgr.build_context("what server_port does the proxy listen on")
        assert "11437" in window.expressed_context
    finally:
        mgr.close()


def test_build_context_auto_target_lifts_the_floor():
    """Default auto sizing gives the deep doc enough room that no cut is
    needed at all (2 candidates share a 7000-token budget)."""
    deep_doc = _doc_with_deep_answer()
    mgr = _make_manager(
        [deep_doc, "short unrelated doc"],
        scores=[10.0, 9.0],
    )
    try:
        window = mgr.build_context("completely unrelated question")
        assert "11437" in window.expressed_context
    finally:
        mgr.close()
