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

## Verdicts pending

- G1 (event-loop/executor wall vs SQLite): needs the full c=1..16 read-arm
  curve post-sweep.
- G2 (write contention): needs `run_write_contention.py` arms a–d.
