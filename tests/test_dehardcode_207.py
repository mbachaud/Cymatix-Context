"""Issue #207 (de-hardcoding wave 2, items 1-3): config knobs for the SPLADE /
SEMA model IDs, the SPLADE ingest content cap, and the citation-shortener
ingest-root anchors. Every default must reproduce the prior hardwired literal
byte-for-byte; overrides let air-gap / mirror deployments repoint the models
and stop owner path segments leaking into citations.

Dense fast-follow (2026-07-10): PR #261 deferred the BGE-M3 dense model ID
and its 2000-char passage cap (both were hardwired across THREE encode
paths -- inline ingest via ``context_manager.ingest``, query-side store
encode via ``KnowledgeStore._encode_dense_v2_blob``, and offline backfill
via ``scripts/backfill_bgem3_v2.py``). The tests below cover that
deferral: config defaults/overrides, KnowledgeStore forwarding, and a
cross-path byte-identity check -- the #1 review risk the deferral flagged
is the cap silently drifting between the inline-ingest and offline-backfill
slices.

Wave 2 items 5-6 (2026-07-10 lite follow-up, #207):
  item 5 -- deny-list extensibility. knowledge_store.DENY_PATTERNS is now a
  documented public constant; ``[ingestion] deny_list_extra`` ORs extra
  regex fragments onto it and ``locale_demotion_enabled`` toggles the
  non-English ``locale/`` demotion pattern independently.
  item 6 -- the small-model/MoE decoder-capability table
  (SMALL_MODEL_PATTERNS) required an exact model-string match and silently
  missed whole families (mistral, deepseek, granite) that use the same
  ":NNb" Ollama tag convention. ``resolve_model_capability_class`` adds a
  generic ":NNb" parse fallback plus a ``[budget] decoder_mode_overrides``
  operator escape hatch, with the hand-calibrated tables kept ahead of both
  in priority.
"""
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

from helix_context.config import (
    AbstainClassFloors,
    AbstainConfig,
    BudgetConfig,
    HelixConfig,
    IngestionConfig,
    RetrievalConfig,
    load_config,
)
from helix_context.context_manager import (
    MOE_MODEL_FAMILIES,
    SMALL_MODEL_PATTERNS,
    SMALL_MODEL_THRESHOLD_B,
    _parse_model_param_size_b,
    _shorten_source_path,
    resolve_model_capability_class,
)
from helix_context.knowledge_store import (
    DENY_PATTERNS,
    LOCALE_DENY_PATTERN,
    KnowledgeStore,
    build_deny_regex,
    is_denied_source,
)
from helix_context.storage.indexes import sync_splade_index
from tests.conftest import FakeBGEM3Codec, make_gene


def test_ingestion_knob_defaults_match_prior_literals():
    ing = HelixConfig().ingestion
    assert ing.splade_model == "naver/splade-cocondenser-ensembledistil"
    assert ing.sema_model == "all-MiniLM-L6-v2"
    assert ing.splade_content_cap == 1000
    assert ing.citation_path_anchors == ["sources", "Projects"]


