# Cymatix Knowledge Graph

> How documents, dimensions, and connections form a queryable knowledge graph
> in the cymatix-context + Headroom stack.

## Graph Structure

```
                        ┌─────────────┐
                        │   QUERY     │
                        │  "how does  │
                        │  auth work?"│
                        └──────┬──────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
        ┌──────────┐   ┌──────────┐      ┌──────────┐
        │   TAGS   │   │   FTS5   │      │   ΣĒMA   │
        │ D2 match │   │ D1 match │      │ D1 cos.  │
        └────┬─────┘   └────┬─────┘      └────┬─────┘
             │              │                  │
             └──────────────┼──────────────────┘
                            ▼
                   ┌─────────────────┐
                   │ Candidate Docs   │
                   │  (from store)    │
                   └────────┬────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼              ▼
        ┌──────────┐ ┌──────────┐  ┌──────────┐
        │ CYMATICS │ │   TIER   │  │ WORKING  │
        │ D6 score │ │ D5 tier  │  │ SET D4   │
        └────┬─────┘ └────┬─────┘  └────┬─────┘
             │             │              │
             └─────────────┼──────────────┘
                           ▼
                  ┌─────────────────┐
                  │  Ranked Docs     │
                  │  (top-k scored)  │
                  └────────┬────────┘
                           │
                           ▼
                  ┌─────────────────┐
                  │  HEADROOM        │
                  │  compress → out  │
                  └─────────────────┘
```

---

## Node Types

### Document (primary node)

The fundamental unit. Every piece of knowledge in cymatix is a document.

```
┌─────────────────────────────────────────────┐
│  DOCUMENT: a7f3b2c1                         │
│                                             │
│  content:    "## Auth Middleware\n..."       │
│  complement: "JWT auth with session..."     │
│  source_id:  "fleet/skills/auth.py"         │
│  tier:  0 (OPEN)                            │
│                                             │
│  ┌─ Fragments ──────────────────────────┐   │
│  │  auth:1.0  jwt:0.8  session:0.5     │   │
│  │  security:0.8  middleware:0.6        │   │
│  └──────────────────────────────────────┘   │
│                                             │
│  ┌─ Tags ───────────────────────────────┐   │
│  │  domains: [auth, security, backend]  │   │
│  │  entities: [JWT, OAuth, OWASP]       │   │
│  └──────────────────────────────────────┘   │
│                                             │
│  ┌─ Key-Values ─────────────────────────┐   │
│  │  token_expiry: "30m"                 │   │
│  │  algorithm: "HS256"                  │   │
│  │  session_store: "redis"              │   │
│  └──────────────────────────────────────┘   │
│                                             │
│  ┌─ Embedding (ΣĒMA 20-dim) ───────────┐   │
│  │  [0.08, 0.01, 0.07, -0.05, ...]     │   │
│  └──────────────────────────────────────┘   │
│                                             │
│  ┌─ Signals ────────────────────────────┐   │
│  │  access_rate: 2.3/hour               │   │
│  │  decay_score: 0.95                   │   │
│  │  co_activated_with: [b3e1..., c4f2]  │   │
│  └──────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
```

### Edge Types (connections between documents)

```
 Document A ─── co_activated_with ──────► Document B
               (epigenetics field; legacy: co-activation)
               "retrieved together in same query"

 Document A ─── harmonic_link ──────────► Document B
               (harmonic_links table)
               "spectral similarity via cymatics"
               weight: cosine of frequency spectra

 Document A ─── entity_link ────────────► Document B
               (entity_graph table)
               "share a named entity"
               entity: "JWT", "OAuth", etc.

 Document A ─── supersedes ─────────────► Document B
               (genes.supersedes field)
               "newer version of same content"
               version: document.version

 Document A ─── relation ───────────────► Document B
               (gene_relations table)
               "semantic relation"
               type: entails | contradicts | neutral
```

---

## The Six Active Dimensions as Graph Filters

Each dimension acts as a filter or scoring function over the graph:

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  D1  Semantic    ─── FTS5 term match + SPLADE expansion     │
│                      + ΣĒMA cosine similarity               │
│                      "does this document CONTAIN relevant terms?"│
│                                                             │
│  D2  Tags        ─── domain + entity tag intersection       │
│                      + synonym expansion (cymatix.toml)       │
│                      "is this document ABOUT the right topic?"  │
│                                                             │
│  D3  Source      ─── deny-list filter + authority bonus     │
│                      "is this document FROM a trusted source?"  │
│                                                             │
│  D4  Working-set ─── access_rate(document, window) tiebreaker │
│                      "is this document RECENTLY active?"        │
│                                                             │
│  D5  Tier        ─── tier filter (hot/warm/cold)            │
│                      + cold-tier ΣĒMA fallthrough           │
│                      "is this document ACCESSIBLE right now?"   │
│                                                             │
│  D6  Cymatics    ─── frequency-domain resonance scoring     │
│                      "does this document RESONATE with query?"  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

D2 was originally named *Promoter* and D5 *Chromatin*; both dimensions
are shown above under their canonical software names (Tags, Tier).

---

## Traversal Example

Query: `"how does the auth middleware handle expired tokens?"`

