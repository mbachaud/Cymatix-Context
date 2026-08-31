# Second bench corpus — scouting report

**Date:** 2026-08-28 · **Charter:** user ("we may want to look into finding
another corpora so we aren't tuning just for ERB … larger than 100k files
ideally") · Produced by a 2-agent fan-out (external survey + repo
feasibility grounding); full agent reports archived with the session.

## Why

Every retrieval knob graduated so far (rrf_k=20, all-classes eps_band, and
whatever W2.1 becomes) is evidenced on ONE corpus family: ERB — synthetic
enterprise comms. A second bed with document-level gold and a genuine
paraphrase query split is the guard against ERB-overfitting, and doubles as
the out-of-domain check the cc-exchange thread keeps circling.

## Recommendation: EnronQA (+ raw-Enron distractor padding)

**EnronQA** (arXiv:2505.00263; HF `MichaelR207/enron_qa_0922`, CC BY 4.0) is
the only surveyed candidate scoring on all five axes:

- **103,638 real Enron emails**, 528,304 QA pairs across 150 inboxes; each
  question has a **single gold email** — true document-level gold, no
  passage→doc mapping.
- **Built-in paraphrase split**: every question ships a
  `rephrased_questions` variant. The ORIGINAL questions are lexically easy
  (paper: BM25 R@5 = 87.5) — **the rephrased split is the semantic test**;
  bench both splits and difference them for a paraphrase-sensitivity number
  ERB cannot give us.
- **Real recurring entities** (people, desks, counterparties, deals) across
  inboxes — the strongest COVER-graph exerciser available, on REAL text (vs
  ERB's synthetic entity distribution; a direct external check on the W2.1
  lane).
- **Size**: 103k native (hours of ingest at the measured 16.16 genes/s
  bulk_load rate); padable to ~500k with the remaining raw Enron corpus
  (517,401 emails, public record) as distractors without touching gold.
- **License**: CC BY 4.0 dataset over a public-record corpus — receipts
  publishable.

It is also the ideal contrast bed: same genre as ERB (enterprise comms),
real instead of synthetic — the cross-bed comparison isolates
"synthetic-corpus artifacts" from "genre effects".

**Runners-up:** LoTTE technology split (639k StackExchange posts, ~14k
GooAQ natural-question queries — the statistical-power option, weaker
entity graph); CLERC subset (US case law, "indirect" queries describe
precedents without naming them — the purest description-not-terms test,
must be subset from 1.84M docs; confirm dataset-card license first).

**Rejected** (one line each, so they stay rejected): MS MARCO (3.2M,
research-only grey license, weak entity graph), Robust04/GOV2/ClueWeb
(signed-agreement friction, ≤250 topics), BEIR NQ/Climate-FEVER
(passage-level gold needs mapping; oversized), HotpotQA (5.2M docs; questions
name their entities — weak paraphrase test), FiQA/SciFact/MultiHop-RAG/HERB
(too small), ESCI (keyword queries), LongMemEval/LoCoMo (not document
corpora).

## Adapter checklist (from the code, verified)

1. Emit emails as files under a new `PROFILES` root
   (`scripts/build_fixture_matrix.py:676-733`; ext allow-list :199-205, size
   gate 50..200,000 bytes; `source_id` = absolute path :307-312).
2. Parameterize the needle resolver (`scripts/resolve_erb_needles.py`
   hardcodes ERB_ROOT/CORPUS_BASE at :37-38); gold joins on exact
   `source_id`, so the emitter and resolver must agree on paths.
3. Re-resolve gold per bed (gene_ids are content hashes — any re-chunk
   invalidates `gold_by_needle`); emit
   `needles_resolved_enron_{full,half}.json`, `gold_by_needle_enron.json`,
   `needles_enron_meta.json` with a type-stratified half split (ERB
   convention, seed recorded).
4. Sample the needle set: ~470-500 questions stratified original vs
   rephrased (the paired original/rephrased design is the point — same
   gold, two phrasings).
5. Build pins: `--mode blob --mem-profile bulk_load --commit-batch 5000`,
   `CYMATIX_BFM_SPLADE=0 CYMATIX_BFM_DENSE_BACKFILL=0
   CYMATIX_BFM_SEED_EDGES=0 CYMATIX_DISABLE_LEARN=1`, ingest_c recorded,
   provenance-stamped (`scripts/stamp_bed_provenance.py`). 103k ≈ ~4 h;
   padded 517k ≈ ~1 day.
6. New BASELINES row on first measurement; nothing on this bed is
   comparable to ERB rows (different corpus, own baseline arm first).

**Status: recommendation only — awaiting user approval before download +
adapter + ingest are scheduled** (ties up the rig for hours to a day).
