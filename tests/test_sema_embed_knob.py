"""#227: [ingestion] sema_embed_on_ingest gates the ingest-time SEMA encode.

Setting it False lets a lexical-only / multi-worker bench skip the MiniLM
load entirely (the manager then never constructs the SEMA codec, so there is
no per-worker OOM).

2026-08-19 default flip (#371): the default is now False — at shipped
defaults the only live retrieval consumer of ``gene.embedding`` was the
Tier-4 sema_boost re-rank (consumer audit, PR #399) and the deciding 829k
read-gate cell (n=469, delivered basis, PR #400) measured the flip as a
wash (delivered 264 -> 265/469, ranks byte-identical). An explicit toml
``true`` still opts back in.
"""
from cymatix_context.config import IngestionConfig, load_config


def test_default_is_false():
    """2026-08-19 default flip (#371): sema_embed_on_ingest defaults False —
    Tier-4 sema_boost was its only live consumer at shipped defaults and the
    829k read-gate cell was a wash (+1 delivered needle, PR #400 receipt)."""
    assert IngestionConfig().sema_embed_on_ingest is False


def test_toml_can_enable(tmp_path):
    """An explicit ``sema_embed_on_ingest = true`` must win over the flipped
    default — the re-opt-in path."""
    p = tmp_path / "cymatix.toml"
    p.write_text("[ingestion]\nsema_embed_on_ingest = true\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.ingestion.sema_embed_on_ingest is True


def test_toml_can_disable(tmp_path):
    """Explicit false is honored too (now redundant with the default, but the
    #227 multi-worker-bench opt-out spelling keeps working)."""
    p = tmp_path / "cymatix.toml"
    p.write_text("[ingestion]\nsema_embed_on_ingest = false\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.ingestion.sema_embed_on_ingest is False


def test_toml_omitted_keeps_default_false(tmp_path):
    p = tmp_path / "cymatix.toml"
    p.write_text('[ingestion]\nbackend = "cpu"\n', encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.ingestion.sema_embed_on_ingest is False
