# Perf campaign slice 2 — server-mode concurrency: harnesses, receipts, and three bugs

**Date:** 2026-08-02 · **Branch:** `perf/slice1-instrumentation` · **Bed:** dogfood
(`13f8bbe3d3a37912`, 6,427 genes, blob mode) · **Box:** Windows 11, Python 3.14.3

Campaign plan: `~/.claude/plans/latency-and-concurrency-architecture-agile-hickey.md`.
Slice 1 (Phase-0 instrumentation) landed as `942aff4` + `b040306`.

## TL;DR

1. **The dead-encode deletion (W0.4) is worth ~10% at c=1**: in-proc median
   807ms (July) → **729ms**, recall unchanged (0.6667), same bed.
2. **Cold start fully attributed**: request #1 ≈ 14.0s of which **12.8s is
   model/encoder load** (SPLADE+torch 6.4s, BGE-M3 weights 3.9s, MiniLM/SEMA
   2.4s, dense matrix 53ms); warm ≈ 1.0s. A warmup hook (W1.7) is worth ~13s.
3. **The server could not serve two concurrent `/context` requests at all.**
   Not a slowdown — a wedge, with the event loop dead too. Three distinct
   defects, each found by the new harnesses, fixed test-first:
   a. shared reader/writer connections across threads (livelock → on
      py3.14 sqlite3, outright deadlock) → **thread-local reader
      connections**; co-activation expansion + `stats()` moved to them;
   b. `stats()` full-table aggregates ran per-request on the hot path via
      `_compute_health` (also a Wave-1 latency item on its own);
   c. the unlocked-committer class (plan serialization point #1) observed
      live: concurrent `log_health` commits stole each other's implicit
      transaction ("cannot commit - no transaction is active") →
      `log_health` + route CWoLa under `_write_lock`; full W2.3-A sweep
      pulled forward (background loops included — they ran blocking SQLite
      on the event loop, on timers, which is the intermittent variant).
4. **In-proc scaling control (item 5)**: c=2 → 1.43× median / 1.41× qps;
   c=4 → **2.13× median / 1.93× qps**, recall flat, overlap ≥0.985.
   Four independent processes already lose ~½ their throughput to the box —
   the G1 comparator for the server-mode c-curve.

## Receipts

| Receipt | File |
|---|---|
| In-proc control c=1,2 | `benchmarks/dogfood/latency_scaling_slice2_c12.json` |
| In-proc control c=1,4 | `benchmarks/dogfood/latency_scaling_slice2_c14.json` |
| Cold-start smoke (1 iter, gate PASS, residual 118ms) | `benchmarks/dogfood/logs/cold_start_smoke.json` |
| Server-scaling smoke (post-fix, c=1,2 tiny) | `benchmarks/dogfood/logs/server_scaling_smoke.json` |

Full baselines (cold-start N=5, server scaling c=1..16 read arm, write
contention arms a–d) run after the W2.3-A sweep lands — TBD below.

## The concurrency wedge, in evidence order

1. Server-scaling smoke c=2: both workers' warmup POSTs timed out at 300s;
   canary (`GET /debug/pipeline/recent`, in-process, no DB) also timed out —
   event loop dead, server wedged after clients gave up.
2. faulthandler bisect (no HTTP, two threads on one manager): both threads
   inside `expand_by_entity_graph` — `active+gil` in py-spy, i.e. spinning,
   on scans interleaved over ONE shared connection (reader for tiers, writer
   for the expansion). Single thread: ~1s. Two: never returns.
3. Post-fix py-spy: threads progressed to `stats()` (same shared-writer scan
   collision, next layer down) — fixed the same way.
4. Post-fix probe: one worker completed at 857ms, the other died with
   `sqlite3.OperationalError: cannot commit - no transaction is active` in
   `log_health` — the 2026-07-18 unlocked-committer class, live at c=2.
   Under py3.14 the same race can also hard-deadlock inside the INSERT
   (both hammer threads blocked; reproduced by
   `test_concurrent_log_health_no_commit_steal`).
5. Post-lock smoke: levels complete, canary p95 17/42ms, but 2/6 requests
   timed out intermittently → timer-driven writers (background checkpoint /
   registry sweep, blocking SQLite ON the event loop thread, unlocked) —
   W2.3-A + loop-half-of-W2.5 pulled forward to close the class.

**Python 3.14 note.** Earlier interpreters serialized cross-thread use of a
sqlite3 connection (slow but forward-progressing). On 3.14.3 the same
pattern blocks outright — both threads stuck inside `execute`. The shared
connections were always a defect (the plan's serialization points #1/#2);
3.14 upgraded the failure mode from latent to fatal, which is why the first
ever server-mode concurrency measurement found it within minutes.

## Fixes landed this slice (all TDD, semantics-preserving)

| Fix | Where |
|---|---|
| Thread-local reader connections (registry-tracked, `:memory:` fallback) | `knowledge_store.read_conn` |
| Co-activation expansion → reader conn | `_expand_coactivated` |
| `stats()` → reader conn | `knowledge_store.stats` |
| `log_health` under `_write_lock` | `knowledge_store.log_health` |
| Route CWoLa writes under `_write_lock` (interim until W2.4) | `routes_context` |
| W2.3-A sweep: remaining write sites + `to_thread` background loops | (in flight) |
| BenchServer spawn-token identity (venv-shim pid false positive) | `bench_orchestrator`, `/health` |
| Model-load attribution: SPLADE/BGE-M3/SEMA/dense-matrix + torch imports | `backends/*`, `knowledge_store` |

## New W-items discovered (for the backlog)

- **W: `stats()` off the hot path** — `_compute_health` calls it per request
  (COUNT + two `SUM(LENGTH(...))` scans). Cache or slim it (Wave 1 class).
- **W: cwola read-only residue** — `/context` writes `cwola_log` even with
  `read_only=true`; the store's writer conn opens read-write regardless.
  Harness `--copy-bed` exists as the strict no-touch mode; gate cwola under
  read_only properly in W2.4.
- **W: recall@12 at c=2 (0.500 vs 0.667 on 4 samples)** — small-n at smoke
  scale; the full-n baseline must confirm recall holds under concurrency or
  a delivered-results race (W1.6 class) exists.
- **Windows venv shims break Popen-pid identity checks** — anywhere else
  pid equality is assumed, audit (fixed in BenchServer via spawn token).

## The fourth defect: reads on the writer connection

The first battery pass wedged intermittently (scaling c=4 warmups, arm a
of write contention) — py-spy on the live wedge caught an executor thread
blocked in `ray_trace._load_co_activated` (rerank stage): a request-path
READ on the shared **writer** connection. `_write_lock` serializes writers
against writers; an unlocked read on the same connection still interleaves
cross-thread with locked writes and deadlocks on py3.14. Rule enforced:
**request-path reads never touch the writer connection** — ray_trace (×2)
and the session-delivery lookups moved to the per-thread reader. (Two
polluting factors were also removed before the final pass: leaked pre-fix
probe processes had been spinning through the entire first battery.)

## Final baselines (clean box, all fixes)

**Server-mode read arm** (18 needles × 2 rounds, bed COPY; recall measured
within delivered citations — a different basis than in-proc raw-ranking
0.667, consistent across levels which is what gates compare):

| c | median | p95 | qps | r@12 | canary p95 | errors |
|---|---|---|---|---|---|---|
| 1 | 968ms | 1106ms | 1.04 | 0.556 | 30ms | 0 |
| 2 | 1384ms | 1628ms | 1.45 | 0.556 | 41ms | 0 |
| 4 | 2655ms | 3106ms | 1.47 | 0.556 | 52ms | 0 |
| 8 | 5302ms | 6180ms | 1.46 | 0.556 | 59ms | 0 |
| 16 | 10476ms | 12252ms | 1.47 | 0.556 | 59ms | 0 |

**In-proc control (post-sweep):** c=1 823ms, c=2 1.34×/1.52×,
c=4 1.88×/2.16× (median×/qps×). The lock sweep costs ~13% at c=1
(729→823ms) and buys strictly better concurrent scaling
(c=4: 2.13×→1.88× median, 1.93×→2.16× qps).

**Cold start (N=5):** boot 5.1s; request #1 median 16.3s, 91.4% attributed
model load; warm 1.3s; warmup endpoint (W1.7) worth ~15.1s median.
Attribution gate PASS ×5.

**Write contention** (arms on a bed copy, 4 readers × 36 queries):

| arm | topology | read median vs control | busy | WAL max | verdict |
|---|---|---|---|---|---|
| a | readers only | 1.0× (2669ms) | 0 | 1.5MB | PASS |
| b | + HTTP /ingest writer | 1.17× | 0 | 4.7MB | PASS |
| c | + direct bypass writer (tagger) | **1.45×** | 0 | 11.9MB | PASS |
| d | two direct writers, no server | n/a | 0 | 5.5MB | PASS |

recall@12 identical (0.5556) across a/b/c — the first-pass arm-b recall
collapse (0.258) was zombie-process contamination, not a real quality loss.
Zero lost writes, zero partial publish, integrity ok everywhere.

## Gate verdicts

- **G1 — FIRED, wall = in-process serving architecture.** Server-mode
  throughput saturates at **1.46 qps from c=2 through c=16** (pure
  executor queueing; latency linear in c; loop healthy) while
  subprocess-per-worker reaches 2.16× qps at c=4. Not SQLite, not the box.
  → Fork 1's serving half (N read workers) justified; **Postgres is not**
  (per the gate table). Cheapest next experiment: W3.2 executor sizing on
  main — find the knee before cutting the fork.
- **G2 — PARTIAL.** Bypass-writer (tagger topology) costs readers 1.45×
  median with perfect integrity and zero busy errors — real but bounded
  degradation, no wedge post-fixes. The July "2 agents benign" conclusion
  survives; sustained-ingest degradation is the Fork-1 (writer daemon)
  motivation, at lower urgency than G1. Longer soak arms (full tagger
  runtime) remain future work.

## Session lesson for the harness bed

`TaskStop` on a bash wrapper does not kill python children on Windows —
two wedged pre-fix probes survived four hours and contaminated the first
battery. Bench discipline: `taskkill /T` for cleanup, and a preflight
process sweep (benchmark_monitor-style) before any receipt run.
