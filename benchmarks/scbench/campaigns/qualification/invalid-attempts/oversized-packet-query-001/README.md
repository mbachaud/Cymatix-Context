# Invalid qualification attempt: oversized packet query

The predeclared replicate-2 order ran Cymatix first, so the control arm did not
start. Treatment checkpoint 1 completed, evaluated, and synchronized its
conversation and source snapshot successfully. Checkpoint 2 failed before its
first model call while constructing the first real context packet.

The sidecar traceback records `sqlite3.OperationalError: too many terms in
compound SELECT`. The packet query had copied the entire SCBench checkpoint
specification into lexical retrieval, whose prefix tier creates two indexed
range branches per query term. The retained receipts contain two terminal
checkpoint artifacts: the completed first checkpoint and the failed second
checkpoint with zero model steps.

Retrieval-query construction now limits the prompt-derived portion to 200
whitespace-delimited terms, retaining equal head and tail excerpts plus an
explicit truncation marker. Evaluation-path contamination checks still inspect
the complete original prompt, and the prompt delivered to Codex remains
unmodified.
