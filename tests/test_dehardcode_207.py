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
"""
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

from helix_context.config import HelixConfig, IngestionConfig, RetrievalConfig, load_config
from helix_context.context_manager import _shorten_source_path
from helix_context.knowledge_store import KnowledgeStore
from helix_context.storage.indexes import sync_splade_index
from tests.conftest import FakeBGEM3Codec


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
