# ROSETTA Tier-2 Aliases (PR A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every user-facing surface (config, env, CLI, HTTP, MCP) gains a
canonical software-term name; legacy biology names keep working unchanged.

**Architecture:** Purely additive aliases at each surface's dispatch seam
(config section/key normalization, argparse alias registration, an extra
FastAPI route bound to the same handler, MCP pass-through tools), plus one
real bug fix (StatsResult tier counts). No behavior change for existing
configs/calls — legacy wins on collision, with a warning.

**Tech Stack:** Python 3.11, FastAPI + TestClient, pytest, argparse, MCP SDK.

**Spec:** `docs/superpowers/specs/2026-08-30-v091-readme-wiki-rosetta-design.md`

## Global Constraints

- Branch `claude/rosetta-tier2-aliases` off `origin/master`, own worktree.
- Additive only: no legacy name removed, no default changed, no wire byte
  altered for existing calls (Tier 3 stays out — see spec "Out of scope").
- Collision rule everywhere: if both legacy and canonical are supplied,
  **legacy wins** and a `log.warning` names both.
- Every task: failing test first, then minimal code, then suite slice.
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Final gate: `python -m pytest tests/ -m "not live"` fully green (the
  known-environmental `test_cymatix_console_scripts_present` failure is
  acceptable if it fails identically on unmodified master).

---

### Task A1: Config aliases + unknown-section warning

**Files:**
- Modify: `cymatix_context/config.py` (section parse seams at ~:1366
  `if "ribosome" in raw`, ~:1426 `if "genome" in raw`; `_warn_unknown`
  ~:1256; env read ~:1313)
- Test: `tests/test_lexicon_aliases_config.py` (new)

**Interfaces:**
- Consumes: `load_config(path)` (existing public loader).
- Produces: `[compressor]` ≡ `[ribosome]`; `[knowledge_store]` ≡
  `[genome]`; `[budget] retrieval_tokens` ≡ `expression_tokens`;
  `[budget] max_docs_per_turn` ≡ `max_genes_per_turn`;
  env `CYMATIX_STORE_PATH` ≡ `CYMATIX_GENOME_PATH`; unknown top-level
  sections produce a warning naming the section.

- [ ] **Step 1: Write failing tests** — read the top of
  `tests/test_config*.py` first and reuse the existing tmp-toml fixture
  idiom. Tests (behavioral, via `load_config` on a tmp `cymatix.toml`):

```python
def test_compressor_section_aliases_ribosome(tmp_path):
    p = tmp_path / "cymatix.toml"
    p.write_text('[compressor]\ntimeout = 123\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.ribosome.timeout == 123

def test_knowledge_store_section_aliases_genome(tmp_path):
    p = tmp_path / "cymatix.toml"
    p.write_text('[knowledge_store]\npath = "x/y.db"\n', encoding="utf-8")
    assert load_config(p).genome.path == "x/y.db"

def test_budget_key_aliases(tmp_path):
    p = tmp_path / "cymatix.toml"
    p.write_text('[budget]\nretrieval_tokens = 4321\nmax_docs_per_turn = 9\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.budget.expression_tokens == 4321
    assert cfg.budget.max_genes_per_turn == 9

def test_legacy_wins_on_collision_with_warning(tmp_path, caplog):
    p = tmp_path / "cymatix.toml"
    p.write_text('[ribosome]\ntimeout = 1\n[compressor]\ntimeout = 2\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.ribosome.timeout == 1
    assert any("compressor" in r.message and "ribosome" in r.message for r in caplog.records)

def test_unknown_section_warns(tmp_path, caplog):
    p = tmp_path / "cymatix.toml"
    p.write_text('[compresser]\ntimeout = 1\n', encoding="utf-8")  # typo section
    load_config(p)
    assert any("compresser" in r.message for r in caplog.records)

def test_store_path_env_alias(tmp_path, monkeypatch):
    monkeypatch.delenv("CYMATIX_GENOME_PATH", raising=False)
    monkeypatch.setenv("CYMATIX_STORE_PATH", "env/store.db")
    p = tmp_path / "cymatix.toml"; p.write_text("", encoding="utf-8")
    assert load_config(p).genome.path == "env/store.db"

def test_genome_env_wins_over_store_env(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CYMATIX_GENOME_PATH", "legacy.db")
    monkeypatch.setenv("CYMATIX_STORE_PATH", "new.db")
    p = tmp_path / "cymatix.toml"; p.write_text("", encoding="utf-8")
    assert load_config(p).genome.path == "legacy.db"
```

