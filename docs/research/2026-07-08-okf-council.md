# OKF Council Report — helix-context Open Knowledge Format support

**Status:** Decision record for the OKF (Open Knowledge Format) interop
track. Council convened 2026-07-08; verdict GO_WITH_CHANGES 5/5. Phase 1
(ingestion adapter, `helix ingest --okf`) is implemented in this PR;
Phase 2 (link-priors design doc) and Phase 3 (exporter) are gated — see
§4. Report body below is archived verbatim.

---

**VERDICT: GO_WITH_CHANGES — 5/5 (architecture 0.82, strategy 0.70, risk-cost 0.78, determinism 0.86, sequencing 0.80); no NO_GO, no unconditional GO.** The council unanimously endorses the workstream — OKF maps onto helix unusually well and parallelizes cleanly with the #239 critical path — but every lens independently found the same two defects in the brief: the determinism promise is falsifiable as written, and Phase 1's link persistence silently ships an ungated retrieval-scoring change unless it lands in a new inert table.

---

## 1. What the council agreed on

**The spec is real and the opportunity is genuine, but the brief misstates the spec in two places (spec recon is authoritative):**

- **Claim 5 is wrong as briefed.** "Malformed = warn + ingest as generic doc, never fail bundle" is *not* in OKF v0.1. §9 permissiveness covers missing optional fields, unknown types, unknown keys, broken links, and missing index.md — it does **not** cover missing/unparseable frontmatter or empty `type` (those make the bundle non-conformant, and the spec is silent on consumer behavior). "Treat as generic" applies only to *unknown type values*; the word "warning" appears nowhere. Helix's graceful-degradation policy is fine to build, but it must be documented as **helix's own policy**, not attributed to the spec.
- **The §4 citation for the index.md/frontmatter rule is wrong** — it's §6 (index files contain no frontmatter) plus §11 (bundle-root index.md may carry frontmatter *solely* for `okf_version`). Concept files always carry frontmatter; a frontmattered non-root index.md is strictly a conformance violation.
- Additional spec facts all lenses relied on: exactly **one** required frontmatter field (`type`); the **reference implementation's validator requires four fields** (type/title/description/timestamp) and must not be copied; link examples target paths *with* `.md` while concept IDs strip it (normalization trap); type taxonomy is deliberately free-form; bundles must be accepted as plain directories; UTF-8 required; consumers may synthesize a missing index.md.

**Unanimous technical findings (5/5, all verified in-repo):**

1. **The determinism requirement is false under default config.** SEMA/MiniLM, BGE-M3 dense, and SPLADE ingest-time writes are all default-**on** and emit platform-variant fp32; spaCy NER output is model-version-dependent and feeds FTS + promoter_index; `genes.last_seen`/`last_verified_at`/epigenetics timestamps are wall-clock-stamped on every upsert; tagger tie-breaking iterates hash-randomized sets (PYTHONHASHSEED-sensitive); Windows `source_id` paths use backslashes. "Byte-identical genome contribution across runs and platforms" fails on the **same machine, seconds apart**. The genuine deterministic spine — `gene_id = sha256(content)[:16]`, content_hash, deterministic parent-doc IDs, deterministic chunking — supports a rescoped *canonical-digest* claim (Amendment 2), and this matters doubly because determinism is the pitch to a community that will test it.
2. **Both existing edge tables are retrieval-live.** The Tier-5 harmonic boost adds a flat per-edge bonus for *any* `harmonic_links` row between co-candidates, ignoring `source`, `weight`, and the `seeded_edges_enabled` flag (`knowledge_store.py:~2683-2704`); `gene_relations` feeds tie-breaking (`retrieval/tie_break.py:120-131`). Writing OKF links to either during "pure ingestion" changes scoring ungated — violating the brief's own "ask before touching scoring" constraint and pre-empting Phase 2's human-review gate. **Phase 1 links must go to a new inert table.**
3. **The adapter must route through `HelixContextManager.ingest`**, never `upsert_doc`-direct (the historical density-gate-bypass path). One ~10-line seam between `pack()` and `upsert_doc` merging caller-supplied domains/entities/key_values makes YAML-sourced tags indistinguishable from tagger output across every index (promoter_index, genes_fts, entity_graph, path_key_index) and benefits CLI/HTTP/MCP at once.
4. **pyyaml is a latent packaging bug, not dependency creep.** `vault/writer.py:18` and `vault/pruner.py:16` already `import yaml` unconditionally while pyyaml appears in no dependency list; the only in-repo frontmatter parser (`mem_sync.py`) is flat k:v and cannot parse OKF's list-valued `tags`. Declaring pyyaml fixes an existing bug.
5. **Cold-genome link sparsity.** `harmonic_links` populates only at query time; `seed_edges()` has zero production callers and is gated off. A freshly-ingested genome exports a nearly link-free bundle — the flagship "knowledge graph" demo would be embarrassingly empty without mitigation.
6. **Phase 3 is more expensive than the brief prices** (three link stores, two weight scales, per-dir index.md net-new, cross-links currently a hardcoded placeholder string, chunk-vs-document granularity), while vault/ supplies real plumbing (~40-60% head start: atomic writer, sorted-key YAML dump, deterministic filenames, full+incremental drivers, CLI/HTTP triggers).
7. **Sequencing is favorable.** OKF Phase 1 is rig-independent, pure-Python, delegable work that does not contend with #239 Run 3 (the roadmap critical path) for the bench rig — only for attention. The interop claim is a feature claim, not bench-gated.

