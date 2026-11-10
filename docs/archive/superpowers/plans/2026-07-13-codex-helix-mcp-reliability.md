# Codex Helix MCP Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `python -m helix_context.mcp_server` complete MCP stdio initialization and configure Codex to load that server reliably against the running dogfood Helix instance.

**Architecture:** Keep `helix_context.mcp_server` as an import-compatible shim, but explicitly delegate to the real module's `main()` only when executed as `__main__`. A subprocess-level MCP SDK test pins initialize and tool discovery through the exact command Codex uses. Machine-local Codex TOML then adds explicit working-directory, startup, tool, and Helix HTTP timeout settings.

**Tech Stack:** Python 3.11+, MCP Python SDK, FastMCP, pytest-asyncio, Codex CLI, TOML

## Global Constraints

- Preserve stdio transport and the `helix_context.mcp_server` compatibility path.
- Do not start MCP on ordinary module import and call the real `main()` exactly once on `python -m` execution.
- Keep the lean five-tool profile and existing Helix identity environment unchanged.
- Set Codex `startup_timeout_sec = 30`, `tool_timeout_sec = 120`, `required = true`, and `enabled = true`.
- Set `HELIX_MCP_TIMEOUT = "90"` and `cwd = 'F:\Projects\helix-context'`.
- Every direct Windows `subprocess.Popen` must use `creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)`; this plan uses the MCP SDK instead of a direct `Popen`.
- Every HTTP request retains an explicit timeout.
- Catch `Exception`, never a bare `except`, and log cleanup failures at warning level or higher.
- Use native Windows `python`, not `uv run`.
- Preserve unrelated worktree changes in `helix.toml` and `all_utterance.jsonl`.

---

## File Structure

- Create `tests/test_mcp_stdio_entrypoint.py`: exact-command MCP initialize/list-tools contract and opt-in live health acceptance.
- Modify `helix_context/mcp_server.py`: execute the real server when invoked with `python -m` while preserving import aliasing.
- Modify `C:/Users/max/.codex/config.toml`: harden the existing `mcp_servers.helix-context` block; this machine-local file is not committed.

### Task 1: Pin and repair the stdio entrypoint contract

**Files:**
- Create: `tests/test_mcp_stdio_entrypoint.py`
- Modify: `helix_context/mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `sys.executable -m helix_context.mcp_server`, MCP SDK `ClientSession`, and the real module `helix_context.mcp.mcp_server` as `_real`.
- Produces: import alias compatibility, `_real.main()` delegation only under `__name__ == "__main__"`, and an initialized session whose discovered tool names are the exact lean core: `helix_context`, `helix_context_packet`, `helix_ingest`, `helix_health`, and `helix_sessions_list`.

- [ ] **Step 1: Write the failing end-to-end test**

Create `tests/test_mcp_stdio_entrypoint.py`:

```python
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
CORE_TOOLS = {
    "helix_context",
    "helix_context_packet",
    "helix_ingest",
    "helix_health",
    "helix_sessions_list",
}


def _server_params(tmp_path: Path, helix_url: str) -> StdioServerParameters:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "PYTHONPATH": str(ROOT),
            "HELIX_MCP_URL": helix_url,
            "HELIX_MCP_TIMEOUT": "1",
            "HELIX_MCP_HANDLE": "stdio-contract-test",
            "HELIX_MCP_HOST": "pytest",
            "HELIX_AGENT_KIND": "pytest",
        }
    )
    env.pop("HELIX_MCP_FULL", None)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "helix_context.mcp_server"],
        env=env,
        cwd=str(ROOT),
    )


@pytest.mark.asyncio
async def test_compat_module_starts_stdio_and_lists_lean_tools(tmp_path):
    params = _server_params(tmp_path, "http://127.0.0.1:9")

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        try:
            async with asyncio.timeout(15):
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()
        except Exception as exc:
            errlog.seek(0)
            pytest.fail(f"MCP stdio handshake failed: {exc}\n{errlog.read()}")

    names = {tool.name for tool in result.tools}
    assert names == CORE_TOOLS
