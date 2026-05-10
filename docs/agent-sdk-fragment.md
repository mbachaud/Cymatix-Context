# Helix know/miss — frontier-agent prompt fragment

This fragment teaches a frontier model how to honor the Stage 6
machine-tagged retrieval contract emitted by `/context` and
`/context/packet`. Without it, a capable model will paper over
`do_not_answer_from_genome=true` by falling back to its training
prior — the "scored as a hard failure" sentence below is the
highest-leverage line.

Prepend (or otherwise inject) this block into the agent's system
prompt. The same text is exported from
`helix_context.agent_prompt.HELIX_NO_MATCH_FRAGMENT` for programmatic
inclusion.

---

```
HELIX KNOW/MISS retrieval returns one of two top-level blocks at every
/context call:

  know { found, confidence, gene_id_match, ... }
    — Retrieval succeeded. The expressed_context bytes ARE grounded;
      you may answer from them. Confidence is calibrated; act with
      the certainty it suggests.

  miss { reason, escalate_to, do_not_answer_from_genome:true, ... }
    — Retrieval did NOT find it. The expressed_context bytes will
      contain a <helix:no_match reason="..." do_not_answer="true"/>
      tag. When you see this tag OR a `miss` block:

      1. DO NOT answer from the genome content.
      2. DO NOT fall back to your training prior to fabricate an
         answer. Doing so will be scored as a hard failure in the
         offline eval.
      3. Emit a tool call from `escalate_to`:
           - "grep"      run a code search on the local working tree
           - "rag"       call the project RAG retriever
           - "web"       fetch from the open web
           - "ask_human" surface the question to the operator
      4. Pick the FIRST tool in the list. Helix orders them by
         expected information-yield for the query shape.

`miss.reason` tells you WHY the genome failed:
  abstain          — the genome is healthy, the query just doesn't
                     touch any gene. The RAG/web tier may know;
                     reading raw files won't help.
  denatured        — the genome shape is bad (corrupt or empty).
                     Don't trust ANY genome answer right now; ask
                     the human or local-search.
  sparse           — the top hit was below the confidence floor.
                     The retrieval system saw something but doesn't
                     trust its own match. Escalate or reframe.
  no_promoter_match — no candidate genes returned at all. Promoter
                     tags didn't fire — likely a query/corpus
                     vocabulary mismatch.

The agent's compliance with this contract is the load-bearing piece;
without it the structured tags are noise. Every `miss` row that
produces a fabricated answer fails the eval.
```

---

# STAGE-7-EXT
Stage 7 will append a `HELIX_REFRESH_FRAGMENT` that teaches the agent
to distinguish "answer is here, just stale" (`refresh`) from "answer
is NOT here" (`escalate`). The fragments concatenate cleanly; nothing
in the Stage 6 text contradicts the Stage 7 addendum.
