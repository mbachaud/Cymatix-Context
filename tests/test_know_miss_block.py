"""Stage 6 — machine-tagged know/miss contract tests.

Spec: docs/specs/2026-05-08-stage-6-know-miss-blocks.md §10, §13.

All tests are mock-only — no Ollama, no sklearn fit. The discriminator
uses default coefficients from helix_context.know_calibration; the
calibration script has its own smoke test in test_calibration_script.

# STAGE-7-EXT: this file pre-declares the freshness-related test
# cases as parametrize markers so Stage 7 can flesh them out without
# having to relocate fixtures.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from helix_context import context_manager as cm
from helix_context.agent_prompt import HELIX_NO_MATCH_FRAGMENT
from helix_context.context_packet import _attach_know_or_miss, build_context_packet
from helix_context.scoring.know_calibration import (
    DEFAULT_BETAS,
    DEFAULT_EMIT_FLOOR,
    KnowCalibration,
    compute_confidence,
    fit_betas_from_features,
    load_calibration_from_toml,
)
from helix_context.scoring import know_decision as know_decision_module
from helix_context.scoring.know_decision import (
    _agree_from_tier_contributions,
    _gene_id_beacon,
    _is_code_shaped,
    _pick_escalation,
    decide_know_or_miss,
)
from helix_context.schemas import (
    ContextHealth,
    ContextPacket,
    ContextResponseEnvelope,
    ContextWindow,
    EpigeneticMarkers,
    ESCALATE_TARGETS,
    Gene,
    KnowBlock,
    MISS_REASONS,
    MissBlock,
    PromoterTags,
)


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_gene():
    """A Gene with a path that names the file the query targets."""
    return Gene(
        gene_id="g_ctxmgr",
        sequence="def build_context(): pass",
        content="def build_context(): pass",
        complement="dummy complement",
        codons=[],
        promoter=PromoterTags(),
        epigenetics=EpigeneticMarkers(created_at=0.0),
        source_id="F:/Projects/helix-context/helix_context/context_manager.py",
    )


@pytest.fixture
def healthy_window():
    """ContextWindow shaped like a successful retrieval."""
    return ContextWindow(
        ribosome_prompt="",
        expressed_context="(genes here)",
        expressed_gene_ids=["g_ctxmgr"],
        context_health=ContextHealth(
            ellipticity=0.95,
            coverage=0.7,
            density=0.8,
            freshness=1.0,
            genes_available=100,
            genes_expressed=4,
            status="aligned",
        ),
    )


@pytest.fixture
def abstain_window():
    """ContextWindow shaped like the ABSTAIN-tier branch."""
    return ContextWindow(
        ribosome_prompt="",
        expressed_context=cm._no_match_token("abstain"),
        expressed_gene_ids=[],
        context_health=ContextHealth(
            ellipticity=0.0,
            coverage=0.0,
            density=0.0,
            freshness=0.0,
            genes_available=100,
            genes_expressed=0,
            status="abstain",
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# §10 case 1: know block on found + high confidence
# ─────────────────────────────────────────────────────────────────────

def test_know_block_emitted_when_found_and_high_confidence(
    healthy_window, fake_gene
):
    out = decide_know_or_miss(
        window=healthy_window,
        query="what does context_manager do",
        top_score=2.5,
        score_gap=1.0,
        lexical_dense_agree=True,
        coordinate_confidence=0.85,
        top_gene=fake_gene,
    )
    assert isinstance(out, KnowBlock)
    assert out.found is True
    assert out.confidence > 0.7, f"got {out.confidence}"
    # Beacon is present (filename-token match wins).
    assert out.gene_id_match in ("context", "manager", "context_manager")
    # Envelope invariant: only know is set.
    env = ContextResponseEnvelope(know=out)
    assert env.miss is None and env.know is out


# ─────────────────────────────────────────────────────────────────────
# §10 case 2: miss block on abstain
# ─────────────────────────────────────────────────────────────────────

def test_miss_block_emitted_on_abstain(abstain_window):
    out = decide_know_or_miss(
        window=abstain_window,
        query="totally novel question",
        top_score=0.05,
        score_gap=0.01,
        lexical_dense_agree=False,
        coordinate_confidence=0.0,
    )
    assert isinstance(out, MissBlock)
    assert out.reason == "abstain"
    assert out.do_not_answer_from_genome is True
    assert len(out.escalate_to) >= 1
    # Each escalate target must be in the canonical set.
    for tool in out.escalate_to:
        assert tool in ESCALATE_TARGETS


# ─────────────────────────────────────────────────────────────────────
# §10 case 3: gene_id_match beacon — exact match only
# ─────────────────────────────────────────────────────────────────────

class _SyntheticGene:
    __slots__ = ("gene_id", "source_id")

    def __init__(self, gid, sid):
        self.gene_id = gid
        self.source_id = sid


def _make_packet_gene(content: str, domain: str, *, gene_id: str | None = None):
    """A complete Gene for end-to-end build_context_packet tests.

    build_context_packet reads gene-local metadata (content, epigenetics,
    promoter, source_id) so a slots-only stub like _SyntheticGene is not
    enough — we reuse conftest.make_gene and pin a source_id so the
    coordinate signals resolve.
    """
    from tests.conftest import make_gene

    gene = make_gene(content, domains=[domain], gene_id=gene_id)
    gene.source_id = f"F:/Projects/helix-context/helix_context/{domain}.py"
    gene.source_kind = "code"
    gene.volatility_class = "stable"
    gene.authority_class = "primary"
    return gene


def test_gene_id_match_beacon_only_on_exact_filename_match():
    g = _SyntheticGene(
        "g1", "F:/Projects/helix-context/helix_context/context_manager.py"
    )
    # Filename match — wins.
    assert _gene_id_beacon("context_manager", g) in ("context", "manager", "context_manager")
    # Substring of a folder token — must NOT match.
    assert _gene_id_beacon("manage", g) is None
    # Short folder-only token (length < 4) — blocked.
    assert _gene_id_beacon("src", g) is None
    # No source_id — None.
    g2 = _SyntheticGene("g2", None)
    assert _gene_id_beacon("anything", g2) is None
    # Top-1 gene None — None.
    assert _gene_id_beacon("anything", None) is None


# ─────────────────────────────────────────────────────────────────────
# §10 case 4: expressed_context carries the no_match tag on miss
# ─────────────────────────────────────────────────────────────────────

def test_expressed_context_has_no_match_tag_on_miss():
    # Exact byte-equality assertion (§6).
    assert (
        cm._no_match_token("abstain")
        == '<helix:no_match reason="abstain" do_not_answer="true"/>'
    )
    assert (
        cm._no_match_token("denatured")
        == '<helix:no_match reason="denatured" do_not_answer="true"/>'
    )
    assert (
        cm._no_match_token("sparse")
        == '<helix:no_match reason="sparse" do_not_answer="true"/>'
    )
    assert (
        cm._no_match_token("no_promoter_match")
        == '<helix:no_match reason="no_promoter_match" do_not_answer="true"/>'
    )
    # The deprecated alias points at the abstain form.
    assert cm._ABSTAIN_MARKER == cm._no_match_token("abstain")


# ─────────────────────────────────────────────────────────────────────
# §10 case 5: agent.recommendation = escalate on code-shaped miss
# ─────────────────────────────────────────────────────────────────────

def test_agent_recommendation_escalate_on_code_shaped_miss():
    # Code-shaped queries pick (grep, rag) regardless of reason.
    assert _pick_escalation("def parse_promoter()", "sparse") == ["grep", "rag"]
    assert _pick_escalation("helix_context.config.HelixConfig", "no_promoter_match") == ["grep", "rag"]
    # Code shape detection itself
    assert _is_code_shaped("def foo(): pass")
    assert _is_code_shaped("module.submodule.fn")
    assert not _is_code_shaped("what is helix philosophy")


# ─────────────────────────────────────────────────────────────────────
# §10 case 6: envelope rejects both blocks set
# ─────────────────────────────────────────────────────────────────────

def test_envelope_rejects_both_blocks_set():
    kb = KnowBlock(
        confidence=0.8,
        top_score=1.0,
        score_gap=0.5,
        lexical_dense_agree=True,
        coordinate_confidence=0.7,
    )
    mb = MissBlock(
        reason="abstain",
        top_score=0.0,
        ratio=0.0,
        escalate_to=["rag"],
    )
    with pytest.raises(ValidationError):
        ContextResponseEnvelope(know=kb, miss=mb)
    with pytest.raises(ValidationError):
        ContextResponseEnvelope()  # neither set


# ─────────────────────────────────────────────────────────────────────
# §10 case 7: confidence below floor → MissBlock(reason="sparse")
# ─────────────────────────────────────────────────────────────────────

def test_below_floor_becomes_sparse_miss(healthy_window):
    # Inputs that the default logistic should map below 0.55 (the
    # default emit_floor): low scores, no agreement, no coord conf.
    out = decide_know_or_miss(
        window=healthy_window,
        query="something obscure",
        top_score=0.05,
        score_gap=0.01,
        lexical_dense_agree=False,
        coordinate_confidence=0.0,
    )
    assert isinstance(out, MissBlock), f"got {type(out).__name__}"
    assert out.reason == "sparse"
    assert len(out.escalate_to) >= 1


# ─────────────────────────────────────────────────────────────────────
# Calibration logistic: defaults yield well-separated probabilities
# ─────────────────────────────────────────────────────────────────────

def test_default_logistic_separates_high_low_signal():
    high = compute_confidence(
        top_score=2.5,
        score_gap=1.0,
        lexical_dense_agree=True,
        coordinate_confidence=0.8,
    )
    low = compute_confidence(
        top_score=0.05,
        score_gap=0.01,
        lexical_dense_agree=False,
        coordinate_confidence=0.0,
    )
    assert high > 0.7
    assert low < DEFAULT_EMIT_FLOOR


def test_calibration_load_falls_back_to_defaults_on_missing_toml(tmp_path):
    cal = load_calibration_from_toml(tmp_path / "no_such_file.toml")
    assert cal.betas == DEFAULT_BETAS
    assert cal.emit_floor == DEFAULT_EMIT_FLOOR


def test_calibration_load_falls_back_on_malformed_betas(tmp_path):
    bad_toml = tmp_path / "helix.toml"
    bad_toml.write_text(
        '[know]\nbetas = ["nope"]\nemit_floor = 0.55\n', encoding="utf-8"
    )
    cal = load_calibration_from_toml(bad_toml)
    assert cal.betas == DEFAULT_BETAS  # silent fallback


def test_fit_betas_separates_synthetic_data():
    # Stage 7 (2026-05-08): N_FEATURES bumped from 4 to 5 with the
    # addition of freshness_min as feature index 4. The synthetic
    # row vectors below carry the same separability shape across all
    # five features so the gradient descent's expected sign pattern
    # (intercept negative, all coefficients positive) still holds.
    feats = (
        [[0.95, 0.95, 1.0, 0.9, 0.95]] * 30  # positives — fresh
        + [[0.05, 0.01, 0.0, 0.0, 0.05]] * 30  # negatives — stale
    )
    labels = [1] * 30 + [0] * 30
    betas = fit_betas_from_features(feats, labels, lr=0.5, epochs=200)
    # Intercept negative; positive feature coefficients positive.
    assert betas[0] < 0  # intercept
    assert betas[1] > 0
    assert betas[2] > 0
    assert betas[3] > 0
    assert betas[4] > 0
    assert betas[5] > 0  # Stage 7 — β5 (freshness_min)


# ─────────────────────────────────────────────────────────────────────
# MissBlock validation — extension-friendly reason vocabulary
# ─────────────────────────────────────────────────────────────────────

def test_miss_block_rejects_unknown_reason():
    with pytest.raises(ValidationError):
        MissBlock(
            reason="unknown_reason_value",
            top_score=0.0,
            ratio=0.0,
            escalate_to=["rag"],
        )


def test_miss_block_rejects_unknown_escalate_target():
    with pytest.raises(ValidationError):
        MissBlock(
            reason="abstain",
            top_score=0.0,
            ratio=0.0,
            escalate_to=["nonexistent_tool"],
        )


def test_miss_reasons_extension_point_documented():
    """Sanity check: the reason vocabulary lives in MISS_REASONS so
    Stage 7 can extend it with one tuple-append. If anyone hard-codes
    the reason set elsewhere, this test stays green by accident, so
    treat it as a smoke check on the symbol's existence."""
    assert "abstain" in MISS_REASONS
    assert "denatured" in MISS_REASONS
    assert "sparse" in MISS_REASONS
    assert "no_promoter_match" in MISS_REASONS
    # STAGE-7-EXT: stale | cold | superseded will join here.


