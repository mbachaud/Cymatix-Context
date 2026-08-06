# Fork 1 slice 1 kickoff — hardware-aware encoder daemon + RemoteCodec (N workers)

Session prompt for starting the fork. Everything cited is on branch
`perf/slice1-instrumentation` (PR #330) with receipts.

---

## Prompt

Build **Fork 1 slice 1: an encoder daemon + RemoteCodec client** for
cymatix-context, in a NEW git worktree branched from
`perf/slice1-instrumentation` (it needs the per-layer device knobs and the
scale fixes). Scope is the encoder split ONLY — no writer daemon, no worker
fleet, no registry port (those are later slices; design notes in
`docs/benchmarks/2026-08-04-rerank-and-fork1-inputs.md` §3).

**Why (measured):** one BGE-M3 stack costs ~5.2GB VRAM / ~2.2GB RAM per
process, so N worker processes without sharing cap at ~2 on the 12GB RTX
3080 Ti. The daemon makes model memory flat in N. Throughput is not the
constraint: measured encode ceilings are 421.6 enc/s BGE batched / 135.5
SPLADE / 155 SEMA on GPU (~62 bundles/s) and ~9.3 bundles/s on CPU
(`benchmarks/dogfood/erb/receipts/encoder_throughput*.txt`).

**Architecture requirements:**
1. `encoder_daemon.py` — one process owning BGE-M3 + SPLADE + SEMA (and
   the cross-encoder when rerank lands), serving localhost HTTP/TCP.
   Single `encode_bundle` endpoint (query → dense vec + splade sparse +
   sema vec in ONE round trip) plus per-model endpoints and a batch form —
   batching is where the GPU 22.7× lives; never serve item-at-a-time
   internally when requests can coalesce (micro-batch window ~5-10ms).
2. `RemoteCodec` client plugging the existing seams, OFF by default:
   - dense: `get_shared_codec` (cymatix_context/backends/bgem3_codec.py:261)
   - splade: `splade_backend._ensure_loaded` / `encode`
   - sema: the SEMA codec ctor auto-branch
   Daemon URL set (config `[encoder_daemon] url` + env
   `CYMATIX_ENCODER_URL`) → RPC; unset → in-process models, byte-identical
   to today. Client must fall back to in-process on daemon-unreachable
   with a one-time warning (never fail a query because the daemon is down).
3. **Hardware-aware:** the daemon resolves its own devices through the
   existing `hardware.resolve_layer_device()` and `[hardware]`
   dense/splade/sema/rerank_device knobs (landed 51f3f4a) — GPU profile =
   CUDA torch + auto; cheap profile = CPU pins. Daemon startup logs a
   capacity self-report: device per layer, measured warm enc/s per model
   (5-encode probe), VRAM/RAM footprint, and the implied bundles/s. Batch
   sizes via `hardware.recommended_batch_size()`.
4. **Warmup/ready gating:** `/ready` returns 503 until all models are
   loaded AND one probe bundle has round-tripped (cold loads measured
   ~13s; a cold daemon must never brown out a worker fleet). Workers
   (RemoteCodec) poll `/ready` before first use.
5. Multi-client correctness: the daemon must serve concurrent clients
   without cross-request state; add a 2-client hammer test (the py3.14
   sqlite lessons don't apply — no DB in the daemon — but GIL-side
   tokenizer state does).

**Discipline (non-negotiable, from the campaign):**
- TDD; suite stays green (`python -m pytest tests/ -m "not live" -q`,
  ~3,490 tests).
- Receipts before/after with the existing harnesses, benched via
  `BenchServer(repo_root=<fork worktree>)` — no harness changes needed:
  (a) dogfood c=1,2,4 with daemon vs without: quality must be
      bit-identical (same vectors → same ranking; any delta is a bug),
      c=1 median regression budget ≤ +15% (RPC overhead), c≥2 qps must
      IMPROVE (that's the point);
  (b) erb_100k c=1 sanity (baseline ≈2.6s with trim/`CYMATIX_DISABLE_HEADROOM=1`,
      ≈5.1s with headroom, `splice_ab.txt`);
  (c) daemon capacity receipt: bundles/s at client counts 1/2/4/8 —
      accept bar: ≥ 3.5 bundles/s CPU profile, ≥ 30 bundles/s GPU profile
      (headroom below the measured 9.3/62 ceilings for RPC cost).
- Gates in a `check_perf_receipts.py`-style ratio checker, absolute ms
  never asserted in tests.
- Windows: `creationflags=CREATE_NO_WINDOW` on the daemon subprocess,
  `taskkill //T //F` cleanup in benches, explicit `timeout=` on every
  HTTP call, spawn-token identity on /ready (venv shims re-exec python —
  see BenchServer._wait_healthy for the pattern).

**Known traps (each cost this campaign a debugging pass):**
- Raw `KnowledgeStore()` ctor defaults disable dense/splade vs config —
  probes must pass knobs explicitly.
- Optional-dep parity across venvs (headroom-ai changed the splice
  backend silently); the CUDA venv lives at `f:/tmp/venv-cuda`
  (torch 2.11+cu128 + headroom); the main `.venv` is CPU torch and its
  DLLs are locked by live MCP processes — never swap torch in place.
- BGEM3Codec ctor defaults device="cpu" — always pass resolved devices.
- Per-stage ring attribution (`/debug/pipeline/recent`) is the fastest
  truth source when numbers look wrong.

**Deliverables:** daemon + client + tests + the three receipts + a
`docs/benchmarks/` write-up with the go/no-go verdict vs the bars above,
and a one-page graduation note: what changes to ship the RemoteCodec flag
on main (seam-level, no fork merge required).