def test_ingestion_knobs_load_from_toml(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [ingestion]
        splade_model = "/mnt/mirror/splade"
        sema_model = "/mnt/mirror/minilm"
        splade_content_cap = 2500
        citation_path_anchors = ["corpus", "sources"]
    """), encoding="utf-8")
    ing = load_config(str(toml)).ingestion
    assert ing.splade_model == "/mnt/mirror/splade"
    assert ing.sema_model == "/mnt/mirror/minilm"
    assert ing.splade_content_cap == 2500
    assert ing.citation_path_anchors == ["corpus", "sources"]


@pytest.mark.parametrize("src,anchors,expected", [
    # last occurrence of `sources` -> preserve source-type prefix
    ("F:/tmp/x/sources/confluence/a/b.json", ["sources", "Projects"], "confluence/a/b.json"),
    # no `sources`, fall through to `Projects`
    ("F:/Projects/ERB/gen/x/y.json", ["sources", "Projects"], "ERB/gen/x/y.json"),
    # exact-match only: `sources_attached` is NOT a `sources` segment, so the
    # anchor stays on the real `sources` (byte-identical to the prior logic)
    ("/root/sources/slack/t/sources_attached/f.json", ["sources"], "slack/t/sources_attached/f.json"),
    # custom air-gap ingest root
    ("/data/corpus/mydocs/report.json", ["mydocs"], "report.json"),
    # no anchor match -> last-3 fallback (fixes #146 over-truncation)
    ("/a/b/c/d/e.json", ["nomatch"], "c/d/e.json"),
    # <=3 segments and no match -> return src unchanged
    ("short.json", ["sources"], "short.json"),
    # synthetic / empty sources -> ""
    ("_pending_1", ["sources"], ""),
    ("", ["sources"], ""),
])
def test_shorten_source_path(src, anchors, expected):
    assert _shorten_source_path(src, anchors) == expected


def test_shorten_prefers_first_matching_anchor():
    # both 'sources' and 'Projects' present; 'sources' is first in the list
    src = "F:/Projects/ERB/generated_data/sources/github/pr-1.json"
    assert _shorten_source_path(src, ["sources", "Projects"]) == "github/pr-1.json"


def test_sync_splade_index_honours_cap_and_model(monkeypatch):
    captured = {}

    def fake_encode(text, top_k=128, model_name="unset"):
        captured["text_len"] = len(text)
        captured["model_name"] = model_name
        return {"tok": 1.0}

    import helix_context.backends.splade_backend as sb
    monkeypatch.setattr(sb, "encode", fake_encode)

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE splade_terms (gene_id TEXT, term TEXT, weight REAL)")
    sync_splade_index(
        con.cursor(), "g1", "x" * 5000, True,
        content_cap=1500, model_name="/mnt/mirror/splade",
    )
    assert captured["text_len"] == 1500          # cap applied, not the old 1000
    assert captured["model_name"] == "/mnt/mirror/splade"


def test_sync_splade_index_defaults_are_byte_identical(monkeypatch):
    captured = {}

    def fake_encode(text, top_k=128, model_name="unset"):
        captured["text_len"] = len(text)
        captured["model_name"] = model_name
        return {}

    import helix_context.backends.splade_backend as sb
    monkeypatch.setattr(sb, "encode", fake_encode)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE splade_terms (gene_id TEXT, term TEXT, weight REAL)")
    sync_splade_index(con.cursor(), "g1", "y" * 5000, True)  # no cap/model args
    assert captured["text_len"] == 1000
    assert captured["model_name"] == "naver/splade-cocondenser-ensembledistil"


# ── Dense fast-follow (2026-07-10): BGE-M3 model ID + passage cap ────────


def test_dense_knob_defaults_match_prior_literals():
    assert RetrievalConfig().dense_model == "BAAI/bge-m3"
    assert IngestionConfig().dense_passage_char_cap == 2000


def test_dense_knobs_load_from_toml(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [retrieval]
        dense_model = "/mnt/mirror/bge-m3"

        [ingestion]
        dense_passage_char_cap = 3500
    """), encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.retrieval.dense_model == "/mnt/mirror/bge-m3"
    assert cfg.ingestion.dense_passage_char_cap == 3500


def test_knowledge_store_dense_ctor_defaults_match_prior_literals():
    store = KnowledgeStore(":memory:")
    try:
        assert store._dense_model == "BAAI/bge-m3"
        assert store._dense_passage_char_cap == 2000
    finally:
        store.close()


def test_knowledge_store_dense_ctor_forwards_overrides():
    store = KnowledgeStore(
        ":memory:", dense_model="/mnt/mirror/bge-m3", dense_passage_char_cap=3500,
    )
    try:
        assert store._dense_model == "/mnt/mirror/bge-m3"
        assert store._dense_passage_char_cap == 3500
    finally:
        store.close()


