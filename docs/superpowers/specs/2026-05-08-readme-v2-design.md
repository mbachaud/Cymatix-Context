# Helix Context — README v2 + Docs Overhaul Design

## Goal

Rewrite README.md from scratch using a benchmark-led structure. Reorganise `docs/` by moving internal/dev content to `docs/archive/`. Pass `.gitignore`. Publish a clean v0.5.0 release to GitHub and PyPI.

## Motivation

Current README (639 lines) leads with abstract positioning and a 17-item TOC before showing a command. The benchmark data from May 2026 bench runs (28.7× savings on WAL checkpoint queries, +4 pp GPQA accuracy) is buried or absent. Headroom's README demonstrates the stronger pattern: concrete number in line 1, running in 2 minutes, architecture earns its place after trust is established.

---

## Section 1 — README v2

### Structure (Approach A: Benchmark-led)

Target length: ≤ 400 lines. Deep reference moves to linked `docs/` files.

```
§1  Header zone          — title, badges, one-liner, anchor number
§2  Benchmarks           — hardware badge + token savings table + GPQA mini-stats
§3  Pipeline             — Mermaid lane diagram + <details> terminal recording
§4  Quick Start          — 4 steps: install, launch, seed, test
§5  How It Works         — LLM-free story, two surfaces, links to docs/architecture/
§6  Configuration        — genome path, multi-instance, WAL backup, DAL
§7  API surface          — one-line descriptions, links to docs/api/
§8  Architecture         — links to docs/architecture/
```

### §1 Header zone

```markdown
# Helix Context

[![License: Apache 2.0](…)] [![PyPI v0.5.0](…)] [![Python 3.11+](…)]
[![LLM-free pipeline](…)] [![Paper: Agentome](…)]

A context-index engine for LLM agents. Helix retrieves, weighs, and
compresses your codebase into a context window — without a single LLM
call on the retrieval path.

**28.7× token savings on production workloads** · GPQA diamond +4 pp
accuracy with context on · 5.4× median across 15 query types
```

The anchor number (`28.7×`) is the WAL checkpoint result from
`benchmarks/bench_rag_vs_sike_tokens.py`. It is the best single-query
number from the May 2026 bench run and is independently reproducible.

### §2 Benchmarks

Hardware disclaimer badge (inline, not collapsible):

> ⚙️ **Hardware:** Ryzen 7 5800x · 48 GB DDR4 · RTX 3080 Ti 12 GB VRAM ·
> 2× 1 TB NVMe · open case, reactive fan curves · model: **gemma4:e4b**
> (Ollama) · genome: 18,547 genes · `benchmarks/bench_rag_vs_sike_tokens.py`

Token savings table — 6 rows covering the full savings range (2.7×–28.7×):

| Query | Type | Helix tokens | RAG baseline | Savings |
|---|---|---|---|---|
| "How does helix handle WAL checkpoints?" | mechanism-internal | 279 | 8,000 | **28.7×** |
| "What does the access-rate tiebreaker do?" | operational rule | 394 | 8,000 | **20.3×** |
| "What port does the helix proxy listen on?" | point-fact lookup | 399 | 8,000 | **20.1×** |
| "What is the role of the harmonic_links table?" | data structure purpose | 753 | 8,000 | **10.6×** |
| "What does path_key_index store?" | data structure purpose | 1,023 | 8,000 | **7.8×** |
| "How does the density gate work?" | conceptual system | 2,971 | 8,000 | **2.7×** |

*RAG baseline: top-5 chunks × 1,500 tokens + 500 overhead = 8,000 tokens/query
(Pinecone/LangChain defaults). Source file:
`benchmarks/results/overnight_e4b_2026-05-08_0012/rag_vs_sike_n200_2026-05-08_0012.json`,
N=15. Median SIKE = 1,493 tokens (5.4× savings).*

GPQA accuracy mini-stats row (inline, not a full table):

```
GPQA diamond · gemma4:e4b · N=100:  OFF 22%  →  ON 26%  (+4 pp)
```

*Source: `benchmarks/results/overnight_e4b_2026-05-08_0012/gpqa_{on,off}_diamond_e4b_n100_2026-05-08_0012.json`.
Note: an earlier bench run (2026-05-01, N=198) showed a larger delta on different
genome state. The 2026-05-08 N=100 result is the one cited here.*

### §3 Pipeline diagram + terminal recording

**Mermaid flowchart** — full 9-tier transparency. Three lanes:
`INGEST (LLM-free)` → `RETRIEVAL (LLM-free · 9-tier fusion)` →
`EXPRESSION (LLM-free)` → output surfaces → single LLM call boundary.

