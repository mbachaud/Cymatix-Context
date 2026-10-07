"""Compaction-recall bench: does an arm recover details lost to context compaction?

Mines Claude Code transcripts (``~/.claude/projects/*/*.jsonl``) for
``compact_boundary`` events. A *needle* is a specific detail (commit SHA, path,
issue ref, number, identifier) that appeared before a compaction and that the
agent used again after it. Each arm is scored on whether the context it would
have handed the post-compaction agent contains that detail.

Modules: ``transcript`` (event reconstruction), ``needles`` (extraction,
masking, scoring predicates), ``arms`` (today/summary/plan/handoff text arms,
BM25 / dense / cymatix retrieval arms), ``run`` (CLI + receipts).
"""
