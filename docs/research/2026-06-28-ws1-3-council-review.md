# WS1–WS3 stack — large council review (8 lenses)

Branch `feat/ws3-symbol-pagerank` (cAST + #227 + WS2 + WS3), ~1002 lines / 14 files.
Eight independent review agents (correctness, architecture, performance, security,
tests, lexical-first, config, devil's-advocate). Scores: 7,8,7,8,7,8,8,6 (avg ~7.4).

## Vote tally (per component)

| component | APPROVE | APPROVE-W/-CHANGES | BLOCK |
|---|---|---|---|
| cAST chunking | 4 | 4 | 0 |
| #227 SEMA | **8** | 0 | 0 |
| WS2 symbol graph | 1 | 7 | 0 |
| WS3 PageRank/cap | 3 | 5 | 0 |

No blocks. **#227 is unanimous-ship; cAST is near-unanimous (minor fixes).** WS2/WS3 approve-with-changes.

## Cross-validated findings (flagged independently by ≥2 lenses → high weight)

1. **Missing lexical-first guard test** — Tests + Lexical-first + Devil's (×3). The council's own slice-2 consensus made "exact-identifier → rank 1" a release gate; it was never written. **Required.**
2. **Missing cap-bounds-the-expansion test** — Tests + Devil's. The core WS3 mechanism is untested at the `expand_coactivated` level. **Required.**
3. **PageRank personalization is inert in production** — Architecture + Devil's. At retrieval `query_symbol_nodes=cand_ids` (uniform — no 10× bias) and `session_nodes` is never passed (no 50×); PageRank runs only past the cap. So the live signal ≈ plain centrality, and the measured fp recovery is most plausibly **the cap**, not personalization. → ablation `cap+PageRank` vs `cap+in-degree`; honest doc either way.
4. **char_cut UTF-8 byte/char split** (Correctness, MAJOR) — `char_cut` advances by `max_chars` *bytes* over a *char* budget, can split a multibyte codepoint → `decode(replace)` breaks the byte-exact invariant on non-ASCII atomic leaves.
5. **`RecursionError` escapes `_chunk_code`'s narrow `except (ImportError, ValueError)`** (Security, MAJOR) — adversarial deeply-nested code aborts ingest instead of degrading to regex.
6. **span recovery silent `find()==-1` fallback** (Correctness, MAJOR) — fabricates a span and shifts every later chunk's bucket window, misattributing symbols.
7. **No index on `gene_relations(relation, gene_id_a)`** (Performance, MAJOR) — the query-time SYMBOL_REF fetch post-filters `relation` at scale.
8. **Commit-per-file on ingest** (Performance, MAJOR) — two fsync commits per file (defs + edges).
9. **Config drift** — 3 new knobs (`sema_embed_on_ingest`, `symbol_graph`, `symbol_expansion_cap`) absent from `helix.toml`/`CLAUDE.md` (the exact #218/#219 failure mode). **Required.**
10. **tree-sitter dark fallback** (Config + Tests + Devil's) — extras-only; default install silently no-ops the whole stack while `symbol_graph` defaults true. Task #27 "tree-sitter core dep" did not land on this branch.
11. **Scope gaps undocumented** — symbol refs are **python-only** (cAST is 8-lang); **sharded mode skips WS2/WS3** entirely (blob-only wins). Both untested + undisclosed in the regression log.
12. **Validation thin** — +2.1pp packet is sub-one-task on a single 26-task smoke, no held-out; held-out sweep deferred.
13. **Over-claim** — commits/task say "fusion tier + budget-ordered trim"; only Phase 2a (bounded expansion) shipped (correct scope, wrong label).
14. **`build_adjacency` generator-exhaustion** (Architecture, MINOR) — re-materializes after the generator is consumed; latent footgun.
15. **`resolve_symbol` / `symbol_defs` write-only** — populated, never read; docstring over-promises a cross-file path that doesn't exist yet.

## Conflict zone — merge strategy

- **Most lenses:** merge-after-fixes (the data-gating + reversibility make default-on safe once the fixes + tests land).
- **Devil's Advocate:** **merge-partial** — ship cAST + #227 now (validated, low-risk); **hold WS2/WS3** behind flags (or dark-ship, default OFF) until (a) the lexical-first guard test, (b) a held-out A/B, and (c) a PageRank-vs-in-degree ablation.

## Consensus recommendation

**Phased merge.**
1. **cAST + #227 → merge first** after two cheap MAJOR fixes (char_cut UTF-8 boundary; broaden `_chunk_code` except). Near-unanimous, validated, low-risk.
2. **WS2 + WS3 → fix then decide default.** Land: the find()==-1 skip, the `gene_relations` index, `build_adjacency` materialize, the two mandated guard/cap tests, the config-doc sync, and the scope/over-claim honesty edits. Then the **default-on vs dark-ship** call is gated on a **held-out A/B + the PageRank-vs-in-degree ablation** — if PageRank doesn't beat raw in-degree, simplify (drop the module) per YAGNI; if the held-out gain doesn't survive, dark-ship (`symbol_graph` default false).

**Reversibility:** high — all config-gated (`symbol_graph=false` / `symbol_expansion_cap=0`).

> Heuristic multi-perspective review; not a substitute for human domain experts.