# ─────────────────────────────────────────────────────────────────────
# Lex/dense agreement helper
# ─────────────────────────────────────────────────────────────────────

def test_agree_from_tier_contributions():
    # gene g1 wins both clusters → agree.
    contribs = {
        "g1": {"fts5": 1.0, "splade": 0.8},
        "g2": {"fts5": 0.5},
        "g3": {"sema_boost": 0.3},
    }
    assert _agree_from_tier_contributions(contribs, k=3) is True

    # Only lexical wins, no dense candidate → no agree.
    contribs2 = {
        "g1": {"fts5": 1.0},
        "g2": {"fts5": 0.5},
    }
    assert _agree_from_tier_contributions(contribs2, k=3) is False

    # Empty → False (safe default).
    assert _agree_from_tier_contributions({}, k=3) is False
    assert _agree_from_tier_contributions(None, k=3) is False


def test_agree_from_tier_contributions_dense_tier_bucketed():
    """Regression for the BGE-M3 dense-tier bucket gap (follow-up to
    PR #135). ``knowledge_store.py`` writes the dense-cosine recall
    score under the tier name ``"dense"`` (blob path ~L1939), and
    ``shard_router.py`` re-publishes that map verbatim (~L508/L613), so
    sharded mode emits the same name. ``_DENSE_TIERS`` must contain
    ``"dense"`` or ``lexical_dense_agree`` can never fire for a BGE-M3
    dense retrieval — it could only agree via SPLADE/SEMA.
    """
    # g1 tops the lexical ranker (fts5) AND the dense ranker (dense).
    # The intersection of the per-ranker top-K is non-empty → agree.
    contribs = {
        "g1": {"fts5": 1.0, "dense": 0.82},
        "g2": {"fts5": 0.5},
        "g3": {"dense": 0.40},
    }
    assert _agree_from_tier_contributions(contribs, k=3) is True

    # Pin the failure direction: this same map yields False against the
    # pre-fix dense-tier set (which omitted "dense"). We simulate the
    # old set rather than re-import a frozen copy so the assertion
    # documents exactly which name closed the gap.
    pre_fix_dense_tiers = frozenset({"splade", "sema_boost", "sema_cold"})
    lex_tiers = know_decision_module._LEXICAL_TIERS

    def _agree_with(dense_tiers, tier_contributions, k=3):
        lex_scores, dense_scores = [], []
        for gid, tier_map in tier_contributions.items():
            lex = sum(float(tier_map.get(t, 0.0)) for t in lex_tiers)
            dense = sum(float(tier_map.get(t, 0.0)) for t in dense_tiers)
            if lex > 0:
                lex_scores.append((gid, lex))
            if dense > 0:
                dense_scores.append((gid, dense))
        lex_scores.sort(key=lambda kv: kv[1], reverse=True)
        dense_scores.sort(key=lambda kv: kv[1], reverse=True)
        lex_top = {gid for gid, _ in lex_scores[:k]}
        dense_top = {gid for gid, _ in dense_scores[:k]}
        return bool(lex_top & dense_top)

    # Pre-fix set: "dense" is unrecognized → dense ranker is empty →
    # no intersection → the signal silently never fires.
    assert _agree_with(pre_fix_dense_tiers, contribs) is False
    # Post-fix set (the live one) agrees, confirming the fix is what
    # closes the gap.
    assert _agree_with(know_decision_module._DENSE_TIERS, contribs) is True


