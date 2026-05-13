# R3 — Internal symbol rename (canonical software vocabulary)

**Status:** Stage A + B + C shipped via PR #88 (`3b92b02`).
Stage D + E in flight via PR #89.

**Predecessor specs:** [R2 prose sweep design](2026-05-11-rename-r2-prose-sweep-design.md).
**Tracking issue:** #87.
**Lexicon source-of-truth:** [`docs/ROSETTA.md`](../../ROSETTA.md).

---

## Why this exists

R1 (Rosetta Stone + alias module) and R2 (prose sweep across 91 files)
established that helix's canonical software vocabulary lives in
`docs/ROSETTA.md` and can be adopted incrementally without breaking
back-compat. R2 explicitly **deferred identifier-level changes** to R3
per spec §2 + §7.

R3 closes the loop: the canonical names become the *real* class
definitions, the module filenames match the class identities, and the
internal method/helper surface uses the canonical vocab. Legacy
biology names survive as one-line aliases declared adjacent to the
canonical definition, so every existing caller — internal or external,
test or runtime — continues to work unchanged.

**Decisions baked in** (per #87 + plan approval, 2026-05-13):

1. **Class-def flip direction**: canonical (`Document`) becomes the
   real class; legacy (`Gene`) becomes the alias. Inverts the R1
   `aliases.py` direction.
2. **Module renames**: ship in R3 with shim modules at the old paths.
3. **MCP slimdown (PR #78)**: not in scope. MCP + CLI both stay as-is.

Both `Gene is Document` and `Document is Gene` continue to hold —
Python class assignment makes them the same object. Only `__name__`
changes (now reports the canonical). No wire-format change, no SQL
schema change, no MCP tool rename.

---

## Out of scope (still protected, per R2 spec §2)

- **SQL schema** — tables `genes`, `gene_attribution`, `harmonic_links`;
  columns `gene_id`, `promoter`, `epigenetics`, `chromatin`, `codons`.
- **Pydantic field names** on the wire. Renaming forces a DB migration.
- **Prometheus metric / label names** — `helix_genome_size_genes`,
  `helix_chromatin_state_total`, `helix_ribosome_call_seconds`,
  `call_kind="replicate"` etc. Dashboard contract.
- **`ChromatinState` enum value strings** — `OPEN` / `EUCHROMATIN` /
  `HETEROCHROMATIN`. Serialized as TEXT in pydantic; queried.
- **MCP tool names** — `helix_gene_get`, `helix_splice_preview` etc.
  R4 territory.
- **`agent_prompt.py::HELIX_NO_MATCH_FRAGMENT`** + parallel
  `docs/agent-sdk-fragment.md` — JSON contract field names that
  frontier LLMs key on.
- **LLM prompt strings** in `compressor.py` (`_PACK_SYSTEM`,
  `_EXPRESS_SYSTEM`, `_splice_system`, `_REPLICATE_SYSTEM`) — these
  are contracts with the small local models, which were trained on
  the biology vocab. Renaming risks degrading retrieval quality on a
  downstream-LLM-keyed surface.

---

## Stage summary (as built)

### Stage A — Class-def flip + alias inversion (1 commit, `56fcbed`)

Surgical edit to `schemas.py`, `genome.py`, `ribosome.py`, and
`aliases.py`. For each pair:

```python
# schemas.py BEFORE
class Gene(BaseModel):
    gene_id: str
    ...

# schemas.py AFTER
class Document(BaseModel):
    gene_id: str        # field name stays — SQL contract
    ...

Gene = Document         # back-compat alias; identity preserved
```

7 pairs flipped:

| Module | Canonical (real def) | Legacy (alias) |
|---|---|---|
| `schemas.py` | `LifecycleTier` | `ChromatinState` |
| `schemas.py` | `DocumentTags` | `PromoterTags` |
| `schemas.py` | `DocumentSignals` | `EpigeneticMarkers` |
| `schemas.py` | `Document` | `Gene` |
| `schemas.py` | `DocumentAttribution` | `GeneAttribution` |
| `genome.py` | `KnowledgeStore` | `Genome` |
| `ribosome.py` | `Compressor` | `Ribosome` |

### Stage B — Module file moves + shim modules (5 commits, `460d824..9e7471f`)

| Old path | New path |
|---|---|
| `helix_context/ribosome.py` | `helix_context/compressor.py` |
| `helix_context/genome.py` | `helix_context/knowledge_store.py` |
| `helix_context/codons.py` | `helix_context/fragments.py` |
| `helix_context/replication.py` | `helix_context/persistence.py` |
| `helix_context/hgt.py` | `helix_context/cross_store_import.py` |

Each old path is now a 30-line shim that walks `dir(_new_module)` and
re-exports every non-dunder attribute (covers private-name leakage:
`_parse_json`, `_EXPRESS_SYSTEM`, `_splice_system`, `_kv_keys_from_list`).

### Stage C — Internal method renames (4 commits, `edc0194..71469ba`)

**Compressor** (was Ribosome) — methods + intra-class aliases:

| Old | New |
|---|---|
| `pack(content, content_type)` | `encode(content, content_type)` |
| `splice(genes, ...)` | `trim(genes, ...)` |
| `replicate(query, response)` | `persist(query, response)` |
| `re_rank(query, candidates, k)` | `rerank(query, candidates, k)` |

**KnowledgeStore** (was Genome) — methods + intra-class aliases:

| Old | New |
|---|---|
| `upsert_gene(gene, apply_gate)` | `upsert_doc(gene, apply_gate)` |
| `query_genes(...)` | `query_docs(...)` |
| `query_genes_ann(...)` | `query_docs_ann(...)` |
| `query_genes_dense_recall(...)` | `query_docs_dense_recall(...)` |
| `get_gene(gid)` | `get_doc(gid)` |

**Internal helpers** in other modules:

| File | Old | New |
|---|---|---|
| `context_manager.py` | `_express(...)` | `_retrieve(...)` |
| `context_manager.py` | `_make_parent_gene_id(...)` | `_make_parent_doc_id(...)` |
| `context_manager.py` | `_upsert_parent_gene(...)` | `_upsert_parent_doc(...)` |
| `fragments.py` | `codon_id(tokens)` | `fragment_id(tokens)` |
| `cymatics.py` | `gene_spectrum(...)` | `doc_spectrum(...)` |
| `cymatics.py` | `cached_gene_spectrum(...)` | `cached_doc_spectrum(...)` |
| `cymatics.py` | `_cached_gene_spectrum(...)` | `_cached_doc_spectrum(...)` |
| `cymatics.py` | `interference_splice(...)` | `interference_trim(...)` |

The reflection edge case at `context_manager.py:2336`
(`hasattr(self.ribosome, "re_rank")`) was co-updated to use the
canonical `rerank` string. All ~30 internal callers across
`helix_context/*.py` were migrated to canonical names; tests that
monkey-patch via instance-attribute assignment were updated to patch
both legacy + canonical names (alias semantics diverge under instance
patches).

### Stage D — Local-variable sweep (in flight, PR #89)

Per-file pass focused on the high-visibility patterns:

- `for gene in candidates:` → `for doc in candidates:` (loop counters)
- `gene_a, gene_b` → `doc_a, doc_b` (pair-pattern in cymatics helpers)
- `genes: List[Gene]` → `docs: List[Document]` (param + type annotation)
- `gene = Gene(...)` followed by `gene.X` attribute access → `doc = Document(...)`

Stage D is bounded by safety: it intentionally avoids renaming
standalone `gene =`, `gene_id =`, or `gene.X` patterns that overlap
with kwargs (`Gene(gene_id=...)`), SQL column refs (`row["gene_id"]`),
or method-call kwargs to surfaces that retained their legacy parameter
names.

### Stage E — ROSETTA.md refresh + this design spec

The phase-status table in `docs/ROSETTA.md` is brought current with
commit SHAs for R1, R2, and the four R3 sub-stages. This file
serves as the durable spec record alongside R2's spec for any future
contributors auditing the rename effort.

---

## Identity contract after R3

Across all 29 alias pairs that ship in R3:

| Layer | Pairs |
|---|---|
| Class (schemas + genome + ribosome) | 7 |
| Compressor methods | 4 |
| KnowledgeStore methods | 5 |
| context_manager helpers | 3 |
| cymatics helpers | 4 |
| fragments helpers | 1 |
| Module re-exports (shim) | 5 |

For each pair: `legacy is canonical` evaluates True; the canonical
name owns `__name__`; both names appear in the module's namespace.

For methods specifically, intra-class assignment (`pack = encode`)
preserves function-object identity, so `Class.legacy is Class.canonical`
holds at the class level. Instance-attribute patches (`obj.pack = X`)
DO diverge — tests that monkey-patch must patch both names if internal
code may use either.

---

## Verification

After Stages A + B + C (PR #88 baseline, `3b92b02`):

| Gate | Result | Wallclock |
|---|---|---|
| Pre-Stage-A baseline | 127/127 focused | 2:18 |
| Post-A full mock | 1933 / 0 / 15 / 21 / 2 | 9:07 |
| Post-B full mock | 1933 / 0 / 15 / 21 / 2 | 8:52 |
| Post-C full mock | 1933 / 0 / 15 / 21 / 2 | 7:40 |

Stage D will rerun the full mock at the end of PR #89 and is expected
to match the same 1933 / 0 / 15 / 21 / 2 baseline (R3 is symbol-only;
any retrieval-quality movement would indicate a bug).

---

## Stage D — known not-fully-completed scope

The original plan estimated ~200+ local variable renames. PR #89 ships
the **clearest-win subset** — function parameters, type annotations,
loop counters, and the obvious `gene = Gene(...)` patterns in
compressor.py — but does **not** exhaustively walk every `gene_id` or
`gene.X` reference in module bodies. The aliases make these renames
purely cosmetic; future cleanup PRs can sweep them incrementally if
desired, with no behavioral impact.

The Stage D files actually touched in PR #89:

- `helix_context/cymatics.py` — full pass (params + types + locals)
- `helix_context/compressor.py` — encode/persist body + return types + import widening
- `helix_context/context_manager.py` — 5 loop-counter sites
- `helix_context/context_packet.py` — 1 loop-counter site
- `helix_context/knowledge_store.py` — 2 loop-counter sites in `_apply_dense_rerank`
- `helix_context/shard_router.py` — 1 loop-counter site

Not touched (intentionally — would require larger scope or surface
larger risk):

- The bulk of variable references inside `knowledge_store.py`
  (~4500-line module). Local `gene` ↔ `doc` renames in tier-scoring
  internals are aliases-only-cosmetic; defer.
- Test files using legacy names in fixtures (`tests/conftest.py`,
  `tests/test_genome.py`, etc.). Tests were already updated in R3
  Stage C to patch both names; remaining stylistic prose stays.
- `agent_prompt.py` system prompts and the parallel
  `docs/agent-sdk-fragment.md` — explicit JSON contract surface.

---

## Lookup

For any term not covered here, consult [`docs/ROSETTA.md`](../../ROSETTA.md)
— it remains the canonical bidirectional glossary across the entire
biology-tax rename effort.
