# Prepared migration gates: #417, #430 and #431

Status: **prepared, not run**. The implementation remains opt-in:
`budget.wire_format="legacy"`, `budget.tier_seat_floor_enabled=false`, and
`retrieval.harmonic_batching_enabled=false`. No corpus comparison, benchmark
snapshot, default graduation, or performance claim accompanies these tools.

`benchmarks/dogfood/migrations/prepare.py` writes full copies of the supplied
shipped TOML with all three migration flags explicit. It reads configuration,
needle and gold files, resolves the requested code ref, and emits `plan.json`
with executable argument arrays. It never opens a benchmark bed or Headroom
database. `capture.py` is the separate command that copies data and executes
queries. `gates.py` compares captured receipts and exits nonzero on a failed
gate. These tools reuse `erb/ablation_ladder.py` for actual retrieval, warmup,
gold grading, stage capture and error accounting.

## Arm definitions and acceptance

| Arm | Wire format | Tier floor | Harmonic batching | Harmonic bind threshold |
|---|---|---|---|---|
| `baseline` | legacy | false | false | real SQLite limit |
| `wire` | canonical | false | false | real SQLite limit |
| `tier` | legacy | true | false | real SQLite limit |
| `harmonic_on` | legacy | false | true | real SQLite limit |
| `harmonic_staged` | legacy | false | true | benchmark-only threshold 1 |

For **#417**, compare `baseline` with `wire`. Require identical complete score
maps, final retrieval order, delivered IDs/order, delivered gold, confidence
tier, and normalized context/decoder hashes. Raw context and decoder hash
changes are reported separately. Normalization recognizes owned document
headers, wrapper boundaries, attribute escaping and exact built-in decoder
templates, including populated answer slates. It adjusts the header character
count for wrapper length and preserves document and slate bytes. It never
globally replaces words in user content. Ambiguous headers/attributes,
truncated legacy wrappers, header-free nonempty windows, and security escaping
that changes body bytes cannot silently pass. Such rows need explicit review;
the gate does not erase these differences. Capture exercises assembly; API,
CLI and MCP schema projection remains covered by the wire unit/integration
tests rather than HTTP requests from this runner.

For **#430**, compare `baseline` with `tier`. Require identical complete score
maps, final retrieval order, score-map gold ranks, final gold ranks and tier
labels, with **zero paired delivered-gold losses**. Report every gained/lost
needle. More delivery seats and different assembled bytes are expected. Use
the full 470-needle v09x bank for the deciding 947,531-document ERB cell; a
smaller probe is its own campaign and cannot stand in for that cell. Preserve
the shipped `min_delivered_docs=12` in both arms. Score-gate exclusions,
available pool size, model caps and the hard token budget still apply.

For **#431**, compare `harmonic_on` with `harmonic_staged`. Both enable the
feature; the second patches only the harmonic limit probe to force TEMP
staging. Other SQLite statements keep their real limit. Require nonempty
`harmonic_links`, observed harmonic contributions and staged calls, no natural
overflow in the reference, and identical score maps, final order, delivered
IDs/gold, confidence tiers and raw context/decoder hashes. An empty or inert
fixture fails the gate. This compares staged SQL with the ordinary SQL
reference. The separate `baseline`/`harmonic_on` receipts describe rollout
behavior: above the real cap, baseline intentionally skips Tier 5, so that
pair can change rankings and is not a byte-equivalence comparison.

All three comparators reject a wrong/duplicate control arm, flag mismatch,
missing delivery fields, errors, reordered/partial/duplicate needle banks,
or mismatched code, configuration, data, runtime or Headroom pins. They check
the declared bank count and ordered-name hash against actual records.

## Reproducible execution after code is frozen

Use a clean checkout at a frozen tag or immutable commit that includes these
tools. Capture refuses a different or dirty checkout. Put output outside that
checkout. Preparation may be inspected before committing, but such a plan
must be regenerated against the final code/configuration pin before capture.

The example uses the existing v09x ERB bed and full bank. Supply the actual
Headroom TOIN, CCR and config input paths used for the campaign; the runner
copies those inputs separately for every arm and leaves Headroom enabled.
Modern beds whose build provenance lacks the tagger version also require
`--fixture-manifest <path> --fixture-target <target-key>` from the fixture
builder's manifest. Pre-2026-08-30 beds are v1 under the ledger's explicit
historical rule. Unknown provenance fails visibly.

