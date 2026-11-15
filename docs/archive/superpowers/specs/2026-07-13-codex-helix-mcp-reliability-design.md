# Codex Helix MCP Reliability Design

**Date:** 2026-07-13

**Status:** Approved for implementation planning

## Summary

Make the configured Helix MCP command complete a real stdio initialization and
make future startup failures visible. Helix HTTP is already healthy; the
failure is in the backward-compatibility module used by Codex. Running
`python -m helix_context.mcp_server` imports the real server module but never
calls its `main()` function, so the process exits before replying to MCP
`initialize`.

## Goals

- Preserve the public `helix_context.mcp_server` module path.
- Make the exact Codex-configured command start the real MCP server.
- Prove stdio initialization and tool discovery in an end-to-end regression
  test.
- Give server startup and Helix-backed tools realistic, explicit timeouts.
- Surface a broken required server at Codex startup instead of silently
  omitting its tools.
- Keep the existing Helix identity environment and dogfood HTTP endpoint.

## Non-goals

- Replace stdio MCP with a different transport.
- Change Helix retrieval, ingestion, or learning behavior.
- Rename MCP tools or expand the lean tool profile.
- Make the MCP process own or restart the Helix HTTP server.
- Modify unrelated launcher process management or log rotation.

## Entrypoint Repair

`helix_context/mcp_server.py` remains a compatibility shim over
`helix_context.mcp.mcp_server`. Import behavior stays compatible for callers
that reference the old module path. When executed as a module, however, the
shim explicitly calls the real module's `main()` exactly once.

The implementation must not start the server merely on import. This preserves
testability and avoids side effects for code that imports the compatibility
module.

## Codex Configuration

The existing `mcp_servers.helix-context` configuration continues to use the
installed native Windows Python and the repository on `PYTHONPATH`. It gains
explicit reliability settings:

- `cwd = 'F:\Projects\helix-context'`
- `enabled = true`
- `required = true`
- `startup_timeout_sec = 30`
- `tool_timeout_sec = 120`
- `HELIX_MCP_TIMEOUT = "90"` in the server environment

The existing identity fields (`HELIX_AGENT`, `HELIX_MCP_HANDLE`, user,
organization, device, and party identifiers) and
`HELIX_MCP_URL=http://127.0.0.1:11437` remain unchanged.

`required = true` intentionally makes a future initialization regression a
visible Codex startup error. It does not require Helix HTTP to be reachable at
MCP process import time: tool registration remains best-effort, and HTTP
availability is reported by health/tool calls.

The config change affects new Codex processes only. After verification, the
user restarts Codex or opens a new session and can confirm the server through
the MCP status UI.

## End-to-End Contract Test

A regression test launches the exact supported command:

```text
<native-python> -m helix_context.mcp_server
```

It speaks MCP over stdio, completes `initialize`, and calls `list_tools`. The
assertion covers the lean profile's stable core tools, including
`helix_context` and `helix_health`, without coupling the test to incidental
ordering.

The test has an explicit timeout and always terminates the child process. Any
direct Windows `subprocess.Popen` call includes
`creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)`. Stderr remains
available for diagnosis but must not be mistaken for protocol stdout.

The test does not require live Helix HTTP because initialization and tool
discovery are the entrypoint contract. A separate local acceptance probe calls
`helix_health` against the already-running dogfood server.

## Failure Semantics

- Premature EOF during initialization fails the contract test.
- Missing expected tools fails the contract test with the discovered tool
  names in the assertion output.
- Initialization and tool calls are bounded by explicit timeouts.
- Cleanup failures are caught as `Exception` and logged at warning level; they
  are not silently swallowed.
- HTTP-backed tool failures retain Helix's existing structured error behavior
  and do not crash the MCP process.

## Verification Strategy

Tests are written before production changes.

1. Add the stdio regression test and confirm it fails against the current shim
   because the connection closes during initialization.
2. Repair the compatibility entrypoint and confirm initialize/list-tools pass.
3. Run the focused MCP test suite and existing module-import compatibility
   tests.
4. Launch an isolated stdio client against the exact configured command and
   verify `helix_health` reports the dogfood server at port 11437.
5. Validate the edited Codex TOML through `codex mcp get helix-context` or the
   equivalent read-only configuration command.
6. Restart/new-session loading is the final user-side acceptance step because
   the active Codex process cannot hot-add MCP tools.

## Implementation Boundary

Repository work is limited to the compatibility entrypoint and its tests.
Machine-local work is limited to the existing Helix MCP block in the user's
Codex configuration. Launcher supervision, alternate transports, tool-profile
expansion, and automatic HTTP-server restart are separate concerns.
