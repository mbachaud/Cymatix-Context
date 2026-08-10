# Invalid qualification attempt: missing spaCy language model

The predeclared replicate-2 order ran Cymatix first, so the control arm did not
start. Treatment checkpoint 1 completed its Codex work, but the mandatory
conversation sync failed closed after spaCy imported successfully: the sidecar
venv lacked `en_core_web_sm`, which Cymatix's CPU tagger loads explicitly.

The retained receipts, sidecar traceback, checkpoint artifacts, and snapshot
are the evidence of record. Preflight now loads `en_core_web_sm` with the same
SCBench venv used to spawn the sidecar before recording the spaCy version, so a
library-only installation cannot pass qualification.

Re-evaluating the retained snapshot also exposed two independent Windows-host
fork defects. `evaluation-lf-recheck/` records the first run clearing the CRLF
shell failure but hitting `.venv/lib64` cleanup; `evaluation-cleanup-recheck/`
records the final model-free proof: 46 tests collected, 45 passed, one genuine
solution failure, and `infrastructure_failure=false`. The harness now rejects
any zero-collected or infrastructure-failed evaluation as an invalid arm.
