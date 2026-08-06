# Graduation note — shipping the RemoteCodec flag on main (seam-level)

One page. What `master` needs to ship `[encoder_daemon] url` /
`CYMATIX_ENCODER_URL`, without merging the fork branch wholesale. Receipts:
`docs/benchmarks/2026-08-05-fork1-encoder-daemon-receipts.md` (all bars GO).

## Prerequisite

**PR #330 (`perf/slice1-instrumentation`) must land first.** The seams call
`hardware.resolve_layer_device()` and the `[hardware]`
dense/splade/sema/rerank_device knobs (51f3f4a), and the daemon's startup
sequence depends on `init_from_config` ordering — none of that exists on
master today. No other fork dependency exists.

## What ships (self-contained, additive except three seam branches)

| Piece | Files | Nature |
|---|---|---|
| Config | `config.py` (`EncoderDaemonConfig`, parse block, env override), `tests/test_config_default_honesty.py` tuple, `scripts/gen_config_reference.py` registry + `docs/config-reference.md` region, CLAUDE.md row | additive |
| Client | `backends/encoder_client.py` (transport, circuit, shared ready gate, daemon-process flag, RemoteBGEM3Codec, RemoteSemaCodec, per-item timeout scaling) | new file |
| Daemon | `encoder_daemon.py` (registry, micro-batchers, /ready, /health capacity) | new file |
| Seam branches | `backends/bgem3_codec.py` (`get_shared_codec` remote branch), `backends/splade_backend.py` (encode/encode_batch branch), `context_manager.py` (configure() call, sema factory branch, dense-ctor leak fix) | **the only edits to existing behavior** |
| Tests | `tests/test_encoder_client.py`, `test_encoder_daemon.py`, `test_remote_codecs.py`, `test_check_encoder_receipts.py`, conftest `_clean_encoder_url_env` autouse | additive |
| Receipts tooling | `benchmarks/dogfood/erb/encoder_daemon_capacity.py`, `benchmarks/dogfood/check_encoder_receipts.py` | additive |

The three seam branches are guarded by `active_url()` truthiness; with the
flag off they are a single string check — the OFF path was verified
byte-identical by literal diff comparison in review plus an ON/OFF
float-identical ranking smoke. The manager dense-ctor leak fix is the one
deliberate behavior change with the flag off: ingest-side dense encodes now
go through the shared codec with the resolved device (removes a duplicate
2 GB model and a silent CPU pin; flagged in the receipts doc).

## Suggested landing shape

Cherry-pick or re-land as one PR on top of #330; the fork's commit sequence
(b6c156b..HEAD) is already review-clean (4 task reviews + whole-branch
review + fix waves, suite 3,669 green). No schema changes, no migrations,
default-off, `--deterministic` profile untouched.

## Before flag-on-by-default (slice-2 backlog, none blocking flag shipment)

1. **Transport**: connection reuse + TCP_NODELAY (c=1 overhead is ~11.7%
   budget-clearing but mostly fixed HTTP cost — pooling should halve it);
   bundle-prefetch coalescing (3 per-seam RPCs → 1 `/encode/bundle` at the
   top of `query_docs`).
2. **Ingest**: client-side chunking to daemon `max_batch` (timeout scaling
   shipped; chunking is the robust form). Until then the daemon is
   sanctioned for query-side + moderate ingest, not bulk CPU re-ingest.
3. **Config parity**: client-side cross-check of
   `/health.capacity.dense_model`/`dim` at gate time (model-name skew is
   currently silent).
4. Deferred minors from the ledger: `RemoteSemaCodec.warm()` bypasses the
   shared gate; `encode_bundle` response validation; `_warm_rates` wedge
   timeout; capacity `ready_gate` fast-path never restores post-recovery.
5. **Fleet slices** (separate forks per the 2026-08-04 design review):
   writer daemon, worker wiring, mmap dense sidecar, registry port — the
   daemon deliberately does none of these.

## Operating profiles (receipt-backed)

- **cheap-operate (CPU)**: daemon on main .venv; capacity 6.8 bundles/s
  saturated — supports ~7-13 dogfood-class workers before encoder
  saturation; memory flat in N.
- **fast-bench (GPU)**: daemon on venv-cuda (or any CUDA torch); capacity
  51.4 bundles/s saturated; VRAM ~5 GB in ONE process instead of per-worker.
- Micro-batch window default 2 ms (`CYMATIX_ENCODER_BATCH_WINDOW_MS`) —
  receipts-calibrated; do not raise it back toward 8 ms without re-running
  the dogfood c=1 gate.
