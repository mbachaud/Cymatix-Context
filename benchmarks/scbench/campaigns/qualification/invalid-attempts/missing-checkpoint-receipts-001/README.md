# Invalid qualification attempt: missing checkpoint receipts

Both arms completed all four checkpoints and passed SCBench's terminal
infrastructure checks. The runner wrote `valid: true` in its operational pair
receipt, and the selected evidence hashes replayed successfully. This attempt
is nevertheless excluded from qualification because the independent receipt
verifier returned only the pair receipt: no full checkpoint receipts had ever
been persisted.

Task 7 had introduced strict `CheckpointReceipt`, `ArmReceipt`, and
`PairReceipt` schemas, but Task 10 used a separate operational receipt family
without assembling the Task 7 models. The treatment adapter also wrote only
artifact references, discarding in-memory retrieval latency, rendered token
count, deletion filtering, warm-up, and synchronization metadata after the
agent process exited. Backfilling those values would not be an honest replay.

The adapter now persists metadata-only lifecycle observations alongside its
existing content-addressed references. The runner builds a dedicated receipt
tree for both arms, content-addresses prompts, Codex streams, score rows, and
workspace manifests, writes one terminal receipt per checkpoint, and replays
the tree before a pair can become valid. The verifier requires exactly twice
the catalog checkpoint count, so a pair-only receipt can no longer pass.