def test_get_dense_codec_passes_configured_model_name(monkeypatch):
    """``KnowledgeStore._get_dense_codec`` must thread ``self._dense_model``
    into ``get_shared_codec(model_name=...)`` -- the cache key is
    ``(model_name, dim, device)`` (bgem3_codec.get_shared_codec), so a
    repointed model gets its own singleton rather than reusing the default
    BAAI/bge-m3 instance.

    ``tests/conftest.py``'s autouse ``_stub_dense_codec`` fixture replaces
    ``KnowledgeStore._get_dense_codec`` wholesale (so non-live tests never
    build a real ``BGEM3Codec``); this test needs the REAL method to
    exercise the ``get_shared_codec`` forwarding, so it reverts that one
    monkeypatch first via ``monkeypatch.undo()`` (both fixtures share this
    test node's single ``MonkeyPatch`` instance -- a documented pattern for
    reverting early).
    """
    monkeypatch.undo()  # restore the real KnowledgeStore._get_dense_codec

    import helix_context.backends.bgem3_codec as bgem3_codec

    captured = {}

    def fake_get_shared_codec(dim=1024, model_name="BAAI/bge-m3", share=True):
        captured["dim"] = dim
        captured["model_name"] = model_name
        return object()

    monkeypatch.setattr(bgem3_codec, "get_shared_codec", fake_get_shared_codec)

    store = KnowledgeStore(":memory:", dense_model="/mnt/mirror/bge-m3")
    try:
        store._get_dense_codec()
    finally:
        store.close()
    assert captured["model_name"] == "/mnt/mirror/bge-m3"


def test_encode_dense_v2_blob_uses_configured_cap():
    """``_encode_dense_v2_blob`` must slice content to
    ``self._dense_passage_char_cap`` before encoding, not the module's
    ``PASSAGE_CHAR_CAP`` literal.
    """
    class _CapSpyCodec:
        def __init__(self, dim):
            self.dim = dim
            self.last_text = None

        def encode(self, text, task="passage"):
            self.last_text = text
            return [0.0] * self.dim

    cap = 37
    dim = 4
    store = KnowledgeStore(
        ":memory:", dense_embed_on_ingest=True, dense_embedding_dim=dim,
        dense_passage_char_cap=cap,
    )
    spy = _CapSpyCodec(dim)
    store._dense_codec = spy  # pre-assign: bypasses _get_dense_codec's lazy-build
    try:
        content = "x" * 500
        blob = store._encode_dense_v2_blob(content)
    finally:
        store.close()
    assert blob is not None
    assert spy.last_text == content[:cap]
    assert len(spy.last_text) == cap


def test_dense_cap_byte_identical_across_ingest_and_backfill(tmp_path):
    """Core risk the #207 dense fast-follow deferral flagged: with the SAME
    configured cap, the query-side store-encode path
    (``KnowledgeStore._encode_dense_v2_blob``) and the offline backfill path
    (``scripts/backfill_bgem3_v2.backfill_dense_db``) must slice content to
    the identical length and therefore encode byte-identical BLOBs for
    identical content. Uses a non-default cap (1234, not the 2000 literal)
    so the assertion cannot pass by both paths silently falling back to the
    shared PASSAGE_CHAR_CAP default -- it proves the SAME configured value
    threads through both seams.
    """
    cap = 1234
    dim = 8
    content = "gene body word " * 400  # far longer than cap; the slice point must match exactly

    # -- query-side store encode (KnowledgeStore._encode_dense_v2_blob) --
    store = KnowledgeStore(
        ":memory:", dense_embed_on_ingest=True, dense_embedding_dim=dim,
        dense_passage_char_cap=cap,
    )
    store._dense_codec = FakeBGEM3Codec(dim)
    try:
        store_blob = store._encode_dense_v2_blob(content)
    finally:
        store.close()
    assert store_blob is not None

    # -- offline backfill (backfill_bgem3_v2.backfill_dense_db) ----------
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "scripts"))
    try:
        import backfill_bgem3_v2 as bf2

        db_path = tmp_path / "backfill_cap_check.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE genes (gene_id TEXT PRIMARY KEY, content TEXT, "
            "chromatin INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO genes (gene_id, content) VALUES (?, ?)", ("g1", content)
        )
        conn.commit()
        conn.close()

        bf2.backfill_dense_db(
            str(db_path), dim=dim, char_cap=cap,
            codec=FakeBGEM3Codec(dim),  # separate instance -- pure fn of (text, dim)
            log_fn=lambda _msg: None,
        )
    finally:
        sys.path.remove(str(repo_root / "scripts"))

    conn = sqlite3.connect(str(db_path))
    backfill_blob = conn.execute(
        "SELECT embedding_dense_v2 FROM genes WHERE gene_id = 'g1'"
    ).fetchone()[0]
    conn.close()

    assert bytes(backfill_blob) == bytes(store_blob), (
        "ingest-path and backfill blobs differ for identical content+cap -- "
        "the passage cap has drifted between the two encode paths"
    )