```

- [ ] **Step 2: Run the test and confirm premature connection close**

Run:

```powershell
python -m pytest tests/test_mcp_stdio_entrypoint.py::test_compat_module_starts_stdio_and_lists_lean_tools -q
```

Expected: FAIL with `MCP stdio handshake failed`, `Connection closed`, or premature EOF because the compatibility module exits without calling `main()`.

- [ ] **Step 3: Add the minimal entrypoint delegation**

Change `helix_context/mcp_server.py` to:

```python
"""Backward-compat shim -- real module at helix_context.mcp.mcp_server."""

import sys

from . import mcp as _mcp_pkg  # noqa: F401 - ensure parent package loaded
from .mcp import mcp_server as _real

sys.modules[__name__] = _real

if __name__ == "__main__":
    _real.main()
```

- [ ] **Step 4: Run the stdio contract test**

Run:

```powershell
python -m pytest tests/test_mcp_stdio_entrypoint.py::test_compat_module_starts_stdio_and_lists_lean_tools -q
```

Expected: `1 passed`; initialization completes and the exact five core tools are listed.

- [ ] **Step 5: Run MCP import/profile regression tests**

Run:

```powershell
python -m pytest tests/test_mcp_server.py tests/test_mcp_stdio_entrypoint.py -q
```

Expected: all tests pass, proving import behavior remains side-effect-free and the lean profile is unchanged.

- [ ] **Step 6: Commit the tested contract and entrypoint repair**

```powershell
git add helix_context/mcp_server.py tests/test_mcp_stdio_entrypoint.py
git commit -m "fix(mcp): run compatibility stdio entrypoint"
```

### Task 2: Harden the machine-local Codex MCP configuration

**Files:**
- Modify: `C:/Users/max/.codex/config.toml` (machine-local, never commit)

**Interfaces:**
- Consumes: the existing `[mcp_servers.helix-context]` and `.env` blocks.
- Produces: an enabled, required stdio server with explicit working-directory and timeout values.

- [ ] **Step 1: Read and preserve the current Helix MCP block**

Run:

```powershell
codex mcp get helix-context
```

Expected: command points to the native Windows Python with args `-m helix_context.mcp_server`; existing identity variables and `HELIX_MCP_URL` are present and redacted by the CLI.

- [ ] **Step 2: Add server-level reliability keys**

In `C:/Users/max/.codex/config.toml`, make the existing server block contain these keys before `[mcp_servers.helix-context.env]`, without rewriting its command or args:

```toml
[mcp_servers.helix-context]
args = ["-m", "helix_context.mcp_server"]
command = 'C:\Users\max\AppData\Local\Python\pythoncore-3.14-64\python.exe'
cwd = 'F:\Projects\helix-context'
enabled = true
required = true
startup_timeout_sec = 30
tool_timeout_sec = 120
```

- [ ] **Step 3: Add the Helix HTTP timeout without changing identity**

In the existing environment block add exactly one variable:

```toml
[mcp_servers.helix-context.env]
HELIX_MCP_TIMEOUT = "90"
```

Keep every existing `HELIX_AGENT`, `HELIX_AGENT_KIND`, `HELIX_DEVICE`, `HELIX_MCP_HANDLE`, `HELIX_MCP_HOST`, `HELIX_MCP_URL`, `HELIX_ORG`, `HELIX_PARTY_ID`, `HELIX_USER`, and `PYTHONPATH` value unchanged.

- [ ] **Step 4: Parse and assert the final TOML values**

Run:

```powershell
python -c "from pathlib import Path; import tomllib; d=tomllib.loads((Path.home()/'.codex'/'config.toml').read_text(encoding='utf-8')); s=d['mcp_servers']['helix-context']; assert s['cwd']==r'F:\Projects\helix-context'; assert s['enabled'] is True; assert s['required'] is True; assert s['startup_timeout_sec']==30; assert s['tool_timeout_sec']==120; assert s['env']['HELIX_MCP_TIMEOUT']=='90'; print('helix-context config OK')"
codex mcp get helix-context
```

Expected: `helix-context config OK`, followed by CLI output showing `enabled: true`, the repository `cwd`, and the same command/args. The CLI may redact environment values.

- [ ] **Step 5: Leave the local config uncommitted**

Do not add `C:/Users/max/.codex/config.toml` to the repository and do not copy identity values into tests, logs, or documentation.

### Task 3: Verify live dogfood health through the repaired stdio command

**Files:**
- Modify: `tests/test_mcp_stdio_entrypoint.py`

**Interfaces:**
- Consumes: live Helix at `http://127.0.0.1:11437` only when `HELIX_LIVE_MCP_TEST=1`.
- Produces: an opt-in acceptance test that calls `helix_health` through MCP rather than querying HTTP directly.

