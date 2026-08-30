# v0.9.1 README + dual wiki + ROSETTA finalization — design

Date: 2026-08-30. Approved scope (owner decisions, 2026-08-30): ROSETTA
Tier 1 + Tier 2; full ~15-page wiki; minimal ~120-line README; content
written against master **plus** the open 0.9.1 PRs (#407, #409, #412,
#413) described as shipped.

## Goal

Ship v0.9.1 with a readable, minimal README; move depth into a wiki that
exists in two synchronized renders (GitHub wiki + cymatixcontext.com/wiki);
finish the biology→software lexicon so every user-facing surface has a
canonical software name; retire `docs/ROSETTA.md` to a wiki Lexicon
remnant that links the Agentome paper
(<https://mbachaud.substack.com/p/agentome>).

## Deliverable 1 — wiki (single source, two renders)

**Source of truth:** new top-level `wiki/` directory in the repo,
markdown, PR-reviewed. GitHub-wiki page naming (`Getting-Started.md` →
page "Getting Started"). Shared SVG assets in `wiki/assets/`.

**Pages (15):** Home · Getting-Started · Pipeline · Retrieval-Dimensions ·
Configuration · HTTP-API · CLI · MCP-and-IDE-Integration · Agent-Contract ·
Benchmarks-and-Receipts · Observability · Troubleshooting ·
Architecture-Map · Lexicon · Roadmap-and-Releases. Plus `_Sidebar.md` and
`_Footer.md` (GitHub wiki chrome; excluded from the website render's page
list, used to build the site's nav instead).

Content rules:

- Software-canonical terms only; legacy biology name given once per page
  at first mention where it aids recognition ("knowledge store (legacy:
  genome)").
- Bullet-first prose; every quantitative claim carries its receipt link
  and any honesty caveat that the README/CHANGELOG attaches to it today.
  No new numbers may be invented; every number must trace to README,
  CHANGELOG, BASELINES.md, a receipt JSON, or a PR body (#407/#409/#412/#413).
- Configuration page = per-section summaries + defaults + flip history,
  linking `docs/config-reference.md` for exhaustive keys (the 120 KB
  reference is not duplicated).
- Dated docs (benchmarks/research/councils/…) are linked as the archive
  layer, never copied.

**Visuals (4 SVGs, authored once, theme-safe on GitHub light/dark and the
site palette):** pipeline flow (7 stages + know/miss); surfaces diagram
(CLI/MCP/proxy → pipeline → SQLite store); token-economics chart (2.9×
median / 5.7× best / 2.1× worst vs top-5@1500 baseline, caveats in
caption); shipped-defaults switchboard (which layers are on/off at
defaults + receipt dates). No JS, no mermaid dependency.

**GitHub render:** `scripts/sync_github_wiki.py` — clones
`Cymatix-Context.wiki.git`, mirrors `wiki/`, commits, pushes. Blocked
until the wiki's first page is created once in the GitHub UI (the wiki
git remote 404s at zero pages).

**Website render:** `scripts/build_wiki_site.py` — renders `wiki/*.md`
(python `markdown` package) into the existing site chrome
(header/brand/footer, `style.css`, light+dark) and writes
`site/public/wiki/<slug>/index.html` + `/wiki/` index. `--site-dir`
argument (default `F:/Projects/cymatix-context/site`); the site folder is
self-gitignored and live-served, so the build runs only on finished
content. Site header on existing pages gains a Wiki link.

## Deliverable 2 — README (~120 lines)

Landing-page style: badges · hook paragraph · condensed proof block (the
headline shipped-defaults numbers + wave-1 delta, each with its caveat in
one clause and a link to Benchmarks-and-Receipts for the full honesty
prose) · install + 4-command quickstart · pipeline diagram (kept) ·
surfaces table · top-5 gotchas · documentation links (wiki, website,
Discord, docs/). Everything else moves to the wiki: config/endpoint/package
collapsibles, security section, knowledge-store management, IDE snippets,
extended caveat prose, acknowledgments trimmed to one line.

The "How this was built" authorship section stays (owner transparency
preference).

## Deliverable 3 — ROSETTA final pass (Tier 1 + Tier 2)

**Tier 1 — text, zero behavior change** (branch: this one):

- Rewrite biology-primary prose in current (undated, non-archive) docs —
  the ~27-file offender list from the 2026-08-30 inventory, headed by
  config-reference.md prose (generated regions handled in Tier 2 via the
  generator), operator-runbooks.md, TROUBLESHOOTING.md,
  KNOWLEDGE_GRAPH.md, api/*.md, clients/cli.md, SETUP.md,
  ops/SKILLS_BUNDLE.md, skills/cymatix/SKILL.md.
- CLI help prose, launcher UI strings ("Genome" tab etc.), MCP server
  module docstrings — display text only, no identifier changes.
- Dated/historical docs untouched (ROSETTA's standing rule).
- `docs/ROSETTA.md` → short stub: canonical-lexicon statement, link to
  wiki Lexicon page + Agentome paper. CLAUDE.md pointer updated.

**Tier 2 — additive aliases, both names work** (separate branch off
master, merges before the docs branch):

- Config: `[compressor]` alias of `[ribosome]`; `[knowledge_store]` alias
  of `[genome]`; key aliases `retrieval_tokens` → `expression_tokens` and
  `max_docs_per_turn` → `max_genes_per_turn` in `[budget]`;
  unknown-*section* warning in `_warn_unknown` (today `[compressor]` is
  silently ignored — the sharpest gap); alias-collision rule: legacy
  section/key wins with a warning if both present.
- Env: `CYMATIX_STORE_PATH` alias of `CYMATIX_GENOME_PATH` (legacy wins +
  warning on conflict).
- CLI: `cymatix document get|preview` alias of `cymatix gene`;
  `--max-docs` alias of `--max-genes` on packet/refresh-targets.
- HTTP: `GET /documents/{document_id}` alias route of `/genes/{gene_id}`.
- MCP: add missing `cymatix_document_neighbors`; include the
  `document_*` aliases in `_MCP_CORE_TOOLS` (canonical names visible
  without `CYMATIX_MCP_FULL=1`); R4 (#87) docstring deprecation nudges on
  legacy bio-named tools ("prefer cymatix_document_get"). No removals.
- Bug fix: `api.py` StatsResult reads `chromatin_open|euchromatin|
  heterochromatin` keys that `knowledge_store.stats()` never emits
  (emits `open|euchromatin|heterochromatin`) — tier counts always 0 in
  `cymatix diag corpus`. Fix + regression test.
- `docs/api/mcp-tools.md` rewritten (stale: documents 3 of the tools,
  no aliases).
- config-reference regenerated with alias-aware section headings.
- Tests for every alias seam; full non-live suite green.

**Tier 3 — explicitly deferred, filed as a tracked issue** (v1.0-scale,
receipt-invalidating): `<GENE>` assembly blocks, decoder prompts,
`[gene=` legibility headers, wire field names (`expressed_gene_ids`,
`gene_id_match`, `do_not_answer_from_genome`, `/stats` keys,
euchromatin/heterochromatin strings), SQL schema (stays frozen per
ROSETTA). Changing these alters delivered bytes — the control-tag flip
was gated on byte-identical windows, so this class of change requires its
own A/B gate.

## Branch / PR plan

1. **PR A** (`claude/rosetta-tier2-aliases`, off master): Tier 2 code +
   tests + stats bugfix + mcp-tools.md + config-reference regen +
   CHANGELOG entries. Merges first.
2. **PR B** (this branch `claude/v0-9-1-readme-wiki-060b42`): this spec,
   `wiki/` content + assets, both scripts, README rewrite, Tier 1 docs
   sweep, ROSETTA stub, CHANGELOG entries. Notes the merge-order
   dependency on PR A (docs present canonical names as primary).
3. Tier 3 remainder filed as a GitHub issue; ROSETTA phase table closes
   R4 as shipped and points the wire rename at that issue.
4. Website deploy = running `build_wiki_site.py` after PR B merges (site
   folder is untracked; content goes live on write). GitHub wiki push
   after the one-click first-page creation.

## Out of scope

- No SQL schema rename, no removal of any legacy name, no decoder-prompt
  or assembled-window byte changes, no metric renames (Prometheus
  contract), no version bump / release cut (0.9.1 release itself happens
  after the open PRs land).
