"""`python -m helix_context.mcp_server` must actually start the MCP server.

Regression for bugbash BUG-2: the back-compat shim at
helix_context/mcp_server.py aliased the real module
(helix_context.mcp.mcp_server) into sys.modules but never dispatched to
``main()``, so the documented ``python -m helix_context.mcp_server``
invocation exited 0 after ~1.6s without ever entering the stdio loop.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp SDK extra not installed")

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_python_m_mcp_server_blocks_instead_of_exiting():
    """The -m entry must enter the stdio serve loop (block), not exit 0."""
    env = dict(os.environ)
    # Dead port: registry handshake is best-effort and must not block or
    # crash startup; connection-refused on localhost fails fast.
    env["HELIX_MCP_URL"] = "http://127.0.0.1:1"
    env["HELIX_MCP_LOG_LEVEL"] = "WARNING"
    proc = subprocess.Popen(
        [sys.executable, "-m", "helix_context.mcp_server"],
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
            f"python -m helix_context.mcp_server exited rc={rc} instead of "
            f"serving MCP stdio. stderr:\n{stderr[:2000]}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
