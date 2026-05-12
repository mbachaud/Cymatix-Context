# Operator Runbooks

This document is the operator reference for `helix-context` post-deployment
maintenance and the three calibration scripts the 2026-05-08 7-stage
retrieval-fix landed in `master` on 2026-05-10. After upgrading to a
release with the Stage 2 / Stage 4 / Stage 6 / Stage 7 changes, the new
code paths take effect on a production knowledge store only after these scripts
run against it.

Assumptions: `helix.toml` at repo root (or `HELIX_CONFIG`); active
knowledge store at `genomes/main/genome.db`; server bound to loopback
at `127.0.0.1:11437`. Admin endpoints are auth-free by design — bind
to loopback or a reverse proxy that enforces auth in front; do not
expose to a public interface.

## Overview

Apply the three calibration runbooks in this order, exactly once, the
first time you bring a knowledge store up against a release containing PR #46
(Stage 2) through the Stage 7 freshness gate:

1. **Pull and stop traffic** — block writers (`/v1/chat/completions`,
   `/ingest`, `/consolidate`) by stopping the helix process.
2. **Runbook 1 — BGE-M3 1024-dim backfill** (Stage 2). Re-encode every
   document into the new `embedding_dense_v2 BLOB` column.
3. **Runbook 2 — Threshold calibration** (Stage 4). Derive a
   margin-over-random ANN cutoff and per-classifier floors from
   `located_n1000`.
4. **Runbook 3 — Know-confidence calibration** (Stage 6 + Stage 7). Fit
   the 5-feature logistic for `KnowBlock.confidence`. Stage 7 added
   `freshness_min` as β5.
5. **Resume traffic** — restart helix or POST `/admin/refresh`. The
   runbooks for `/admin/refresh`, `/admin/vacuum`, `/consolidate`, and
   `/context/refresh-plan` follow as day-2 operations.

The three calibration scripts (`scripts/backfill_bgem3_v2.py`,
`scripts/calibrate_thresholds.py`, `scripts/calibrate_know_confidence.py`)
have their CLI surfaces documented inline below; every flag has been
verified against the script's `argparse` definitions in this worktree.

---

## Runbook 1: BGE-M3 1024-dim backfill (Stage 2)

`scripts/backfill_bgem3_v2.py` re-encodes each document's content at the full
1024-dim BGE-M3 embedding and writes raw little-endian fp32 bytes into a
new `embedding_dense_v2 BLOB` column on the `genes` table. After Stage 2,
this column is what the dense recall path reads; the legacy
`embedding_dense TEXT` (256-dim JSON) column stays in the schema for one
release as a rollback safety net.

### When to run

After upgrading to a release containing PR #46 (Stage 2 dense recall).
First-time only on a given knowledge store; idempotent on rerun. The Stage 2 spec
ships an init-time warning at `Genome.__init__`: if
`dense_embedding_enabled=True` AND v2 coverage is non-zero, helix warns
once that `ann_similarity_threshold` is calibrated for dim=256 and
invalid against dim=1024 — Runbook 2 is the fix.

### Wall time

- CPU sentence-transformers BGE-M3: ~30-90 minutes for an 18.9k-document knowledge store.
- FlagEmbedding + GPU: ~5-15 minutes for the same corpus.

Source: docstring at `scripts/backfill_bgem3_v2.py:22-23`. The dominant
cost is the embedding forward pass, not the SQLite write. Disk space:
18.9k × 1024 × 4 = 77.6 MiB raw fp32 BLOB vs ~600 MiB for the legacy
JSON column (spec `docs/specs/2026-05-08-stage-2-dense-recall.md` §3) —
allow ~5 MiB per 1,000 documents.

### Pre-flight checklist

1. Stop the helix process. Background `replicate()` and `/admin/compact`
   both UPSERT the `genes` table; concurrent writes interleave partial
   rows that the idempotent skip-clause then has to retry.
2. Snapshot-copy `genomes/main/genome.db` to a side path. The script is
   non-destructive (adds a column, writes to NULL rows), but the snapshot
   guards an interrupted encode that leaves a half-written row.
3. Verify free disk space: ~5 MiB per 1,000 documents on top of the existing
   DB size, plus headroom for the WAL during the encode.

### Backfill command

The CLI is the script's `main()` argparse block at
`scripts/backfill_bgem3_v2.py:66-84`:

```bash
python scripts/backfill_bgem3_v2.py [DB_PATH]
       [--batch INT]
       [--limit INT]
       [--dim INT]
```

Flags:

- Positional `db_path` (optional). When omitted the script reads
  `[genome] path` from `helix.toml`
  (`scripts/backfill_bgem3_v2.py:87-88`).
- `--batch INT` — encode batch + SQLite commit cadence. Default `64`
  (`scripts/backfill_bgem3_v2.py:73`). Spec §3 describes "every 100
  rows"; the actual default is 64. Pass `--batch 100` for bit-for-bit
  spec alignment.
