# Invalid qualification attempt: missing spaCy language model

The predeclared replicate-2 order ran Cymatix first, so the control arm did not
start. Treatment checkpoint 1 completed its Codex work, but the mandatory
conversation sync failed closed after spaCy imported successfully: the sidecar
venv lacked `en_core_web_sm`, which Cymatix's CPU tagger loads explicitly.

The retained receipts, sidecar traceback, checkpoint artifacts, and snapshot
are the evidence of record. Preflight now loads `en_core_web_sm` with the same
SCBench venv used to spawn the sidecar before recording the spaCy version, so a
library-only installation cannot pass qualification.
