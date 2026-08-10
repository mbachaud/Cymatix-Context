# Invalid qualification attempt: stale Codex model schema

This non-scored control arm was invalidated before any model steps or tokens
were used. Codex CLI `0.130.0` could not decode the current model catalog after
the service added reasoning effort `max`; checkpoint 1 therefore ended in an
agent error and the Cymatix mate did not run.

The retained receipts and SCBench inference result are the evidence of record.
The host, symmetric agent configs, and identical Docker images were upgraded to
Codex CLI `0.147.0` in SCBench fork commit
`07c73218b6ca5ca988b7a87def1f465c3c298fa8`.
