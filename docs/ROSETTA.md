# Rosetta Stone — retired to the wiki Lexicon

This document used to carry the full biology-lexicon ↔ software-lexicon
mapping table. As of v0.9.1 that content has moved and been expanded
into the **Lexicon** page on the project wiki:

- <https://github.com/mbachaud/Cymatix-Context/wiki/Lexicon>
- <https://cymatixcontext.com/wiki/lexicon/>

**Canonical lexicon.** The canonical vocabulary is standard software
terminology (`Document`, `KnowledgeStore`, `Compressor`, `tags`,
`signals`, `fragments`, ...). Legacy biology terms (`Gene`, `Genome`,
`Ribosome`, `chromatin`, `codon`, `splice`, ...) remain valid Python
aliases — both names resolve to the same objects — so older handoffs,
papers, and commit messages stay readable without modification.

**Why biology in the first place?** The original vocabulary borrowed
from molecular biology as a generative metaphor for the architecture.
That backstory — and why the project moved off it — is told in the
Agentome paper: <https://mbachaud.substack.com/p/agentome>.

**Historical docs keep their vocabulary.** Dated docs (benchmarks,
council verdicts, dated plans, `docs/archive/`) are point-in-time
records and are intentionally left unmodified — they read in whatever
vocabulary was current when they were written.

**Wire vocabulary.** The default `[budget] wire_format = "legacy"`
preserves existing responses. The opt-in `"canonical"` format uses
`<DOCUMENT>` blocks, `[document=...]` headers, software terminology in
decoder prompts, and canonical response keys across HTTP, CLI and MCP.
For example, `gene_id` becomes `document_id`, `gene_id_match` becomes
`document_id_match`, and `do_not_answer_from_genome` becomes
`do_not_answer_from_knowledge_store`. Stats retain `open` and use `warm`
and `cold`. See the complete explicit mapping in
[`cymatix_context/wire.py`](../cymatix_context/wire.py).
Packet evidence items distinguish the local `document_id` (legacy
`gene_id`) from `source_document_id` (the existing portable, source-derived
`document_id`). Both identities are preserved; item kind `gene` becomes
`document`.

This remains an experimental measurement arm for
[#417](https://github.com/mbachaud/Cymatix-Context/issues/417).
Default promotion requires paired full-scale wire and delivery receipts.
Client applications select the server's format through its configuration;
MCP forwards that server's format. Both route and command aliases remain
available. Configuration inspection retains its existing config vocabulary.
Restart the server after changing `[budget] wire_format` or
`[retrieval] harmonic_batching_enabled`. `/admin/reload` retains the active
values for these startup settings and lists pending changes in
`restart_required`; other configuration settings continue to reload.
Opaque user metadata, document text, identifiers, and upstream OpenAI
responses are not rewritten by the response serializer. Canonical assembly
escapes reserved control syntax in document text before adding its own tags.

**Storage is frozen.** SQL names such as the `genes` table and the Python
model fields used to persist records remain unchanged in both formats.
`Document.model_dump()` and store `stats()` keep their internal contracts;
the wire projection applies only at delivery boundaries. No data migration
is needed to enable or disable the experimental format.
