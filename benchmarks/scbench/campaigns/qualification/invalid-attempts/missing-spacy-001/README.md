# Invalid qualification attempt: missing sidecar NLP dependency

The predeclared replicate-2 order ran Cymatix first, so the control arm did not
start. Treatment checkpoint 1 completed 18 Codex steps and created the initial
implementation, but the mandatory post-checkpoint conversation sync failed
closed: the SCBench venv that hosts the sidecar lacked Cymatix's `spacy` CPU
extra, and `/ingest` returned HTTP 422 with `ModuleNotFoundError: spacy`.

The retained receipts, sidecar logs, checkpoint artifacts, and snapshot are the
evidence of record. Preflight now imports and records the spaCy version from the
same SCBench venv used to spawn the sidecar, preventing code-only ingest checks
from hiding a broken conversation-learning path.
