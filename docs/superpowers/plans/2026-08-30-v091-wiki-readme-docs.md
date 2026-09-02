# v0.9.1 Wiki + README + Tier-1 Docs (PR B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A 15-page wiki (single markdown source, rendered to both the
GitHub wiki and cymatixcontext.com/wiki), a ~120-line README, and a
software-term sweep of all current docs, shipping as one PR on
`claude/v0-9-1-readme-wiki-060b42`.

**Architecture:** `wiki/` is the source of truth; two thin scripts render
it (site HTML build; GitHub-wiki git mirror). Docs sweep and README
rewrite are text-only. PR A (`docs/superpowers/plans/2026-08-30-rosetta-tier2-aliases.md`)
merges first — this PR's docs present those canonical names as primary.

**Tech Stack:** Markdown, hand-authored SVG, Python (`markdown` package)
for the site build, git for the wiki mirror.

**Spec:** `docs/superpowers/specs/2026-08-30-v091-readme-wiki-rosetta-design.md`

## Global Constraints

- **Terminology:** software-canonical terms primary everywhere; legacy
  biology term once per page at first mention as `(legacy: …)` where it
  aids recognition. Literal identifiers that are still legacy-named on
  the wire/SQL (e.g. `gene_id` fields in JSON examples, `genome.db`
  paths, `[ribosome]` when showing a raw file) appear verbatim in code
  blocks — never fake a rename the code doesn't have. Canonical alias
  forms from PR A (`[compressor]`, `cymatix document get`,
  `/documents/{id}`, `cymatix_document_*`, `CYMATIX_STORE_PATH`,
  `--max-docs`, `retrieval_tokens`, `max_docs_per_turn`) are shown as
  the primary spelling in examples.
