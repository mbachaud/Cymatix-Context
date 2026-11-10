# Launcher Graph Summary Design

**Date:** 2026-07-13

**Status:** Approved for implementation planning

## Summary

Add a read-only graph summary to the launcher dashboard's Genome tab and fix
the database-selection modal so an active genome is not obscured. The summary
is deliberately a compact inventory of graph layers, not an interactive node
renderer. It gives an honest picture of the active genome without asking the
browser to lay out thousands of nodes and tens of thousands of edges.

## Goals

- Show useful graph-scale information for whichever genome is active.
- Preserve the existing dark amber launcher design and server-rendered polling
  model.
- Avoid repeated full SQLite scans during the dashboard's two-second refresh.
- Keep schema differences, locks, or missing databases from breaking the rest
  of the dashboard.
- Ensure an element marked `hidden` remains hidden even when it also has the
  modal backdrop class.

## Non-goals

- Render, filter, or navigate an interactive graph.
- Export an Obsidian vault or generate wikilinks.
- Add a graph visualization dependency or a new client-side state system.
- Combine relation layers into a single estimated node or edge total whose
  meaning would be ambiguous.

## User Interface

The Genome tab gains a **Graph summary** panel. It contains a compact layer
ledger rather than a force-directed canvas:

| Layer | Primary value | Supporting value |
| --- | --- | --- |
| Genes | total genes | genes participating in COVER relations |
| Semantic | distinct undirected COVER links | stored COVER rows, when useful for diagnosis |
| Structure | CHUNK_OF relation rows | relation code label |
| Co-activation | harmonic link rows | learned/live label |
| Entities | distinct entities | entity-to-gene membership rows |

Counts are formatted for scanning and every row names what is counted. The
panel carries a small `SUMMARY` or `renderer deferred` treatment so it cannot
be mistaken for an interactive graph. The current dogfood database should
therefore describe approximately 5,636 genes, 4,049 COVER-connected genes,
29,061 distinct COVER pairs, 4,240 CHUNK_OF rows, 28 harmonic links, 14,186
entities, and 42,236 entity memberships, but these values are not hard-coded.

If summary data cannot be read, the panel renders a quiet
`Graph summary unavailable` state while the rest of the Genome tab continues
to function.

## Data Collection

The launcher reads the active SQLite database using a dedicated, read-only
summary function. It returns a typed mapping with explicit fields for every
displayed count. Queries use existing graph tables when present:

- `genes`
- `gene_relations`, with relation codes `5` (COVER) and `100` (CHUNK_OF)
- `harmonic_links`
- `entity_graph`

Distinct COVER links are counted as undirected endpoint pairs so reciprocal
rows do not inflate the visible link count. COVER-connected genes are the
distinct union of both relation endpoints. Entity count is derived from the
entity identifier stored by `entity_graph`; memberships remain the row count.

The result is cached by resolved database path with a short time-to-live of
approximately 30 seconds. A database switch changes the cache key and yields a
fresh summary. The cache is process-local and bounded; it is not persisted and
does not modify the genome. The collector catches `Exception`, logs at warning
level, and returns an unavailable state rather than silently swallowing an
error.

The existing panel-fragment response includes the graph summary state so no
new browser endpoint or fetch loop is required.

## Database Modal Fix

The server already emits the database modal with the HTML `hidden` attribute
when `needs_db_selection` is false. The existing `.modal-backdrop` rule assigns
`display: flex`, which overrides the browser's default hidden presentation.

Add a narrowly scoped rule equivalent to:

```css
.modal-backdrop[hidden] {
  display: none !important;
}
```

No JavaScript workaround is needed. Modal discovery and dismissal behavior
remain unchanged for the legitimate no-database state.

## Failure and Performance Semantics

- Missing optional graph tables produce an unavailable summary, not invented
  zeroes that could be confused with valid empty data.
- A locked, corrupt, or unreadable database produces the same contained error
  state and a warning log.
- All SQLite connections are short-lived and read-only in effect.
- The two-second dashboard poll normally reads cached data; graph aggregation
  runs only on cache miss or expiry.
- No work is scheduled in the browser beyond replacing the existing panel
  fragment.

## Verification Strategy

Tests are written before production changes.

- Unit tests create a temporary SQLite genome with reciprocal COVER rows,
  CHUNK_OF rows, harmonic links, and repeated entities, then assert all summary
  semantics.
- A cache test verifies repeated reads reuse the cached result and a different
  database path does not.
- An unavailable-state test covers a missing or incomplete graph schema.
- A template or launcher response test verifies the panel appears on the
  Genome tab with formatted values.
- A CSS regression test verifies `.modal-backdrop[hidden]` resolves to
  `display: none` and the visible modal behavior remains intact.
- Existing launcher tests run as a regression suite.

## Implementation Boundary

This change is limited to launcher summary collection, panel state/rendering,
the modal CSS correction, and their tests. Interactive visualization,
graph-specific APIs, Obsidian export, and graph traversal controls remain
future work.
