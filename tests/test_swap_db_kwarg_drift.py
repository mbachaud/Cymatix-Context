"""Regression guard: the ``/admin/swap-db`` rebuild must stay in sync with boot.

Both ``CymatixContextManager.__init__`` (the boot path) and the
``/admin/swap-db`` handler construct the retrieval store through
``open_read_source(...)``.  ``open_read_source`` forwards ``**genome_kwargs``
verbatim to the solo ``Genome`` *and* — under ``CYMATIX_USE_SHARDS=1`` — to
``ShardRouter`` and every per-shard ``Genome``.  Any knob the boot path passes
but the swap path omits therefore silently reverts to the ``KnowledgeStore``
constructor default the moment an operator hot-swaps a fixture.

That has drifted three times already (see the dated comments in
``routes_admin.py``: the dense/ANN family 2026-05-18, the RRF-gate family
under #260, and the large weight/rerank/pool-hygiene batch this file was
written for).  Each round was found by hand, after the fact.  These two tests
turn "found by hand" into "found by CI":

``test_swap_path_passes_every_boot_kwarg``
    A mechanical AST comparison of the two call sites.  Catches an *addition*
    to the boot path that nobody mirrored onto the swap path — the failure
    mode that produced every prior round of drift.

``test_swap_preserves_non_default_retrieval_config``
    A behavioural check: build a config whose retrieval knobs are all
    non-default, perform a real swap through the HTTP endpoint, and assert the
    rebuilt store still carries those values.  Catches wiring the AST test
    cannot see — a kwarg that is present but reads the wrong config section,
    or a value the store drops on the floor.

Keep both.  If one had to go, keep the AST test: drift here has always been an
omission at the call site, and the AST test is the one that names the exact
missing kwargs in its failure message.
"""

from __future__ import annotations

import ast
from pathlib import Path

from cymatix_context.config import (
    GenomeConfig,
    IngestionConfig,
    RetrievalConfig,
)
from cymatix_context.knowledge_store import KnowledgeStore

from tests.conftest import MockCompressorBackend, make_client, make_cymatix_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BOOT_FILE = _REPO_ROOT / "cymatix_context" / "context_manager.py"
_SWAP_FILE = _REPO_ROOT / "cymatix_context" / "server" / "routes_admin.py"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
# Kwargs whose *value expression* is legitimately different at the two call
# sites.  This list is the load-bearing part of the AST test: every entry is a
# hole in the guard, so it stays small and every entry carries its reason.
#
# Note there is deliberately NO allowlist for kwarg *names*.  The two call
# sites must pass the identical set of kwarg names -- the swap-specific inputs
# below are swap-specific in their *value*, not in their presence.
_VALUE_EXPR_ALLOWLIST = {
    # The whole point of a swap: boot opens the configured store, the swap
    # handler opens the caller-supplied fixture path from the request body.
    "genome_path": (
        "boot opens config.genome.path; swap opens the request-body path"
    ),
    # Boot constructs the SEMA codec and holds it on self; the swap handler
    # has to re-read it off the live manager instance instead. Same object,
    # different reach -- the swap must NOT build a second codec.
    "sema_codec": (
        "boot holds self._sema_codec; swap re-reads it off the live manager"
    ),
}