- **No invented numbers.** Every quantitative claim must appear in
  README.md@master, CHANGELOG.md, docs/benchmarks/BASELINES.md, a
  receipt JSON path, or the PR bodies of #407/#409/#412/#413 — and
  carries its caveat (unverified-estimate labels, "not isolated",
  latency-regression disclosure, know-confidence recalibration, sharded
  gap #275) wherever the source attaches one.
- **0.9.1 framing:** wave-1 flip (#407), `min_delivered_docs` knob
  (#409, default 0), `entity_autolink_hub_cutoff` knob (#412, default 0),
  tagger v2 (#413, behavior change + `tagger_version` comparability),
  lexicon Tier 1+2 — described as part of 0.9.1.
- **Dated docs are immutable** (anything with a YYYY-MM-DD prefix, all of
  `docs/archive/`, benchmarks/research/councils/design/investigations
  campaign files): link, never edit, never quote-as-current.
- Wiki pages: bullet-first; H1 title; every page ends with a "Go deeper"
  link block (repo docs + related wiki pages). Internal wiki links use
  GitHub-wiki style `[[Page-Name]]`? **No** — use standard relative links
  `[text](Page-Name)` (works on GitHub wiki; the site build rewrites to
  `/wiki/page-name/`).
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task B1: Site build + wiki-mirror scripts

**Files:**
- Create: `scripts/build_wiki_site.py`, `scripts/sync_github_wiki.py`
- Test: `tests/test_build_wiki_site.py` (new)

**Interfaces:**
- Produces: `build_wiki_site.py --site-dir <dir>` (default
  `F:/Projects/cymatix-context/site`) renders every `wiki/*.md` except
  `_Sidebar.md`/`_Footer.md` to `<site-dir>/public/wiki/<slug>/index.html`
  (slug = lowercase filename, `Getting-Started` → `getting-started`;
  `Home.md` → `/wiki/` index), copies `wiki/assets/` to
  `public/wiki/assets/`, and rewrites intra-wiki hrefs
  `href="Page-Name"` → `href="/wiki/page-name/"`, `Home` → `/wiki/`.
  `sync_github_wiki.py [--remote URL]` clones
  `https://github.com/mbachaud/Cymatix-Context.wiki.git` to a temp dir,
  replaces its `*.md` + `assets/` with `wiki/`'s, commits, pushes; exits
  with a clear message if the remote 404s (wiki not initialized).
- Page template: the exact chrome of `site/public/contributing/index.html`
  (header, brand SVG, nav links + a new Wiki link, footer, `/style.css`),
  with `<title>{page} — Cymatix Context wiki</title>`, canonical URL,
  and content inside `<main class="wiki-page">`.

- [ ] **Step 1: Failing test** (no site dir needed — render to tmp):

```python
def test_build_renders_pages_and_rewrites_links(tmp_path):
    wiki = tmp_path / "wiki"; wiki.mkdir()
    (wiki / "Home.md").write_text("# Home\n[go](Getting-Started)", encoding="utf-8")
    (wiki / "Getting-Started.md").write_text("# Getting Started\nhello", encoding="utf-8")
    (wiki / "_Sidebar.md").write_text("nav", encoding="utf-8")
    site = tmp_path / "site" / "public"; site.mkdir(parents=True)
    build(wiki_dir=wiki, site_dir=tmp_path / "site")
    idx = (site / "wiki" / "index.html").read_text(encoding="utf-8")
    assert 'href="/wiki/getting-started/"' in idx
    assert (site / "wiki" / "getting-started" / "index.html").exists()
    assert not (site / "wiki" / "_sidebar").exists()
```

- [ ] **Step 2:** run → FAIL (`build` missing).
- [ ] **Step 3:** implement `build_wiki_site.py`: `markdown` package with
  `extensions=["tables", "fenced_code", "toc"]`; guard the import with a
  friendly `pip install markdown` message; embed the page template as a
  module constant (copy chrome from `site/public/contributing/index.html`,
  add the Wiki nav link); href rewrite via regex over the rendered HTML
  (`href="([A-Za-z0-9-]+)"` → known page slugs only). Expose
  `build(wiki_dir, site_dir)` for the test; `main()` parses args.
  Implement `sync_github_wiki.py` (subprocess git; no test — network).
- [ ] **Step 4:** test → PASS.
- [ ] **Step 5: Commit** `feat(scripts): wiki site build + GitHub-wiki mirror`.

### Task B2: Shared SVG assets (4)

**Files:**
- Create: `wiki/assets/pipeline-flow.svg`, `wiki/assets/surfaces.svg`,
  `wiki/assets/token-economics.svg`, `wiki/assets/defaults-switchboard.svg`

**Interfaces:**
- Produces: four standalone SVGs, `viewBox` responsive, **no external
  fonts** (system sans stack), every text/shape uses explicit mid-tone
  fills legible on white AND near-black (slate `#7d8590` lines, accent
  `#e8883a`, box fills `#8884` translucent) — GitHub renders SVGs on
  light/dark and the site is dark-first.

- [ ] **Step 1:** `pipeline-flow.svg` — the README's 7-stage flow
  (Classify → Extract → Retrieve → Re-rank(opt) → Splice(opt) → Assemble
  +freshness+session-delivery → Persist(bg)), one annotation line per
  stage, terminal fork `know { }` / `miss { }`.
- [ ] **Step 2:** `surfaces.svg` — CLI / MCP / OpenAI-proxy / HTTP
  clients → one pipeline box → SQLite knowledge store (single file),
  side-tap to OTel/Grafana.
- [ ] **Step 3:** `token-economics.svg` — horizontal bars: median 2,757
  vs RAG-baseline 7,500-ish denominator drawn as "top-5 × 1500" reference
  bar; labels 2.9× / 5.7× / 2.1×; caption line "configurable baseline;
  N=15, May 2026" (caveat travels in the image).
- [ ] **Step 4:** `defaults-switchboard.svg` — two-column ON/OFF board at
  shipped defaults: ON = FTS5+tags, synonyms, co-activation, cymatics,
  RRF (rrf_k=20), freshness gate, session delivery, entity_graph; OFF
  (opt-in) = dense (2026-08-15), SPLADE (2026-08-16), PKI (2026-08-19),
  rerank, sema/dense ingest embeds (2026-08-19), min_delivered_docs,
  hub cutoff. Date-stamped flips.
- [ ] **Step 5:** visual check (open each in the browser pane, light+dark
  background), then **Commit** `docs(wiki): shared SVG assets`.

### Tasks B3–B7: wiki pages (15 + sidebar/footer)

**Files:** Create `wiki/<Page>.md` per the briefs below.
**Interfaces:** page names are load-bearing (links + slugs) — use exactly:
`Home`, `Getting-Started`, `Pipeline`, `Retrieval-Dimensions`,
`Configuration`, `HTTP-API`, `CLI`, `MCP-and-IDE-Integration`,
`Agent-Contract`, `Benchmarks-and-Receipts`, `Observability`,
`Troubleshooting`, `Architecture-Map`, `Lexicon`, `Roadmap-and-Releases`.

Per-page briefs (sources are mandatory reading for the writer):

- [ ] **B3a `Home`** — what Cymatix Context is (from README hook +
  `docs/MISSION.md` + `docs/DESIGN_TARGET.md`), the surfaces.svg, a
  6-bullet "why" (local-first, LLM-free default path, know/miss contract,
  token economics headline + caveat, SQLite single file, receipts
  culture), version banner (0.9.1), grouped page directory, links to
  site/Discord/repo.
- [ ] **B3b `Getting-Started`** — from `docs/SETUP.md` + README install:
  requirements, pip extras decision table, 4-command quickstart, first
  ingest tips (synonym map gotcha), verify with `cymatix status` /
  `/health`, three workflows (CLI-only, proxy, MCP) each 3-5 bullets,
  troubleshooting link.
- [ ] **B3c `Troubleshooting`** — condensed from `docs/TROUBLESHOOTING.md`
  (top ~10 symptoms as tight Symptom→Fix bullets, software terms:
  "knowledge store is locked" with the literal `genome.db` filename in
  the code block) + README Gotchas list. Link the full doc.
- [ ] **B4a `Pipeline`** — pipeline-flow.svg + per-stage bullets (from
  README pipeline section + CLAUDE.md 7-stage text +
  `docs/architecture/PIPELINE_LANES.md`): what each stage does, what's
  optional, what's algorithmic vs model; the wave-1 ranking defaults
  (rrf_k=20, eps_band all classes, delivered 0.555→0.630 at 829k, #407)
  in the Retrieve/Re-rank stage notes; freshness gate + session delivery
  in Assemble.
- [ ] **B4b `Retrieval-Dimensions`** — from
  `docs/architecture/DIMENSIONS.md`: the D1–D9 dimension list as a table
  (dimension, signal, cost, default state), RRF vs additive fusion
  (+12pp receipt), cymatics honesty paragraph (from README: mnemonic,
  not-yet-isolated), links to the retrieval-layer ledger.
- [ ] **B4c `Architecture-Map`** — package table (README/CLAUDE.md 16
  packages), knowledge-store shape (from
  `docs/architecture/KNOWLEDGE_GRAPH.md`, software terms: documents,
  tags, signals, lifecycle tiers, co-activation edges), session registry
  + launcher one-paragraph summaries with links, storage notes (SQLite
  WAL, FTS5, single file, sharded-gap caveat #275).
- [ ] **B5a `Configuration`** — per-section table for every
  `cymatix.toml` section (canonical name primary: `[compressor]
  (legacy [ribosome])`): purpose, 3-5 key settings w/ defaults, flip
  dates for the default-off encoders; the new 0.9.1 knobs
  (`min_delivered_docs` default 0 — receipts justify but flip not taken,
  answer-quality caveat; `entity_autolink_hub_cutoff` default 0 — ~340×
  per-link on hub-heavy beds, edge-delta caveat); env precedence
  (`CYMATIX_*`, OTel env-wins rule); link `docs/config-reference.md` as
  the exhaustive reference. Source: CLAUDE.md config table +
  config-reference + #409/#412 PR bodies.
- [ ] **B5b `HTTP-API`** — endpoint tables from README/CLAUDE.md grouped
  (retrieval / ingestion+maintenance / identity+sessions / diagnostics),
  `/documents/{id}` alias noted, a full `/context/packet` request+response
  example (from `docs/api/context-endpoint.md` — verbatim field names,
  including legacy wire fields with a "wire names are Tier-3 legacy"
  note), `caller_model_class`, security knobs summary (`admin_token`,
  `swap_db_roots`, `neutralize_control_tags`) linking Configuration.
- [ ] **B5c `CLI`** — from `docs/clients/cli.md`: command table
  (canonical spellings: `cymatix document get`, `--max-docs`), the
  `--json` agent workflow, `cymatix status` / `diag corpus` output
  explained (incl. the fixed tier counts), no-daemon cold-start note.
- [ ] **B5d `MCP-and-IDE-Integration`** — MCP config JSON (from README),
  tool roster (canonical `document_*` primary + legacy table, core-set
  vs `CYMATIX_MCP_FULL`), Claude Code routing notes
  (`docs/clients/claude-code.md`), Continue (Chat-mode caveat), OpenAI
  proxy one-liner.
- [ ] **B6a `Agent-Contract`** — know/miss shape (from README + 
  `docs/agent-sdk-fragment.md` + `docs/api/context-endpoint.md`): the
  `know {found, confidence}` / `miss {reason, escalate_to}` contract,
  the confidence-recalibration honesty block (#287: KnowBlock never
  fires at shipped defaults — rely on found/reason), the prompt fragment
  (`cymatix_context.agent_prompt.full_fragment()`), freshness downgrade
  reasons, packet verified/stale_risk/refresh_targets semantics.
- [ ] **B6b `Benchmarks-and-Receipts`** — the receipts culture (every
  default flip receipt-gated); token-economics.svg + table w/ caveats;
  shipped-defaults 829k table (56.5% delivered, r@12 0.659) + wave-1
  delta (0.555→0.630→0.681 r@12, #407) + min_delivered_docs measured
  .630→.668 opt-in (#409); ERB official 0.8.x all-encoders-on numbers
  with the quote-as-a-pair rule (41.6% correctness, 79% when delivered);
  encoder-isolation receipts (why dense/SPLADE are off); latency
  disclosure (#374 ×2.5–2.6 at 100k); tagger v2 + `tagger_version` and
  `ingest_c` comparability rules (#413, EXP-A); BASELINES.md as ledger;
  sharded gap #275. Sources: README proof section, CHANGELOG 0.9.0,
  `docs/benchmarks/BASELINES.md`, `BENCHMARK_RATIONALE.md`, PR bodies.
- [ ] **B6c `Observability`** — from `docs/architecture/OBSERVABILITY.md`
  + README: setup scripts, enable env/toml + precedence, the 7 Grafana
  dashboards (one line each), key metric families (canonical panel
  vocabulary; metric names verbatim — Prometheus contract), redaction.
- [ ] **B7a `Lexicon`** — **the ROSETTA remnant.** Opening: the project's
  vocabulary history (biology metaphor origin → software-canonical
  lexicon; helix→cymatix product rename note) + **the Agentome paper
  link <https://mbachaud.substack.com/p/agentome>** as the intellectual
  backstory. The full bidirectional mapping table, terms-that-stay list,
  identity vocabulary (org/party/participant/agent), what stays legacy
  on the wire/SQL and why (Tier-3 issue link from PR A), the completed
  phase table R1→R4 (R4: shipped in 0.9.1, PR A). Source:
  `docs/ROSETTA.md` (this page replaces it).
- [ ] **B7b `Roadmap-and-Releases`** — 0.9.1 highlights (wave-1 flip,
  two knobs, tagger v2, lexicon finalization, wiki+site+README), 0.9.0
  post-flip summary w/ disclosures, deferred ledger pointer (#377
  comments), `docs/ROADMAP.md` link, CHANGELOG link, versioning/release
  cadence note.
- [ ] **B7c `_Sidebar.md`** — grouped nav (Start: Home, Getting-Started,
  Troubleshooting · Concepts: Pipeline, Retrieval-Dimensions,
  Agent-Contract, Architecture-Map, Lexicon · Reference: Configuration,
  HTTP-API, CLI, MCP-and-IDE-Integration, Observability · Project:
  Benchmarks-and-Receipts, Roadmap-and-Releases). `_Footer.md` — one
  line: site · Discord · repo · Apache-2.0.
- [ ] Each page task ends: self-check against Global Constraints
  (terms, numbers-with-receipts, dated-docs untouched), then **Commit**
  per cluster (`docs(wiki): <pages>`).

### Task B8: Tier-1 docs sweep

**Files (modify — prose only, identifiers/code blocks stay literal):**
Group 1: `docs/TROUBLESHOOTING.md`, `docs/SETUP.md`,
`docs/operator-runbooks.md`. Group 2:
`docs/architecture/KNOWLEDGE_GRAPH.md` (incl. ASCII diagram labels →
`DOCUMENT:`/`SIGNALS`/`TIER`/`coactivation`),
`docs/architecture/PIPELINE_LANES.md`, `docs/ops/SKILLS_BUNDLE.md`.
Group 3: `docs/api/context-endpoint.md`, `docs/api/endpoints.md`,
`docs/clients/cli.md`, `docs/clients/claude-code.md`. Group 4:
`docs/config-reference.md` (prose outside `BEGIN GENERATED` regions
only), `docs/INTEGRATING_WITH_EXISTING_RAG.md`, `skills/cymatix/SKILL.md`,
remaining undated non-archive files from the 2026-08-30 inventory list.
Also: CLI help prose (`cymatix_context/cli/dispatcher.py` help strings,
`cmd_gene.py` help text), launcher UI strings
(`launcher/templates/dashboard.html`, `templates/components/database_panel.html`:
"Genome" → "Knowledge store"), `cymatix_context/mcp/server.py` module
docstring.

**Rules (verbatim for every sweep worker):**
1. Replace biology-primary PROSE with software terms; first mention per
   doc may carry `(legacy: gene)` once.
2. NEVER change: code identifiers, JSON/wire field names, SQL, file
   paths (`genome.db`), env/config keys inside code blocks, metric
   names, dated docs, `docs/archive/`, generated regions, receipts.
   Where an example shows a legacy-named literal, keep it and let the
   surrounding prose use the software term ("the knowledge store file
   (`genomes/main/genome.db`)").
3. Canonical alias spellings from PR A become the primary form in
   *command examples* (`cymatix document get`, `--max-docs`,
   `[compressor]`, `CYMATIX_STORE_PATH`) with the legacy spelling noted
   once.
4. After each file: `git diff --stat` sanity (prose-sized delta), and
   `python -m pytest tests/test_config_reference_sync.py -q` after any
   config-reference edit.

- [ ] Sweep each group; commit per group
  (`docs: software-term sweep — <group> (ROSETTA Tier 1)`).
- [ ] `docs/ROSETTA.md` → stub (~25 lines): canonical-lexicon statement,
  wiki Lexicon + site URL, Agentome paper link, "historical docs keep
  their vocabulary" rule, Tier-3 issue link. Update the two CLAUDE.md
  references to say "wiki Lexicon page (docs/ROSETTA.md is a stub)".
  Commit `docs: ROSETTA.md retires to the wiki Lexicon remnant`.

### Task B9: README rewrite (~120 lines)

**Files:** Modify: `README.md`

Structure (target ≤130 lines): badges (existing row) → 2-sentence hook
(drop the cymatics-name paragraph to the wiki Lexicon/Dimensions pages) →
"Proof (30 seconds)": the token table (3 rows) + one shipped-defaults
line (56.5% gold delivery at 829k, n=469) + one wave-1 line (delivered
0.555→0.630, #407) + single caveat sentence linking
Benchmarks-and-Receipts for methodology/latency/ERB context → Get started
(install + extras one-liner + 4 commands) → Pipeline diagram (keep ASCII
block verbatim, software terms) → Surfaces table (CLI/MCP/HTTP) → know/miss
3-bullet summary with the confidence-provisional clause → Gotchas top-5
(store path, synonym map, session delivery, sharded gap, agent fragment) →
Documentation block: wiki (GitHub + cymatixcontext.com/wiki), Discord,
docs/ table trimmed to 6 links → How this was built (keep) → License.
Cut entirely (wiki owns them): config/endpoint/package collapsibles,
security section, knowledge-store management, IDE snippets, observability
section, testing section, acknowledgments row → one line in Home page.

- [ ] Draft, count lines (≤130), verify every retained number matches
  master's README/CHANGELOG claims + caveats, all links resolve.
- [ ] **Commit** `readme: v0.9.1 minimal landing — depth moves to the wiki`.

### Task B10: CHANGELOG, render, PR B, wiki push

- [ ] CHANGELOG `## Unreleased`: wiki (source + both renders + scripts),
  README rewrite, Tier-1 sweep, ROSETTA retirement (Agentome link),
  merge-order note re PR A.
- [ ] Run `python scripts/build_wiki_site.py` against the real site dir;
  eyeball `/wiki/` + 2 subpages in the browser (light + dark), check
  header Wiki link added to `site/public/index.html` + the three
  existing subpages (manual edit, since site is untracked).
- [ ] `python -m pytest tests/test_build_wiki_site.py tests/test_config_reference_sync.py -q` → green.
- [ ] Push branch; `gh pr create` (body: deliverables, merge-after-PR-A
  note, wiki-init one-click instruction, site-deploy note).
- [ ] Attempt `python scripts/sync_github_wiki.py`; if the remote 404s,
  report the one-click "Create the first page" step to the owner.
