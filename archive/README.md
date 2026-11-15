# archive/

Retired scripts, benchmark harnesses and data that are no longer part of the
working toolset but are kept in-repo as history and as reproduction paths for
older receipts.

**Not maintained.** Nothing here is tested, linted beyond syntax, imported by
`cymatix_context`, shipped in the wheel or sdist, or run by CI. Files may
reference modules, config keys, local paths (`F:/Projects/...`) or
servers that no longer exist.

## Path rule

Every file keeps its original repo path under `archive/`:

```
scripts/deep_ingest.py                    ->  archive/scripts/deep_ingest.py
benchmarks/dogfood/erb/dense_ceiling_probe.py
                                          ->  archive/benchmarks/dogfood/erb/dense_ceiling_probe.py
```

So a historical doc, receipt or CHANGELOG entry that cites `scripts/X` or
`benchmarks/Y` resolves to `archive/scripts/X` or `archive/benchmarks/Y` when
the original path no longer exists. Those citations are left unedited on
purpose: they record where the file lived when the receipt was taken.

`git log --follow archive/<path>` shows the full history from before the move.

## Running something from here

Scripts compute repo-relative paths from their own location, so most will not
run in place. Either check out the commit a receipt cites, or `git mv` the file
back to its original path first and fix what has drifted.

## What was archived and why

See `docs/reviews/2026-11-10-archive-efficiency-audit.md` for the audit and the
per-file classification. In short:

- `scripts/`: one-off backfills from April 2026, ingest drivers for personal
  directories (superseded by `cymatix ingest`, `scripts/ingest_all.py` and
  `[sync]`), closed experiment campaigns (`e8_*`, `pwpc/*` phase 0 and lockstep,
  `docs_tidy_*`, `ops/overnight_*`), and bench-chain watchers and sequencers.
  `scripts/pwpc/sprint3.py` and `export_with_window_features.py` stay live:
  they train the shipped `training/models/stacked_plr.joblib`.
- `benchmarks/`: harnesses and probes with no referrer in code, tests or live
  docs, including the W2.1 COVER-walk reachability probe (feature killed by
  receipt, PR #408).
