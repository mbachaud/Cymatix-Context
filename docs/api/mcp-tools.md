# Helix Context — MCP Tools

Tools exposed when helix runs as an MCP server (`python -m helix_context.mcp_server`).

## helix_context

Retrieve and compress context for a query. Equivalent to `POST /context`.

**Input schema:**
```json
{
  "query": { "type": "string", "description": "What to retrieve context for" },
  "session_context": { "type": "object", "description": "Optional active-file hints" },
  "decoder_mode": { "type": "string", "enum": ["full", "condensed", "minimal", "none"] }
}
```

**Returns:** compressed expressed_context string with `<GENE>` blocks.

## helix_ingest

Ingest content into the knowledge store.

**Input schema:**
```json
{
  "content": { "type": "string" },
  "source_id": { "type": "string", "description": "File path or URL identifier" },
  "source_kind": { "type": "string", "description": "code | doc | conversation | config" }
}
```

## helix_fingerprint

Navigation-first retrieval — returns scores and source pointers without assembling content. Use when the agent needs to decide whether to fetch, not when it needs the content directly.

**Input schema:**
```json
{
  "query": { "type": "string" },
  "score_floor": { "type": "number", "description": "Minimum score threshold (0.0–10.0)" }
}
```

## MCP server setup

### Claude Code (`~/.claude/settings.json` or project `.claude/settings.json`)

```json
{
  "mcpServers": {
    "helix-context": {
      "command": "python",
      "args": ["-m", "helix_context.mcp_server"],
      "cwd": "/absolute/path/to/your/project",
      "env": {
        "HELIX_MCP_URL": "http://127.0.0.1:11437"
      }
    }
  }
}
```

### Cursor / Continue

Add the same `mcpServers` block under your IDE's MCP configuration file.
