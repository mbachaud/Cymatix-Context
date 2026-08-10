# Invalid qualification attempt: empty-corpus warm-up

The predeclared replicate-2 order ran Cymatix first, so this non-scored attempt
made no model calls and did not start the control arm. Empty-workspace
initialization and its verified zero-ingest receipt succeeded, but the unscored
packet warm-up received the expected `Zero genes matched across all tiers`
response as HTTP 500 because the corpus was still empty.

The retained receipts, sidecar database, and logs are the evidence of record.
Packet warm-up is now skipped only for a verified empty initial manifest and is
recorded as zero milliseconds. Non-empty initial workspaces still require the
warm-up; checkpoint 1 remains the bare A/A sentinel, and checkpoint 1's delta
sync supplies the first real corpus before checkpoint 2 retrieval.