# ─────────────────────────────────────────────────────────────────────
# Acceptance §13: synthetic round-trip — ALL miss rows have escalate_to,
# ALL know rows have confidence > 0.7, envelope never raises.
# ─────────────────────────────────────────────────────────────────────

def test_acceptance_synthetic_round_trip(healthy_window, abstain_window, fake_gene):
    # Build a fixture of N=20 known-good and N=20 known-miss rows.
    success_rows = [
        dict(
            window=healthy_window,
            query="context_manager " + str(i),
            top_score=2.0 + (i % 3) * 0.3,
            score_gap=0.7 + (i % 4) * 0.05,
            lexical_dense_agree=True,
            coordinate_confidence=0.7 + (i % 3) * 0.05,
            top_gene=fake_gene,
        )
        for i in range(20)
    ]
    miss_rows = [
        dict(
            window=abstain_window,
            query="totally novel " + str(i),
            top_score=0.05,
            score_gap=0.005,
            lexical_dense_agree=False,
            coordinate_confidence=0.0,
        )
        for i in range(20)
    ]

    n_know_high_conf = 0
    n_miss_with_escalate = 0
    for row in success_rows:
        out = decide_know_or_miss(**row)
        assert isinstance(out, KnowBlock), f"got {type(out).__name__}"
        if out.confidence > 0.7:
            n_know_high_conf += 1
        # Envelope must accept.
        env = ContextResponseEnvelope(know=out)
        assert env.miss is None
    for row in miss_rows:
        out = decide_know_or_miss(**row)
        assert isinstance(out, MissBlock), f"got {type(out).__name__}"
        if len(out.escalate_to) >= 1:
            n_miss_with_escalate += 1
        env = ContextResponseEnvelope(miss=out)
        assert env.know is None

    # §13(a): >= 99% of success rows have confidence > 0.7
    assert n_know_high_conf == len(success_rows), (
        f"only {n_know_high_conf}/{len(success_rows)} success rows had "
        f"confidence > 0.7"
    )
    # §13(b): 100% of miss rows have len(escalate_to) >= 1
    assert n_miss_with_escalate == len(miss_rows)


