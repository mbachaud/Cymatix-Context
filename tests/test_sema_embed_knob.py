"""#227: [ingestion] sema_embed_on_ingest gates the ingest-time SEMA encode.

Default True preserves prior behaviour; setting it False lets a lexical-only /
multi-worker bench skip the MiniLM load entirely (the manager then never
constructs the SEMA codec, so there is no per-worker OOM).
"""
from cymatix_context.config import IngestionConfig, load_config


def test_default_is_true():
    assert IngestionConfig().sema_embed_on_ingest is True


def test_toml_can_disable(tmp_path):
    p = tmp_path / "helix.toml"
    p.write_text("[ingestion]\nsema_embed_on_ingest = false\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.ingestion.sema_embed_on_ingest is False


def test_toml_omitted_keeps_default_true(tmp_path):
    p = tmp_path / "helix.toml"
    p.write_text('[ingestion]\nbackend = "cpu"\n', encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.ingestion.sema_embed_on_ingest is True