---

## 2. Contested points

**Phase ordering — architecture vs the field.** Architecture argued **1 → 3 → 2-impl**: the exporter doesn't depend on Phase 2's prior-weighting decisions, and ingest+export unlocks the round-trip conformance test (ingest sample bundle → export → structural diff), the marquee determinism demo. Strategy, risk-cost, and sequencing all pushed the other way: Phase 3 is the least-validated, most expensive phase on a Draft spec, is where the wiki-tool positioning risk concentrates, and should be a **separate go/no-go** decided at (or after) the Phase-2 human review, possibly permitting only a descoped link-light v0. **Council resolution (3-1): Phase 3 is gated, not pre-committed** — but the Phase-2 review explicitly considers architecture's round-trip argument and may authorize a minimal exporter whose link source is the Phase-1 `okf_links` table itself (which neatly sidesteps cold-genome sparsity).

**spaCy in the OKF path — architecture vs determinism.** Architecture: the tagger must still run (complement/intent/summary/codons feed downstream consumers), so frontmatter tags **merge** with tagger output. Determinism: spaCy contaminates the digest across environments; strongly prefer the adapter constructing domains/entities entirely from frontmatter. **Resolution: both, at different layers** — the tagger runs and frontmatter merges additively (architecture wins on gene construction), but the canonical digest declares pinned spaCy+adapter version as an invariance precondition, and the digest fields are frontmatter-derivable wherever possible (determinism wins on the claim).

**How hard the claim can be sold now.** Strategy would take "helix ingests OKF, deterministically" public after Phase 1 with compressor-first framing; sequencing insists the full "ingests **and emits**" one-liner is deferred until Phase 3 lands and worded as a feature claim, never a benchmark number (README numbers are separately gated on the bench-validity wave). Not fully resolved — see Open Question 2.

**Phase-2 blocking condition.** Risk-cost wanted any prior-injection implementation gated behind #239 Run 3 completion; sequencing warned that hard-blocking on #250's follow-up decisions (diagnosis-only, no owner, no date) is a false dependency that stalls OKF indefinitely. **Resolution:** the design doc waits only for PRs #250/#251 to *merge* (docs-only, days), **cites** their open questions as design inputs rather than blocking on their resolution, and adopts the 2026-07-07 causal-use circuit-tracer yardstick as its acceptance bar for any future implementation.

---

## 3. Binding amendments

1. **Inert link table.** Phase 1 persists the OKF cross-link graph in a NEW table — `okf_links(bundle_id, source_concept_id, target_concept_id, resolved_source_gene_id, resolved_target_gene_id NULLABLE, link_text)` — with **zero readers in any retrieval tier**. Writes to `harmonic_links` and `gene_relations` are prohibited in Phase 1. Graduation into live edges happens only via Phase 2's reviewed design (natural path: a 4th `SOURCE_WEIGHT_MULTIPLIER` provenance class `'asserted'` in `seeded_edges.py`). Links resolve `.md`-suffixed targets to concept IDs by normalization; multi-chunk concepts resolve to the deterministic **parent** gene_id.

