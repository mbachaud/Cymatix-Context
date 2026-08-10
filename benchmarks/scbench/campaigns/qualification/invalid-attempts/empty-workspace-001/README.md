# Invalid qualification attempt: empty initial workspace

The non-scored control arm completed all four checkpoints, passed terminal
verification, and reported 128 total agent steps with `$1.50333` in cumulative
SCBench cost accounting. The Cymatix arm then stopped before its first model
call because `file-merger` legitimately begins with no source files, while the
coordinator treated an empty initial allowlist as an ingest failure.

The retained receipts, sidecar database, and logs are the evidence of record.
Initialization now records a verified zero-ingest sync for create-from-scratch
workspaces; the checkpoint-1 A/A sentinel remains unchanged, and the normal
post-checkpoint delta sync seeds the genome before checkpoint 2. Setup failures
also close partially initialized coordinators so their sidecars cannot leak.