# ─────────────────────────────────────────────────────────────────────
# Calibration script smoke (Task D §11)
# ─────────────────────────────────────────────────────────────────────

def test_calibration_script_smoke_runs():
    """The script's --smoke mode must wire together with sklearn or the
    pure-Python fitter, fit a separable synthetic fixture, and exit 0."""
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "calibrate_know_confidence.py"
    assert script.exists()
    proc = subprocess.run(
        [sys.executable, str(script), "--input", "/dev/null", "--smoke"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"smoke run failed: stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "separation OK" in proc.stderr or "separation OK" in proc.stdout


# ─────────────────────────────────────────────────────────────────────
# Agent-prompt fragment exposed
# ─────────────────────────────────────────────────────────────────────

def test_helix_no_match_fragment_constant():
    assert isinstance(HELIX_NO_MATCH_FRAGMENT, str)
    assert "do_not_answer_from_genome" in HELIX_NO_MATCH_FRAGMENT
    assert "<helix:no_match" in HELIX_NO_MATCH_FRAGMENT
    assert "scored as a hard failure" in HELIX_NO_MATCH_FRAGMENT


# ─────────────────────────────────────────────────────────────────────
# Packet-side integration: _attach_know_or_miss
# ─────────────────────────────────────────────────────────────────────

def test_packet_attach_know_on_strong_signal(default_calibration):
    g = _SyntheticGene(
        "g1", "F:/Projects/helix-context/helix_context/context_manager.py"
    )
    p = ContextPacket(task_type="explain", query="context_manager")
    _attach_know_or_miss(
        p,
        query="context_manager",
        genes=[g],
        score_map={"g1": 2.5, "g2": 1.0},
        coordinate_confidence=0.8,
    )
    assert p.know is not None
    assert p.miss is None
    assert p.know.confidence > 0.7


def test_packet_attach_miss_on_no_genes():
    p = ContextPacket(task_type="explain", query="nothing")
    _attach_know_or_miss(
        p,
        query="nothing",
        genes=[],
        score_map={},
        coordinate_confidence=0.0,
    )
    assert p.miss is not None
    assert p.know is None
    assert p.miss.reason == "no_promoter_match"
    assert len(p.miss.escalate_to) >= 1


# ─────────────────────────────────────────────────────────────────────
# Packet-side: lexical_dense_agree must reflect the tier contributions
#
# Regression for the 2026-05-16 pipeline deep review, finding #3
# (docs/reviews/2026-05-16-deep-review/pipeline-03-scoring-conflict.md).
# _attach_know_or_miss used to hard-code ``tier_contrib = {}``, which
# pinned lexical_dense_agree to False for every /context/packet block.
# build_context_packet now plumbs genome.last_tier_contributions through
# the ``tier_contributions`` kwarg; these tests pin both directions.
# ─────────────────────────────────────────────────────────────────────

def test_packet_attach_lexical_dense_agree_true_on_agreeing_tiers(
    default_calibration,
):
    """Lexical (fts5/tag_exact) + dense (splade/sema_boost) tiers that
    both rank the same gene_id at the top must yield
    KnowBlock.lexical_dense_agree=True."""
    g = _SyntheticGene(
        "g1", "F:/Projects/helix-context/helix_context/context_manager.py"
    )
    # g1 wins both the lexical and the dense ranker -> intersection
    # non-empty -> lexical_dense_agree must be True.
    tier_contributions = {
        "g1": {"fts5": 0.9, "tag_exact": 0.8, "splade": 0.7, "sema_boost": 0.6},
        "g2": {"fts5": 0.1, "splade": 0.1},
    }
    p = ContextPacket(task_type="explain", query="context_manager")
    _attach_know_or_miss(
        p,
        query="context_manager",
        genes=[g],
        score_map={"g1": 2.5, "g2": 1.0},
        coordinate_confidence=0.8,
        tier_contributions=tier_contributions,
    )
    assert p.know is not None
    assert p.miss is None
    assert p.know.lexical_dense_agree is True


@pytest.fixture
def default_calibration(monkeypatch):
    """Pin decide_know_or_miss to the DEFAULT calibration for tests that
    assert lexical_dense_agree PLUMBING via the know-block shape. Since
    the 2026-07-06 first real fit, helix.toml [know] carries fitted
    betas + a deliberately conservative emit_floor under which these
    synthetic agree=False scenarios correctly land below the floor
    (MissBlock instead of KnowBlock). The plumbing contract under test
    is calibration-independent, so pin defaults rather than track
    whatever the shipped calibration currently is.
    """
    from helix_context.scoring import know_calibration as kc

    monkeypatch.setattr(
        kc, "load_calibration_from_toml", lambda *a, **k: kc.KnowCalibration()
    )


def test_packet_attach_lexical_dense_agree_false_on_disjoint_tiers(
    default_calibration,
):
    """When the lexical ranker tops g1 but the dense ranker tops g2
    (no top-K intersection), lexical_dense_agree must be False — the
    safe no-agreement direction. Pins the negative case so the
    plumbing fix can't regress into a false-positive boost."""
    g = _SyntheticGene(
        "g1", "F:/Projects/helix-context/helix_context/context_manager.py"
    )
    g2 = _SyntheticGene(
        "g2", "F:/Projects/helix-context/helix_context/codons.py"
    )
    # Lexical tiers favour g1; dense tiers favour g2. Disjoint top-K.
    tier_contributions = {
        "g1": {"fts5": 0.9, "tag_exact": 0.8},
        "g2": {"splade": 0.9, "sema_boost": 0.8},
    }
    p = ContextPacket(task_type="explain", query="context_manager")
    _attach_know_or_miss(
        p,
        query="context_manager",
        genes=[g, g2],
        score_map={"g1": 2.5, "g2": 1.0},
        coordinate_confidence=0.8,
        tier_contributions=tier_contributions,
    )
    assert p.know is not None
    assert p.miss is None
    assert p.know.lexical_dense_agree is False


def test_packet_attach_lexical_dense_agree_false_when_no_tiers_passed(
    default_calibration,
):
    """Back-compat: a caller that passes nothing for tier_contributions
    still works and lands on the safe False direction (no KeyError, no
    crash) — guards the older direct-caller path."""
    g = _SyntheticGene(
        "g1", "F:/Projects/helix-context/helix_context/context_manager.py"
    )
    p = ContextPacket(task_type="explain", query="context_manager")
    _attach_know_or_miss(
        p,
        query="context_manager",
        genes=[g],
        score_map={"g1": 2.5, "g2": 1.0},
        coordinate_confidence=0.8,
        # tier_contributions intentionally omitted.
    )
    assert p.know is not None
    assert p.know.lexical_dense_agree is False


def test_build_context_packet_plumbs_tier_contributions_from_genome(
    default_calibration,
):
    """End-to-end: build_context_packet must lift
    ``last_tier_contributions`` off the genome handle and feed it into
    the know/miss block. A genome whose query surfaces agreeing
    lexical+dense tiers must produce a packet with
    know.lexical_dense_agree=True. This is the plumbing the
    2026-05-16 deep review finding #3 was about."""

    class _FakeGenome:
        """Minimal genome stand-in: query_docs returns one document and
        publishes the agreeing tier map on ``last_tier_contributions``,
        exactly as knowledge_store.py / shard_router.py would."""

        def __init__(self):
            self.last_query_scores: dict = {}
            self.last_tier_contributions: dict = {}

        def query_docs(self, *, domains, entities, max_genes, read_only):
            gene = _make_packet_gene(
                "context manager orchestrates the pipeline",
                "context_manager",
            )
            self.last_query_scores = {gene.gene_id: 2.6}
            # Lexical and dense rankers agree on this gene_id.
            self.last_tier_contributions = {
                gene.gene_id: {
                    "fts5": 0.9,
                    "tag_exact": 0.8,
                    "splade": 0.7,
                    "sema_boost": 0.6,
                }
            }
            return [gene]

    genome = _FakeGenome()
    packet = build_context_packet(
        "context_manager",
        task_type="explain",
        genome=genome,
        now_ts=10_000.0,
    )
    assert packet.know is not None
    assert packet.miss is None
    assert packet.know.lexical_dense_agree is True


def test_build_context_packet_lexical_dense_agree_false_on_disjoint_genome_tiers(
    default_calibration,
):
    """End-to-end negative: a genome whose lexical and dense rankers
    disagree must yield know.lexical_dense_agree=False (the packet
    builder must not invent agreement)."""

    class _FakeGenome:
        def __init__(self):
            self.last_query_scores: dict = {}
            self.last_tier_contributions: dict = {}

        def query_docs(self, *, domains, entities, max_genes, read_only):
            g1 = _make_packet_gene(
                "context manager orchestrates the pipeline",
                "context_manager",
                gene_id="g_ctx",
            )
            g2 = _make_packet_gene(
                "codons chunk and encode fragments",
                "codons",
                gene_id="g_cod",
            )
            self.last_query_scores = {"g_ctx": 2.6, "g_cod": 1.1}
            # Lexical tiers favour g_ctx; dense tiers favour g_cod.
            self.last_tier_contributions = {
                "g_ctx": {"fts5": 0.9, "tag_exact": 0.8},
                "g_cod": {"splade": 0.9, "sema_boost": 0.8},
            }
            return [g1, g2]

    genome = _FakeGenome()
    packet = build_context_packet(
        "context_manager",
        task_type="explain",
        genome=genome,
        now_ts=10_000.0,
    )
    assert packet.know is not None
    assert packet.know.lexical_dense_agree is False


# ─────────────────────────────────────────────────────────────────────
# Stage 7 forward-compat smoke (no-op today; placeholder for Stage 7)
# ─────────────────────────────────────────────────────────────────────

def test_stage7_forward_compat_seams_in_place():
    """The schema file ships Stage 7's three new reasons (stale, cold,
    superseded) and the refresh_targets field. Stage 7 PR (#50) made
    the seam concrete; this test now enforces that the extension is
    wired and the new validator (refresh-class reasons require
    refresh_targets, escalate-class reasons forbid them) is in place.
    """
    # Stage 7 added three reasons additively.
    assert isinstance(MISS_REASONS, tuple)
    assert "stale" in MISS_REASONS
    assert "cold" in MISS_REASONS
    assert "superseded" in MISS_REASONS

    # A MissBlock with reason="stale" must carry a non-empty
    # refresh_targets list (Stage 7 spec §8 mutual-exclusivity).
    mb = MissBlock(
        reason="stale", top_score=0.5, ratio=1.2,
        escalate_to=[],
        refresh_targets=["/some/path/to/file.py"],
    )
    assert mb.reason == "stale"
    assert mb.refresh_targets == ["/some/path/to/file.py"]
    assert mb.escalate_to == []