```powershell
$python = 'F:/Projects/cymatix-context/.venv/Scripts/python.exe'
$codeRef = Read-Host 'Frozen tag or full commit containing the migration tools'
$toin = Read-Host 'Source Headroom TOIN JSON path'
$ccr = Read-Host 'Source Headroom CCR SQLite path'
$headroomConfig = Read-Host 'Source Headroom config directory'
$campaign = 'F:/tmp/migration-gates/erb947k-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
$env:PYTHONPATH = (Get-Location).Path
$env:PYTHONHASHSEED = '0'
$env:PYTHONUTF8 = '1'
& $python -m benchmarks.dogfood.migrations.prepare `
  --code-ref $codeRef `
  --source-bed 'F:/tmp/erb_blob_v09x.db' `
  --bed-identity '11a05b915f8c6d5597edfa1fb57bfa2ba104ec74006a2b48ad04263de998bda6' `
  --ingest-c 6 --tagger-version 1 `
  --resolved 'benchmarks/dogfood/erb/needles_resolved_v09x_full.json' `
  --gold 'benchmarks/dogfood/erb/gold_by_needle_v09x.json' `
  --headroom-toin $toin --headroom-ccr $ccr --headroom-config $headroomConfig `
  --out-dir $campaign
```

Stop here for preparation only. Inspect the five full TOMLs and `plan.json`.
No query or snapshot has run. The current ERB bed has no harmonic links, so
run only its wire and tier comparisons. Prepare a separate campaign using a
populated harmonic-link bed and its own needle/gold/provenance inputs for
#431. When executing the approved ERB campaign later, run from the same
clean pinned checkout:

```powershell
$plan = Get-Content -LiteralPath (Join-Path $campaign 'plan.json') -Raw | ConvertFrom-Json
foreach ($command in $plan.commands) {
    # Harmonic validation requires a separate populated-link campaign.
    if ($command -contains 'harmonic_on' -or $command -contains 'harmonic_staged' -or $command -contains 'harmonic') { continue }
    $exe = $command[0]
    $arguments = $command[1..($command.Count - 1)]
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Migration command failed: $exe" }
}
```

For the separate harmonic campaign, execute its `baseline`, `harmonic_on`
and `harmonic_staged` capture commands, followed by its `harmonic` comparator.
Use a bank whose reference pools fit the natural bind limit for the exact SQL
equivalence check. Also run the `baseline`/`harmonic_on` rollout pair on a
large populated bed that exceeds that limit and review its per-needle
ranking/delivery changes; the forced-staging equivalence check alone does
not certify that large-bed behavior. The rollout pair intentionally has no
automatic byte-equivalence verdict.

Every capture arm creates a fresh SQLite backup from a URI `mode=ro` source,
including WAL state. Five 947k snapshots require roughly **125 GB** plus
Headroom inputs, SQLite working space and receipts. Existing snapshots and
receipts are never overwritten. Run arms sequentially on one idle host.
The source's logical `last_accessed` state must be at least one hour old;
the original source remains unchanged between arms, preserving the same
retrieval-time access window. Snapshot copying warms filesystem caches.

Capture records/verifies the build identity and ingest concurrency and
recomputes the current sorted-document-ID digest on the snapshot. This digest
identifies the ID set; it is **not a content/tag byte digest**. Cross-arm source
DB/WAL size and nanosecond mtime pins provide the additional source-state
comparison. SHM lock bookkeeping is outside that preservation claim. TOIN
and Headroom config bytes are hashed; CCR is copied through SQLite backup
and its source DB/WAL state is pinned. Configuration tree copying must use
disjoint source and output directories. Config runtime overrides are recorded,
with secret-shaped environment values hashed. Python/SQLite versions, the
common effective configuration and runtime environment must match.

The standard pins are `PYTHONHASHSEED=0`, `PYTHONUTF8=1`,
`CYMATIX_DISABLE_LEARN=1`, and `CYMATIX_SQLITE_CACHE_SIZE=-12582912`.
Encoder/config/store-path overrides, the Headroom disable flag and the
ABSTAIN disable flag are cleared. Shipped encoders remain off. All measured
queries use `read_only=True`, `ignore_delivered=True`, `max_genes=12`, and
one untimed warmup per arm. Manager initialization and diagnostic bookkeeping
may write to the disposable snapshot, which is why the original bed is never
opened through a manager.

The receipt keeps ladder wall times, which include capture overhead, and
`build_only_ms`, timed before hashing/normalization. Neither is a cold-cache
latency claim. A performance graduation for #431 still needs separately
reviewed repetitions with controlled cache state; the exact-equivalence
gate is ready without pretending those timings have been measured.

## Ledger and verification

Add a **planned/not-run** campaign row to `BASELINES.md` only when the final
code pin, actual bed/input pins and chosen scale cells are fixed. Record the
three treatment comparisons in that campaign and attach raw receipts plus
the verdicts after execution. Compare within that row. The September 8
merged-stack witness is a provenance/setup reference, not a substitute
comparison arm. Do not graduate any default from preparation or tiny tests.

The focused verification is:

```powershell
& $python -m pytest tests/test_migration_gates.py tests/test_budget_tier_seat_floor.py -q -rs
& $python -m ruff check --select E9,F63,F7,F82 benchmarks/dogfood/migrations tests/test_migration_gates.py
```

Tests exercise full arm preparation without a bed, malformed bank rejection,
paired verdict failures, genuine assembled wrappers and populated decoder
slates, a tiny real ladder run including warmup, and source-preserving SQLite
backup. They certify the tooling's tested paths, not ERB retrieval outcomes.
