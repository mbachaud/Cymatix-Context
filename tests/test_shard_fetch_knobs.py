"""Config-threaded sharded-retrieval knobs: #222 (per-shard fetch depth) and
#223 (co-activation reserved budget / link boost).

These promote the dark-shipped env knobs (see
``tests/test_shard_depth_and_coact_budget.py`` for the standalone env-helper
contract) into ``[retrieval]`` config fields threaded through ``ShardRouter``
the same way ``fusion_mode`` / ``semantic_broaden_routing`` are.

Coverage:
    - Defaults byte-identical (fetch value + truncation behaviour match the
      pre-knob router when the knobs are unset).
    - #222 multiplier / sqrt(n_shards) scale math, including the cap.
    - #223 reserved-slot semantics via the resolved path.
    - Config threading (ShardRouter reads kwargs; load_config parses TOML).
    - Validation (negative values raise).
    - Env-over-config precedence (HELIX_SHARD_* still wins).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from cymatix_context.config import RetrievalConfig, load_config
from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)
from cymatix_context.shard_router import (
    SHARD_FETCH_SCALE_CAP,
    ShardRouter,
    _apply_coact_reserve,
    _env_int,
    _validate_shard_knobs,
    compute_per_shard_fetch,
)
from cymatix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)


@pytest.fixture(autouse=True)
def _clean_env():
    """Isolate the dark-ship env overrides so config precedence tests are hermetic."""
    saved = {
        k: os.environ.pop(k, None)
        for k in ("HELIX_SHARD_FETCH_FACTOR", "HELIX_SHARD_COACT_RESERVE")
    }
    yield
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


@pytest.fixture
def bare_main_db():
    """An initialised main.db with no registered shards.

    Enough for ShardRouter to construct (it probes the shards table) and to
    exercise knob threading / resolution without a full fan-out.
    """
    td = tempfile.TemporaryDirectory()
    main_path = str(Path(td.name) / "main.db")
    main = open_main_db(main_path)
    init_main_db(main)
    main.close()
    yield main_path
    td.cleanup()


# ── #222: compute_per_shard_fetch pure math ──────────────────────────

@pytest.mark.parametrize("mg,mult,scale,n,expected", [
    # Legacy: flat 2.0×, scale off — byte-identical to the pre-knob cut.
    (8, 2.0, False, 6, 16),
    (8, 2.0, False, 1, 16),
    (10, 2.0, False, 100, 20),   # scale off => n_shards ignored
    (1, 2.0, False, 3, 2),
    # Flat multiplier != 2 (env override / operator config).
    (8, 4.0, False, 6, 32),
    (8, 6.0, False, 6, 48),
    # Sub-1 and zero multipliers floor to max_genes (never fetch fewer).
    (8, 0.5, False, 6, 8),
    (8, 0.0, False, 6, 8),
    # Scale on but n_shards <= 1 => no amplification (single-shard fast path).
    (8, 2.0, True, 1, 16),
    (8, 2.0, True, 0, 16),
    # Scale on: multiplier × sqrt(n_shards).
    (8, 2.0, True, 4, 32),       # 2·√4 = 4  -> 32
    (8, 2.0, True, 9, 48),       # 2·√9 = 6  -> 48
    (8, 1.0, True, 16, 32),      # 1·√16 = 4 -> 32
    # Scale on: cap at SHARD_FETCH_SCALE_CAP × max_genes.
    (8, 2.0, True, 100, 80),     # 2·√100 = 20 -> 160, capped to 10·8 = 80
    (8, 2.0, True, 400, 80),     # 2·√400 = 40 -> capped to 80
])
def test_compute_per_shard_fetch_math(mg, mult, scale, n, expected):
    assert compute_per_shard_fetch(mg, mult, scale, n) == expected


def test_compute_per_shard_fetch_default_equals_global_cut():
    """The #222 lever: with legacy defaults the per-shard fetch equals the
    global 2×max_genes output cut — zero oversampling headroom."""
    for mg in (1, 4, 8, 16, 32):
        assert compute_per_shard_fetch(mg, 2.0, False, 6) == 2 * mg


def test_compute_per_shard_fetch_custom_cap():
    # 2·√400 = 40×; cap_mult lowers the ceiling explicitly.
    assert compute_per_shard_fetch(8, 2.0, True, 400, cap_mult=5) == 40
    assert compute_per_shard_fetch(8, 2.0, True, 400, cap_mult=SHARD_FETCH_SCALE_CAP) == 80


def test_compute_per_shard_fetch_negative_multiplier_raises():
    with pytest.raises(ValueError):
        compute_per_shard_fetch(8, -1.0, False, 4)


# ── #223: reserved-slot semantics via the pure helper ────────────────

def _mk_union(n: int, promoted_at: list[int]):
    union = [f"g{i}" for i in range(n)]
    corrected = {f"g{i}": float(n - i) for i in range(n)}   # descending
    rrf = {f"g{i}": 0.0 for i in range(n)}
    promoted = {f"g{i}" for i in promoted_at}
    return union, promoted, corrected, rrf


def test_reserve_zero_is_byte_identical_truncation():
    union, promoted, corrected, rrf = _mk_union(6, promoted_at=[5])
    assert _apply_coact_reserve(union, promoted, corrected, rrf, limit=3, reserve=0) == [
        "g0", "g1", "g2",
    ]


def test_reserve_rescues_promoted_and_displaces_bottom_incumbent():
    """Promoted g5 (below the cut) is rescued; the weakest non-promoted
    survivor (g2) is dropped; output size is unchanged."""
    union, promoted, corrected, rrf = _mk_union(6, promoted_at=[5])
    out = _apply_coact_reserve(union, promoted, corrected, rrf, limit=3, reserve=1)
    assert out == ["g0", "g1", "g5"]
    assert len(out) == 3


def test_reserve_output_size_unchanged_and_sorted():
    union, promoted, corrected, rrf = _mk_union(8, promoted_at=[6, 7])
    out = _apply_coact_reserve(union, promoted, corrected, rrf, limit=4, reserve=2)
    assert len(out) == 4
    scores = [corrected[g] for g in out]
    assert scores == sorted(scores, reverse=True)


# ── Validation ───────────────────────────────────────────────────────

def test_validate_negative_multiplier_raises():
    with pytest.raises(ValueError):
        _validate_shard_knobs(-0.1, 0, 0.5)


def test_validate_negative_reserved_slots_raises():
    with pytest.raises(ValueError):
        _validate_shard_knobs(2.0, -1, 0.5)


def test_validate_negative_link_boost_raises():
    with pytest.raises(ValueError):
        _validate_shard_knobs(2.0, 0, -0.5)


def test_validate_legacy_defaults_ok():
    # Byte-identical defaults must not raise.
    _validate_shard_knobs(2.0, 0, 0.5)


def test_env_int_helper():
    assert _env_int("HELIX_DEFINITELY_UNSET_XYZ") is None
    os.environ["HELIX_SHARD_FETCH_FACTOR"] = "5"
    assert _env_int("HELIX_SHARD_FETCH_FACTOR") == 5
    os.environ["HELIX_SHARD_FETCH_FACTOR"] = "junk"
    assert _env_int("HELIX_SHARD_FETCH_FACTOR") is None
    os.environ["HELIX_SHARD_FETCH_FACTOR"] = ""
    assert _env_int("HELIX_SHARD_FETCH_FACTOR") is None


# ── Config threading: RetrievalConfig + load_config(TOML) ─────────────

def test_retrieval_config_defaults_are_legacy():
    rc = RetrievalConfig()
    assert rc.shard_fetch_multiplier == 2.0
    assert rc.shard_fetch_scale_with_shards is False
    assert rc.coact_reserved_slots == 0
    assert rc.coact_link_boost == 0.5


def test_load_config_parses_shard_knobs():
    toml = (
        "[retrieval]\n"
        "shard_fetch_multiplier = 4.0\n"
        "shard_fetch_scale_with_shards = true\n"
        "coact_reserved_slots = 3\n"
        "coact_link_boost = 0.75\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "helix.toml"
        p.write_text(toml, encoding="utf-8")
        cfg = load_config(str(p))
    assert cfg.retrieval.shard_fetch_multiplier == 4.0
    assert cfg.retrieval.shard_fetch_scale_with_shards is True
    assert cfg.retrieval.coact_reserved_slots == 3
    assert cfg.retrieval.coact_link_boost == 0.75


def test_load_config_absent_section_keeps_defaults():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "helix.toml"
        p.write_text("[retrieval]\nfusion_mode = \"rrf\"\n", encoding="utf-8")
        cfg = load_config(str(p))
    assert cfg.retrieval.shard_fetch_multiplier == 2.0
    assert cfg.retrieval.shard_fetch_scale_with_shards is False
    assert cfg.retrieval.coact_reserved_slots == 0
    assert cfg.retrieval.coact_link_boost == 0.5


# ── Router threading + env precedence ────────────────────────────────

def test_router_defaults_are_legacy(bare_main_db):
    r = ShardRouter(bare_main_db)
    try:
        assert r._shard_fetch_multiplier == 2.0
        assert r._shard_fetch_scale_with_shards is False
        assert r._coact_reserved_slots == 0
        assert r._coact_link_boost == 0.5
        assert r._resolved_fetch_multiplier() == 2.0
        assert r._resolved_coact_reserve() == 0
    finally:
        r.close()


def test_router_threads_config_kwargs(bare_main_db):
    r = ShardRouter(
        bare_main_db,
        shard_fetch_multiplier=4.0,
        shard_fetch_scale_with_shards=True,
        coact_reserved_slots=3,
        coact_link_boost=0.75,
    )
    try:
        assert r._shard_fetch_multiplier == 4.0
        assert r._shard_fetch_scale_with_shards is True
        assert r._coact_reserved_slots == 3
        assert r._coact_link_boost == 0.75
        assert r._resolved_fetch_multiplier() == 4.0
        assert r._resolved_coact_reserve() == 3
    finally:
        r.close()


def test_router_construct_negative_multiplier_raises(bare_main_db):
    with pytest.raises(ValueError):
        ShardRouter(bare_main_db, shard_fetch_multiplier=-1.0)


def test_router_construct_negative_reserve_raises(bare_main_db):
    with pytest.raises(ValueError):
        ShardRouter(bare_main_db, coact_reserved_slots=-2)


def test_env_fetch_factor_overrides_config(bare_main_db):
    r = ShardRouter(bare_main_db, shard_fetch_multiplier=3.0)
    try:
        assert r._resolved_fetch_multiplier() == 3.0
        os.environ["HELIX_SHARD_FETCH_FACTOR"] = "5"
        assert r._resolved_fetch_multiplier() == 5.0  # env wins
        os.environ["HELIX_SHARD_FETCH_FACTOR"] = "junk"
        assert r._resolved_fetch_multiplier() == 3.0  # invalid env => config
    finally:
        r.close()


def test_env_coact_reserve_overrides_config(bare_main_db):
    r = ShardRouter(bare_main_db, coact_reserved_slots=2)
    try:
        assert r._resolved_coact_reserve() == 2
        os.environ["HELIX_SHARD_COACT_RESERVE"] = "7"
        assert r._resolved_coact_reserve() == 7  # env wins
        os.environ["HELIX_SHARD_COACT_RESERVE"] = "junk"
        assert r._resolved_coact_reserve() == 2  # invalid env => config
    finally:
        r.close()


# ── Integration: real two-shard fan-out with the knobs on ────────────

def _mk_gene(content, domains, entities, source) -> Gene:
    return Gene(
        gene_id="",
        content=content,
        complement=content[:50],
        codons=[],
        promoter=PromoterTags(domains=domains, entities=entities, sequence_index=0),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
        source_id=source,
    )


@pytest.fixture
def two_shard_setup():
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    main_path = str(root / "main.db")
    shard_a_path = str(root / "shard_a.db")
    shard_b_path = str(root / "shard_b.db")

    ga = Genome(shard_a_path)
    gene_a_id = ga.upsert_gene(
        _mk_gene("Helix design doc. Context retrieval via fingerprints.",
                 ["docs"], ["helix"], "/docs/intro.md"),
        apply_gate=False,
    )
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    gb = Genome(shard_b_path)
    gene_b_id = gb.upsert_gene(
        _mk_gene("Auth module. JWT sessions expire every 15 minutes.",
                 ["auth"], ["jwt"], "/code/auth.py"),
        apply_gate=False,
    )
    gb.conn.close()
    if gb._reader:
        gb._reader.close()

    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_a_path, gene_count=1)
    register_shard(main, "shard_b", "participant", shard_b_path, gene_count=1)
    upsert_fingerprint(
        main, gene_id=gene_a_id, shard_name="shard_a", source_id="/docs/intro.md",
        domains_json=json.dumps(["docs"]), entities_json=json.dumps(["helix"]),
        key_values_json="[]",
    )
    upsert_fingerprint(
        main, gene_id=gene_b_id, shard_name="shard_b", source_id="/code/auth.py",
        domains_json=json.dumps(["auth"]), entities_json=json.dumps(["jwt"]),
        key_values_json="[]",
    )
    main.close()

    yield {"main_path": main_path, "gene_a_id": gene_a_id, "gene_b_id": gene_b_id}
    td.cleanup()


def test_query_genes_default_and_scaled_return_same_golds(two_shard_setup):
    """The scaled fetch path must not drop golds the default path returns —
    a deeper per-shard fetch is a superset of the shallow one."""
    ids_default = _query_ids(two_shard_setup, multiplier=2.0, scale=False)
    ids_scaled = _query_ids(two_shard_setup, multiplier=2.0, scale=True)
    for gid in (two_shard_setup["gene_a_id"], two_shard_setup["gene_b_id"]):
        assert gid in ids_default
        assert gid in ids_scaled


def _query_ids(setup, *, multiplier, scale):
    r = ShardRouter(
        setup["main_path"],
        shard_fetch_multiplier=multiplier,
        shard_fetch_scale_with_shards=scale,
    )
    try:
        genes = r.query_genes(domains=["auth", "docs"], entities=[], max_genes=10)
        return {g.gene_id for g in genes}
    finally:
        r.close()