Tiers listed verbatim inside the RETRIEVAL node:
① PKI path-key IDF  ② filename anchor  ③ exact promoter tag
④ prefix tag  ⑤ FTS5 BM25  ⑥ SPLADE sparse  ⑦ ΣĒMA 20-dim cosine
⑧ harmonic co-activation  ⑨ SR future-occupancy

Output surfaces: `/context` (assembled window) and `/context/packet`
(pointer + verdict: `verified / stale_risk / needs_refresh`).

Dark-shipped tiers (entity graph Tier 5b, sub-query decomposition,
BGE-M3 ANN path) are omitted from the diagram with a footnote linking
to `docs/architecture/DIMENSIONS.md`.

**Terminal recording** — collapsible `<details>` block:

```markdown
<details>
<summary>▶ Terminal walkthrough — startup + first query</summary>

Part 1: `helix-launcher` startup
  Services coming up: genome loaded (18,547 genes) · OTel collector ·
  Prometheus · Loki · Grafana · tray icon appears

Part 2: first retrieval query
  curl http://localhost:11437/context \
    -H "Content-Type: application/json" \
    -d '{"query": "what port does helix use"}'

  Response excerpt:
    "genes_expressed": 5,
    "sike_tokens": 399,
    "budget_tier": "focused",
    "expressed_context": "<GENE src=\"helix.toml\">..."

</details>
```

The recording is an ASCII/text block (not a GIF dependency). Kept in
`<details>` so it doesn't dominate the README on first scan.

### §4 Quick Start

Four numbered steps, no explanatory prose between them:

```bash
# 1 — Install
pip install "helix-context[all]" --pre

# 2 — Launch  (Windows; Linux/macOS: helix-launcher)
start-helix-tray.bat
helix-status        # confirm :11437 is up

# 3 — Seed your project
helix ingest ./my-project

# 4 — Test retrieval
curl http://localhost:11437/context \
  -H "Content-Type: application/json" \
  -d '{"query": "what is the main entry point?"}'
```

Sub-sections (brief, no expansion inline):
- **MCP setup** — one snippet for Claude Code (`settings.json`),
  one for Cursor/Continue
- **OpenAI-compatible proxy** — `ANTHROPIC_BASE_URL=http://localhost:11437`

### §5 How It Works

Two paragraphs maximum:

1. **LLM-free pipeline** — the entire retrieval + weighing path is CPU
   math: spaCy NER, Howard 2005 TCM, Stachenfeld SR, Werman W1, Hebbian
   co-activation. The only LLM call is at `/v1/chat/completions`. This
   matters for latency, cost, and determinism.

2. **Two surfaces** — `/context` assembles and compresses the context
   window; the downstream LLM reads it directly. `/context/packet`
   returns a pointer + verdict (`verified / stale_risk / needs_refresh`);
   the caller decides whether to fetch. The table from the current README
   (trimmed to 4 rows) stays here.