- `--limit INT` — cap rows processed (smoke tests only;
  `scripts/backfill_bgem3_v2.py:77-78`).
- `--dim INT` — override encode dim. Defaults to
  `cfg.retrieval.dense_embedding_dim` (1024 post-Stage-2,
  `scripts/backfill_bgem3_v2.py:81-83`). Set only for non-default
  Matryoshka 768/512 re-runs.

Typical invocation against a snapshot:

```bash
python scripts/backfill_bgem3_v2.py genomes/main/genome.db --batch 64
```

### What it does

The main loop (`scripts/backfill_bgem3_v2.py:126-156`):

1. `_ensure_v2_schema(conn)` (`scripts/backfill_bgem3_v2.py:43-55`) —
   adds the `embedding_dense_v2 BLOB` column when missing, then
   unconditionally `CREATE INDEX IF NOT EXISTS idx_genes_dense_v2_hot ON
   genes(gene_id) WHERE embedding_dense_v2 IS NOT NULL AND lifecycle tier <
   2`. The partial index covers hot-tier rows; heterochromatin
   (lifecycle tier=2) is reachable via `query_cold_tier()`.
2. Pre-flight coverage report: `genes total=N v2_populated_before=K`.
3. Selects rows where `embedding_dense_v2 IS NULL OR
   length(embedding_dense_v2) != dim*4` — the length check guards
   half-written rows from a crashed previous run.
4. For each row: `BGEM3Codec(dim=dim).encode(content[:2000],
   task="passage")` packed via `np.asarray(vec,
   dtype="<f4").tobytes(order="C")` into a `dim*4`-byte little-endian
   fp32 BLOB (`scripts/backfill_bgem3_v2.py:58-63, 136-145`), then
   UPDATEd into the row.
5. Commit + progress log every `--batch` rows. Empty content rows are
   skipped.
6. Post-flight: re-counts and prints `coverage=NN.NN% elapsed=S.s`
   (`scripts/backfill_bgem3_v2.py:161-172`).

### Progress signals

Stdout prints (in order):

- `[backfill] DB: ...`, `[backfill] dim=1024 expected_bytes_per_row=4096`
- `[backfill] rows to process: K`
- Every `--batch` rows: `[backfill] i/K processed=P skipped=S rate=R
  genes/s`
- Final: `[backfill] DONE. processed=P skipped=S
  v2_populated_after=N coverage=NN.NN% elapsed=S.s`

Sub-100% coverage on a complete run usually means empty `content`
(skipped at line 131-133) or a dim-mismatch `ValueError` (line 139-142).
Both print to stdout — grep the run log for `WARN:`.

### Backfill verification

After the script reports DONE, run the verification SQL against the
backfilled database:

```bash
sqlite3 genomes/main/genome.db "
  SELECT
    (SELECT COUNT(*) FROM genes) AS total,
    (SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL) AS v2_done,
    1.0 * (SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL)
        / (SELECT COUNT(*) FROM genes) AS coverage;
"
```

Expect `coverage = 1.0` (or very close — a handful of empty-content rows
are tolerated). If `coverage < 0.95`, the dense recall path's coverage
gate (Stage 2 spec §4) refuses to run and `query_genes_dense_recall`
returns `[]` with a one-time warn; lexical-only retrieval continues to
work but `bench_needle_1000` recall numbers will not improve.

### Backfill rollback

The backfill is non-destructive. To revert:

1. Set `[retrieval] dense_embedding_enabled = false` in `helix.toml` —
   this skips the dense recall path; lexical-only retrieval resumes.
2. Restart helix or POST `/admin/refresh`.

The legacy `embedding_dense TEXT` (256-dim JSON) column stays intact for
one release after Stage 2, so retrieval falls back to pre-Stage-2
behavior. The `embedding_dense_v2 BLOB` column stays in the schema and
is harmless when unused. To re-encode at a different Matryoshka dim
(768/512), drop the column and re-run with `--dim` — the length-equality
skip-clause selects all rows for re-encode automatically when the dim
changes.

### Resumption after interruption

The WHERE clause `embedding_dense_v2 IS NULL OR
length(embedding_dense_v2) != ?` is the resumption mechanism. After a
Ctrl-C, OOM, or reboot, restart with the same arguments and it picks up
at the first un-encoded row. The pre-flight `v2_populated_before` print
tells you how far the previous run got; if it is unexpectedly low after
a long run, run `PRAGMA integrity_check` before assuming the script is
the problem.

---

## Runbook 2: Threshold calibration (Stage 4)

`scripts/calibrate_thresholds.py` derives two artifacts from a backfilled
knowledge store plus a `located_n1000` bench JSON:

1. The margin-over-random ANN cosine cutoff,
   `threshold = mu + sigma_mult * sigma`, computed from N=10,000 random
   document-pair cosines drawn from `embedding_dense_v2`.
