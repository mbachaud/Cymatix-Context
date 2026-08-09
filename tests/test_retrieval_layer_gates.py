"""Retrieval-side gates for the two largest UNMEASURED storage layers.

Wave 2a/2b of the A/B data campaign
(``docs/superpowers/plans/2026-08-09-ab-data-campaign.md``):

  * **PKI** (#334) — ``path_key_index``, 8.56 GB, the Tier-0 compound
    (path_token, kv_key) scorer at ``knowledge_store.py`` Tier 0.
  * **tags** (retrieval-layer ledger action 6) — ``promoter_index``,
    2.58 GB, Tier 1 ``tag_exact`` + Tier 2 ``tag_prefix``  *(next commit)*.

Both layers shipped ungated: the ablation ladder could not arm them, so
11.1 GB of storage has never been measured against a null.

Two contracts are under test, and the FIRST one is the campaign's insurance
policy:

1. **The shipped default is byte-identical to pre-gate behaviour.**
   ``tests/fixtures/retrieval_gate_golden.json`` was frozen by running this
   file's own corpus + queries against the worktree at commit
   ``cf101bc`` — i.e. *before* either gate existed. Every score, every
   per-tier contribution, every returned id and every emitted signal key,
   under both fusion modes, must still match exactly. This is not a
   tautology check against the new code; it is a diff against the old code.

   The campaign's immediately preceding fix (``1fd34b4``, entity_graph)
   flipped a shipped default as a side effect of adding a gate and
   invalidated the comparability of the committed ladder receipts. This
   test exists so that cannot happen twice.

2. **Gating OFF removes the layer's timing signal**, because signal ABSENCE
   is what the ablation ladder validates arms with
   (``benchmarks/dogfood/erb/ablation_ladder.py`` ``VALIDATION_SIGNAL``).
   An arm whose treatment silently fails to apply reports the layer as
   worthless — the #354 failure mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cymatix_context.config import RetrievalConfig
from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "retrieval_gate_golden.json"


# ── Corpus + query fixtures (must stay in lockstep with the golden) ──────
#
# Any edit here invalidates the golden. That is deliberate: the golden's
# whole value is that it was produced by the PRE-gate code against exactly
# this input, so the input is frozen alongside it.


def _gene(gene_id, content, domains, entities, source_id=None, key_values=None):
    return Gene(
        gene_id=gene_id,
        content=content,
        complement=f"Summary of: {content[:50]}",
        codons=["chunk_0", "chunk_1", "chunk_2"],
        promoter=PromoterTags(
            domains=domains,
            entities=entities,
            intent="test",
            summary=content[:80],
        ),
        epigenetics=EpigeneticMarkers(co_activated_with=[]),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
        source_id=source_id,
        key_values=key_values or [],
    )


# (gene_id, content, domains, entities, source_id, key_values)
# The pki-* rows carry source_id + key_values, which is what
# storage.indexes.sync_path_key_index needs to materialise PKI rows.
DOCS = [
    ("pki-a", "cymatix server configuration notes for the proxy",
     ["cymatix"], ["server"],
     "F:/Projects/Cymatix/server_config.py", ["port=11437", "host=127.0.0.1"]),
    ("pki-b", "cymatix launcher tray settings and supervisor",
     ["cymatix"], ["tray"],
     "F:/Projects/Cymatix/launcher/tray.py", ["port=11439"]),
    ("pki-c", "onyx ingestion notes about the harvester",
     ["onyx"], ["harvester"],
     "F:/Projects/Onyx/harvester.py", ["port=9000", "timeout=30"]),
    ("tag-a", "alpha beta gamma retrieval notes",
     ["alpha"], ["beta"], None, []),
    ("tag-b", "alphabet soup and betamax cassettes",
     ["alphabet"], ["betamax"], None, []),
    ("fts-a", "the port number is configured elsewhere in cymatix",
     ["misc"], [], None, []),
    ("noise-1", "completely unrelated content about gardening",
     ["garden"], [], None, []),
    ("noise-2", "another unrelated body about baking bread",
     ["baking"], [], None, []),
]

QUERIES = [
    ("q_pki_only", ["cymatix"], ["port"]),
    ("q_pki_and_tags", ["cymatix", "alpha"], ["port", "beta"]),
    ("q_tags_only", ["alpha"], ["beta"]),
    ("q_no_pki_no_tags", ["gardening"], ["bread"]),
    ("q_onyx", ["onyx"], ["timeout"]),
]

FUSION_MODES = ("rrf", "additive")


def _build_store(fusion_mode="rrf", **kwargs):
    g = Genome(path=":memory:", fusion_mode=fusion_mode, **kwargs)
    for row in DOCS:
        g.upsert_gene(_gene(*row), apply_gate=False)
    return g


def _snapshot(g, domains, entities, **query_kwargs):
    """Mirror of the golden generator — same shape, same float repr."""
    try:
        genes = g.query_docs(domains, entities, max_genes=6, read_only=True,
                             **query_kwargs)
        ids = [x.gene_id for x in genes]
    except Exception as exc:  # PromoterMismatch etc.
        return {"error": type(exc).__name__}
    return {
        "returned_ids": ids,
        "scores": {k: repr(float(v)) for k, v in sorted(g.last_query_scores.items())},
        "tier_contrib": {
            gid: {t: repr(float(s)) for t, s in sorted(d.items())}
            for gid, d in sorted(g.last_tier_contributions.items())
        },
        "signal_keys": sorted(g.last_signal_timings.keys()),
    }


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _run(fusion_mode, query_name, **kwargs):
    domains, entities = next(
        (d, e) for n, d, e in QUERIES if n == query_name
    )
    store_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("use_")}
    call_kwargs = {k: v for k, v in kwargs.items() if k.startswith("use_")}
    g = _build_store(fusion_mode, **store_kwargs)
    try:
        return _snapshot(g, domains, entities, **call_kwargs)
    finally:
        g.close()


# ══════════════════════════════════════════════════════════════════════
# 1. THE INSURANCE POLICY — shipped default == pre-gate behaviour
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("fusion_mode", FUSION_MODES)
@pytest.mark.parametrize("query_name", [q[0] for q in QUERIES])
def test_shipped_default_reproduces_pre_gate_golden(golden, fusion_mode, query_name):
    """Every score/contribution/id/signal matches the PRE-gate freeze.

    The golden was produced by ``knowledge_store.py`` as it stood at commit
    ``cf101bc``, before ``pki_enabled`` / ``tags_enabled`` existed. Passing
    here means the default resolve is genuinely inert, not merely
    self-consistent.
    """
    expected = golden["fusion_modes"][fusion_mode][query_name]
    observed = _run(fusion_mode, query_name)
    assert observed["returned_ids"] == expected["returned_ids"]
    assert observed["scores"] == expected["scores"]
    assert observed["tier_contrib"] == expected["tier_contrib"]
    assert observed["signal_keys"] == expected["signal_keys"]


def test_shipped_config_default_is_on():
    """The knob ships TRUE == today's behaviour (no silent default flip)."""
    assert RetrievalConfig().pki_enabled is True


