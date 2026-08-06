"""W3.2 executor sizing (perf campaign, gate G1 follow-through).

G1 verdict (docs/benchmarks/2026-08-02-perf-slice2-concurrency.md): server
qps saturates at 1.46 from c=2 through c=16 because every /context and
/ingest funnels through a module-global 2-thread pool. The pool becomes
lazy + env-sized (CYMATIX_EXECUTOR_WORKERS) so the c-curve receipt can
sweep sizes without code edits; the default stays 2 until the receipt
picks the knee.
"""

from __future__ import annotations

import pytest

from cymatix_context import context_manager as cm


@pytest.fixture
def _fresh_executor(monkeypatch):
    """Reset the lazy singleton around each test and shut down pools we
    create so worker threads don't leak into other tests."""
    created = []
    old = cm._executor
    cm._executor = None
    yield created
    if cm._executor is not None and cm._executor is not old:
        cm._executor.shutdown(wait=False, cancel_futures=True)
    cm._executor = old


def test_executor_defaults_to_two_workers(_fresh_executor, monkeypatch):
    monkeypatch.delenv("CYMATIX_EXECUTOR_WORKERS", raising=False)
    ex = cm._get_executor()
    assert ex._max_workers == 2


def test_executor_env_override(_fresh_executor, monkeypatch):
    monkeypatch.setenv("CYMATIX_EXECUTOR_WORKERS", "5")
    ex = cm._get_executor()
    assert ex._max_workers == 5


def test_executor_env_invalid_falls_back(_fresh_executor, monkeypatch):
    monkeypatch.setenv("CYMATIX_EXECUTOR_WORKERS", "banana")
    ex = cm._get_executor()
    assert ex._max_workers == 2


def test_executor_env_floor_is_one(_fresh_executor, monkeypatch):
    monkeypatch.setenv("CYMATIX_EXECUTOR_WORKERS", "0")
    ex = cm._get_executor()
    assert ex._max_workers == 1


def test_executor_is_lazy_singleton(_fresh_executor, monkeypatch):
    monkeypatch.setenv("CYMATIX_EXECUTOR_WORKERS", "3")
    assert cm._get_executor() is cm._get_executor()


def test_no_use_site_bypasses_the_getter():
    """Every run_in_executor call must go through _get_executor() — a bare
    module-global reference would freeze the size at import time."""
    import inspect

    src = inspect.getsource(cm)
    assert "run_in_executor(_executor" not in src, (
        "a run_in_executor call still references the raw module global"
    )
