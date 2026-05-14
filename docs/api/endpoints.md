# Helix Context — HTTP Endpoints

Full reference for all HTTP endpoints exposed by the helix server at `http://localhost:11437`.

## Retrieval

### POST /context

Retrieve and compress context for a query. Returns the assembled, compressed
content blob, structured per-document citations, `ContextHealth` metadata,
and an agent-mode `know`/`miss` block.

**Request:**
```json
{
  "query": "string",
  "session_context": {},
  "include_cold": null,
  "decoder_mode": "full",
  "party_id": "string"
}
```

**Response:** a single-element list wrapping the response dict.

```json
[
  {
    "name": "Helix Genome Context",
    "description": "5 genes expressed, 4.2x compression, health=aligned (Δε=0.32)",
    "content": "[gene=abc12345 ◆ fired=harmonic:2.3,lex_anchor:1.1 1200→320c]\nspliced text...\n---\n[gene=def67890 ◇ fired=sema_boost:1.8 180c]\n...",
    "context_health": {
      "status": "aligned",
      "ellipticity": 0.32,
      "genes_expressed": 5,
      "genes_available": 12
    },
    "agent": {
      "recommendation": "trust",
      "hint": "Context is well-grounded. Use directly.",
      "citations": [
        {"gene_id": "abc12345abc1", "source": "path/to/file.py", "score": 1.42},
        {"gene_id": "def67890def6", "source": "README.md", "score": 0.93}
      ],
      "latency_ms": 87.4,
      "total_tokens_est": 1820,
      "compression_ratio": 4.2,
      "budget_tier": "focused"
    },
    "know": {"found": true, "confidence": 0.74, "gene_id_match": "abc12345abc1"}
  }
]
```

Per-document metadata lives in `agent.citations[]` (gene_id, source,
score, optional `authored_by_party`/`authored_by_handle`). The inline
`[gene=...]` headers in `content` are the legibility-header format
described in `helix_context/encoding/legibility.py`.

> **Note (legacy format).** Helix used to embed each delivered document
> as `<GENE src="...">BODY</GENE>` inside `expressed_context`. The live
> renderer no longer emits that markup; parsers should consume
> `agent.citations` instead. The `<GENE src=...>` form survives only in
> historical JSONL captures -- see `benchmarks/_citations.py` for the
> shared parser that handles both shapes.

### POST /context/packet

Agent-safe retrieval. Returns pointers + verdict without assembling content.

**Request:** same as `/context`

**Response:**
```json
{
  "items": [
    {
      "gene_id": "c084a6dc",
      "source_id": "/path/to/file.py",
      "source_path": "/path/to/file.py",
      "verdict": "verified",
      "coord_confidence": 0.92,
      "freshness": 0.88,
      "refresh_targets": []
    }
  ],
  "context_health": {}
}
```

Verdict values: `verified` | `stale_risk` | `needs_refresh`

### GET /fingerprint

Navigation-first retrieval — scores and metadata without content. Supports `score_floor` filtering and honest accounting (`evaluated_total / above_floor_total / filtered_by_floor / truncated_by_cap`).

## Ingest / Lifecycle

### POST /ingest

Ingest a document or conversation exchange into the knowledge store.

**Request:**
```json
{
  "content": "string",
  "source_id": "/path/to/file.py",
  "source_kind": "code",
  "party_id": "string"
}
```

### POST /replicate

Persist a context exchange back into the knowledge store (co-activation learning).

### POST /compact

Compact the knowledge store — run the density gate over all OPEN documents and demote low-signal ones to EUCHROMATIN/HETEROCHROMATIN.

## Admin / Maintenance

### GET /stats

Returns knowledge store size, compression ratio, and tier metrics.

```json
{
  "total_genes": 18547,
  "compression_ratio": 5.0,
  "chromatin_open": 14200,
  "chromatin_euchromatin": 3100,
  "chromatin_heterochromatin": 1247
}
```

### GET /health

Liveness check. Returns `{"status": "ok"}`.

### POST /admin/refresh

Hot-reload helix.toml config without restarting the server.

## OpenAI-compatible proxy

### POST /v1/chat/completions

Drop-in replacement for the OpenAI chat completions endpoint. Helix intercepts the messages, runs `/context` to build the context window, injects it into the system message, then forwards to the configured downstream model.

```bash
ANTHROPIC_BASE_URL=http://localhost:11437 claude
OPENAI_BASE_URL=http://localhost:11437/v1 your-app
```