def test_store_ctor_default_is_on():
    g = _build_store()
    try:
        assert g._pki_enabled is True
    finally:
        g.close()


def test_explicit_true_override_matches_the_default(golden):
    """``use_pki=True`` == the default resolve.

    Guards the ``None``-vs-``True`` branch of the per-call resolve: an
    override that took a different code path from the default would make
    every ladder control arm incomparable.
    """
    for fusion_mode in FUSION_MODES:
        for query_name, _d, _e in QUERIES:
            expected = golden["fusion_modes"][fusion_mode][query_name]
            observed = _run(fusion_mode, query_name, use_pki=True)
            assert observed["scores"] == expected["scores"], (
                f"{fusion_mode}/{query_name}: explicit-True diverged from default"
            )
            assert observed["tier_contrib"] == expected["tier_contrib"]
            assert observed["returned_ids"] == expected["returned_ids"]


# ══════════════════════════════════════════════════════════════════════
# 2a. PKI gate (#334)
# ══════════════════════════════════════════════════════════════════════


def test_pki_signal_fires_at_baseline():
    """Precondition for the ladder arm: 'pki' must be PRESENT at baseline.

    ``ablation_ladder.validate_arm`` calls an arm *unfalsifiable* when the
    ablated signal never appeared in the control. The Tier-0 block emits its
    signal for every query with at least one token, whether or not it scores,
    so the baseline side of the contract holds on any bed.
    """
    obs = _run("rrf", "q_pki_only")
    assert "pki" in obs["signal_keys"]
    assert obs["tier_contrib"]["pki-a"]["pki"] == repr(5.0)
    assert obs["tier_contrib"]["pki-b"]["pki"] == repr(5.0)


