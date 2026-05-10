"""Importable agent-prompt fragments for the Helix know/miss contract.

Spec: docs/specs/2026-05-08-stage-6-know-miss-blocks.md §12.

The Stage 6 machine-tagged contract is load-bearing only if the
frontier agent's system prompt teaches it to honor the
``<helix:no_match/>`` tag and the ``do_not_answer_from_genome=true``
field. This module exposes the fragment as a Python constant so callers
can prepend it to the system prompt without parsing markdown.

The same text lives in ``docs/agent-sdk-fragment.md`` — keep them in
sync. The markdown is the human-readable canonical form; this constant
is the programmatic mirror.

# STAGE-7-EXT: when Stage 7 lands, this module also exports
# HELIX_REFRESH_FRAGMENT (the "answer is here but stale, refresh"
# branch) and a convenience ``full_fragment()`` that concatenates them.
"""

from __future__ import annotations


HELIX_NO_MATCH_FRAGMENT: str = """\
HELIX KNOW/MISS retrieval returns one of two top-level blocks at every
/context call:

  know { found, confidence, gene_id_match, ... }
    -- Retrieval succeeded. The expressed_context bytes ARE grounded;
       you may answer from them. Confidence is calibrated; act with
       the certainty it suggests.

  miss { reason, escalate_to, do_not_answer_from_genome:true, ... }
    -- Retrieval did NOT find it. The expressed_context bytes will
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
  abstain          -- genome is healthy, query just doesn't touch any gene
  denatured        -- genome shape is bad (corrupt or empty)
  sparse           -- top hit was below the confidence floor
  no_promoter_match -- no candidate genes returned at all
"""


__all__ = ["HELIX_NO_MATCH_FRAGMENT"]
