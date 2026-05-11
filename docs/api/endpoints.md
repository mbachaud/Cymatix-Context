# Helix Context — HTTP Endpoints

Full reference for all HTTP endpoints exposed by the helix server at `http://localhost:11437`.

## Retrieval

### POST /context

Retrieve and compress context for a query. Returns `expressed_context` (assembled, compressed window) and `ContextHealth` metadata.

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

**Response:**
```json
{
  "expressed_context": "<GENE src=\"...\">...</GENE>",
  "genes_expressed": 5,
  "budget_tier": "focused",
  "context_health": {
    "retrieval_rate": 1.0,
    "top_score": 8.3,
    "score_ratio": 4.1
  }
}
```

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
