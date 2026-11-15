# Benchmark index

Where each benchmark's pieces live. Read this before searching. Numbers live
elsewhere: the board in the top-level `README.md` holds the current headline
numbers, and `BASELINES.md` (this folder) is the authority on what may be
compared with what.

## Corpora

Two tiers. The **quick set** builds in minutes to an hour and is the default
when someone asks for "the smaller benchmarks". The **large set** takes hours
to build and should be run deliberately.

| Corpus | Tier | Needles and gold (`benchmarks/dogfood/<dir>/`) | Notes |
|---|---|---|---|
| LoCoMo | quick | `locomo/`: `needles_resolved_locomo_full.json`, `gold_by_needle_locomo.json` | Conversation memory. `locomo_obs/` is the observation-enriched variant |
| EnronQA v2 | quick | `enronqa/`: `needles_enronqa_pairs.json`, `gold_by_needle_enronqa.json` | Email. Native bed is about 85k chunks |
| FinanceBench | quick | `financebench/`: `needles_resolved_financebench_full.json` | Page-level gold over filings. Weak row, an open problem |
| CosQA | quick | `cosqa/`: `needles_resolved_cosqa_full.json` | Code search |
| CodeRAG-Bench solutions / library docs | quick | `coderag_solutions/`, `coderag_docs/` | Code |
| BEIR small sets (scifact, nfcorpus, fiqa, arguana, ...) | quick | `beir_<name>/`; runner `sweeps/run_beir_round1.py`; nDCG via `beir/beir_ndcg.py` | `cqadupstack` unpacks to about 15 GB |
| SWE-bench Verified | quick | `swebench/` | Code localization |
| MULoc | quick | `muloc/` | Multi-doc localization; moves a few needles between hash seeds |
| EnterpriseRAG-Bench (ERB) 947k | large | `erb/`: `gold_by_needle_blob.json`; harnesses in `erb/` and `scripts/bench_chain/` | The release witness bed. The official leaderboard export is `scripts/bench_chain/erb_official_answers_export.py` and the doc-id mapping is `erb_dsid_adapter.py` |
| ERB carves (10k, 100k, 250k, 500k) | large | `erb/carve_subset.py` | The 100k carve has its own row on the README board |
| EnronQA padded (about 597k) | large | `enronqa_padded/` | |
| Dogfood (own repos) | quick | `benchmarks/dogfood/README.md` | Gold pack is shareable |

Not wired: **ExtractBench** has no harness in this repo.

## Bed builds

- The builder is `scripts/build_fixture_matrix.py`. Profiles are defined in the
  file (search for `"label":`).
- **Bed locations are per install.** Max's box and Joe's GB10 use different
  paths. Many scripts default to `F:/tmp/<corpus>_bed/` or `F:\Projects\...`
  (the authoring box), so pass explicit paths on any other machine. Record each
  bed's path, gene count, `ingest_c` and tagger version in your local notes.
- The receipts repo's `ledger/ledger.json` (`mbachaud/cymatix-receipts`) lists
  30 beds with family, gene count, `ingest_c`, tagger version and the authoring
  box path. It was built 2026-09-02 and does not yet include the October grain
  ladder beds.
- Cross-bed comparison rules (`ingest_c`, tagger version, frozen tags) are in
  `BASELINES.md`.

## Profiles

| Name | Meaning | Where |
|---|---|---|
| shipped | `cymatix.toml` at repo root, untouched | repo root |
| c4000 / c1824 / c580 | Chunk grain in characters. Shipped beds are c4000 | `benchmarks/dogfood/grain_ladder/configs/grain_c*.toml` |
| `*_classcap` | Classifier caps sized in characters (`classifier_cap_ref_chars`) | same |
| `*_charcap`, `*_widecap` | Other cap variants used by the grain ladder | same |
| Standard bench profile | GPU encoder daemon on, per-box worker counts | `BASELINES.md`, "Standard bench profile" |

"The c4000 profile" is ambiguous: it can mean the shipped config on a c4000
bed, or `grain_c4000_classcap.toml`. Name the TOML file.

## Runners

| What | Script |
|---|---|
| Retrieval ladder (delivered, r@12, final r@12) | `benchmarks/dogfood/run_ladder.py`; sweep wrappers in `benchmarks/dogfood/sweeps/` |
| Grain comparisons | `benchmarks/dogfood/grain_ladder/compare_grain_matched.py` |
| Answer A/B with a model in the reader seat | `benchmarks/dogfood/jev/sonnet_answer_ab.py` (runs `claude -p`) |
| Packet token cost | `benchmarks/dogfood/erb/packet_token_cost.py` |
| Harness overview | `benchmarks/README.md` |

Standard env for a reproducible ladder: `PYTHONHASHSEED=0`,
`CYMATIX_DISABLE_LEARN=1`, and no stray `CYMATIX_ENCODER_URL` or
`CYMATIX_CONFIG` unless the profile calls for them.

## Judges

The judge has varied with the week and available capacity. **Record the
answerer and judge model in every answer-quality receipt.** As of 2026-11-14:

- **ERB official validation:** GPT-5.4 via the OpenAI API, ERB only. It is no
  longer available on the subscription. The ERB scorer lives in the external
  `EnterpriseRAG-Bench` checkout (`src/scripts/answer_evaluation/`).
- **Internal judging:** Sonnet 5.5 recently.
- **Pre-check:** Sonnet 5 has been used to grade a Sonnet 5 run before
  re-judging with a different judge. A pre-check score is not a reportable
  result.

Retrieval-only ladders need no judge.

## Receipts

Receipt trees are gitignored here and live in `mbachaud/cymatix-receipts`:
`mirror/` (exported from a pinned commit) and `untracked/` (archived from a
working tree with its `scripts/archive_working_receipts.py`). A receipt is
citable only once `BASELINES.md` has a row for it.
