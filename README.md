# Cymatix Context

[![Website](https://img.shields.io/badge/website-cymatixcontext.com-ff9f43.svg)](https://cymatixcontext.com)
[![Discord](https://img.shields.io/badge/discord-join-5865F2.svg?logo=discord&logoColor=white)](https://discord.gg/pX7x7pA3Da)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![PyPI version](https://img.shields.io/pypi/v/cymatix-context.svg)](https://pypi.org/project/cymatix-context/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 4000+](https://img.shields.io/badge/tests-4000%2B-brightgreen.svg)](tests/)
[![LLM-free pipeline](https://img.shields.io/badge/pipeline-LLM--free-brightgreen.svg)](docs/architecture/PIPELINE_LANES.md)

> **Same query, same answer: deterministic RAG for agents.** Retrieves,
> weighs, and compresses your codebase and documents into a context window —
> without a single LLM call on the retrieval path.

One SQLite knowledge store on your own machine, a seven-stage pipeline, and an explicit `know` / `miss` contract on every response, so an agent can tell grounded context from a miss instead of guessing. *The engine's namesake cymatics stage — an MD5-binned 256-dimensional term spectrum — is a candidate-reordering signal that has **not yet been isolated** against hashed bag-of-words or random-bin controls; treat it as an experimental cheap feature, not a proven one.*

## Get started

Python 3.11+. The core install is dependency-light (FastAPI + SQLite, no torch); extras add what you turn on — `cpu` (spaCy ingest tagging), `mcp`, `embeddings` (opt-in dense recall, pulls torch), `ast`, `otel`, `launcher-tray`, `all`. Full extras matrix, GPU detection, and three worked workflows: [Getting Started](https://github.com/mbachaud/Cymatix-Context/wiki/Getting-Started) · [docs/SETUP.md](docs/SETUP.md).

```bash
pip install "cymatix-context[cpu,mcp]"               # recommended working set, no torch
python -m spacy download en_core_web_sm              # ingest tagger model

cymatix ingest path/to/your/project/ --recursive     # 1. build the store
cymatix query "how does the splice step work?"       # 2. ask it — no server
cymatix packet "edit the splice step" --task-type edit --json   # 3. agent bundle
cymatix-server                                       # 4. proxy on 127.0.0.1:11437
```

Dense recall is off by default since 2026-08-15 ([receipts](docs/benchmarks/2026-08-14-encoder-isolation-scale-curve.md)); add the `embeddings` extra only if you opt back in.

## Proof

Three numbers, each with a receipt:

- **Public leaderboard.** On [EnterpriseRAG-Bench](https://huggingface.co/spaces/onyx-dot-app/EnterpriseRAG-Bench-Leaderboard) (Onyx; every entry judged by GPT-5.4), Cymatix scores **Overall 33.93%** — Correctness 42.2%, Completeness 42.74%, Document Recall 50.7% — rank 23 of 26 as of the 2026-09-18 update. That entry was measured on **v0.6.3/0.6.4** (v0.6.3 is the frozen external-validation snapshot) with zero LLM calls on the retrieval path. Later versions have not been resubmitted.
- **Retrieval since then.** On the 947,531-chunk ERB bed, the shipped defaults now deliver the gold document in **66.8%** of 470 questions. That is our own retrieval-layer metric on our own bed build — related to, but not the same as, the leaderboard's Document Recall column — and not a judged end-to-end score, so it does not replace the leaderboard number. Every release from v0.10.0 through v0.11.4 reproduced it exactly (0/470 per-needle differences). Receipts: [v0.10.0 witness](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/receipts/sweep_v0100_witness_947k_2026-09-29.json) and the per-release witness rows in [BASELINES.md](docs/benchmarks/BASELINES.md).
- **Token cost.** On that same bed, a shipped-default packet is **8,345 tokens** on average (tokenizer-counted, table below).

**Packet cost at scale.** These are tokenizer-counted (tiktoken `o200k_base`) means over 500 EnterpriseRAG questions on the 947,531-chunk bed, using saved packets:

| Packet | Tokens per packet (mean / p90) |
|---|---|
| Shipped default: compressed, 12 seats, `expression_tokens = 7000` | **8,345** / 9,197 |
| Full text, 12 seats | 12,650 / 13,804 |
| Opt-in 12 + 4 companion profile | 15,917 / 17,398 |

The 7,000 budget is a characters-over-four estimate, so a full 12-seat packet runs about 19% over it. The shipped default cuts about a third of these queries to 6 seats (#430), so its real average is at or below the 12-seat row. See the [write-up](docs/benchmarks/2026-09-29-packet-token-cost.md) and its [receipt](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/erb/receipts/packet_token_cost_947k_2026-09-29.json). Multi-turn session delivery elides documents a session has already received; its savings are an **unverified design estimate** pending the `cymatix_session_tokens_saved_total` counter.

**Internal benchmark board (retrieval layer, shipped defaults).** Every row is one committed receipt; beds differ, so compare rows only down a column's meaning, never as one averaged score. *Delivered* = gold document inside the delivered window; *r@12* = gold in the top-12 score map; *final r@12* = gold in the top-12 final order.

| Corpus | Lane | n | Delivered | r@12 | Final r@12 | Receipt |
|---|---|---:|---:|---:|---:|---|
| EnterpriseRAG-Bench, 947,531 chunks | enterprise docs | 470 | **0.668** | 0.681 | 0.683 | [v0.10.0 witness](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/receipts/sweep_v0100_witness_947k_2026-09-29.json), reproduced through v0.11.4 |
| EnterpriseRAG-Bench, 100k carve | enterprise docs | 141 | **0.667** | 0.702 | 0.702 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/erb/receipts/ladder_erb100k_beta_seed0_2026-09-04.json) |
| EnronQA v2 | email | 500 | **0.856** | 0.876 | 0.872 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/enronqa/receipts/ladder_enronqa_v2_beta_seed0_2026-09-04.json) |
| EnronQA, padded | email | 500 | **0.792** | 0.808 | 0.816 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/enronqa_padded/receipts/ladder_enronqa_padded_beta_seed0_2026-09-04.json) |
| LoCoMo | conversation memory | 2,378 | **0.435** | 0.486 | 0.487 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/locomo/receipts/ladder_locomo_beta_seed0_2026-09-04.json) |
| MULoc | multi-doc localization | 680 | **0.443** | 0.484 | 0.488 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/muloc/receipts/ladder_muloc_beta_seed0_2026-09-04.json) |
| FinanceBench | financial filings | 150 | **0.153** | 0.153 | 0.153 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/financebench/receipts/ladder_financebench_beta_seed0_2026-09-04.json) |
| CodeRAG-Bench, solutions | code | 663 | **0.971** | 0.982 | 0.982 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/coderag_solutions/receipts/ladder_coderag_solutions_beta_seed0_2026-09-04.json) |
| SWE-bench | code localization | 489 | **0.599** | 0.638 | 0.640 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/swebench/receipts/ladder_swebench_beta_seed0_2026-09-04.json) |
| CosQA | code search | 500 | **0.286** | 0.362 | 0.358 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/cosqa/receipts/ladder_cosqa_beta_seed0_2026-09-04.json) |
| CodeRAG-Bench, library docs | code docs | 709 | **0.212** | 0.227 | 0.227 | [ladder](https://github.com/mbachaud/Cymatix-Context/blob/e3825e4b0463069e5d23f4d0a9f429f9ba7d651e/benchmarks/dogfood/coderag_docs/receipts/ladder_coderag_docs_beta_seed0_2026-09-04.json) |

The 947k row is the v0.10.0 release witness (`a5a5dbef`). It exactly reproduces the v0.9.2 witness, which in turn exactly reproduced the frozen floor-12 reference. The v0.11.1, v0.11.2, v0.11.3 and v0.11.4 release witnesses each reproduced it again (EXACT_REPRODUCE, +0/−0 delivered; v0.11.3 and v0.11.4 ran with packet tracing on). Their raw ladders live in the private receipts archive; the rows in [BASELINES.md](docs/benchmarks/BASELINES.md) record commit, config hash and bed identity. The other rows are the beta witness sweep at `21606a0` (2026-09-04, shipped `cymatix.toml`, `PYTHONHASHSEED=0`, `CYMATIX_DISABLE_LEARN=1`). Releases since then add only opt-in features, and these rows have not been re-run on them. Receipt links point at commit `e3825e4b`, the last commit that tracked the receipt trees before they moved to the archive. MULoc moves by a few needles between hash seeds (0.443–0.449 across receipts). The weak rows are real: FinanceBench, CosQA and library-docs retrieval are open problems, not tuned-away ones.

Release-by-release history (the 0.9.x default flips and their paired receipts), the modeled May 2026 token-economics table, older end-to-end runs, the sharded gap ([#275](https://github.com/mbachaud/Cymatix-Context/issues/275)), and the dense-off latency disclosure ([#374](https://github.com/mbachaud/Cymatix-Context/issues/374)) all live on [Benchmarks and Receipts](https://github.com/mbachaud/Cymatix-Context/wiki/Benchmarks-and-Receipts).

### Experimental: chunk size, 580 vs 4,000 characters (October 2026)

Shipped beds cut files into chunks of up to 4,000 characters (c4000). The grain-size ladder rebuilt the same corpora at 580 characters (c580) and asked whether smaller chunks deliver more gold. Every row is single-seed (`PYTHONHASHSEED=0`) and joined by query text. The beds are tagger v2 with lean bench writes and dense/SPLADE off. c580 runs the opt-in `grain_c580_classcap.toml` profile: classifier caps sized in characters (`classifier_cap_ref_chars`), ladder k=83. Two c4000 controls run on the same c4000 bed: the same character-capped policy (`grain_c4000_classcap.toml`, k=12) and the shipped `cymatix.toml`.

| Corpus | n | c580 classcap | c4000 classcap (paired) | c4000 shipped (paired) | chars/packet, c580 vs shipped | Receipts |
|---|---:|---:|---:|---:|---:|---|
| EnterpriseRAG-Bench, 947k | 470 | **0.638** | 0.619 (+34/−25) | — | 19.0k vs 17.4k¹ | [matched](benchmarks/dogfood/grain_ladder/erb_matched_c580_vs_c4000_classcap_2026-10-19.json) |
| EnronQA v2 | 500 | **0.886** | 0.816 (+54/−19) | 0.836 (+43/−18) | 20.3k vs 21.1k | [classcap](benchmarks/dogfood/grain_ladder/enronqa_matched_c580_vs_c4000_classcap_2026-10-20.json), [shipped](benchmarks/dogfood/grain_ladder/enronqa_matched_c580_vs_c4000_shipped_2026-10-20.json) |
| LoCoMo | 2,366 | **0.608** | 0.486 (+289/−0) | 0.435 (+410/−0) | 16.0k vs 3.7k | [classcap](benchmarks/dogfood/grain_ladder/locomo_matched_c580_vs_c4000_classcap_2026-10-20.json), [shipped](benchmarks/dogfood/grain_ladder/locomo_matched_c580_vs_c4000_shipped_2026-10-20.json) |
| CosQA | 500 | **0.618** | 0.362 (+128/−0) | 0.286 (+166/−0) | 20.3k vs 6.0k | [classcap](benchmarks/dogfood/grain_ladder/cosqa_matched_c580_vs_c4000_classcap_2026-10-20.json), [shipped](benchmarks/dogfood/grain_ladder/cosqa_matched_c580_vs_c4000_shipped_2026-10-20.json) |
| SWE-bench | 489 | 0.554 | 0.374 (+102/−14) | **0.599** (c580 +39/−61) | 10.1k vs 25.6k | [classcap](benchmarks/dogfood/grain_ladder/swebench_matched_c580_vs_c4000_classcap_2026-10-20.json), [shipped](benchmarks/dogfood/grain_ladder/swebench_matched_c580_vs_c4000_shipped_2026-10-20.json) |
| MULoc | 680 | 0.418 | 0.257 (+125/−16) | **0.444** (c580 +41/−59) | 9.1k vs 25.6k | [classcap](benchmarks/dogfood/grain_ladder/muloc_matched_c580_vs_c4000_classcap_2026-10-20.json), [shipped](benchmarks/dogfood/grain_ladder/muloc_matched_c580_vs_c4000_shipped_2026-10-20.json) |
| BEIR SciFact | 300 | **0.727** | 0.703 (+20/−13)² | — | 8.3k vs 12.9k² | [summary](benchmarks/dogfood/grain_ladder/summary_2026-10-15_charcap.json) |
| BEIR NFCorpus | 323 | **0.687** | 0.659 (+17/−8)² | — | 8.2k vs 11.7k² | [summary](benchmarks/dogfood/grain_ladder/summary_2026-10-15_charcap.json) |

Paired counts are c580 wins/losses: needles where only one side delivered gold. ¹ ERB compares against the c4000 classcap bed, not the shipped witness bed (tagger v1, 0.668). Against that witness, c580 classcap is 0.638 (+25/−39) ([receipt](benchmarks/dogfood/grain_ladder/erb_full_c580_classcap_vs_c4000_reference_2026-10-17.json)). ² The BEIR rows use the 10-15 char-capped c4000 control (`grain_c4000_charcap.toml`) instead of a classcap control.

What this shows, and what it does not:

- **Most of the large gains come from packet size, not chunk size.** On LoCoMo and CosQA the c580 packet carries 4–5× the characters of the shipped packet. On ERB, with packets of similar size, c580 vs c4000 is +34/−25: net +9 of 470, inside single-seed noise. Most of the earlier +46/−24 against the tagger-v1 reference came from tagger v2 and classifier caps, not from grain ([receipt](benchmarks/dogfood/grain_ladder/erb_full_c580_classcap_vs_c4000_reference_2026-10-17.json)).
- **On the code beds c580 loses to the shipped config.** Its packets underfill (about 10k vs 25.6k characters), so SWE-bench and MULoc deliver less gold than shipped c4000. The c4000 classcap control sits far below shipped there, so the classcap pairs overstate c580 on code.
- **Latency moves both ways.** ERB p50 wall rose from 12.0 s to 46.9 s (3.9×; the c580 bed holds about 5.1M chunks). EnronQA fell from 2.3 s to 0.4 s, and SWE-bench from 6.9 s to 2.9 s against shipped.
- **Without char caps, c580 loses.** At the legacy seat caps, c580 delivered 0.590 vs 0.713 on SciFact and 0.591 vs 0.666 on NFCorpus ([summary](benchmarks/dogfood/grain_ladder/summary_2026-10-15.json)). Lean c580 with char caps lost on ERB, +28/−49 ([receipt](benchmarks/dogfood/grain_ladder/erb_full_c580_vs_c4000_reference_2026-10-16.json)).

**Nothing changed at the shipped defaults.** The knobs (`tier_tight_chars`, `tier_focused_chars`, `classifier_cap_ref_chars`, `CYMATIX_BFM_CHUNK_CHARS`) are opt-in and unreceipted for a default flip. Gaps: one seed, raw ladder JSONs held locally (the committed files are the paired comparisons), and the ERB c580 run predates the pinned scoring clock. Harness: [`benchmarks/dogfood/grain_ladder/`](benchmarks/dogfood/grain_ladder/).

### Experimental: an external packet judge (System One / Jev)

The opt-in `[system_one]` hook (default off; it sends the query and item text to the provider) was tested with TypeSafe's Jev on the c4000 classcap ERB packets. These rows judge the same packets and are not retrieval results:

- **Packet know/miss:** Jev AUROC **0.900** on ERB 947k, against 0.703 for cymatix `top_score` and 0.50 for the know/miss contract (which calls every ERB packet a miss). [Write-up](docs/benchmarks/2026-10-23-jev-system-one-packet-judge.md).
- **Answers from packets (Sonnet 5.5):** reordering items by Jev changed nothing measurable (96/100 paired labels identical). As a gate on committed answers, Jev reached AUROC 0.774 against 0.535 for `top_score`. [Write-up](docs/benchmarks/2026-10-23-sonnet-answer-jev-erb.md).
- **Crater needles (gold never delivered):** a search loop over the 947k bed, with Jev judging each search result, answered 34/50 with Sonnet 5.5 and 33/50 with Opus 5.5, against 0/50 one-shot. The gap between models is inside run-to-run variance. Write-ups: [Sonnet](docs/benchmarks/2026-10-23-sonnet-crater-search-jev.md), [Opus](docs/benchmarks/2026-10-25-opus-crater-search-jev.md).

### Opt-in: full-text companion profile (v0.10.0+)

Experimental. It keeps complete stored chunks and appends up to four question-ranked chunks from the same selected sources. It needs **v0.10.0 or later**; earlier versions log an unknown-key warning and ignore these settings. Enable it explicitly in `cymatix.toml`:

```toml
[budget]
full_text_delivery = true
expression_tokens = 25000
companion_chunks = 4
context_max_chars = 100000
```

For packet API/CLI/MCP calls, request `max_genes=12` (CLI: `--max-docs 12`). Structured packets expose supplements in `companions`, with their own freshness labels. It costs roughly twice the tokens of the default packet (see the table above); the compressed defaults are unchanged. [Settings, experimental evidence, and limitations](docs/research/2026-09-14-companion-release-settings.md).

## Pipeline

Seven stages per turn, all LLM-free except the optional splice. Stage by stage, and where the model boundary actually sits: [Pipeline](https://github.com/mbachaud/Cymatix-Context/wiki/Pipeline).

```
  query
    ▼
  0. Classify   rule-based: decoder mode + assembly cap
    ▼
  1. Extract    heuristic keyword + entity extraction
    ▼
  2. Retrieve   FTS5 BM25 + tags (+ opt-in BGE-M3 dense) + synonym expansion
    │           + co-activation + SR + cymatics 256-bin spectrum scoring,
    ▼           ranked via RRF (default) or additive fusion
  3. Re-rank    CPU classifier scores (optional)
    ▼
  4. Splice     Headroom Kompress (CPU) or LLM compressor (optional)
    ▼
  5. Assemble   token budget + legibility headers (fired tiers, confidence
  + Stage 7     ◆/◇/⬦, compression ratio) + freshness gate (stale/cold/
    ▼           superseded → miss) + session delivery (elide seen docs)
  6. Persist    query+response → knowledge store (background)
    ▼
  know { } or miss { }
```

## Surfaces

Three ways in, same retrieval primitives, same JSON shapes. Direct MCP needs neither the model proxy nor the tray — a healthy headless server is sufficient. Configuration lives in `cymatix.toml`; env vars use the `CYMATIX_*` prefix. Reference: [CLI](https://github.com/mbachaud/Cymatix-Context/wiki/CLI) · [HTTP API](https://github.com/mbachaud/Cymatix-Context/wiki/HTTP-API) · [MCP and IDE Integration](https://github.com/mbachaud/Cymatix-Context/wiki/MCP-and-IDE-Integration) · [Configuration](https://github.com/mbachaud/Cymatix-Context/wiki/Configuration).

| Surface | Best for | Example |
|---|---|---|
| **CLI** | Scripts, CI, cold-start agents | `cymatix document get abc123 --json` *(legacy: `cymatix gene get`)* |
| **MCP** | Claude Code, Codex, Gemini CLI, Antigravity | `python -m cymatix_context.mcp_server` · [client guides](docs/clients/cymatix-context.md) |
| **HTTP** | Continue IDE, `OPENAI_BASE_URL` redirect | `POST /context/packet` |

## The know/miss contract

- **`know { found, confidence }`** — the context is grounded; the agent may answer.
- **`miss { reason, escalate_to }`** — don't answer from the knowledge store; escalate, or refetch from `refresh_targets`. The freshness gate downgrades stale / cold / superseded results into a `miss`.
- **The shape is stable; the confidence is provisional.** The contract shape is load-bearing, but the `confidence` scalar is under active recalibration and is not yet a reliable trust signal on current internal beds ([#287](https://github.com/mbachaud/Cymatix-Context/issues/287), [#239](https://github.com/mbachaud/Cymatix-Context/issues/239)) — rely on `found` / `reason`. Full semantics: [Agent Contract](https://github.com/mbachaud/Cymatix-Context/wiki/Agent-Contract).

## Gotchas

- **Knowledge store path** is `genomes/main/genome.db`, not the project root. Delete it to start fresh; it auto-creates on first use.
- **The synonym map is critical.** "No relevant context" usually means the query keywords don't map to the tags assigned at ingest — add them under `[synonyms]`.
- **Session delivery** (`session_delivery_enabled = true`) elides already-delivered documents per session; the ~40% multi-turn saving is an unverified design estimate. Pass `ignore_delivered: true` in the `/context` body for benchmarks.
- **Sharded scale gap.** The sharded path trails the unsharded engine by ~31pp recall@10 / ~30pp MRR on the xl bed ([#275](https://github.com/mbachaud/Cymatix-Context/issues/275)) — prefer unsharded for accuracy-sensitive corpora.
- **The agent prompt fragment is load-bearing.** Without it, frontier models confabulate past a `miss`. Import `cymatix_context.agent_prompt.full_fragment()`.

## Observability

Optional Grafana/Tempo/Loki sidecar: `scripts\setup-grafana-telem.ps1` (Windows) or `scripts/setup-grafana-telem.sh` (Linux/macOS), dashboards at `localhost:3000`. Full surface: [docs/architecture/OBSERVABILITY.md](docs/architecture/OBSERVABILITY.md) · [wiki: Observability](https://github.com/mbachaud/Cymatix-Context/wiki/Observability).

## Documentation

The **[wiki](https://github.com/mbachaud/Cymatix-Context/wiki)** is the narrative documentation — 15 pages, [Troubleshooting](https://github.com/mbachaud/Cymatix-Context/wiki/Troubleshooting) included, also rendered at <https://cymatixcontext.com/wiki/>. Questions, and arguments about the receipts: [Discord](https://discord.gg/pX7x7pA3Da).

| Repo doc | What it is |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | The canonical install path, with every nuance |
| [docs/config-reference.md](docs/config-reference.md) | Every `cymatix.toml` key, default, and flip date |
| [docs/api/endpoints.md](docs/api/endpoints.md) | Full HTTP schema |
| [docs/clients/cli.md](docs/clients/cli.md) | Full CLI reference |
| [docs/benchmarks/BASELINES.md](docs/benchmarks/BASELINES.md) | The receipt ledger and its comparability rules |
| [wiki: Lexicon](https://github.com/mbachaud/Cymatix-Context/wiki/Lexicon) | Biology-to-software lexicon ([docs/ROSETTA.md](docs/ROSETTA.md) is now a stub that points there) |

Built on [spaCy](https://spacy.io/) NER, SQLite FTS5 BM25, [BGE-M3](https://huggingface.co/BAAI/bge-m3), [Kompress](https://huggingface.co/chopratejas/kompress-base), [Headroom](https://github.com/chopratejas/headroom), and the Howard 2005 TCM / Stachenfeld 2017 SR literature — full attributions in [NOTICE](NOTICE).

## How this was built

Cymatix Context is architected and QA-directed by Michael Bachaud. Implementation,
refactoring, draft documentation, and test generation are produced by AI coding
agents under spec- and benchmark-gated review. The human owns the product thesis,
architecture selection, acceptance criteria, experiment design, and falsification
authority; the models own the code production.

## License

[Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party attributions.