Adapt attribute names to the real config model (read `RibosomeConfig` /
`GenomeConfig` / `BudgetConfig` field names in config.py before writing;
if `[compressor] timeout` is not a real ribosome key, pick two real keys —
the point is section mapping, not the specific key).

- [ ] **Step 2:** `python -m pytest tests/test_lexicon_aliases_config.py -v` → all FAIL.
- [ ] **Step 3: Implement** in `config.py`: a single normalization pass on
  the raw parsed TOML dict *before* section dispatch:

```python
_SECTION_ALIASES = {"compressor": "ribosome", "knowledge_store": "genome"}
_KEY_ALIASES = {"budget": {"retrieval_tokens": "expression_tokens",
                           "max_docs_per_turn": "max_genes_per_turn"}}

def _apply_lexicon_aliases(raw: dict) -> dict:
    for new, old in _SECTION_ALIASES.items():
        if new in raw:
            if old in raw:
                log.warning("[%s] ignored: legacy [%s] also present and wins", new, old)
            else:
                raw[old] = raw.pop(new)
    for section, keys in _KEY_ALIASES.items():
        body = raw.get(section)
        if isinstance(body, dict):
            for new, old in keys.items():
                if new in body:
                    if old in body:
                        log.warning("[%s] %s ignored: legacy %s also present and wins", section, new, old)
                    else:
                        body[old] = body.pop(new)
    return raw
```

  Call it where the TOML dict first lands. Add the unknown-section check
  next to `_warn_unknown` (~:1256): compare `raw` top-level keys against
  the known-section set (the literals already dispatched on, plus the two
  new aliases) and `log.warning` each unknown. Env alias at the
  `CYMATIX_GENOME_PATH` read (~:1313): fall back to `CYMATIX_STORE_PATH`;
  warn if both set and different. Also register the aliases wherever
  `_warn_unknown`'s known-keys source lives so alias keys aren't flagged
  as unknown keys.
- [ ] **Step 4:** re-run new tests → PASS; then
  `python -m pytest tests/ -k "config" -m "not live" -q` → green.
- [ ] **Step 5: Commit** `feat(config): [compressor]/[knowledge_store] section aliases, budget key aliases, CYMATIX_STORE_PATH, unknown-section warning (Tier 2)`.

### Task A2: StatsResult tier-count bug fix

**Files:**
- Modify: `cymatix_context/api.py:341-343`
- Test: `tests/test_stats_result_tiers.py` (new)

**Interfaces:**
- Consumes: `knowledge_store.stats()` dict, which emits keys
  `open` / `euchromatin` / `heterochromatin` (see `knowledge_store.py:5164`).
- Produces: `StatsResult.chromatin_open/euchromatin/heterochromatin`
  populated from those keys (field names unchanged — wire/back-compat).

- [ ] **Step 1: Failing test** — read `api.py:330-360` first to get the
  real constructor path (likely `StatsResult.from_stats(stats_dict)` or
  inline in a `stats()` helper), then:

```python
def test_stats_result_reads_tier_counts():
    stats = {"total_genes": 10, "open": 3, "euchromatin": 5, "heterochromatin": 2}
    r = _build_stats_result(stats)   # whatever api.py:341 does — call that path
    assert (r.chromatin_open, r.chromatin_euchromatin, r.chromatin_heterochromatin) == (3, 5, 2)
```

- [ ] **Step 2:** run → FAIL (all zeros today).
- [ ] **Step 3:** fix the three `stats.get("chromatin_open", 0)`-style
  reads to `stats.get("open", 0)` etc. (keep a fallback to the old keys:
  `stats.get("open", stats.get("chromatin_open", 0))`).
- [ ] **Step 4:** run new test + `python -m pytest tests/ -k "stats or diag" -m "not live" -q` → green.
- [ ] **Step 5: Commit** `fix(api): StatsResult tier counts read the keys stats() actually emits — cymatix diag corpus no longer prints zeros`.

### Task A3: CLI `document` alias + `--max-docs`

**Files:**
- Modify: `cymatix_context/cli/dispatcher.py` (`_build_parser` :52-91,
  `_resolve` :96-128), `cymatix_context/cli/cmd_packet.py:51`,
  `cymatix_context/cli/cmd_refresh_targets.py:42`
- Test: `tests/test_cli_document_alias.py` (new)

**Interfaces:**
- Consumes: existing `cymatix gene get|preview` handlers (`cmd_gene.py`).
- Produces: `cymatix document get|preview <id>` runs the same handlers;
  `--max-docs` accepted wherever `--max-genes` is.

