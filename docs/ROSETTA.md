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

**What's still legacy-named, and why.** The wire/SQL surface (JSON
field names like `gene_id`, the `genes` SQL table, `/stats` keys, the
legacy `<GENE .../>` inline tag) is deliberately not renamed — doing
so would change bytes on the wire for existing callers. That remaining
Tier-3 surface is tracked at
<https://github.com/mbachaud/Cymatix-Context/issues/417>.