def test_pki_config_gate_off_removes_signal_and_score():
    """``[retrieval] pki_enabled = false`` removes cost AND contribution."""
    obs = _run("rrf", "q_pki_only", pki_enabled=False)
    assert "pki" not in obs["signal_keys"], (
        "gated-off PKI still emitted its timing signal — the ladder arm "
        "would validate vacuously"
    )
    for gid, contribs in obs["tier_contrib"].items():
        assert "pki" not in contribs, f"{gid} still carries a pki contribution"


def test_pki_per_call_override_off_removes_signal():
    """``use_pki=False`` ablates for ONE call without touching global config."""
    obs = _run("rrf", "q_pki_only", use_pki=False)
    assert "pki" not in obs["signal_keys"]
    for contribs in obs["tier_contrib"].values():
        assert "pki" not in contribs


def test_pki_per_call_override_on_beats_a_disabled_config():
    """``use_pki=True`` re-arms the tier on a store configured off."""
    obs = _run("rrf", "q_pki_only", pki_enabled=False, use_pki=True)
    assert "pki" in obs["signal_keys"]
    assert obs["tier_contrib"]["pki-a"]["pki"] == repr(5.0)


def _traced_sql(store, domains, entities):
    """Every SQL statement SQLite actually executes for one query_docs call.

    ``sqlite3.Connection.set_trace_callback`` fires per prepared statement, so
    this is the real cost surface — not a mock of it.
    """
    seen: list[str] = []
    conn = store.read_conn
    conn.set_trace_callback(seen.append)
    try:
        store.query_docs(domains, entities, max_genes=6, read_only=True)
    finally:
        conn.set_trace_callback(None)
    return seen


def test_pki_gate_does_not_run_the_sql():
    """The gate wraps the SQL, not just the bonus — cost is removed too."""
    on = _build_store()
    try:
        baseline_sql = _traced_sql(on, ["cymatix"], ["port"])
    finally:
        on.close()
    assert any("path_key_index" in s for s in baseline_sql), (
        "control: the PKI SELECT must run at baseline, or this test is vacuous"
    )

    off = _build_store(pki_enabled=False)
    try:
        gated_sql = _traced_sql(off, ["cymatix"], ["port"])
    finally:
        off.close()
    assert not any("path_key_index" in s for s in gated_sql), (
        "path_key_index SQL still executed with the gate off"
    )


def test_pki_gate_off_leaves_other_tiers_untouched(golden):
    """Ablating PKI must not perturb tag/FTS contributions.

    A gate that accidentally short-circuits its neighbours would make the
    arm measure the wrong layer.
    """
    expected = golden["fusion_modes"]["rrf"]["q_pki_only"]["tier_contrib"]
    observed = _run("rrf", "q_pki_only", pki_enabled=False)["tier_contrib"]
    for gid, contribs in expected.items():
        for tier, value in contribs.items():
            if tier in ("pki", "authority"):
                continue  # pki is the ablation; authority is a post-fusion additive
            assert observed.get(gid, {}).get(tier) == value, (
                f"{gid}/{tier} moved when PKI was ablated"
            )


def test_tag_signals_fire_at_baseline():
    """Baseline record for the tags gate landing in the next commit."""
    obs = _run("rrf", "q_tags_only")
    assert "tag_exact" in obs["signal_keys"]
    assert "tag_prefix" in obs["signal_keys"]
    assert "tag_exact" in obs["tier_contrib"]["tag-a"]
    assert "tag_prefix" in obs["tier_contrib"]["tag-b"]