2. **Rewrite the determinism requirement.** The Phase-1 requirement now reads exactly:

   > *For a fixed adapter version, pinned spaCy model version, and OKF spec version, ingesting the same bundle yields a byte-identical **canonical digest**, across runs and platforms. The digest is sha256 over a canonical JSON serialization (sorted keys, LF newlines, UTF-8, POSIX-normalized forward-slash paths) of, per concept: gene_id (=sha256(content)[:16]), content_hash, type→taxonomy mapping, title, description, sorted(domains), sorted(entities), sorted(key_values); plus the bundle cross-link edge set as a sorted list of (source_concept_id, target_concept_id) pairs. Embeddings (SEMA, BGE-M3), SPLADE term weights, wall-clock fields (last_seen, last_verified_at, epigenetics.created_at/last_accessed), and any REAL-valued score are excluded from the digest by construction. A documented **deterministic-ingest profile** (sema_embed_on_ingest=false, dense_embed_on_ingest=false, splade_enabled=false) yields a genome with no float tensors, with embeddings backfilled per-host afterward as per-host artifacts never covered by the interop claim.*

   The determinism test ingests each sample bundle twice **in separate processes with different PYTHONHASHSEED values**, asserts digest equality, asserts digest stability after a clock advance, and asserts that the timestamp columns *differ* between runs (documenting why they're outside the guarantee). Never hash the SQLite file or raw rows. Drop "byte-identical genome contribution across platforms" from all public text.

3. **Single ingestion path + seam.** The adapter routes exclusively through `HelixContextManager.ingest`. Add the ~10-line seam in `context_manager.ingest` merging `metadata["domains"]/["entities"]/["key_values"]` into tagger output, as its **own commit**. Two required tests: (a) equivalence — YAML-supplied tags produce identical promoter_index/genes_fts/path_key_index rows to tagger-produced tags; (b) bench-neutrality — the seam is a provable no-op when those keys are absent, so existing beds and the #239 rig are untouched.

4. **Merge, don't bypass.** Change "tags BYPASSING the tagger regexes" to "tags MERGED with tagger output": the tagger still runs (complement/intent/summary/codons have downstream consumers, and today it actively *drops* frontmatter values via `_KV_SKIP_KEYS`). Frontmatter is stripped from content before chunking; raw `type` and concept ID are kept in `promoter.metadata` for lossless round-trip; `type` maps to `source_kind` (free-form strings verified to pass through); `source_id` = bundle-relative concept path, POSIX-normalized.

5. **Declare pyyaml** (core dep or `okf` extra — Max decides, this report is the constitutionally-required "ask"), fixing the undeclared `import yaml` in vault in the same commit. No markdown-parsing dependency; regex link extraction with fenced-code-block exclusion suffices.

6. **Conformance suite from the vendored spec, not the reference impl.** Vendor a SPEC.md snapshot pinned to commit `ee67a5ca` into `tests/fixtures/okf/`; enforce exactly the spec's **one** required field; include a minimal type-only bundle fixture that must be accepted; treat any upstream spec bump as an explicit ROADMAP item. Document helix's degradation policy as its own (per the spec-recon correction, the spec does not define one).

7. **Module placement:** new `helix_context/okf/` package (bundle reader, link capture, later the export renderer) + thin CLI entry. Not `adapters/` (internal storage ports), not `server/` (invisible to CLI/API/MCP). Phase 3's renderer imports vault plumbing but is a parallel renderer, not a subclass.

8. **Fixtures:** `tests/fixtures/okf/<bundle>/` (verified clean of .gitignore patterns — note `temp_*.md` and `logs/` match at any depth), with a manifest test asserting exact per-bundle file counts on a fresh clone; Apache-2.0 NOTICE attribution; a 30-minute scan of the stackoverflow bundle for embedded CC BY-SA post text before committing. Never under `benchmarks/`.

9. **Cut the tarball/zip stretch** — zip-slip surface for zero v0.1 value; a plain-directory ingester covers it.

10. **Phase 3, if authorized, is descoped:** one concept file per **source document** (reassemble chunks via parent doc + sequence_index), not per gene; strip `last_seen_ts`/`live_truth_score` and all mutable/wall-clock fields from frontmatter; explicit `ORDER BY (gene_id, concept_id)` on every ordering query (the current vault SELECT has none); cross-link emission from the Phase-1 `okf_links` table first, weighted harmonic export deferred to the Phase-2 decision, with any float threshold quantized-before-compare or replaced by rank selection; round-trip byte-identity test (export twice, two PYTHONHASHSEED values, byte-identical bundles). Demo only against a warmed genome or okf_links-sourced links.

11. **Branch/claim discipline:** worktrees off `master` (never off `research/faithfulness-semantic-reach`), one PR per phase; ROADMAP entry added only after PR #251 merges, as a new "Interop track — OKF (not bench-gated)" item; after Phase 1 claim only "ingests OKF" in `docs/INTEGRATING_WITH_EXISTING_RAG.md`, framed compressor-first ("OKF bundle → compressed agent context, no LLM on the retrieval path"); "ingests and emits" waits for Phase 3.

---

## 4. Amended execution plan

**Phase 1 — OKF ingestion adapter (approved, start now).** Branch `feat/okf-ingest` in a worktree off master; runs parallel to #239 Run 3, zero rig usage. Scope: `helix_context/okf/` reader (pyyaml frontmatter, frontmatter stripped from body, merge-not-bypass tags, `.md`-normalized link capture into the new inert `okf_links` table), the seam commit, fixtures + NOTICE, conformance suite pinned to `ee67a5ca` (1-required-field, type-only fixture, degradation-policy tests), canonical-digest determinism tests per Amendment 2, equivalence + bench-neutrality tests. One PR. Cut: tarball/zip.

**Phase 2 — design doc (approved; docs-only PR).** Written after Phase 1 lands and PRs #250/#251 merge; `docs/research/2026-07-XX-okf-link-priors.md`. Covers graduation of `okf_links` into the seeded-edges machinery via an `'asserted'` provenance class, weights vs statistical edges, decay/eviction, bad-prior failure modes; cites #250's open questions (RRF tier-breadth bias, ANN threshold disposition) as inputs, not blockers; adopts the causal-use circuit-tracer yardstick as the acceptance bar. The Stack Overflow FK-link eval is **offline analysis only** (link counts, entity-graph overlap, simulated weight distributions) — no code path into retrieval. **Human review gate stands; no implementation without it.**

**Phase 3 — exporter (separate go/no-go, decided at the Phase-2 review).** If authorized: descoped v0 per Amendment 10 on `feat/okf-export`, reusing vault plumbing. The round-trip conformance test (ingest sample bundle → export → structural diff) becomes the determinism showpiece. "Ingests and emits" claim and any expanded Show-and-tell land here.

**Gated on human review:** Phase-2 implementation (any write to live edge tables, any retrieval-scoring contact), Phase-3 authorization and scope, the Show-and-tell post text (determinism wording per Amendment 2), and pyyaml placement.

---

## 5. Open questions for Max

1. **pyyaml:** approve adding it (your ask-before-deps constraint) — core dependency or `okf` extra? Council notes vault already imports it undeclared either way.
2. **Phase 3 pre-commitment:** does the "ingests **and emits**" one-liner matter enough to authorize a minimal link-light exporter alongside Phase 2's design review, or is "ingests OKF" sufficient for the first Show-and-tell? (Council split 3-1 toward gating; architecture's round-trip-demo argument is the counterweight.)
3. **Show-and-tell timing:** post after Phase 1 (ingest-only, compressor-first framing, canonical-digest determinism claim) or hold for the round-trip demo? Strategy notes the visibility value is captured mostly up front; risk-cost notes the community will test whatever is claimed.
4. **Deterministic profile default:** should `helix ingest --okf` default to the deterministic-ingest profile (embeddings off, backfill per host) or to standard config with the digest-scoped claim only? This sets which behavior the public claim describes out of the box.

---

*Provenance: 9-agent council workflow, 2026-07-08. Ground truth: OKF spec fetched from GoogleCloudPlatform/knowledge-catalog (snapshot at scratchpad/okf_spec.md, pinned commit ee67a5ca); helix ingest/export internals verified in-repo with file:line refs. Votes: architecture 0.82, strategy 0.70, risk-cost 0.78, determinism 0.86, sequencing 0.80 — all GO_WITH_CHANGES.*