- [ ] **Step 1: Failing tests** — read `dispatcher.py` and one existing
  CLI test for the invocation idiom (parser-level, no server):

```python
def test_document_get_resolves_to_gene_handler():
    # parse only; assert the resolved command/handler equals `gene get`'s
    ns_legacy = _parse(["gene", "get", "abc123"])
    ns_alias = _parse(["document", "get", "abc123"])
    assert _handler(ns_alias) is _handler(ns_legacy)

def test_max_docs_flag_alias():
    ns = _parse(["packet", "q", "--max-docs", "7"])
    assert ns.max_genes == 7
```

(`_parse`/`_handler` = thin helpers over the dispatcher's real functions;
write them in the test against what dispatcher.py exposes.)
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** implement — argparse: `add_parser("document", aliases=...)`
  is not enough because "document"/"gene" are separate subcommands with
  subcommands; simplest is registering `document` via
  `subparsers.add_parser("document", ...)` sharing the same
  sub-subparser construction function as `gene` (factor the `gene`
  builder into a helper called twice), and in `_resolve` map
  `document`→`gene`. For flags: `parser.add_argument("--max-genes",
  "--max-docs", dest="max_genes", ...)` (argparse long-option alias in
  the same `add_argument` call). Update help strings to canonical-first
  ("document ID" not "gene ID") — display text only.
- [ ] **Step 4:** run new tests + `python -m pytest tests/ -k "cli" -m "not live" -q` → green.
- [ ] **Step 5: Commit** `feat(cli): cymatix document get|preview alias + --max-docs flag alias (Tier 2)`.

### Task A4: HTTP `/documents/{document_id}` alias route

**Files:**
- Modify: `cymatix_context/server/routes_admin.py` (`GET /genes/{gene_id}` at :152)
- Test: `tests/test_http_documents_alias.py` (new)

**Interfaces:**
- Consumes: the existing `/genes/{gene_id}` handler function.
- Produces: `GET /documents/{document_id}` returns byte-identical JSON.

- [ ] **Step 1: Failing test** — reuse the repo's existing TestClient
  fixture (grep `TestClient` in tests/ and copy the app-construction
  idiom; likely a fixture building `create_app` over a tmp store):

```python
def test_documents_route_aliases_genes(client, ingested_gene_id):
    a = client.get(f"/genes/{ingested_gene_id}")
    b = client.get(f"/documents/{ingested_gene_id}")
    assert b.status_code == a.status_code == 200
    assert b.json() == a.json()

def test_documents_route_404_matches(client):
    assert client.get("/documents/nope").status_code == client.get("/genes/nope").status_code
```

- [ ] **Step 2:** run → FAIL (404 on /documents).
- [ ] **Step 3:** implement — add a second decorator on the same handler:

```python
@router.get("/genes/{gene_id}")
@router.get("/documents/{gene_id}")
async def get_gene(gene_id: str, ...):
```

  (FastAPI stacks route decorators fine; keep param name `gene_id` so the
  function body is untouched. If decorator stacking conflicts with the
  existing ordering/openapi, register via
  `router.add_api_route("/documents/{gene_id}", get_gene, methods=["GET"])`
  after the function instead.)
- [ ] **Step 4:** run new tests + `python -m pytest tests/ -k "routes or server or admin" -m "not live" -q` → green.
- [ ] **Step 5: Commit** `feat(server): GET /documents/{id} alias of /genes/{id} (Tier 2)`.

### Task A5: MCP — `cymatix_document_neighbors`, core-set aliases, R4 nudges

**Files:**
- Modify: `cymatix_context/mcp/mcp_server.py` (aliases block :913-992,
  `_MCP_CORE_TOOLS` :1026-1032, legacy tool docstrings :506/:833/:854/:875)
- Test: `tests/test_mcp_document_aliases.py` (new)

**Interfaces:**
- Consumes: existing `cymatix_neighbors(query, k)` (:854).
- Produces: `cymatix_document_neighbors(query, k)` pass-through;
  `_MCP_CORE_TOOLS` includes `cymatix_document_get`,
  `cymatix_document_query`, `cymatix_document_preview`,
  `cymatix_document_fingerprint`, `cymatix_document_neighbors`; every
  legacy bio-named tool docstring's first line ends with
  `(legacy name — prefer cymatix_document_*)`.

- [ ] **Step 1: Failing tests** — read how existing MCP tests introspect
  the tool registry (grep `_MCP_CORE_TOOLS` and `mcp` in tests/):

