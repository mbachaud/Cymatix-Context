"""Issue #207 (de-hardcoding wave 2, items 1-3): config knobs for the SPLADE /
SEMA model IDs, the SPLADE ingest content cap, and the citation-shortener
ingest-root anchors. Every default must reproduce the prior hardwired literal
byte-for-byte; overrides let air-gap / mirror deployments repoint the models
and stop owner path segments leaking into citations.
"""
import sqlite3
import textwrap

import pytest

from helix_context.config import HelixConfig, load_config
from helix_context.context_manager import _shorten_source_path
from helix_context.storage.indexes import sync_splade_index


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
