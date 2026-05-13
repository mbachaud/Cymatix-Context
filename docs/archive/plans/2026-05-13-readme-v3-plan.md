# README v3 — Full Rewrite Plan

**Date:** 2026-05-13
**Author:** Raude (Claude Opus 4.6, 1M context)
**Status:** plan, awaiting review
**Trigger:** 3-week audit found README v2 (May 2026) drifting after Stages 1-7, R3 rename, PR #90 restructure, CLI maturation, and know/miss contract landing.

## Why rewrite (not patch)

README v2 was written for the immediate post-Stage-7 state. Since then:

- PR #88-89 (R3): module renames with shims (genome→knowledge_store, ribosome→compressor)
- PR #90: 43 flat modules → 16 sub-packages + server.py split into 4 route modules
- CLI is now a first-class agent surface (query, packet, gene, neighbors, refresh-targets, diag, config, status)
- Know/miss contract is the primary consumer API shape
- Session delivery register went live (10,150 rows, ~40% token savings on multi-turn)
- Legibility headers (per-document confidence + fired tiers) are live
- Expression tokens tightened 12k → 7k
- SR promoted from dark to live
- Bench numbers ("5.4× median") need re-verification against current pipeline state

Patching would leave the structure wrong. The information hierarchy should change: README v2 led with the proxy; the system's identity has shifted to "agent-first coordinate-index engine."

## Reference: Headroom README patterns to adopt

Headroom's README (275 lines, F:\Projects\headroom-main\README.md) uses patterns that work well for dev-tool READMEs:

| Pattern | How Headroom does it | Apply to Helix |
|---------|---------------------|----------------|
| Progressive disclosure | Front-loads value (what + proof + quickstart in first ~100 lines), collapses internals in `<details>` | YES — current Helix "At a glance" is a wall of dense text |
| Proof-first positioning | Bench tables + metric badges BEFORE install instructions | YES — our bench numbers exist but are buried in a paragraph |
| Time-boxed headers | "How it works (30 seconds)", "Get started (60 seconds)" | YES — reduces reader hesitation |
| ASCII pipeline diagram | Box-drawing art, not Mermaid (renders identically everywhere) | CONSIDER — our current Mermaid pipeline is nice on GitHub but breaks in terminals/PyPI. ASCII art is universally portable |
| Collapsible `<details>` blocks | 3 blocks hide internals (Integrations, What's inside, Pipeline) | YES — Architecture, full endpoint list, config sections can collapse |
| Two-column docs table | "Start here / Go deeper" | YES — we have enough docs to do this now |
| Badges | 8 shields.io badges (CI, codecov, PyPI, npm, HF model, vanity "tokens saved", license, docs) | PARTIAL — we have 5 badges. Add CI status, test count, maybe a custom metric |
| Centered GIF demos | 2 GIFs showing the tool in action with metric captions | STRETCH — would be great but requires recording. Park for now. |
| Competitive comparison | Table with honest feature columns + attribution blockquote | CONSIDER — Helix vs vanilla RAG vs Continue context vs bare grep |

## Proposed structure

Target: ~300 lines (same as v2, but restructured for scannability).

```
1.  # Helix Context
2.  Badges (5-8 shields.io)
3.  One-sentence tagline (centered)
4.  ---
5.  > Elevator pitch blockquote (2 sentences max)

6.  ## Proof (why you should care — 30 seconds)
    - Token-savings table (bench data, reproduce command)
    - Agent contract summary (know/miss in 3 lines)
    
7.  ## Get started (60 seconds)
    - 3-step: install, ingest, query
    - helix-server for IDE integration (1 line)
    
8.  ## Agent surfaces
    - CLI (helix query/packet/gene/neighbors)
    - MCP (Claude Code / Desktop / Cursor)
    - HTTP proxy (/v1/chat/completions)
    - Table: which surface for which use case
    
9.  ## Pipeline (how it works — 2 minutes)
    - 7-stage ASCII art diagram (portable, no Mermaid)
    - One-line explanation per stage
    - Know/miss contract: what the agent actually receives
    
10. <details> Configuration
    - helix.toml section table (17 sections, 1-line each)
    - Key tuning knobs called out
    
11. <details> Full endpoint reference
    - Grouped: core retrieval, ingestion, identity, diagnostics, admin
    
12. <details> Package structure
    - 16 packages, 1-line purpose each
    - Note on shims + ROSETTA.md
    
13. ## Knowledge store management
    - Path (genomes/main/genome.db)
    - Backup (1-liner)
    - BGE-M3 backfill (if needed)
    
14. ## Observability
    - Setup (2 lines: script + env var)
    - Dashboard link
    
15. ## Gotchas
    - Bullet list (6-8 items, scannable)
    
16. ## Testing
    - One command + test count
    
17. ## Documentation
    - Two-column table: "Start here" / "Go deeper"
    
18. ## Acknowledgments
    - Keep existing
    
19. ## License
    - One line
```

## Key differences from v2

| Aspect | v2 (current) | v3 (proposed) |
|--------|-------------|---------------|
| Lead section | "At a glance" (dense paragraph) | "Proof" (bench table, know/miss summary) |
| Quickstart position | 3rd section | 2nd section |
| Pipeline diagram | Mermaid (breaks on PyPI, terminals) | ASCII box-drawing art |
| Agent surfaces | Scattered (CLI here, MCP there, proxy elsewhere) | Unified section with decision table |
| Config reference | 5 sections listed | 17 sections in collapsible `<details>` |
| Endpoint list | 11 endpoints in code block | Full list (~45 endpoints) in collapsible `<details>` |
| Package structure | Not mentioned (was pre-PR-90) | 16 packages in collapsible `<details>` |
| Docs navigation | "Further reading" list at bottom | Two-column table (Start here / Go deeper) |
| Bench numbers | Buried in paragraph | Standalone table, early |

## Pre-work needed before writing

1. **Re-run bench** — verify the "5.4× median" and "28.7× best-case" claims still hold after Stages 1-7 + expression_tokens tighten. If numbers changed, update them honestly.
2. **Confirm endpoint count** — the audit found ~45 endpoints. Verify nothing was removed or renamed since PR #90.
3. **Decide on ASCII vs Mermaid** — Max's preference. ASCII is more portable; Mermaid is prettier on GitHub. Both is verbose.
4. **GIF demo** — optional stretch goal. A terminal recording of `helix query` → response with legibility headers would be compelling. Could use `asciinema` or `terminalizer`.

## Execution estimate

- **Writing:** 2-3 hours (one session)
- **Bench verification:** 30 min (run `bench_rag_vs_sike_tokens.py`, compare numbers)
- **Review + iteration:** 1 round expected

## Sections that can be carried forward from v2

- Badges (update if needed)
- Continue IDE Integration (verified still accurate)
- Acknowledgments
- License
- Testing (update test count)
- Knowledge store management (update path if needed)

## Sections that need full rewrite

- "At a glance" → "Proof"
- "Quick Start" → "Get started (60 seconds)"
- "Pipeline" → 7-stage ASCII diagram + know/miss
- "Agent integration" → "Agent surfaces" with decision table
- "Surfaces and endpoints" → collapsible full reference
- "Architecture" → collapsible package structure (post-PR #90)
- "Gotchas" → refresh for current gotchas (session delivery, fusion mode, BGE-M3 backfill)