2. Per-classifier `abstain_top` / `focused_top` / `tight_top` floors,
   derived from the bench's `agent.score_top` distributions split by
   query class (`factual`, `multi_hop`, `arithmetic`, `procedural`,
   `default`).

Both replace hand-picked constants. The output is a TOML snippet that the
operator pastes into `helix.toml`, plus a JSON provenance report.

### When to calibrate thresholds

After Runbook 1 is complete (the calibration reads
`embedding_dense_v2` BLOBs directly), AND after a fresh
`scripts/bench_needle_1000.py --axis located` run produces a current
`located_n1000.json`. The bench must reflect the knowledge store you are
calibrating; calibrating against a stale bench is the most common failure
mode.

### Threshold-calibration pre-flight

1. Confirm `located_n1000.json` exists at the path you intend to pass to
   `--bench`. The script's existence check at
   `scripts/calibrate_thresholds.py:551-553` exits 2 if not.
2. Spot-check that the bench has at least 30 rows per classifier class.
   The threshold is `MIN_ROWS_PER_CLASS = 30`
   (`scripts/calibrate_thresholds.py:57`); below this, the per-class
   floors are flagged degenerate and copied from the `default` floors.
   The TOML snippet emits an inline `# WARNING:` comment for any
   degenerate class
   (`scripts/calibrate_thresholds.py:385-389`).
3. The knowledge store must have at least 2 dense-encoded documents
   (`scripts/calibrate_thresholds.py:136-140`). On a freshly-backfilled
   18.9k-document knowledge store this is trivially true.

### Threshold-calibration wall time

Minutes for an 18.9k-document knowledge store. Random-pair sampling at N=10,000 is
~50 ms; `_load_dense_vectors` is bounded by SQLite read throughput at a
few hundred ms; per-classifier floor compute is `classify_query` once
per bench row (~1000 × ~ms). Total runtime is dominated by imports,
not work — the script is essentially instant.

### Threshold-calibration command

The CLI is the script's `_build_parser` at
`scripts/calibrate_thresholds.py:453-494`:

```bash
python scripts/calibrate_thresholds.py
       --genome PATH           (required)
       --bench PATH            (required)
       [--output-toml PATH]
       [--output-report PATH]
       [--sigma-mult FLOAT]
       [--random-pairs INT]
       [--seed INT]
       [--abstain-pct FLOAT]
       [--focused-pct FLOAT]
       [--tight-pct FLOAT]
       [--dim INT]
       [--write-db | --no-write-db]
       [--dry-run]
       [-v | --verbose]
```

Flags (verified at lines 458-493):

- `--genome PATH` (required) — sqlite path.
- `--bench PATH` (required) — `located_n1000.json` (a JSON list, not
  JSONL).
- `--output-toml PATH` — default `None` (prints to stdout).
- `--output-report PATH` — default `Path("calibration_report.json")`.
- `--sigma-mult FLOAT` — default `3.0` (cosine cutoff is
  `mu + sigma_mult * sigma`).
- `--random-pairs INT` — default `10000`.
- `--seed INT` — default `42` (seeds `random.Random`, not numpy).
- `--abstain-pct FLOAT` — default `85.0` — p85 of MISS scores per class.
- `--focused-pct FLOAT` — default `25.0` — p25 of HIT scores per class.
- `--tight-pct FLOAT` — default `60.0` — p60 of HIT scores per class.
- `--dim INT` — default `1024` (BGE-M3 full).
- `--write-db / --no-write-db` (default `--write-db`) — UPSERT
  `ann_threshold` into `genome_calibration`. The script CREATEs the
  table if missing (`scripts/calibrate_thresholds.py:509-515`).
- `--dry-run` — TOML + report only, no DB write.
- `-v` / `--verbose` — DEBUG log level.

Typical invocation:

```bash
python scripts/calibrate_thresholds.py \
    --genome genomes/main/genome.db \
    --bench results/located_n1000.json \
    --output-toml docs/calibrations/2026-05-10.toml \
    --output-report docs/calibrations/2026-05-10.json
```

The TOML snippet also writes to stdout when `--output-toml` is omitted —
useful for piping or CI.

### Outputs

Three outputs:

1. **TOML snippet** at `--output-toml` (or stdout). Layout per
   `scripts/calibrate_thresholds.py:357-394`: a `[retrieval]` block with
   `ann_threshold_mode = "margin_over_random"` and
   `ann_threshold_sigma_multiplier`, an `[abstain]` block with
   `mode = "per_classifier"`, and per-class blocks
   `[abstain.factual]`, `[abstain.multi_hop]`, `[abstain.arithmetic]`,
   `[abstain.procedural]`, `[abstain.default]` carrying `abstain_top`,
   `focused_top`, `tight_top`. Degenerate classes (n_total < 30) ship
   with default floors (2.5, 2.5, 5.0) and a `# WARNING:` comment.