# ── Item 5 (2026-07-10 lite follow-up): deny-list extensibility ──────────


def test_deny_list_knob_defaults_match_prior_literals():
    ing = HelixConfig().ingestion
    assert ing.deny_list_extra == []
    assert ing.locale_demotion_enabled is True


def test_deny_list_knobs_load_from_toml(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [ingestion]
        deny_list_extra = ["internal_only", "mycorp_scratch"]
        locale_demotion_enabled = false
    """), encoding="utf-8")
    ing = load_config(str(toml)).ingestion
    assert ing.deny_list_extra == ["internal_only", "mycorp_scratch"]
    assert ing.locale_demotion_enabled is False


def test_deny_patterns_is_the_documented_builtin_list():
    """DENY_PATTERNS is public (no leading underscore) and does NOT include
    the locale pattern -- that lives in the separately-toggleable
    LOCALE_DENY_PATTERN (Issue #207 item 5)."""
    assert not any(p is LOCALE_DENY_PATTERN for p in DENY_PATTERNS)
    assert "node_modules" in "".join(DENY_PATTERNS)


def test_build_deny_regex_defaults_are_byte_identical_to_module_level():
    """build_deny_regex() with no args must match every path the prior
    hardwired module-level regex matched."""
    default_re = build_deny_regex()
    assert default_re.search("project/node_modules/react/index.js")
    assert default_re.search("project/locale/de/messages.po")
    assert not default_re.search("project/locale/en/messages.po")
    assert not default_re.search("F:/Projects/helix-context/helix_context/genome.py")


def test_build_deny_regex_extra_patterns_are_ored_in():
    deny_re = build_deny_regex(extra_patterns=[r"[\\/]internal_only[\\/]"])
    assert deny_re.search("F:/corp/internal_only/secrets.md")
    # built-ins still active
    assert deny_re.search("project/node_modules/react/index.js")


def test_build_deny_regex_locale_demotion_disabled():
    deny_re = build_deny_regex(locale_demotion_enabled=False)
    assert not deny_re.search("project/locale/de/messages.po")
    # everything else in the built-in list is unaffected
    assert deny_re.search("project/node_modules/react/index.js")


def test_is_denied_source_accepts_a_precompiled_deny_re():
    custom_re = build_deny_regex(extra_patterns=[r"[\\/]mycorp_noise[\\/]"])
    assert is_denied_source("F:/x/mycorp_noise/y.txt", deny_re=custom_re) is True
    # default (module-level) regex doesn't know about the custom pattern
    assert is_denied_source("F:/x/mycorp_noise/y.txt") is False


def test_knowledge_store_deny_ctor_defaults_match_prior_literals():
    store = KnowledgeStore(":memory:")
    try:
        assert store._deny_list_extra == []
        assert store._locale_demotion_enabled is True
        assert store._deny_re.search("project/locale/de/messages.po")
    finally:
        store.close()


def test_knowledge_store_deny_ctor_forwards_overrides():
    store = KnowledgeStore(
        ":memory:",
        deny_list_extra=[r"[\\/]mycorp_noise[\\/]"],
        locale_demotion_enabled=False,
    )
    try:
        assert store._deny_list_extra == [r"[\\/]mycorp_noise[\\/]"]
        assert store._locale_demotion_enabled is False
        assert store._deny_re.search("F:/x/mycorp_noise/y.txt")
        assert not store._deny_re.search("project/locale/de/messages.po")
    finally:
        store.close()


def test_apply_density_gate_honors_deny_list_extra():
    """A custom deny_list_extra pattern demotes a gene that the built-in
    list alone would NOT catch."""
    store = KnowledgeStore(":memory:", deny_list_extra=[r"[\\/]mycorp_noise[\\/]"])
    try:
        g = make_gene(content="x" * 2000, domains=["code"] * 10)
        g.source_id = "F:/corp/mycorp_noise/report.txt"
        state, reason = store.apply_density_gate(g)
        assert reason == "deny_list"
        from helix_context.schemas import ChromatinState
        assert state == ChromatinState.HETEROCHROMATIN
    finally:
        store.close()


def test_apply_density_gate_locale_demotion_disabled_admits_non_english():
    """With locale_demotion_enabled=False, a non-English locale/ path no
    longer forces deny_list -- it falls through to the score gate like any
    other document."""
    store = KnowledgeStore(":memory:", locale_demotion_enabled=False)
    try:
        g = make_gene(
            content="x" * 2000,
            domains=["code", "js", "lib"] * 10,
        )
        g.key_values = ["k1=v1", "k2=v2", "k3=v3"] * 5
        g.source_id = "project/locale/de/messages.po"
        state, reason = store.apply_density_gate(g)
        assert reason != "deny_list"
    finally:
        store.close()


# ── Item 6 (2026-07-10 lite follow-up): small-model/MoE decoder table ────


@pytest.mark.parametrize("model_name,expected", [
    ("mistral:7b", 7.0),
    ("deepseek-r1:8b", 8.0),
    ("granite3:2b", 2.0),
    ("qwen3:0.6b", 0.6),
    ("llama3.1:70b", 70.0),
    ("some-model:latest", None),   # no :NNb suffix at all
    ("bare-name", None),           # no colon
    ("weird:b", None),             # no digits before the 'b'
])
def test_parse_model_param_size_b(model_name, expected):
    assert _parse_model_param_size_b(model_name) == expected


def test_resolve_model_capability_class_generic_parse_catches_missed_families():
    """The families the issue calls out by name (mistral, deepseek,
    granite) are NOT in SMALL_MODEL_PATTERNS -- they must now resolve via
    the generic ':NNb' fallback instead of silently defaulting to 'large'."""
    assert resolve_model_capability_class("mistral:7b") == "small"
    assert resolve_model_capability_class("deepseek-r1:8b") == "small"
    assert resolve_model_capability_class("granite3:2b") == "small"
    # A large-parameter model correctly stays "large" via the same parser.
    assert resolve_model_capability_class("llama3.1:70b") == "large"


def test_resolve_model_capability_class_unparseable_defaults_large():
    assert resolve_model_capability_class("some-frontier-model:latest") == "large"


def test_resolve_model_capability_class_existing_table_entries_unchanged():
    """Defaults byte-identical for every currently-listed model: table
    lookups (and the gemma4 MoE-family prefix) take priority over the
    generic parse."""
    for name in SMALL_MODEL_PATTERNS:
        expected = "small" if SMALL_MODEL_PATTERNS[name] <= SMALL_MODEL_THRESHOLD_B else "large"
        if any(name.startswith(fam) for fam in MOE_MODEL_FAMILIES):
            expected = "moe"
        assert resolve_model_capability_class(name) == expected, name


def test_resolve_model_capability_class_table_beats_generic_parse(monkeypatch):
    """Issue #207 item 6: 'existing table entries keep priority.' Force a
    table entry whose calibrated value disagrees with what the generic
    ':NNb' parse would infer, and confirm the table wins."""
    monkeypatch.setitem(SMALL_MODEL_PATTERNS, "calibrated-outlier:5b", 999.0)
    # Naive ':NNb' parsing would call this "small" (5.0 <= 10.0), but the
    # hand-calibrated table says otherwise -- the table must win.
    assert resolve_model_capability_class("calibrated-outlier:5b") == "large"


def test_decoder_mode_overrides_default_is_empty():
    assert BudgetConfig().decoder_mode_overrides == {}


def test_decoder_mode_overrides_loads_from_toml(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [budget]
        decoder_mode_overrides = { mistral = "large" }
    """), encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.budget.decoder_mode_overrides == {"mistral": "large"}


def test_decoder_mode_overrides_take_precedence_over_everything():
    """The override is an operator escape hatch -- it wins even over an
    exact SMALL_MODEL_PATTERNS hit and even over a MOE_MODEL_FAMILIES
    prefix match."""
    # Without an override, gemma4:e2b is "moe" (MOE_MODEL_FAMILIES prefix).
    assert resolve_model_capability_class("gemma4:e2b") == "moe"
    assert resolve_model_capability_class(
        "gemma4:e2b", overrides={"gemma4": "large"}
    ) == "large"
    # Substring match, case-insensitive.
    assert resolve_model_capability_class(
        "Mistral:7B", overrides={"mistral": "small"}
    ) == "small"


def test_decoder_mode_overrides_first_matching_key_wins():
    overrides = {"mistral": "large", "mistral:7b": "small"}
    assert resolve_model_capability_class("mistral:7b", overrides=overrides) == "large"


# ── Item 4 (2026-07-16): budget-tier + abstain constants -> knobs ────────
#
# pipeline/tier_logic.py's hard-coded tier constants (tight/focused ratio
# gates 3.0/1.8, the 0.15 hard score-gate floor, the 0.7 Lagrange
# pull-back multiplier) and ABSTAIN ratio thresholds (1.8 additive / 1.5
# RRF-norm) become [budget] tier_* / [abstain] ratio_threshold* knobs.
# Defaults MUST reproduce the prior literals byte-for-byte. NOTE: these
# were additive-calibrated on owner-corpus probes and the abstain gates
# run ratio-only under RRF — exposing them does NOT recalibrate them
# (that is #287's scope).


def _tier_case(vals, **co_activated):
    """Build (candidates, scores) with descending ``vals``; candidates
    arrive pre-sorted by score exactly like the pipeline call site.
    ``co_activated`` maps index (as ``i<N>``) -> co_activated_with list.
    """
    candidates = [
        make_gene(
            f"item4_{i}",
            gene_id=f"item4_gene_{i:08d}",
            co_activated_with=co_activated.get(f"i{i}"),
        )
        for i in range(len(vals))
    ]
    scores = {candidates[i].gene_id: vals[i] for i in range(len(vals))}
    return candidates, scores


def _tier_fingerprint(result):
    """Everything a tier decision consists of, for byte-identity compares."""
    return (
        result.budget_tier,
        result.budget_tokens_est,
        [g.gene_id for g in result.candidates],
        sorted(g.gene_id for g in result.shadow_pool),
        dict(result.shadow_scores),
        result.abstain,
        result.abstain_top_score,
        result.abstain_ratio,
    )


def test_tier_abstain_knob_defaults_match_prior_literals():
    b = BudgetConfig()
    assert b.tier_tight_ratio == 3.0
    assert b.tier_focused_ratio == 1.8
    assert b.tier_hard_floor_frac == 0.15
    assert b.tier_lagrange_frac == 0.7
    a = AbstainConfig()
    assert a.ratio_threshold == 1.8
    assert a.ratio_threshold_rrf_norm == 1.5


def test_tier_abstain_knobs_load_from_toml(tmp_path):
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [budget]
        tier_tight_ratio = 4.0
        tier_focused_ratio = 2.2
        tier_hard_floor_frac = 0.25
        tier_lagrange_frac = 0.55

        [abstain]
        ratio_threshold = 2.1
        ratio_threshold_rrf_norm = 1.7
    """), encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.budget.tier_tight_ratio == 4.0
    assert cfg.budget.tier_focused_ratio == 2.2
    assert cfg.budget.tier_hard_floor_frac == 0.25
    assert cfg.budget.tier_lagrange_frac == 0.55
    assert cfg.abstain.ratio_threshold == 2.1
    assert cfg.abstain.ratio_threshold_rrf_norm == 1.7


def test_tier_abstain_knob_validation_falls_back_to_defaults(tmp_path):
    """Non-positive or non-numeric values warn and keep the shipped
    default instead of propagating (a 0 tight ratio would make EVERY
    query TIGHT); valid keys in the same tables still apply."""
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [budget]
        tier_tight_ratio = -1.0
        tier_hard_floor_frac = 0.0
        tier_lagrange_frac = "high"
        tier_focused_ratio = 2.2

        [abstain]
        ratio_threshold = -2
        ratio_threshold_rrf_norm = 1.7
    """), encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.budget.tier_tight_ratio == 3.0      # fell back
    assert cfg.budget.tier_hard_floor_frac == 0.15  # fell back
    assert cfg.budget.tier_lagrange_frac == 0.7     # fell back
    assert cfg.budget.tier_focused_ratio == 2.2     # valid override kept
    assert cfg.abstain.ratio_threshold == 1.8       # fell back
    assert cfg.abstain.ratio_threshold_rrf_norm == 1.7


def test_abstain_scalar_knobs_coexist_with_per_class_subtables(tmp_path):
    """The [abstain] loader discovers class sub-tables by dict-ness; the
    new scalar keys must not be mistaken for classes (and vice versa)."""
    toml = tmp_path / "helix.toml"
    toml.write_text(textwrap.dedent("""
        [abstain]
        mode = "per_classifier"
        ratio_threshold = 2.1

        [abstain.default]
        abstain_top = 1.0
        focused_top = 2.0
        tight_top = 4.0
    """), encoding="utf-8")
    cfg = load_config(str(toml))
    assert cfg.abstain.mode == "per_classifier"
    assert cfg.abstain.ratio_threshold == 2.1
    assert set(cfg.abstain.per_class) == {"default"}
    assert cfg.abstain.per_class["default"].tight_top == 4.0


# Golden tier decisions with default knobs. Each scenario pins the
# behavior the prior literals produced (additive mode unless noted).
_GOLDEN_TIER_CASES = [
    # (vals, fusion_mode, expected_tier, expected_n_candidates, expected_abstain)
    # ratio 3.13 >= 3.0, top 9 >= tight floor 5.0 -> TIGHT top-3
    ([9.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0], "additive", "tight", 3, False),
    # ratio 1.825 >= 1.8, top 6 >= focused floor 2.5 -> FOCUSED top-6
    ([6.0, 2.9, 2.9, 2.9, 2.9, 2.9, 2.9, 2.9], "additive", "focused", 6, False),
    # ratio 1.28 < 1.8 but top 4.0 >= abstain floor 2.5 -> BROAD keeps all
    ([4.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0], "additive", "broad", 8, False),
    # top 2.0 < 2.5 AND ratio 1.356 < 1.8 -> ABSTAIN
    ([2.0, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4], "additive", "broad", 8, True),
    # RRF: norm ratio (0.40-0.05)/(0.15-0.05) = 3.5 >= 1.5 -> no abstain;
    # hard floor 0.40*0.15=0.06 cuts the 0.05 tail (4 left), legacy ratio
    # 0.40/0.15=2.67 < 3.0 and only 4 candidates -> BROAD with 4.
    ([0.40, 0.30, 0.20, 0.10, 0.05, 0.05, 0.05, 0.05], "rrf", "broad", 4, False),
]


@pytest.mark.parametrize("vals,fusion,tier,n,abstain", _GOLDEN_TIER_CASES)
def test_tier_golden_decisions_at_defaults(vals, fusion, tier, n, abstain):
    from helix_context.pipeline.tier_logic import apply_budget_tiers
    candidates, scores = _tier_case(vals)
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), fusion_mode=fusion,
    )
    assert result.abstain is abstain
    if not abstain:
        assert result.budget_tier == tier
        assert len(result.candidates) == n


@pytest.mark.parametrize("vals,fusion,tier,n,abstain", _GOLDEN_TIER_CASES)
def test_tier_defaults_byte_identical_to_config_defaults(vals, fusion, tier, n, abstain):
    """Byte-identity proof: calling apply_budget_tiers with NO knob kwargs
    (the former literals, now parameter defaults) and calling it with the
    knob values a default BudgetConfig()/AbstainConfig() threads through
    the context_manager call site must produce identical TierResults."""
    from helix_context.pipeline.tier_logic import apply_budget_tiers
    b, a = BudgetConfig(), AbstainConfig()
    candidates, scores = _tier_case(vals)
    literal = apply_budget_tiers(
        list(candidates), dict(scores), AbstainClassFloors(), fusion_mode=fusion,
    )
    candidates2, scores2 = _tier_case(vals)
    threaded = apply_budget_tiers(
        list(candidates2), dict(scores2), AbstainClassFloors(), fusion_mode=fusion,
        tight_ratio=b.tier_tight_ratio,
        focused_ratio=b.tier_focused_ratio,
        hard_floor_frac=b.tier_hard_floor_frac,
        lagrange_frac=b.tier_lagrange_frac,
        abstain_ratio_threshold=a.ratio_threshold,
        abstain_ratio_threshold_rrf_norm=a.ratio_threshold_rrf_norm,
    )
    assert _tier_fingerprint(literal) == _tier_fingerprint(threaded)


def test_tier_tight_ratio_override_changes_decision():
    from helix_context.pipeline.tier_logic import apply_budget_tiers
    vals = [9.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]  # ratio 3.13
    candidates, scores = _tier_case(vals)
    default = apply_budget_tiers(candidates, scores, AbstainClassFloors())
    assert default.budget_tier == "tight"
    candidates, scores = _tier_case(vals)
    raised = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), tight_ratio=3.5,
    )
    assert raised.budget_tier == "focused"  # 3.13 < 3.5, falls to focused


def test_tier_focused_ratio_override_changes_decision():
    from helix_context.pipeline.tier_logic import apply_budget_tiers
    vals = [6.0, 2.9, 2.9, 2.9, 2.9, 2.9, 2.9, 2.9]  # ratio 1.825
    candidates, scores = _tier_case(vals)
    raised = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), focused_ratio=2.0,
    )
    assert raised.budget_tier == "broad"  # 1.825 < 2.0


def test_abstain_ratio_threshold_override_changes_decision():
    from helix_context.pipeline.tier_logic import apply_budget_tiers
    vals = [2.0, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4]  # ratio 1.356, top < 2.5
    candidates, scores = _tier_case(vals)
    lowered = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), abstain_ratio_threshold=1.2,
    )
    assert lowered.abstain is False  # 1.356 >= 1.2 now clears the gate


def test_abstain_rrf_norm_threshold_override_changes_decision():
    from helix_context.pipeline.tier_logic import apply_budget_tiers
    vals = [0.40, 0.30, 0.20, 0.10, 0.05, 0.05, 0.05, 0.05]  # norm ratio 3.5
    candidates, scores = _tier_case(vals)
    default = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), fusion_mode="rrf",
    )
    assert default.abstain is False
    candidates, scores = _tier_case(vals)
    raised = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), fusion_mode="rrf",
        abstain_ratio_threshold_rrf_norm=4.0,
    )
    assert raised.abstain is True  # 3.5 < 4.0 trips the raised gate


def test_tier_hard_floor_frac_override_changes_gating():
    from helix_context.pipeline.tier_logic import apply_budget_tiers
    # floor = 10*0.15 = 1.5 cuts the five 1.0-docs -> 3 candidates, BROAD
    vals = [10.0, 9.0, 8.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    candidates, scores = _tier_case(vals)
    default = apply_budget_tiers(candidates, scores, AbstainClassFloors())
    assert default.budget_tier == "broad"
    assert len(default.candidates) == 3
    assert len(default.shadow_pool) == 5
    # shadow scores preserved at 0.5x weight (existing behavior, unchanged)
    assert all(v == 0.5 for v in default.shadow_scores.values())
    # floor = 10*0.05 = 0.5 keeps all 8 -> FOCUSED trims to 6 instead
    candidates, scores = _tier_case(vals)
    loose = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), hard_floor_frac=0.05,
    )
    assert loose.budget_tier == "focused"
    assert len(loose.candidates) == 6
    assert len(loose.shadow_pool) == 2


def test_tier_lagrange_frac_override_changes_pullback():
    from helix_context.pipeline.tier_logic import apply_budget_tiers
    # FOCUSED landscape (ratio 1.87): winners = first 6 (winner floor 5.0);
    # the 4.0-doc survives the 1.35 hard floor but lands in the shadow
    # pool via the FOCUSED trim. Its standalone 4.0 >= winner_floor*0.7 =
    # 3.5 and it shares no co-activation with the winners -> the default
    # Lagrange pull-back replaces the weakest winner with it.
    vals = [9.0, 5.0, 5.0, 5.0, 5.0, 5.0, 4.0, 0.5]
    pulled_id = "item4_gene_00000006"
    candidates, scores = _tier_case(vals, i6=["totally_unrelated_gene"])
    default = apply_budget_tiers(candidates, scores, AbstainClassFloors())
    assert default.budget_tier == "focused"
    assert pulled_id in {g.gene_id for g in default.candidates}
    # lagrange_frac=0.9 -> threshold 4.5 > 4.0: no pull-back
    candidates, scores = _tier_case(vals, i6=["totally_unrelated_gene"])
    strict = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), lagrange_frac=0.9,
    )
    assert strict.budget_tier == "focused"
    assert pulled_id not in {g.gene_id for g in strict.candidates}
