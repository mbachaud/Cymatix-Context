"""`python -m cymatix_context.mcp_server` must actually start the MCP server.

Regression for bugbash BUG-2: the back-compat shim at
cymatix_context/mcp_server.py aliased the real module
(cymatix_context.mcp.mcp_server) into sys.modules but never dispatched to
``main()``, so the documented ``python -m cymatix_context.mcp_server``
invocation exited 0 after ~1.6s without ever entering the stdio loop.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "mcp.server.mcpserver",
    reason="mcp SDK extra not installed, or too old to expose "
           "mcp.server.mcpserver (mcp 2.x home of MCPServer) — the spawned "
           "subprocess would die on import, failing the test spuriously",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_invalid_configured_caller_class_fails_startup():
    """A bad process-wide model class fails before the adapter advertises tools."""
    env = dict(os.environ)
    env["CYMATIX_CALLER_MODEL_CLASS"] = "oversized"
    proc = subprocess.run(
        [sys.executable, "-c", "import cymatix_context.mcp.mcp_server"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "CYMATIX_CALLER_MODEL_CLASS" in combined
    assert all(value in combined for value in ("generic", "small_moe", "frontier"))


def test_python_m_mcp_server_blocks_instead_of_exiting():
    """The -m entry must enter the stdio serve loop (block), not exit 0."""
    env = dict(os.environ)
    # Dead port: registry handshake is best-effort and must not block or
    # crash startup; connection-refused on localhost fails fast.
    env["CYMATIX_MCP_URL"] = "http://127.0.0.1:1"
    env["CYMATIX_MCP_LOG_LEVEL"] = "WARNING"
    proc = subprocess.Popen(
        [sys.executable, "-m", "cymatix_context.mcp_server"],
        cwd=str(_REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        try:
            rc = proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            # Still running after the grace window -> stdio loop is up.
            return
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        pytest.fail(
            f"python -m cymatix_context.mcp_server exited rc={rc} instead of "
            f"serving MCP stdio. stderr:\n{stderr[:2000]}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
