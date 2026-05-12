# Rename R2 — prose sweep to canonical software vocab

**Status:** Approved 2026-05-11 (brainstorming session).
**Sprint scope:** R2 only. R3 (file/class flip) and R4 (MCP soft-deprecate) are deferred to future sprints.
**Branch:** `rename/r2-prose-sweep`, off `master` (worktree at `F:/Projects/helix-context-r2-rename`).
**Fixture:** `docs/ROSETTA.md` is permanent. R2 expands it (Prometheus + Dashboard sections drafted in the prior working tree) and otherwise leaves it as the bidirectional translation layer.

---

## 1 · Why R2

Helix's original vocabulary borrowed from molecular biology (gene, genome, ribosome, chromatin, codon, promoter, epigenetics, transcription, expression, replication). The metaphor was generative but every reader — human or LLM — has to hold two mental models in parallel.

**R1 already shipped (commits `09d5548`, `661d8ed`):**
- `helix_context/aliases.py` — identity-preserving canonical aliases (`Document is Gene`, `Compressor is Ribosome`, etc.).
- `docs/ROSETTA.md` — the bidirectional fixture.
- README + CLAUDE.md lead-paragraph swap to canonical vocab.

**R2 is the prose layer.** Every active docstring, code comment, and `docs/*.md` line should say `Document`/`KnowledgeStore`/`Compressor` instead of `Gene`/`Genome`/`Ribosome`. Imports, classes, SQL, Prometheus surfaces, and historical archives stay untouched.

**Why ship R2 before R3.** PR1's diff is text-only — review reads as "did this hit every doc; did anything important slip through." When R3 follows, the reviewer reads symbol churn against a codebase where the prose *already says* `Document`, so the rename feels like the code finally catching up to the docs rather than two unrelated changes mashed together.

---

## 2 · Architecture & boundaries

### What's canonical at end-of-sprint

After R2 ships, *prose* uses canonical vocab. The class definitions themselves are unchanged (`class Gene(BaseModel)` is still in `schemas.py`). Imports still resolve through `helix_context.aliases`:

```python
# After PR1:
from helix_context.aliases import Document, KnowledgeStore, Compressor   # works
from helix_context.schemas import Gene, PromoterTags                       # still works
assert Document is Gene                                                    # still true
```

### What's untouchable — enforced by acceptance criteria

These are SQL/Prom/Pydantic-field contracts. Renaming them forces a DB migration on every existing helix instance or breaks every Grafana panel and alert rule. The R2 sweep does **not** touch any of:

- **Pydantic field names** that map to SQL columns: `promoter`, `epigenetics`, `chromatin`, `gene_id`, `codons`, `harmonic_links`, `gene_attribution`, etc.
- **SQL table & column names**: `genes`, `harmonic_links`, `gene_attribution`, and all column names within.
- **Prometheus metric & label names**: `helix_genome_size_genes`, `helix_ribosome_call_seconds`, `helix_chromatin_state_total`, `helix_harmonic_edges_total`, etc. Per ROSETTA.md, dashboard *panel titles* use engineering vocab with the legacy term referenced inline — that's already done; R2 just verifies the inline reference is present.
- **Package name** `helix_context`.
- **Class names** (`Gene`, `Genome`, `Ribosome`, `ChromatinState`, `PromoterTags`, `EpigeneticMarkers`, `GeneAttribution`). R3 flips these; R2 does not.
- **Module file names** (`ribosome.py`, `genome.py`, `codons.py`, `replication.py`, `hgt.py`). R3 moves these; R2 does not.
- **MCP tool names** (`helix_gene_get`, `helix_splice_preview`). R4 soft-deprecates these; R2 does not.
- **Terms on the "STAYS" list in ROSETTA.md**: `SEMA`, `TCM`, `cymatics`, `SPLADE`, `CWoLa`, `ScoreRift`, `PWPC`, `ellipticity`, `denatured`.
- **`docs/archive/**`** — all historical artifacts.
- **`docs/ROSETTA.md`** itself — the legacy column has to keep saying "Gene." R2 only *adds* to ROSETTA.md (the Prometheus + Dashboard sections from the prior working tree).
- **`docs/ROSETTA.md` legacy-term references in active prose** that take the form `(legacy term: X)` — these are intentional cross-references and stay.

### Translation rule (the heart of the sweep)

For each occurrence of `gene` / `genome` / `ribosome` / `chromatin` / `codon` / `promoter` / `epigenetics` / `expression` / `transcription` / `replication` / `harmonic_link` / `HGT`:

**Substitute the term** unless it falls into one of these classes:
1. A Python identifier (class name, variable, function name, import) — including identifiers inside backtick code-spans and triple-fenced code blocks.
2. A SQL or Prometheus name (table, column, metric, label).
3. A quoted/code-fenced legacy reference (the prose is *talking about* the legacy name).
4. Inside `docs/archive/**` or `docs/ROSETTA.md`.
5. A legitimate non-bio English word: `expression` in "regex/regular expression", `promotion` in ranking/score-math contexts.