2. **JSON provenance report** at `--output-report` — schema URI
   `https://helix-context.dev/schemas/calibration_report.v1.json`. Layout
   per `scripts/calibrate_thresholds.py:397-442`: `computed_at`,
   `genome.{path,gene_count,dim}`, `ann_threshold.{mode,value,mu,sigma,
   sigma_mult,n_pairs,seed}`, `floors.{abstain_pct,focused_pct,tight_pct,
   bench_path,n_bench_rows,n_skipped,per_class}`. Commit these under
   `docs/calibrations/<date>.json` for auditability.

3. **`genome_calibration` row** UPSERTed into the database (when
   `--write-db` and not `--dry-run`,
   `scripts/calibrate_thresholds.py:603-609`). Key `ann_threshold`,
   `value_json` carries `{value, mu, sigma, N, dim, sigma_mult, seed}`.
   `genome.query_genes()` reads this row when `ann_threshold_mode =
   "margin_over_random"`.

### Apply the output

1. In `helix.toml`, add `ann_threshold_mode = "margin_over_random"` and
   `ann_threshold_sigma_multiplier = 3.0` to `[retrieval]` (leave the
   legacy `ann_similarity_threshold` in place as fallback).
2. Set `[abstain] mode = "per_classifier"`.
3. Append every `[abstain.<cls>]` block from the snippet. The loader
   raises `ConfigError` at startup if a per-class block is missing for
   any emitted class (Stage 4 spec §6).
4. Restart helix or POST `/admin/refresh`. The knowledge store reads
   `ann_threshold` from `genome_calibration` on the first
   `query_genes()` after refresh; the cache invalidates on
   `set_replication_manager` rotation per Stage 4 spec §3.

### What it computes

Algorithm per Stage 4 spec §3-§5:

- `_load_dense_vectors` reads every `embedding_dense_v2` BLOB whose
  length equals `dim*4` bytes — skips legacy or partial-coverage rows
  (`scripts/calibrate_thresholds.py:75-114`).
- `calibrate_ann_threshold` L2-normalises defensively, draws `n_pairs`
  unique unordered random pairs, computes pairwise cosines, returns
  `mu + sigma_mult * sigma` with `sigma` as sample std (ddof=1)
  (`scripts/calibrate_thresholds.py:117-189`).
- `calibrate_floors` re-runs `classify_query` on each bench row's
  `query`, splits into hits (`agent.gene_id_top == row.gene_id`) vs
  misses, and computes per class: `abstain_top=p85_miss`,
  `focused_top=p25_hit`, `tight_top=p60_hit`
  (`scripts/calibrate_thresholds.py:244-346`).
- Classes with `n_total < MIN_ROWS_PER_CLASS=30` flag degenerate; the
  `default_floors` fallback at `scripts/calibrate_thresholds.py:574-577`
  carries `abstain_top=2.5, focused_top=2.5, tight_top=5.0` —
  preserving the pre-Stage-4 hand-picked floors for under-sampled
  classes.

### Threshold-calibration verification

After applying the snippet to `helix.toml` and refreshing:

```bash
curl -s http://127.0.0.1:11437/health | jq .calibration
```

Expected fields (server.py:2752-2768):

- `ann_threshold_mode`: `"margin_over_random"`
- `abstain_mode`: `"per_classifier"`
- `abstain_classes`: sorted list of every per-class block found in
  `helix.toml`. Should contain the five known classes.
- `ann_threshold`: nested dict from `Genome.get_calibration_provenance()`
  carrying `value`, `mu`, `sigma`, `N`, `dim`, `sigma_mult`, `seed`,
  `computed_at`. Omitted when mode is `"absolute"` or no calibration row
  exists (server.py:2767-2768).

If the `ann_threshold` sub-key is absent under `calibration`, the
genome_calibration row was not written or could not be read. Re-run the
calibration without `--no-write-db` and without `--dry-run`, then
`/admin/refresh`.

### Threshold-calibration rollback

To revert to pre-Stage-4 behavior:

1. In `helix.toml`, set `[abstain] mode = "global"` and `[retrieval]
   ann_threshold_mode = "absolute"` — the legacy `ann_similarity_threshold`
   value is read.
2. Restart helix or POST `/admin/refresh`.

The legacy floors `TIGHT_SCORE_FLOOR = 5.0`, `FOCUSED_SCORE_FLOOR = 2.5`,
`FOCUSED_SCORE_FLOOR_FOR_ABSTAIN = 2.5` resume. The `genome_calibration`
table row stays intact and unused; flipping `ann_threshold_mode` back to
`"margin_over_random"` re-engages it without re-running calibration.

### Cadence

Re-run when:

- Bench composition changes materially (new query classes, different
  needle taxonomy, corpus grew/shrank > 20%).
- Retrieval signal weights are retuned (Stage 3 RRF) — the score scale
  shifts and floors no longer match.
