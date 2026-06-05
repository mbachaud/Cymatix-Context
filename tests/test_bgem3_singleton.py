"""A1 — BGE-M3 process-singleton dedup.

Each shard's KnowledgeStore lazy-built its own ~2 GB BGEM3Codec; at 100-shard
scale that duplicated the model up to 100x and drove the daemon's RAM ramp to
~120 GB. ``get_shared_codec`` returns one shared instance per
``(model_name, dim, device)`` so the model loads once and every shard reuses it.

These tests exercise only the singleton bookkeeping — ``BGEM3Codec.__init__`` is
cheap (the ~2 GB model loads lazily in ``_load`` on first ``encode``), so no
model weights are touched here.
"""

from __future__ import annotations

import threading

from helix_context.backends.bgem3_codec import (
    BGEM3Codec,
    get_shared_codec,
    _GLOBAL_CODECS,
)


def _clear_cache():
    _GLOBAL_CODECS.clear()


def test_shared_codec_returns_same_instance_for_same_key():
    _clear_cache()
    a = get_shared_codec(dim=1024, device="cpu")
    b = get_shared_codec(dim=1024, device="cpu")
    assert a is b, "same (model, dim, device) must return the identical object"
    assert isinstance(a, BGEM3Codec)


def test_shared_codec_distinct_for_distinct_keys():
    _clear_cache()
    full = get_shared_codec(dim=1024, device="cpu")
    trunc = get_shared_codec(dim=768, device="cpu")
    assert full is not trunc, "different dim must yield a different codec"


def test_share_false_bypasses_cache():
    _clear_cache()
    a = get_shared_codec(dim=1024, device="cpu", share=False)
    b = get_shared_codec(dim=1024, device="cpu", share=False)
    assert a is not b, "share=False must build a fresh per-call instance (legacy A/B)"
    assert len(_GLOBAL_CODECS) == 0, "share=False must not populate the global cache"


def test_shared_codec_thread_safe_single_instance():
    """Concurrent first-construction (the fan-out hazard) yields ONE instance."""
    _clear_cache()
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()  # maximize contention on the construct path
        results.append(get_shared_codec(dim=1024, device="cpu"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    first = results[0]
    assert all(r is first for r in results), "race produced >1 codec instance"
    assert len(_GLOBAL_CODECS) == 1