Class 5 is judgment-call — flagged in the verification step. **Word-boundary false positives also fall under Class 5**: `gene` inside `generate` / `general` / `genetic`, `express` inside `expressive`, `promot` inside `promotional`. The grep in §4 uses `\b$term\b` (both boundaries) to keep these out of the count; any that slip through are left in place.

### Canonical substitutions

The mapping the sweep applies (canonical = right column from ROSETTA.md):

| Legacy term in prose | Canonical replacement |
|---|---|
| gene / Gene (the unit, in prose only) | document / Document |
| genome / Genome (the store, in prose only) | knowledge store / KnowledgeStore |
| ribosome / Ribosome (in prose only) | compressor / Compressor |
| chromatin / ChromatinState (in prose only) | lifecycle tier / LifecycleTier |
| codon / Codon (in prose only) | fragment / Fragment (or chunk / Chunk where the existing prose uses "chunk") |
| promoter (the field/tag concept, in prose only) | tags |
| epigenetics / EpigeneticMarkers (in prose only) | signals / DocumentSignals |
| transcription (in prose only) | encoding |
| express / expression (the pipeline step, in prose only) | retrieve / retrieval |
| replicate / replication (in prose only) | persist / persistence |
| harmonic links (in prose only) | co-activation edges |
| HGT (horizontal gene transfer, in prose only) | cross-store import |

---

## 3 · What ships in PR1

### Ordered scope

1. **Close the ROSETTA fixture.** Commit the Prometheus + Dashboard panel-title sections currently dirty in the prior working tree (`followup/score-aware-budget-trim`) as PR1's **first commit**. After this commit, ROSETTA.md is authoritative for the rest of PR1.
2. **Python docstring + comment sweep** under `helix_context/**`, excluding:
   - `helix_context/aliases.py` (legacy-targeted by design).
   - `helix_context/schemas.py` Pydantic field definitions (SQL contract).
   - Any docstring whose subject is a SQL/Prom name being preserved verbatim.
3. **Active docs prose sweep** under `docs/`:
   - `docs/MISSION.md`, `docs/DESIGN_TARGET.md`, `docs/INTEGRATING_WITH_EXISTING_RAG.md`
   - `docs/architecture/**` (incl. `KNOWLEDGE_GRAPH.md`, `FEDERATION_LOCAL.md`, `PIPELINE_LANES.md`, `LAUNCHER.md`, `SESSION_REGISTRY.md`, `raude_antigravity_persona.md`)
   - `docs/ops/**` (incl. `RESTART_PROTOCOL.md`, `SKILLS_BUNDLE.md`)
   - `docs/benchmarks/**` (incl. `BENCHMARKS.md`, `BENCHMARK_RATIONALE.md`)
   - `docs/clients/**` (incl. `claude-code.md`)
   - Excludes: `docs/archive/**`, `docs/ROSETTA.md`, `docs/superpowers/**` (these are spec/plan docs that talk about the rename — their bio terms are legitimate). **This spec and its sibling plan are explicitly out of scope for the sweep** — a future automated pass must not rewrite the spec's own legacy-term references.
4. **Top-of-repo docs**: `README.md`, `CLAUDE.md`.

### Commit cadence

Five commits expected:

1. `docs(rosetta): close fixture — Prometheus + dashboard panel-title sections`
2. `docs(rename-r2): canonical vocab in helix_context/*.py docstrings + comments`
3. `docs(rename-r2): canonical vocab in docs/architecture/**`
4. `docs(rename-r2): canonical vocab in docs/{ops,benchmarks,clients}/**`
5. `docs(rename-r2): canonical vocab in README.md + CLAUDE.md`

### Mechanics

- **Primary tools:** Edit + Grep. Pure text replacement is faster with these than Serena's symbol tools.
- **Pass shape:** one-pass-per-file. Read the full file, identify every hit, batch the substitutions into as few Edit calls as the file's structure allows. Avoid one-Edit-per-hit — it inflates the commit's surface and the reviewer's read.
- **Serena's role this sprint:** light. Use `find_referencing_symbols` as a verification cross-check when a docstring names another module (confirms the rename target). Save the heavy `rename_symbol` lifting for R3.
- **Doctest guard:** `pytest --doctest-modules helix_context/` runs after the Python sweep to catch any sample code in docstrings that referenced a real function whose name accidentally shifted.

---

## 4 · Verification

### Grep delta

The PR body posts a baseline-vs-post-sweep count for each bio term:

```bash
# baseline (run on master before any sweep)
for term in gene genome ribosome chromatin codon promoter epigenetics \
            transcription express replicat harmonic_link HGT; do
  echo -n "$term: "
  grep -rIE "\\b$term\\b" helix_context/ docs/ README.md CLAUDE.md \
    --exclude-dir=archive --exclude-dir=superpowers \
    --exclude=ROSETTA.md --exclude=aliases.py 2>/dev/null | wc -l
done
```

The grep uses `\b$term\b` on both ends to filter out compound-word false positives (`generate`, `expressive`, `promotional`). `aliases.py` is excluded because it intentionally retains legacy class names by design — its hits would otherwise create a fixed floor that artificially deflates the post-sweep ratio.

**Acceptance:** post-sweep count drops by **≥ 80%** from baseline for each term. The remaining ≤ 20% must each fall into one of the protected classes from §2's translation rule — every remaining hit is justified in a verification comment block in the PR body.

### Functional guards

- `pytest -m "not live"` — full mock suite green.
- `pytest --doctest-modules helix_context/` — embedded doctests green.
- `python -c "from helix_context.aliases import Document, KnowledgeStore, Compressor; from helix_context.schemas import Gene, PromoterTags; assert Document is Gene; print('ok')"` — import-surface contract.
- `python -c "import helix_context; print(helix_context.__version__)"` — package still imports cleanly.

### Lookup-fixture guard

Spot-check that ROSETTA.md still resolves any legacy term a reader might hit:
- Pick three random legacy bio terms from R2's swept files (pre-PR).
- Confirm each appears in ROSETTA.md's mapping table or "STAYS" list.
- If not, add the row to ROSETTA.md as part of PR1.

---

## 5 · Risks & mitigations

| Risk | Mitigation |
|---|---|
| Doctest in a swept docstring references a real function whose name shifted. | `pytest --doctest-modules helix_context/` runs after each commit; revert the offending hunk if red. |
| `expression` (regex) or `promotion` (ranking) gets falsely swept. | Class 5 of the translation rule. Each ambiguous hit is read in context, not blindly substituted. |
| A docstring quotes a legacy class name as part of an example: `# returns a Gene`. | Code-comment example referencing the live class name stays as `Gene` (Class 1 of the rule — Python identifier). |
| Sweep accidentally edits `docs/archive/**`. | Excluded by find/glob. The verification grep treats `archive/` as the cutoff. |
| ROSETTA.md fixture commit collides with the dirty version on `followup/score-aware-budget-trim`. | After PR1 merges, the prior branch's dirty `docs/ROSETTA.md` is reset (`git checkout master -- docs/ROSETTA.md`). |
| Reviewer asks "what about R3 and R4?" | PR description points to ROSETTA.md's phase table and this spec's §7. |

---

## 6 · Sprint exit criteria

- ROSETTA.md fixture-closure commit landed (Prometheus + Dashboard sections in `master`).
- Baseline-vs-post-sweep grep-delta table in PR body shows ≥ 80% reduction per bio term.
- Every remaining hit is justified (Class 1–5 of the translation rule).
- `pytest -m "not live"` green; `pytest --doctest-modules helix_context/` green.
- Import-surface contract unchanged (`Document is Gene` still holds).
- `rename/r2-prose-sweep` PR merged into `master`.

---

## 7 · Deferred to future sprints

These are sketched in `docs/ROSETTA.md`'s phase table and re-confirmed during the 2026-05-11 brainstorming session. Each will get its own spec when its sprint comes up:

- **R3 — internal symbol rename (full depth).** Module file moves: `ribosome.py` → `compressor.py`, `genome.py` → `knowledge_store.py`, `codons.py` → `fragments.py`, `replication.py` → `persistence.py`, `hgt.py` → `cross_store_import.py`. Class-def flip: real defs become `Document` / `KnowledgeStore` / `Compressor` / `LifecycleTier` / `DocumentTags` / `DocumentSignals` / `DocumentAttribution`; legacy names become one-line aliases. Pydantic field names + SQL stay. Old import paths re-export via shim modules. Serena `rename_symbol` is the primary tool.
- **R4 — MCP legacy-tool soft-deprecation, full signal.** Docstring nudges on `helix_gene_get` / `helix_splice_preview`. Structured WARN log on first legacy-tool call per process. New `helix_mcp_legacy_alias_total{legacy_name, canonical}` Prometheus counter for future hard-deprecate data.

---

## 8 · How this spec gets implemented

1. This spec is committed to `rename/r2-prose-sweep`.
2. Spec review loop runs (subagent-driven).
3. User reviews the spec.
4. Once approved, `superpowers:writing-plans` produces the step-by-step implementation plan at `docs/superpowers/plans/2026-05-11-rename-r2-prose-sweep.md`.
5. `superpowers:executing-plans` drives the actual sweep on the worktree.
6. PR opened with the verification artifacts from §4.