- `/health` reports `calibration_age_days > 30` (loader emits a startup
  WARNING in this case, Stage 4 spec §9).

Tag and commit `docs/calibrations/<date>.{toml,json}` per run for
auditability.

---

## Runbook 3: Know-confidence calibration (Stage 6 + Stage 7)

`scripts/calibrate_know_confidence.py` fits the logistic that drives
`KnowBlock.confidence`. Stage 6 was a 4-feature logistic; Stage 7
extended it to 5 features by adding `freshness_min` (β5). The script
reads the same `located_n1000` ground truth as Runbook 2, but in JSONL
form (one row per query) — not JSON like Runbook 2 wants.

### When to calibrate know-confidence

After Runbook 2. Optional but recommended; the default coefficients
(`DEFAULT_BETAS = (-2.0, 2.0, 1.5, 0.7, 1.8, 1.5)` in
`helix_context/know_calibration.py:57`) are a reasonable cold-start, and
the absent `[know]` block falls back to defaults with a `log.warning`.
Calibration mainly improves precision at the boundary — it shifts the
emit_floor toward the data's actual P95 operating point and re-fits the
β intercept against a real positive/negative class balance.

If you do not have a `located_n1000.jsonl` file (Stage 1 bench output),
skip this runbook entirely and rely on the defaults.

### Know-calibration wall time

Seconds, not minutes:

- With `scikit-learn` installed: a single `LogisticRegression(penalty="l2",
  C=1.0, max_iter=1000, solver="lbfgs")` fit on ~800 train rows is
  sub-second (script branch at `scripts/calibrate_know_confidence.py:351-363`).
- Without scikit-learn: pure-Python gradient descent at lr=0.1, 500 epochs,
  l2=1e-4 (`scripts/calibrate_know_confidence.py:364-372`). Single-digit
  seconds for the 4 or 5 features × ~800 rows. The pure-Python path is
  the default fallback when `import sklearn` fails.

Optional: `pip install scikit-learn` for ~1% AUC bump on small calibration
sets and faster convergence on larger ones.

### Know-calibration command

The CLI is at `scripts/calibrate_know_confidence.py:251-295`:

```bash
python scripts/calibrate_know_confidence.py
       --input PATH             (required)
       [--out PATH]
       [--target-precision FLOAT]
       [--seed INT]
       [--smoke]
       [--n-features INT]
```

Flags (verified at lines 252-294):

- `--input PATH` (required) — `located_n1000.jsonl`. JSONL (one object
  per line), not the JSON list Runbook 2 takes. Each row needs:
  `top_score`, `score_gap`, `lexical_dense_agree`,
  `coordinate_confidence`, `label`, and optionally `freshness_min`
  (5th feature). Schema at `scripts/calibrate_know_confidence.py:85-115`.
- `--out PATH` — default `Path("helix.toml")`. The writer at
  `scripts/calibrate_know_confidence.py:187-248` reads the existing
  file, replaces the `[know]` section (or appends if absent). Hand-rolled
  because `tomllib` is parse-only.
- `--target-precision FLOAT` — default `0.95`. Operating-point precision
  for picking `emit_floor` from the held-out test set
  (`scripts/calibrate_know_confidence.py:268-270`).
- `--seed INT` — default `42`. Seeds the train/test split.
- `--smoke` — synthetic separable fixture instead of `--input`. Does NOT
  touch `helix.toml`.
- `--n-features INT` — default `N_FEATURES = 5` (Stage 7,
  `helix_context/know_calibration.py:69`). Stage 6 era was 4; the
  pure-Python fitter still works against a Stage 6 era JSONL because
  `_row_to_features` only emits 4 features when `freshness_min` is
  absent.

Typical invocation:

```bash
python scripts/calibrate_know_confidence.py \
    --input results/located_n1000.jsonl \
    --out helix.toml
```

### What it fits

Per Stage 6 spec §11 and Stage 7 spec §10:

- Reads JSONL rows (`scripts/calibrate_know_confidence.py:68-82`); picks
  `s_ref = median(top_score)`, `g_ref = median(score_gap)` so `tanh(...)`
  saturates at the typical retriever scale
  (`scripts/calibrate_know_confidence.py:332-337`).
- Feature vector: `[tanh(top/s_ref), tanh(gap/g_ref), agree,
  clip(coord, 0, 1)]`, plus `freshness_min` as feature 5 when present
  (`scripts/calibrate_know_confidence.py:109-115`).
- 80/20 train/test split with the seed
  (`scripts/calibrate_know_confidence.py:129-150`).
- Fits sklearn `LogisticRegression(penalty="l2", C=1.0,
  max_iter=1000, solver="lbfgs")` if available, else
  `fit_betas_from_features(lr=0.1, epochs=500, l2=1e-4)`.
- Sweeps held-out test probabilities; picks the lowest threshold where
  precision ≥ `--target-precision` as `emit_floor`. Falls back to
  `DEFAULT_EMIT_FLOOR = 0.55` on small/imbalanced sets
  (`scripts/calibrate_know_confidence.py:166-184`).