- [ ] **Step 1: Add an opt-in live health test**

Append to `tests/test_mcp_stdio_entrypoint.py`:

```python
@pytest.mark.asyncio
async def test_stdio_health_against_live_helix(tmp_path):
    if os.environ.get("HELIX_LIVE_MCP_TEST") != "1":
        pytest.skip("set HELIX_LIVE_MCP_TEST=1 for local dogfood acceptance")

    params = _server_params(tmp_path, "http://127.0.0.1:11437")
    params.env["HELIX_MCP_TIMEOUT"] = "90"

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        try:
            async with asyncio.timeout(120):
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("helix_health")
        except Exception as exc:
            errlog.seek(0)
            pytest.fail(f"Live MCP health probe failed: {exc}\n{errlog.read()}")

    rendered = "\n".join(
        getattr(block, "text", "") for block in result.content
    )
    assert result.isError is False
    assert "available" in rendered
    assert "5,636" in rendered or "5636" in rendered
```

- [ ] **Step 2: Run the offline contract suite**

Run:

```powershell
python -m pytest tests/test_mcp_stdio_entrypoint.py tests/test_mcp_server.py -q
```

Expected: all normal tests pass and the opt-in live test is skipped.

- [ ] **Step 3: Run the live dogfood acceptance test**

Run:

```powershell
$env:HELIX_LIVE_MCP_TEST = "1"
python -m pytest tests/test_mcp_stdio_entrypoint.py::test_stdio_health_against_live_helix -q
Remove-Item Env:HELIX_LIVE_MCP_TEST
```

Expected: `1 passed`; `helix_health` reports available and 5,636 genes through the MCP stdio process.

- [ ] **Step 4: Commit the opt-in acceptance test**

```powershell
git add tests/test_mcp_stdio_entrypoint.py
git commit -m "test(mcp): add live stdio health acceptance"
```

### Task 4: Final verification and Codex reload handoff

**Files:**
- Verify only; modify files only if a failing test reveals a scoped defect.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: repository test evidence, validated local configuration, and a clear reload requirement.

- [ ] **Step 1: Run the focused MCP suite**

```powershell
python -m pytest tests/test_mcp_server.py tests/test_mcp_stdio_entrypoint.py tests/test_status.py -q
```

Expected: all non-live tests pass; the live acceptance test skips unless explicitly enabled.

- [ ] **Step 2: Re-run the live acceptance and configuration assertions**

```powershell
$env:HELIX_LIVE_MCP_TEST = "1"
python -m pytest tests/test_mcp_stdio_entrypoint.py::test_stdio_health_against_live_helix -q
Remove-Item Env:HELIX_LIVE_MCP_TEST
python -c "from pathlib import Path; import tomllib; s=tomllib.loads((Path.home()/'.codex'/'config.toml').read_text(encoding='utf-8'))['mcp_servers']['helix-context']; assert (s['required'],s['startup_timeout_sec'],s['tool_timeout_sec'],s['env']['HELIX_MCP_TIMEOUT'])==(True,30,120,'90'); print('helix-context config OK')"
```

Expected: live test passes and config assertion prints `helix-context config OK`.

- [ ] **Step 3: Check repository hygiene**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only the user's pre-existing `helix.toml` and `all_utterance.jsonl` changes remain uncommitted.

- [ ] **Step 4: Restart Codex and confirm tool loading**

Close and restart Codex, or open a new Codex session. Inspect MCP status and verify `helix-context` is active with its five lean tools. The current session cannot hot-load the newly repaired server, so this restart is required for final host-side acceptance.
