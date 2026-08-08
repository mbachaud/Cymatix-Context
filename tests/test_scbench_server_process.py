"""Fresh-genome Cymatix sidecar lifecycle tests."""

from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

import cymatix_context.integrations.scbench.server_process as server_process_mod
from cymatix_context.integrations.scbench.server_process import (
    CymatixServerProcess,
    SidecarStartupError,
)


def _popen_stub(pid: int = 4242) -> Mock:
    process = Mock()
    process.pid = pid
    process.poll.return_value = None
    process.wait.return_value = 0
    return process


def test_server_process_pins_model_free_config(tmp_path):
    process = CymatixServerProcess.for_run(tmp_path, port=19191)

    text = process.render_config()

    normalized = text.replace("\\", "/")
    assert "splade_enabled = false" in text
    assert "dense_embed_on_ingest = false" in text
    assert "sema_embed_on_ingest = false" in text
    assert "dense_embedding_enabled = false" in text
    assert "[ribosome]\nenabled = false" in text
    assert str(process.genome_path).replace("\\", "/") in normalized
    assert "port = 19191" in text


def test_spawn_hides_windows_console_and_stamps_spawn_identity(monkeypatch, tmp_path):
    spawned = _popen_stub()
    popen = Mock(return_value=spawned)
    tree_kill = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", tree_kill)
    process = CymatixServerProcess.for_run(tmp_path, port=19191)

    returned = process.start(wait_ready=False)

    assert returned is process
    assert popen.call_args.kwargs["creationflags"] == getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0,
    )
    assert popen.call_args.kwargs["env"]["CYMATIX_CONFIG"] == str(
        process.config_path
    )
    assert popen.call_args.kwargs["env"]["CYMATIX_GENOME_PATH"] == str(
        process.genome_path
    )
    assert popen.call_args.kwargs["env"]["CYMATIX_BENCH_SPAWN_TOKEN"] == (
        process.spawn_token
    )
    assert process.config_path.exists()
    process.stop()
    tree_kill.assert_called_once()


def test_start_refuses_preexisting_scored_genome(monkeypatch, tmp_path):
    popen = Mock(return_value=_popen_stub())
    monkeypatch.setattr(subprocess, "Popen", popen)
    process = CymatixServerProcess.for_run(tmp_path, port=19191)
    process.run_dir.mkdir(parents=True, exist_ok=True)
    process.genome_path.write_bytes(b"previous scored state")

    with pytest.raises(SidecarStartupError, match="fresh genome"):
        process.start(wait_ready=False)

    popen.assert_not_called()


def test_start_rejects_stale_responder_identity(monkeypatch, tmp_path):
    spawned = _popen_stub(pid=4242)
    tree_kill = Mock()
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=spawned))
    monkeypatch.setattr(subprocess, "run", tree_kill)

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return None

        def wait_ready(self, *, deadline_s):
            assert deadline_s == 0.1
            return {
                "status": "ok",
                "checks": {"genome_ready": True},
                "genes": 0,
                "pid": 9999,
                "bench_spawn_token": "some-other-run",
            }

    monkeypatch.setattr(server_process_mod, "CymatixClient", FakeClient)
    process = CymatixServerProcess.for_run(tmp_path, port=19191)

    with pytest.raises(SidecarStartupError, match="stale responder"):
        process.start(wait_ready=True, deadline_s=0.1)

    tree_kill.assert_called_once()
