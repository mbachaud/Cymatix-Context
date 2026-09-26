# Dogfood bed baseline — cloud rebuild on v0.10.0b1 (2026-09-26)

A fresh rebuild of the dogfood bed from the four owned repos at their current
heads, followed by the same retrieval-only baseline as
[2026-07-28](2026-07-28-dogfood-baseline.md). Retrieval only: no decoder, no
consumer ladder, no judge. Scored by gene_id set membership.

Raw data, all under `data/`:

- `2026-09-26-dogfood-baseline-k12.json` and `…-k50.json`, from `run_baseline.py`
- `2026-09-26-dogfood-needles-resolved.json`, from `verify_needles.py` (gold re-derived today)
- `2026-09-26-dogfood-gold-by-needle.json` and `…-gene-id-summary.json`, from `emit_gene_ids.py`
- `2026-09-26-dogfood-build-manifest.json`, from `build_dogfood_genome.py`

## Setup

| | |
| --- | --- |
| Host | Linux cloud container, CPU only, Python 3.11.15, SQLite 3.45.1 |
| Install | `pip install -e ".[cpu]"` + `en_core_web_sm`; `psutil` absent, so the conservative SQLite memory budget was used |
| Env | `PYTHONHASHSEED=0`, `CYMATIX_DISABLE_LEARN=1`, shipped `cymatix.toml` |
| Ingest | sequential, 953.7 s |
| Repo heads | cymatix-context `b8709512cf`, driftwatch `2c7e55e698`, scorerift `d8cf8f6eb7`, MaxExpressKit `48ad5a5c19` |

## Bed

| | genes | files |
| --- | ---: | ---: |
| cymatix-context | 6,492 | 933 |
| driftwatch | 522 | 214 |
| scorerift | 162 | 70 |
| MaxExpressKit | 98 | 68 |
| **total** | **7,274** | **1,285** |

Identity `02ee43f0e0f49602…` and gold `462308355297c153…` (473 gold genes).
The full digests are in the gene-id summary. All 18 needles resolve, with 1–13
gold files each.

## Headline

| metric | k=12 | k=50 |
| --- | ---: | ---: |
| recall@1 | 0.389 | 0.389 |
| recall@3 | 0.556 | 0.556 |
| recall@5 | 0.722 | 0.722 |
| recall@k | **0.833** | **0.944** |
| MRR | 0.496 | 0.499 |
| median rank when delivered | 3 | 3 |
| median latency | 89 ms | 102 ms |

**This is not a before/after pair with July.** The bed grew from 6,427 to
7,274 genes, and gold is re-derived from the repos as they stand, so the gold
sets changed for 11 of 18 needles. The July latencies also came from a
different host. The 2026-07-28 shape still holds: the three small repos land
at ranks 1–6, and the misses are all cymatix-context needles
(`proxy_port` r28, `fusion_default` r48, and `cymatics_bins` below).

## `cymatix_cymatics_bins`: a gold-derivation artifact, not a retrieval miss

This needle misses even at k=50, where July found it at rank 14. The retriever
is not what changed. Its anchor is the literal `256 bin`, and since July the
three files that carried the answer stopped matching it:

- `README.md` now says `256-bin` and `256-dimensional`.
- `cymatix.toml` and `docs/config-reference.md` now say `N_BINS (256)`.

The derived gold is therefore four prose files: two 2026-07-25
portfolio-README plan/spec docs, `wiki/Home.md` and
`wiki/Retrieval-Dimensions.md`. None of them enters the 50-candidate pool.
Retrieval itself answers the question well. The rank-1 gene is
`cymatix_context/scoring/cymatics.py`, the chunk defining `N_BINS = 256`, and
17 of the 51 genes containing `256 bin` / `256-bin` / `N_BINS` are in the pool.

Widening the anchor, for example to match `256-bin` and `N_BINS`, would restore
code and config files as gold. It would also change the needle definition, so
it belongs in its own change with a re-derived `needles_resolved.json`.