def _open_read_source_kwargs(path: Path) -> dict[str, str]:
    """Return ``{kwarg_name: unparsed_value_expression}`` for the single
    ``open_read_source(...)`` call in *path*.

    Asserts there is exactly one such call so that adding a second call site
    fails loudly here rather than silently comparing the wrong one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "open_read_source":
            continue
        assert not any(kw.arg is None for kw in node.keywords), (
            f"{path.name}:{node.lineno} splats **kwargs into open_read_source; "
            "this guard can only compare explicit kwargs"
        )
        calls.append({kw.arg: ast.unparse(kw.value) for kw in node.keywords})

    assert len(calls) == 1, (
        f"expected exactly 1 open_read_source(...) call in {path.name}, "
        f"found {len(calls)} -- update this guard if a call site was added"
    )
    return calls[0]


def test_swap_path_passes_every_boot_kwarg():
    """The swap path must forward every kwarg the boot path forwards.

    Anything the boot path passes and the swap path omits reverts to the
    KnowledgeStore ctor default after a hot swap.
    """
    boot = _open_read_source_kwargs(_BOOT_FILE)
    swap = _open_read_source_kwargs(_SWAP_FILE)

    missing = sorted(set(boot) - set(swap))
    assert not missing, (
        f"{len(missing)} kwarg(s) passed on boot "
        f"({_BOOT_FILE.name}) but NOT on swap ({_SWAP_FILE.name}); each one "
        "silently reverts to the KnowledgeStore ctor default after "
        "/admin/swap-db:\n  "
        + "\n  ".join(f"{k}={boot[k]}" for k in missing)
    )

    extra = sorted(set(swap) - set(boot))
    assert not extra, (
        f"{len(extra)} kwarg(s) passed on swap but NOT on boot -- a swapped "
        "store would be configured differently from a freshly booted one:\n  "
        + "\n  ".join(f"{k}={swap[k]}" for k in extra)
    )


def test_swap_path_reads_the_same_config_source_as_boot():
    """Shared kwargs must read the same expression, modulo the allowlist.

    Guards against provenance drift -- e.g. the swap path reading
    ``config.ingestion.dense_model`` where boot reads
    ``config.retrieval.dense_model``.
    """
    boot = _open_read_source_kwargs(_BOOT_FILE)
    swap = _open_read_source_kwargs(_SWAP_FILE)

    divergent = {
        k: (boot[k], swap[k])
        for k in sorted(set(boot) & set(swap))
        if boot[k] != swap[k] and k not in _VALUE_EXPR_ALLOWLIST
    }
    assert not divergent, (
        "kwarg(s) read a different source on swap than on boot; add to "
        "_VALUE_EXPR_ALLOWLIST *with a reason* only if the difference is "
        "genuinely intentional:\n  "
        + "\n  ".join(
            f"{k}:\n      boot: {b}\n      swap: {s}"
            for k, (b, s) in divergent.items()
        )
    )

    # The allowlist must not rot: every entry has to describe a difference
    # that actually exists, or it is dead weight hiding a future regression.
    stale = [
        k for k in _VALUE_EXPR_ALLOWLIST
        if k not in boot or k not in swap or boot[k] == swap[k]
    ]
    assert not stale, (
        f"stale _VALUE_EXPR_ALLOWLIST entries (no longer differ, or no longer "
        f"passed at both sites): {stale}"
    )


# ---------------------------------------------------------------------------
# Behavioural counterpart
# ---------------------------------------------------------------------------

# Sentinel values for every retrieval/ingestion knob the swap path must
# preserve. Each one must differ from the *KnowledgeStore ctor default* --
# that is the value drift reverts to, so a sentinel equal to it is toothless.
# ``test_behavioural_sentinels_differ_from_ctor_defaults`` enforces that.
# All are inert at construction time (no model loads); the gated code paths
# only run during a query, and this test never issues one.
_NON_DEFAULT_RETRIEVAL = {
    # weights
    "pki_weight": 7.25,
    "fts5_weight": 9.5,
    "splade_weight": 8.25,
    "tag_exact_weight": 6.75,
    "tag_prefix_weight": 5.25,
    "sema_boost_weight": 4.75,
    "sema_cold_weight": 6.25,
    "lex_anchor_weight": 3.75,
    "harmonic_weight": 2.25,
    "entity_graph_weight": 1.75,
    # tag / splade discipline
    "tag_idf_enabled": True,
    "tag_df_cap": 0.42,
    "splade_df_cap_fraction": 0.37,
    # filename anchor
    "filename_anchor_enabled": True,  # ctor default is False
    "filename_anchor_weight": 11.5,
    # pool hygiene
    "bm25_shortlist_enabled": True,  # ctor default is False
    "bm25_shortlist_size": 77,
    "bm25_prefilter_enabled": True,
    "bm25_prefilter_size": 321,
    "fts5_candidate_depth": 91,
    # graph + semantic routing
    "entity_graph_retrieval_enabled": True,
    "semantic_dense_additive_weight": 23.5,
    "semantic_broaden_routing": False,
    "authority_path_selectivity": True,
    # rerank family
    # Validated enum: 'additive' (default) | 'fused_tier' | 'eps_band' | 'off'.
    "rerank_combinator": "eps_band",
    "rerank_band_delta": 0.17,
    "rerank_tier_weight": 2.5,
    "rerank_enabled": True,
    "rerank_depth": 33,
    "rerank_model": "cross-encoder/test-drift-sentinel",
}

# Ingestion-section knobs the swap path must preserve too.
_NON_DEFAULT_INGESTION = {
    "dense_embed_on_ingest": True,  # ctor default is False
}

# doc_type_boost_mode is deliberately absent from the behavioural assertions:
# it is a ShardRouter-only knob (it binds on the cross-shard merge) and the
# solo KnowledgeStore accepts but does not store it. The AST test above is
# what guards it.


def _seed_db(path: str) -> None:
    KnowledgeStore(path=path).close()


def test_behavioural_sentinels_differ_from_ctor_defaults():
    """Every sentinel must differ from the KnowledgeStore ctor default.

    Drift reverts a knob to its *ctor* default, so a sentinel that happens to
    equal that default silently stops testing anything. This keeps the
    behavioural test honest as defaults move.
    """
    import inspect

    params = inspect.signature(KnowledgeStore.__init__).parameters
    toothless = []
    for knob, sentinel in {
        **_NON_DEFAULT_RETRIEVAL, **_NON_DEFAULT_INGESTION
    }.items():
        param = params.get(knob)
        assert param is not None, f"{knob} is not a KnowledgeStore ctor kwarg"
        if param.default == sentinel:
            toothless.append((knob, sentinel))
    assert not toothless, (
        "sentinel(s) equal to the KnowledgeStore ctor default -- the "
        "behavioural test cannot detect drift on these; pick a different "
        f"value: {toothless}"
    )


def test_swap_preserves_non_default_retrieval_config(tmp_path, monkeypatch):
    """A hot swap must not silently reset tuned retrieval knobs to ctor defaults."""
    # filename_anchor_enabled ORs in an env override on both paths -- clear it
    # so this test observes the config leg only.
    monkeypatch.delenv("CYMATIX_FILENAME_ANCHOR_ENABLED", raising=False)

    db_a = str(tmp_path / "before.db")
    db_b = str(tmp_path / "after.db")
    _seed_db(db_a)
    _seed_db(db_b)

    config = make_cymatix_config(
        genome=GenomeConfig(path=db_a, cold_start_threshold=5),
        retrieval=RetrievalConfig(**_NON_DEFAULT_RETRIEVAL),
        ingestion=IngestionConfig(**_NON_DEFAULT_INGESTION),
    )
    client = make_client(config=config, backend=MockCompressorBackend())

    # Sanity: the boot path already honours these. If this half fails the
    # problem is upstream of the swap path.
    booted = client.app.state.cymatix.genome
    for knob, want in {**_NON_DEFAULT_RETRIEVAL, **_NON_DEFAULT_INGESTION}.items():
        assert getattr(booted, f"_{knob}") == want, (
            f"boot path dropped {knob}: "
            f"want {want!r}, got {getattr(booted, f'_{knob}')!r}"
        )

    resp = client.post("/admin/swap-db", json={"path": db_b})
    assert resp.status_code == 200, resp.text
    assert resp.json()["swapped"] is True

    swapped = client.app.state.cymatix.genome
    assert swapped is not booted, "swap did not actually rebuild the store"

    reverted = {
        knob: (want, getattr(swapped, f"_{knob}"))
        for knob, want in {
            **_NON_DEFAULT_RETRIEVAL, **_NON_DEFAULT_INGESTION
        }.items()
        if getattr(swapped, f"_{knob}") != want
    }
    assert not reverted, (
        "knob(s) reverted to the KnowledgeStore ctor default across "
        "/admin/swap-db -- the swap path is not forwarding them:\n  "
        + "\n  ".join(
            f"{k}: configured {want!r}, got {got!r}"
            for k, (want, got) in sorted(reverted.items())
        )
    )
