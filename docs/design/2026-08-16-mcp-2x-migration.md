# MCP 2.x migration (#326)

**Date:** 2026-08-16 · **Status:** decided, implemented · **Supersedes:** the `mcp>=1.0,<2` stopgap pin from #325

## Decision 1 — new import home: `mcp.server.mcpserver.MCPServer`

`mcp` 2.0.0 (released 2026-07-28) removed `mcp.server.fastmcp` entirely. The
ergonomic server API now lives at `mcp.server.mcpserver.MCPServer` (docstring:
"MCPServer - A more ergonomic interface for MCP servers"), re-exported as
`mcp.server.MCPServer` — the spelling the upstream README quickstart uses.
Verified against a real 2.0.0 install, not docs from memory. We import from
the defining submodule (`from mcp.server.mcpserver import MCPServer`) so the
test guards (`pytest.importorskip("mcp.server.mcpserver")`) track exactly the
module the server needs — the same submodule-guard discipline #325 introduced
after `importorskip("mcp")` failed to catch the fastmcp removal.

The API is near drop-in for our usage: `MCPServer("cymatix")` takes the name
positionally; `@mcp.tool()` still returns the decorated function unchanged
(typed `Callable[[_CallableT], _CallableT]`), so tests that call the tool
functions directly keep working; `remove_tool(name)` is still public (raises
`ToolError` on unknown names — our prune already wraps per-tool try/except);
and the `_tool_manager._tools` dict that `_apply_mcp_profile` enumerates kept
its shape (`mcp.server.mcpserver.tools.tool_manager.ToolManager`). One
adjacent rename to know about: `mcp.types.Tool.inputSchema` is now the python
attribute `input_schema` (the `inputSchema` wire alias is unchanged); our
server never touches `mcp.types` directly, so only diagnostic scripts care.

## Decision 2 — hard floor `mcp>=2,<3`, no 1.x/2.x shim

The extra is optional (`pip install cymatix-context[mcp]`), nothing in the
core retrieval path imports the SDK, and the resolver has no reason to hold
2.x back for our users. A dual-version shim would buy a try/except import,
a second test matrix, and divergent behavior windows — for an audience of
approximately nobody: anyone stuck on mcp 1.x in a shared environment can
stay on the previous cymatix release, and upstream keeps 1.x alive on its
`v1.x` branch for exactly that case. The `<3` cap is deliberate: this issue
existed because `mcp>=1.0` was unbounded when 2.0.0 dropped; we don't repeat
that with 3.x.

## Decision 3 — stdio transport/lifespan: no host-visible change

`MCPServer.run()` still defaults to `transport="stdio"` and still runs
`anyio.run(self.run_stdio_async)` → `stdio_server()` + lowlevel server loop,
same as 1.x FastMCP, so `python -m cymatix_context.mcp_server` spawned by
Claude Code / Claude Desktop behaves identically (our `main()` needs no
change; registry handshake still happens before `mcp.run()`). 2.0.0 states it
supports the 2026-07-28 MCP spec *and every earlier revision*; an end-to-end
smoke on Windows (2.x stdio client spawning our server as a subprocess)
negotiated protocol `2025-11-25` and listed the lean 5-tool surface over the
wire. The constructor grew an explicit `lifespan=` kwarg (default `None`);
we don't use lifespans, so nothing to port. New in 2.x on Windows: a
`pywin32` dependency (subprocess/stdio plumbing) — installs cleanly, no code
impact.

## Contract proof

Tool name + description + inputSchema dumps of both surfaces (lean 5-tool
default and `CYMATIX_MCP_FULL=1` full 24-tool), taken via public
`list_tools()` on mcp 1.28.1 (pre-port) and mcp 2.0.0 (post-port), diff
byte-for-byte identical. The #219 slice-3 token-budget contract — core names
`cymatix_context`, `cymatix_context_packet`, `cymatix_ingest`,
`cymatix_health`, `cymatix_sessions_list`, plus the `CYMATIX_MCP_FULL=1`
opt-in — survives unchanged.
