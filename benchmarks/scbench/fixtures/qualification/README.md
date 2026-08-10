# Non-scored SCBench qualification fixture

This tiny workspace proves that the Cymatix treatment can retain an
agent-visible compatibility constraint across a checkpoint reset. It is not an
SCBench pilot problem and its results must never be pooled with scored runs.

Only `src/state.py` is eligible for source ingestion. Qualification tests add
synthetic evaluation artifacts at runtime and verify that they never enter a
retrieval packet.
