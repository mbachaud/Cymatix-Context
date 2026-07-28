# Dogfood bed

The corpus cymatix retrieves against when we eat our own cooking: four repos
we own — `cymatix-context`, `driftwatch`, `scorerift`, `MaxExpressKit`.

Owning all four is the point. The previous SIKE needle set drew gold from six
repos, **51 of whose 64 gold files lived in private siblings**, so the matching
gold pack could not be handed to anyone. Every gold file here comes from these
four, and three of them are public — the pack ships as a unit.

## Pipeline

```bash
# 1. build the bed (destructive on its target; --dry-run to preview)
python scripts/build_dogfood_genome.py

# 2. derive + verify gold_source from the live repos
python benchmarks/dogfood/verify_needles.py

# 3. cut the transferable pack
python benchmarks/dogfood/cut_gold_pack.py

# 4. emit identity / per-repo / gold gene_id lists
python benchmarks/dogfood/emit_gene_ids.py

# 5. measure retrieval
python benchmarks/dogfood/run_baseline.py
```

Steps 2–5 each refuse to produce a misleading artifact: `verify_needles` fails
on an unresolvable or non-distinctive needle, `cut_gold_pack` refuses to build
from a failing needle set, `emit_gene_ids` warns on needles whose gold files
produced no genes, and `run_baseline` records `gold_genes` per needle so a
`MISS` against zero gold reads as a bed problem rather than a retrieval one.

## Curation, and why

`scripts/build_dogfood_genome.py` walks a **curated** file set. Two documented
contaminations invalidated earlier beds and the exclusions map to them
one-for-one:

| Excluded | Why |
|---|---|
| `benchmarks/results`, `docs/research/data`, `docs/benchmarks/data`, `benchmarks/contextbench` | Frozen measurement payloads. Re-ingesting a result JSON makes the bed retrieve its own scores — the **echo-gene** defect. |
| `.worktrees`, `_worktrees`, `.clone` | The **worktree-dupe** source: the same file ingested once per git worktree. |
| `benchmarks/aider_polyglot` | A downloaded exercism cache — 1,240 files of someone else's fixtures, 53% of the raw walk. |
| `docs/archive` | Superseded docs read as authoritative and generate confident wrong answers. |
| `tests/fixtures` | Synthetic scaffolding authored to exercise the chunker, not to be retrieved as knowledge. |

Curation takes the walk from 2,679 files to **1,172**, and rebalances it from
87% cymatix-context to 70%.

**By gene count the bed is still 88% cymatix-context** (5,654 of 6,427) — its
files are larger and chunk into more genes than the other three repos'. That
imbalance is not a defect of the curation, it is what a dogfood bed centred on
cymatix looks like, but it is load-bearing for how the baseline reads.

## Needles

18 needles, each pinning a **specific value** — a port, a threshold, a default,
a version. Prose questions need a judge to grade; a number does not.

`gold_source` is **derived, not declared**. `verify_needles.py` scans the four
repos for files literally containing each needle's anchor and writes the list
to `needles_resolved.json`. Hand-written gold paths rot silently when a file
moves; derived ones fail loudly.

Two failure modes, both hard:

- **UNRESOLVED** — the anchor matches nothing. The expected value is stale.
- **TOO_BROAD** — the anchor matches more than `--max-gold` files. This bit
  hard on the first pass: `11437` alone matched **156 of 1,172 files**, so a
  random document had a ~13% chance of counting as gold. Anchors were tightened
  to `localhost:11437`, `expression_tokens = 7000`, `> 0.15` and so on until
  every needle landed in **2–12 gold files**.

## Baselines

`run_baseline.py` measures **retrieval only** — no decoder, no consumer ladder,
no judge. Mixing them is how a retrieval regression hides behind a good model.

It scores by **gene_id set membership**, not path matching. That is deliberate:
substring matching between two machines' directory layouts is what produced
false negatives on the 2026-07-03 bedsweep.

It reports rank, not just delivery. Gold at rank 9 under a budget that seats 5
is retrieved-but-unusable — the #260 finding.

Retrieval runs through `CymatixContextManager.build_context(read_only=True)`,
never `KnowledgeStore` directly: the store constructor defaults dense, SPLADE
and the rest to **off**, so a direct construction would quietly measure a
stripped-down retriever and label it the baseline.

## gene_id lists

`emit_gene_ids.py` writes three things, each answering a different question:

- **identity** — every gene_id plus a rollup sha256. Two beds with the same
  digest are the same corpus. Settles "are we measuring the same thing?"
  before anyone compares numbers, at ~17 bytes per gene.
- **by_repo** — grouped, so a partial or single-repo bed can be checked.
- **gold** + **gold_by_needle** — turns "was gold delivered?" into set
  membership, with no path normalisation between hosts.

This pattern is proven: the 2026-07-27 SIKE decontamination established that
gene_ids survive deletion unchanged, so a list computed on one host selects
exactly the right rows on another, and the delete count verifies itself.

## First baseline (2026-07-28)

6,427 genes over 1,177 files. **recall@12 = 0.667, recall@50 = 1.000** — every
needle's gold is in the pool, so the whole deficit at the shipped budget is
ranking, not recall.

The split is the finding: needles about `driftwatch` / `scorerift` /
`MaxExpressKit` land at ranks 1-7, while **six of eight `cymatix-context`
needles land past k=12** (ranks 23-41). Cymatix ranks worst on the repo it is
88% made of. Write-up:
[`docs/benchmarks/2026-07-28-dogfood-baseline.md`](../../docs/benchmarks/2026-07-28-dogfood-baseline.md).

## Artifacts

Committed: the tooling, `needles_dogfood.py`, `needles_resolved.json`, and
`pack/gold_pack_manifest.json`.

Not committed: the genome (`genomes/`), the pack tarball, and the working
`gene_ids/` + `baseline*.json`. All are reproducible from the above, and the
tarball ships out-of-band the same way the bench beds do.

**Dated measurements are committed as receipts**, under `docs/benchmarks/data/`
— a baseline is a fact about a bed on a day, not a regenerable artifact, since
re-running it after the repos change gives a different number.