### Output

The script writes (or replaces) the `[know]` block in `helix.toml`
(`scripts/calibrate_know_confidence.py:198-214`):

```toml
[know]
emit_floor      = 0.55
s_ref           = 1.0
g_ref           = 0.5
betas           = [-2.0, 2.0, 1.5, 0.7, 1.8, 1.5]
calibrated_at   = "2026-05-10T..."
calibrated_on_n = 1000
```

`betas` is the intercept followed by one coefficient per feature
(length = 1 + N_FEATURES = 6 for Stage 7). β0 is the intercept; β1-β5
multiply the five features in order: top_score-tanh, score_gap-tanh,
lexical_dense_agree, coordinate_confidence, freshness_min. The default
β5 is +1.5 — Stage 7 spec §10. When the JSONL does not carry
freshness_min, β5 falls back to its default and the loader continues to
work (Stage 7 spec §10 falls back to `decay_score` for legacy rows
without `last_verified_at`).

### Know-calibration apply + verify

`helix.toml` is hot-reloaded per `/context` call (the
`know_calibration` loader is pure functions, no per-call I/O beyond the
file read). No restart required. If `--out` writes somewhere your server
does not load (`HELIX_CONFIG` override), copy the `[know]` block over
manually and POST `/admin/refresh`.

```bash
curl -s http://127.0.0.1:11437/health | jq .calibration.know
```

Expect `calibrated_at` recent and `calibrated_on_n` matching your input
row count. If the file write was malformed, the loader warns and falls
back to defaults — `/health` then reports defaults rather than your fit.

Sanity-check the betas by issuing `/context` against a known-hit and a
known-miss and confirming `agent.confidence` separates them cleanly. If
both return `~0.5`, the fit collapsed — re-check the JSONL for missing
labels or all-zero features.

### Skip rationale

Without `located_n1000` ground truth (early bring-up, novel knowledge store, no
Stage 1 bench), the default `(-2.0, 2.0, 1.5, 0.7, 1.8, 1.5)` is a
reasonable cold-start. The loader at
`helix_context/know_calibration.py:160` falls back to defaults on a
missing or malformed `[know]` table with a `log.warning`. Calibration
mainly improves precision at the boundary.

Re-run this calibration after a bench refresh, after Stage 3 RRF signal
weights are retuned (the score scale shifts and `s_ref`/`g_ref` need
re-fitting), or after planted-stale needles are added to the bench
(Stage 7 §10: β5 re-fits to drive stale-needle confidence below
`emit_floor`).

### Optional: smoke test

```bash
python scripts/calibrate_know_confidence.py --smoke
```

Runs an 80-row synthetic separable fixture through the full fit. Does
NOT touch `helix.toml`. Confirms the script wires together.

---

## Runbook 4: `/admin/refresh` (admin-only)

The `/admin/refresh` endpoint at `helix_context/server.py:2805-2810`
forces the knowledge store connection to reopen its WAL snapshot. Effective on
external writers — useful when ingest happened via a sibling replica or
direct sqlite3 write.

### When to refresh

- After manual edits to `helix.toml` that need to take effect without a
  full process restart. The `helix.toml` loader is a pure function
  invoked per relevant code path, so most config flips already hot-reload;
  use `/admin/refresh` for the conservative case where you want the
  knowledge store connection itself reset.
- After a `genome_calibration` UPSERT from `calibrate_thresholds.py`
  outside the server process, if you want the cached `ann_threshold` to
  re-read on the next `query_genes()` call.
- After a manual `sqlite3 genome.db "..."` write that the server's
  long-lived WAL snapshot otherwise wouldn't see until commit.

### Refresh command

```bash
curl -X POST http://127.0.0.1:11437/admin/refresh
```

The endpoint handler at `helix_context/server.py:2806-2810` calls
`helix.genome.refresh()` and then `helix.genome.stats()["total_genes"]`
to confirm the connection is healthy. The response is
`{"refreshed": true, "genes": <count>}`.

### What it triggers

`helix.genome.refresh()` (`helix_context/genome.py:4089-4112`):

- Primary path: `_refresh_snapshot()` commits the read transaction so
  the next SELECT starts a new WAL snapshot, then `SELECT 1` verifies
  the connection is alive.
- Fallback path: if the verify raises, closes and reopens the
  connection with standard PRAGMAs (`journal_mode=WAL`,
  `busy_timeout=30000`, `journal_size_limit` 64 MB).

Spec divergence: Stage 2 spec §4 wording described `/admin/refresh` as
also calling `_invalidate_dense_matrix(force=True)`. The actual handler
does not — the in-memory dense matrix
(`helix_context/genome.py:3062-3074`) is invalidated only by the
upsert/delete paths. After a manual UPDATE of `embedding_dense_v2`
outside the server, restart the helix process to force a lazy-load
rebuild.