```python
def test_document_neighbors_registered_and_passthrough():
    tools = _tool_names()          # however existing tests enumerate tools
    assert "cymatix_document_neighbors" in tools

def test_core_set_exposes_document_aliases():
    from cymatix_context.mcp.mcp_server import _MCP_CORE_TOOLS
    assert {"cymatix_document_get", "cymatix_document_query",
            "cymatix_document_preview", "cymatix_document_fingerprint",
            "cymatix_document_neighbors"} <= set(_MCP_CORE_TOOLS)

def test_legacy_tools_carry_deprecation_nudge():
    for legacy in ("cymatix_gene_get", "cymatix_splice_preview", "cymatix_neighbors"):
        assert "prefer cymatix_document" in _tool_doc(legacy)
```

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** implement — clone the existing alias pattern at
  :913-992 for `cymatix_document_neighbors` (same params/return as
  `cymatix_neighbors`; canonical param name `k` stays). Extend
  `_MCP_CORE_TOOLS` with the five `document_*` names (legacy names stay —
  the lean set grows; note the growth in the CHANGELOG entry). Edit the
  three legacy docstrings' first lines to append the nudge. Also give
  `cymatix_document_preview` a `max_docs` parameter name if the alias
  signature is its own function (it is a pass-through — accept
  `max_docs` and forward as `max_genes`).
- [ ] **Step 4:** run new tests + `python -m pytest tests/ -k "mcp" -m "not live" -q` → green.
- [ ] **Step 5: Commit** `feat(mcp): document_* tools in the core set, cymatix_document_neighbors, R4 deprecation nudges on legacy names (#87, Tier 2)`.

### Task A6: mcp-tools.md rewrite + config-reference regeneration

**Files:**
- Modify: `docs/api/mcp-tools.md` (full rewrite — stale at 1.8 KB),
  `docs/config-reference.md` (regenerated regions + alias notes),
  whatever generator script the repo uses (grep for
  `BEGIN GENERATED: config-tables` producer; check `scripts/`)
- Test: existing `tests/test_config_reference_sync.py` must pass.

**Interfaces:**
- Consumes: Task A1's alias tables (`_SECTION_ALIASES`, `_KEY_ALIASES`),
  Task A5's tool roster.
- Produces: docs that present canonical names as primary with legacy in
  parentheses.

- [ ] **Step 1:** locate the generator (grep `config-tables` in scripts/
  and tests/), run it, confirm a clean regen on unmodified sections.
- [ ] **Step 2:** make section headings alias-aware — canonical primary:
  `## [compressor] (legacy alias: [ribosome])` — by teaching the
  generator a canonical-name map (import `_SECTION_ALIASES` from
  config.py rather than duplicating). Update
  `tests/test_config_reference_sync.py` expectations ONLY if the test
  hard-codes headings.
- [ ] **Step 3:** rewrite `docs/api/mcp-tools.md`: every tool (core set
  first, canonical `document_*` names primary, legacy names in a
  back-compat table), `CYMATIX_MCP_FULL`, the R4 nudge policy, request
  /response field notes. Source: read `mcp_server.py` tool docstrings.
- [ ] **Step 4:** `python -m pytest tests/test_config_reference_sync.py -v` → PASS.
- [ ] **Step 5: Commit** `docs: mcp-tools.md rewrite (document_* primary); config-reference alias-aware headings`.

### Task A7: CHANGELOG + Tier-3 issue + PR A

**Files:**
- Modify: `CHANGELOG.md` (`## Unreleased`)

- [ ] **Step 1:** CHANGELOG entries under `## Unreleased`, one per task
  (A1–A6), each naming the alias pair and the legacy-wins collision
  rule; the A2 entry marked `fix:`; the A5 entry noting the core-set
  growth. Cite the spec path.
- [ ] **Step 2:** `python -m pytest tests/ -m "not live" -q` (full suite,
  background OK) → green modulo the known environmental console-scripts
  failure (verify it fails identically on unmodified master before
  accepting it).
- [ ] **Step 3:** file the Tier-3 issue with `gh issue create` — title
  `lexicon: wire-surface rename remainder (Tier 3) — <GENE> blocks, decoder prompts, response fields, /stats keys`;
  body = spec's Tier-3 paragraph + inventory pointers + "requires its own
  byte-level A/B gate; v1.0-scale". Record the issue number.
- [ ] **Step 4:** push branch, `gh pr create` targeting master; PR body:
  what ships, collision rule, test counts, the Tier-3 issue link, and
  "merge before the docs/wiki PR (its docs present canonical names)".
- [ ] **Step 5: Commit** any final fixes; done.
