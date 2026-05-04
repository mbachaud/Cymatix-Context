def test_upstream_timeout_default_is_180s():
    """Regression test for the 2026-05-02 default bump.

    Helix's default of 120s was observed to silently return Proxy 500s
    on slow gemma4:e4b GPQA queries at ~125s (full breakdown in the
    2026-05-01 overnight report). 180s is the shipping default; 120s
    is a regression.
    """
    from helix_context.config import HelixConfig
    cfg = HelixConfig()
    assert cfg.server.upstream_timeout == 180.0