---

## Runbook 5: `/admin/vacuum` (admin-only)

The `/admin/vacuum` endpoint at `helix_context/server.py:2812-2828`
calls `Genome.vacuum()` to reclaim free pages from the SQLite knowledge store
file after thinning, compaction, or large-scale deletions.

### When to vacuum

- After large bulk ingests that thinned a lot of duplicate documents — the
  pages stay marked free until VACUUM releases them. Common after a
  `scripts/compact_genome_sweep.py` run, or after a `/admin/compact`
  POST with a non-trivial demotion count.
- When the WAL grows excessively (more than ~64 MB sustained — that's
  the `journal_size_limit`).
- Quarterly on a low-traffic window, even if no thinning happened —
  SQLite page fragmentation accumulates over long-lived databases.
- When `genome.db` size is more than 1.5x the logical content size you
  would expect from the document count.

### Vacuum command

```bash
curl -X POST http://127.0.0.1:11437/admin/vacuum
```

Returns `{"ok": true, "before_bytes": ..., "after_bytes": ...,
"reclaimed_bytes": ...}` from `Genome.vacuum()` at
`helix_context/genome.py:4182-4236`.

### Vacuum wall time

Depends on DB size. SQLite `VACUUM` rewrites the entire file:

- Small knowledge store (< 100 MiB): seconds.
- Medium knowledge store (100 MiB - 1 GiB): tens of seconds to a few minutes.
- Large knowledge store (> 1 GiB): minutes.

Disk usage temporarily doubles during the rewrite. Run during a
maintenance window — the operation blocks all writers for its duration.

### Vacuum effect

`Genome.vacuum()` (`helix_context/genome.py:4202-4232`), in order:

1. `PRAGMA wal_checkpoint(TRUNCATE)` flushes the WAL into the main DB.
2. Closes the long-lived connection (VACUUM needs exclusive access).
3. Opens a fresh autocommit connection and runs `VACUUM`.
4. Closes the VACUUM connection; reopens the long-lived connection with
   standard PRAGMAs.

VACUUM rewrites all indexes, so the `idx_genes_dense_v2_hot` partial
index shrinks to match the compacted table footprint. Subsequent queries
hit the rebuilt index with no warm-up cost.

On failure the handler returns `{"ok": false, "error": "..."}` with 500
(server.py:2823-2828); the long-lived connection is already reopened by
the time the exception surfaces, so subsequent reads still work.

---

## Runbook 6: `/consolidate` (rewrite stale document bodies)

The `/consolidate` endpoint at `helix_context/server.py:2672-2690`
distills the session buffer into consolidated knowledge documents,
extracting only new facts, decisions, and discoveries.

### When to consolidate

- A document's source file mtime moved past its `last_verified_at` (Stage 7
  freshness check) AND the document body is structurally outdated (the file
  changed shape, not just a tweak).
- After a session-mode interaction has built up `pending_replication`
  buffer and you want to commit those distilled exchanges as long-lived
  documents rather than letting them age out.
- As the bulk recovery option after a partial restore from WAL — see
  Runbook 9.

Stage 7's `MissBlock(reason="stale")` carries `refresh_targets: list[str]`
that typically point at document source paths the agent should refetch.
`/consolidate` is the server-side counterpart for the case where the
refresh target IS a document the helix process owns and can rewrite from
its source.

### Consolidate command

```bash
curl -X POST http://127.0.0.1:11437/consolidate \
     -H "Content-Type: application/json"
```

The endpoint takes no required body fields — it operates on the active
session buffer. The handler invokes
`helix.consolidate_session_async()` and returns
`{"facts_extracted": <int>, "gene_ids": [...]}`.

(Note: the endpoint signature in the worktree's `server.py:2673` does
not parse a request body; the spec phrasing
`{"gene_id": "..."}` does not match the implementation. To consolidate a
specific document-by-id, use `/admin/compact` with appropriate filters or
work directly against the knowledge store — `/consolidate` operates on the
session buffer aggregate, not a single document.)

### Consolidate effect

- Distills the session buffer (recent in-memory exchanges) into
  long-lived consolidated documents via the compressor's
  `pack`/`re_rank`/`splice` paths.
- Updates `last_verified_at` on rewritten documents.
- Extends the knowledge store's coverage of the session's distilled knowledge,
  which means the next `/context` query against a related topic gets a
  better hit.

If consolidation fails, the handler returns
`{"error": "...", "facts_extracted": 0, "gene_ids": []}` with a 500
status (server.py:2685-2690). The session buffer is unaffected by
failure; retry is safe.

---

## Runbook 7: `/context/refresh-plan` vs `/context`