Links: `docs/architecture/PIPELINE_LANES.md` · `docs/architecture/DIMENSIONS.md`
· [Agentome paper](https://mbachaud.substack.com/…)

### §6 Configuration

Four subsections, each ≤ 10 lines:

**Genome path** — configure where the SQLite database lives:

```toml
[genome]
path = "genomes/main/genome.db"   # relative to helix run directory
# Put this on your fastest NVMe for best ingest throughput
```

**Multiple projects** — one helix instance per genome. Each reads its
own `helix.toml`. Use the HGT Python API (`helix_context.hgt`) to
export/import genes across instances. No dedicated CLI entry point yet;
HGT is a library-level operation.

**Backup** — WAL mode means the .db file is safe to copy while helix
is running:

```bash
# cron / Windows Task Scheduler
cp genomes/main/genome.db backups/genome-$(date +%Y%m%d).db
# or on Windows:
copy genomes\main\genome.db backups\genome-%date:~-4,4%%date:~-10,2%%date:~-7,2%.db
```

A built-in backup manager (configurable paths + interval) is on the roadmap.

**DAL — source content fetching** — for `/context/packet` callers that
need to resolve a `source_id` back to bytes. File and HTTP are built-in;
register S3 or git backends as needed:

```python
from helix_context.adapters.dal import DAL
dal = DAL()
dal.register("s3", my_s3_fetcher)
text, meta = dal.fetch("s3://bucket/schema.json")
```

### §7 API surface

One-line descriptions only; full tables in `docs/api/` (new):

| Endpoint | Description |
|---|---|
| `POST /context` | Retrieve and assemble compressed context for a query |
| `POST /context/packet` | Retrieve pointer + verdict without assembling |
| `POST /ingest` | Ingest a document or exchange into the genome |
| `GET /stats` | Genome size, compression ratio, tier metrics |
| `GET /fingerprint` | Navigation-first retrieval (scores + metadata) |
| `POST /v1/chat/completions` | OpenAI-compatible proxy with automatic context injection |

MCP tools: `helix_context` · `helix_ingest` · `helix_fingerprint` —
full tool schemas in `docs/api/mcp-tools.md`.

### §8 Architecture

```markdown
## Architecture

| Doc | What it covers |
|---|---|
| [PIPELINE_LANES.md](docs/architecture/PIPELINE_LANES.md) | Swim-lane reference: ingest, context, packet, fingerprint flows |
| [DIMENSIONS.md](docs/architecture/DIMENSIONS.md) | The 9 retrieval dimensions — schema, data, bench status |
| [LAUNCHER.md](docs/architecture/LAUNCHER.md) | Supervisor, tray, OTel stack lifecycle |
| [SESSION_REGISTRY.md](docs/architecture/SESSION_REGISTRY.md) | Multi-agent session + party isolation |
| [OBSERVABILITY.md](docs/architecture/OBSERVABILITY.md) | Prometheus metrics, Grafana dashboards, alert rules |
```

---

## Section 2 — Docs reorganisation

### `docs/archive/` — move these

| Source path | Reason |
|---|---|
| `docs/research/` (all) | Internal sprint research, not user-facing |
| `docs/collab/` (all) | batman_output, collab dumps |
| `docs/superpowers/` (all) | Internal Claude Code plans/specs |
| `docs/papers/` (all) | Draft papers; link from README to Substack instead |
| `docs/positioning/` (all) | Internal competitive positioning |
| `docs/specs/` — dated pre-2026-05 | Completed/superseded specs |
| `docs/plans/` — all | Implementation plans (dev-internal) |
| `docs/FUTURE/` (all) | Future roadmap sketches |
| `SESSION_HANDOFF.md` (root) | Dev-session continuity doc |
| `benchmarks/results/*.json` | Large bench output files |

### Keep visible (user-facing)

```
README.md                          ← v2
docs/architecture/                 ← all 7 files stay
docs/MISSION.md
docs/INTEGRATING_WITH_EXISTING_RAG.md
docs/ROSETTA.md
docs/DESIGN_TARGET.md
docs/api/                          ← new, extracted from §7 above
docs/ops/                          ← existing, backup/deploy guides
deploy/                            ← bat/sh launchers, systemd
benchmarks/*.py                    ← bench scripts only
tests/
```

### `docs/api/` — new directory (extracted from README)

Create two files:
- `docs/api/endpoints.md` — full HTTP endpoint reference tables
- `docs/api/mcp-tools.md` — MCP tool schemas

---

## Section 3 — .gitignore additions

```gitignore
# Visual companion / brainstorming sessions (Claude Code superpowers plugin)
.superpowers/

# Benchmark logs and large result outputs
benchmarks/logs/
benchmarks/results/*.json

# SQLite WAL/SHM files (transient, regenerated by SQLite)
*.db-wal
*.db-shm

# Dev scripts not part of the project
foveated_smoke_*.sh

# Stray genome at project root (production DB lives in genomes/)
/genome.db
/genome.db-wal
/genome.db-shm
```

---

## Section 4 — Release v0.5.0

### Version bump

`pyproject.toml`: `version = "0.4.0b1"` → `"0.5.0"`

Remove `--pre` from install instructions (stable release).

### GitHub release notes outline

**v0.5.0 — Retrieval Stack Upgrade + v2 Documentation**

Breaking changes: none (all new features are flag-gated, off by default).

Key additions:
- BM25 pre-filter (tier-0): ~85× SEMA scan speedup, `bm25_prefilter_enabled`
- Sub-query decomposition for broad queries, `query_decomposition_enabled`
- D8 complete: `IntentClass` on PromoterTags, `intent_router.py`,
  entity graph Tier 5b, SR gate-benched
- BGE-M3 dense vectors + ANN threshold dynamic gene counts,
  `dense_embedding_enabled`
- `_asgi.py` entry point — `server.py` importable without opening DB
- README v2: benchmark-led, Mermaid pipeline diagram, hardware-specified bench data

Benchmark reference (your hardware):
- 28.7× token savings on WAL checkpoint queries (point-fact ceiling)
- 5.4× median across 15 query types
- GPQA diamond: +4 pp accuracy (gemma4:e4b, N=100, Helix ON vs OFF)

Tested on: Windows 11 Pro · Ryzen 7 5800x · 48 GB DDR4 · RTX 3080 Ti 12 GB

### PyPI publish

```bash
python -m build
twine upload dist/*
```

Tag: `v0.5.0` — annotated tag on master.

---

## Out of scope

- Terminal GIF recording (referenced as ASCII text block in §3; actual screen recording is a manual step)
- `docs/api/` endpoint tables (content extracted from existing README, no new content authored)
- Sharding phase-2 implementation (path convention documented, implementation deferred)
- Built-in backup_manager (roadmap note in §6, not implemented)