```
Step 1 — Signal extraction
    keywords: [auth, middleware, handle, expired, tokens]
    entities: [auth, middleware, tokens]

Step 2 — Graph traversal (candidate retrieval)
    D1 FTS5:    "auth" OR "middleware" OR "tokens"  → 47 documents
    D1 SPLADE:  expanded terms from splade_terms    → +12 documents
    D2 Tags:    domains ∩ {auth, security, backend} → 31 documents
    D1 ΣĒMA:    cosine(query_embed, doc_embed) > 0.3 → +8 documents
    Deduplicate → 52 unique candidate documents

Step 3 — Scoring (dimension fusion)
    D6 Cymatics resonance_rank:
        Document "auth.py chunk 1"    → score 0.89  (auth + jwt peaks resonate)
        Document "redis session mgr"  → score 0.72  (session + token peaks)
        Document "OWASP top 10 ref"   → score 0.41  (security broad match)
        ...
    D5 Tier: filter out the cold tier (legacy: heterochromatin) (unless include_cold)
    D4 Working-set: tiebreak by access_rate for equal scores
    D3 Source: authority bonus for fleet/skills/* sources
    Top-12 selected (budget.max_genes_per_turn)

Step 4 — Compression (Headroom)
    Document "auth.py chunk 1" (3200 chars) → Kompress → 980 chars
    Document "redis session mgr" (2100 chars) → Kompress → 720 chars
    ...

Step 5 — Assembly
    response[0].content:
      [gene=abc12345 ◆ fired=harmonic:2.3,sema_boost:1.4 3200→980c]
      compressed auth middleware content...
      ---
      [gene=def67890 ◇ fired=harmonic:1.8,working_set:0.5 2100→720c]
      compressed session content...
      ...

    response[0].agent.citations:
      [
        {"gene_id": "abc12345...", "source": "fleet/skills/auth.py",   "score": 0.89},
        {"gene_id": "def67890...", "source": "fleet/session_manager.py", "score": 0.72},
        ...
      ]
```

Per-document metadata (gene_id, source path, score, optional attribution)
lives in `agent.citations[]`. The inline `[gene=...]` headers are the
legibility-header format defined in `cymatix_context/encoding/legibility.py`.

> The pre-2026-05 renderer emitted `<GENE src="..." score="..." facts="...">BODY</GENE>`
> blocks inline. That markup is gone from live `/context` responses;
> historical JSONL files still carry it and are parseable via the
> legacy fallback in `benchmarks/_citations.py`.

---

## Graph Storage Layout

```
genome.db (SQLite)
│
├── genes              ─── Primary node table (17,623 rows)
│   ├── gene_id            (TEXT PK, content-addressed hash)
│   ├── content            (TEXT, full original content)
│   ├── complement         (TEXT, dense summary)
│   ├── codons             (TEXT JSON, weighted term labels)
│   ├── promoter           (TEXT JSON, {domains, entities})
│   ├── epigenetics        (TEXT JSON, {access_rate, decay, co_activated_with})
│   ├── chromatin          (INT, 0=OPEN 1=EUCHRO 2=HETERO)
│   ├── embedding          (TEXT JSON, 20-dim ΣĒMA vector)
│   ├── key_values         (TEXT JSON, extracted KV pairs)
│   ├── source_id          (TEXT, file path or ingest source)
│   ├── version            (INT, monotonic per document lineage)
│   └── supersedes         (TEXT, gene_id of prior version)
│
├── genes_fts          ─── FTS5 full-text index (D1)
├── splade_terms       ─── Sparse expansion terms (D1)
├── promoter_index     ─── Tag index (D2)
│
├── harmonic_links     ─── Cymatics spectral edges (D6 → D8)
│   ├── gene_a, gene_b
│   └── weight             (cosine of frequency spectra)
│
├── entity_graph       ─── Named entity co-occurrence (D8)
│   ├── gene_a, gene_b
│   └── entity             (shared entity string)
│
├── gene_relations     ─── Semantic relations (D8)
│   ├── gene_a, gene_b
│   └── relation           (entails | contradicts | neutral)
│
├── gene_attribution   ─── Party/participant ownership (D7)
│   ├── gene_id
│   ├── party_id           (org/tenant)
│   └── participant_id     (agent/skill/human)
│
├── parties            ─── Tenant registry (D7, 0 rows)
├── participants       ─── Agent/skill registry (D7, 0 rows)
├── hitl_events        ─── HITL pause logger (0 rows)
└── health_log         ─── Knowledge-store health snapshots
```

---

## Cymatix + Headroom Bundle Summary

```
┌─────────────────────────────────────────────────────────────┐
│                     CYMATIX + HEADROOM                      │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                    CYMATIX                             │  │
│  │                                                       │  │
│  │  Ingest ─── Chunk + Tag + Embed + Gate ──► genome.db  │  │
│  │                                                       │  │
│  │  Retrieve ── FTS5 + SPLADE + ΣĒMA + Tags              │  │
│  │              + Cymatics resonance scoring              │  │
│  │              + Lifecycle-tier filtering                │  │
│  │              + Working-set access rate                 │  │
│  │              + Cold-tier ΣĒMA fallthrough              │  │
│  │                                                       │  │
│  │  ALL CPU. No LLM calls.                               │  │
│  └───────────────────────────────────────────────────────┘  │
│                          │                                  │
│                          ▼                                  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                  HEADROOM                              │  │
│  │                                                       │  │
│  │  Compress ── Kompress (ModernBERT) for prose + code   │  │
│  │              LogCompressor for build/test output       │  │
│  │              DiffCompressor for patches                │  │
│  │              Target: ~1000 chars per document           │  │
│  │                                                       │  │
│  │  ALL CPU. No LLM calls.                               │  │
│  └───────────────────────────────────────────────────────┘  │
│                          │                                  │
│                          ▼                                  │
│               Compressed, scored context                    │
│               ready for ANY downstream model                │
│               (local 4B or frontier API)                    │
└─────────────────────────────────────────────────────────────┘
```

**Zero LLM calls from ingest to retrieval.** The first LLM in the chain is
the downstream model that receives the compressed context. This is the core
value proposition: cymatix + Headroom turn raw files into query-ready, compressed,
scored context using only CPU, making it model-agnostic and cost-free at
retrieval time.