`/context/refresh-plan` at `helix_context/server.py:1546-1593` is a thin
convenience over `/context/packet`: it returns only the `refresh_targets`
list — the set of source paths or URLs the agent should refetch — without
the full evidence items (no `KnowBlock`, no `expressed_context`, no
documents). Use it when the caller already has the knowledge store content cached
(typical for an agent that just made a `/context` call seconds ago and
wants to know whether to re-read its sources before acting), and only
needs the cheap reread plan. The handler short-circuits past the
compressor and assembly paths and runs only the refresh-target extractor —
much cheaper than `/context` itself, no LLM calls. For everything else,
use `/context` (the full contract) or `/context/packet` (the full
contract plus the agent-safe verified/stale_risk/refresh_targets
breakout). See Stage 7 spec §11 for the round-trip mapping between
`MissBlock(reason=...).refresh_targets` and the `/context/refresh-plan`
`RefreshTarget` payload.

---

## Runbook 8: Disaster recovery — corrupt `genome.db`

### Symptoms

- `database disk image is malformed` from sqlite3 or any helix endpoint
  that reads the knowledge store.
- `/health` returns `status: "degraded"` with `genome_ready: false` and
  the message `Genome stats failed; inspect the local knowledge store.`
- `Genome.vacuum()` raises in pre-checkpoint or in the VACUUM connection.
- SQLite `PRAGMA integrity_check` returns anything other than `ok`.

### Recovery: backup-from-WAL → reingest-from-source-paths

1. Stop helix. Side-copy `genome.db`, `genome.db-wal`, `genome.db-shm`.
2. Try `sqlite3 genome.db ".recover" | sqlite3 recovered.db` — pulls
   every readable page out of the corrupt DB into a fresh file. If it
   succeeds, swap `recovered.db` in for `genome.db` and skip to step 5.
3. If `.recover` fails, drop back to the most recent WAL-checkpointed
   replica snapshot under `[genome] replicas`.
4. Re-ingest from source paths: each document carries a `source_id`
   pointing back at the file or URL it was extracted from. Bulk path:
   `python scripts/ingest_all.py <source_root>` against the recovered
   knowledge store. Single-source: POST `/consolidate` (Runbook 6), which uses
   each document's `source_id` fingerprint to rewrite the body.
5. Re-run all three calibration runbooks — the `genome_calibration`
   table is gone if you started from a fresh DB.
6. Bring helix up. Verify `/health` reports `status: "ok"` and the document
   count matches the pre-corruption snapshot.

`/consolidate` is also the bulk-recovery option for per-document corruption
(rather than file-level): for any document returning malformed content,
POST `/consolidate` to have the compressor rewrite the body from source.

---

## Runbook 9: Quarterly hygiene checklist

Run this checklist on a low-traffic window every quarter, or after
significant changes to the knowledge store corpus.

1. **Re-run the bench** to detect retrieval drift:

   ```bash
   python scripts/bench_needle_1000.py --axis located \
       --output results/located_n1000.json
   ```

   Compare retrieval@1 against the baseline you committed in
   `docs/calibrations/<previous>.json`.

2. **Re-run the three calibration scripts** if bench retrieval rate
   dropped more than 5 percentage points from the baseline:

   ```bash
   python scripts/backfill_bgem3_v2.py genomes/main/genome.db
   python scripts/calibrate_thresholds.py \
       --genome genomes/main/genome.db \
       --bench results/located_n1000.json \
       --output-toml docs/calibrations/$(date +%Y-%m-%d).toml \
       --output-report docs/calibrations/$(date +%Y-%m-%d).json
   python scripts/calibrate_know_confidence.py \
       --input results/located_n1000.jsonl \
       --out helix.toml
   ```

   Apply the new TOML snippet to `helix.toml`; POST `/admin/refresh`.

3. **Run `/admin/vacuum`** if `genome.db` size is more than 1.5x the
   logical content size (document count × average content length × ~2 for
   indexes and metadata):

   ```bash
   curl -X POST http://127.0.0.1:11437/admin/vacuum
   ```

4. **Verify calibration freshness** in `/context` responses:

   ```bash
   curl -s -X POST http://127.0.0.1:11437/context \
        -H "Content-Type: application/json" \
        -d '{"query": "calibration health probe"}' \
        | jq '.agent.calibration_age_days'
   ```

   Expect `calibration_age_days < 30`. If above, run Runbook 2 + Runbook
   3. The loader emits a startup WARNING when this exceeds 30 days
   (Stage 4 spec §9).

5. **Re-snapshot the knowledge store** to a versioned backup directory before the
   next quarter's work begins.

6. **Audit `docs/calibrations/`**: confirm every `.toml` has a
   side-by-side `.json` provenance report, and that the `computed_at`
   timestamps match the calibration run logs.

7. **Spot-check `/health`** — confirm `calibration.ann_threshold` is
   present and `calibration.abstain_classes` has all five known
   classifier classes:

   ```bash
   curl -s http://127.0.0.1:11437/health | jq '.calibration.abstain_classes'
   ```

   Expected: `["arithmetic", "default", "factual", "multi_hop",
   "procedural"]`.
