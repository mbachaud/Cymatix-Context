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
- **Retrieval since then.** On the 947,531-chunk ERB bed, the shipped defaults now deliver the gold document in **66.8%** of 470 questions. That is our own retrieval-layer metric on our own bed build — related to, but not the same as, the leaderboard's Document Recall column — and not a judged end-to-end score, so it does not replace the leaderboard number. Receipt: [v0.10.0 witness](benchmarks/dogfood/receipts/sweep_v0100_witness_947k_2026-09-29.json).
- **Token cost.** On that same bed, a shipped-default packet is **8,345 tokens** on average (tokenizer-counted, table below).

**Packet cost at scale.** These are tokenizer-counted (tiktoken `o200k_base`) means over 500 EnterpriseRAG questions on the 947,531-chunk bed, using saved packets:

| Packet | Tokens per packet (mean / p90) |
|---|---|
| Shipped default: compressed, 12 seats, `expression_tokens = 7000` | **8,345** / 9,197 |
| Full text, 12 seats | 12,650 / 13,804 |
| Opt-in 12 + 4 companion profile | 15,917 / 17,398 |

The 7,000 budget is a characters-over-four estimate, so a full 12-seat packet runs about 19% over it. The shipped default cuts about a third of these queries to 6 seats (#430), so its real average is at or below the 12-seat row. See the [write-up](docs/benchmarks/2026-09-29-packet-token-cost.md) and its [receipt](benchmarks/dogfood/erb/receipts/packet_token_cost_947k_2026-09-29.json). Multi-turn session delivery elides documents a session has already received; its savings are an **unverified design estimate** pending the `cymatix_session_tokens_saved_total` counter.

**Internal benchmark board (retrieval layer, shipped defaults).** Every row is one committed receipt; beds differ, so compare rows only down a column's meaning, never as one averaged score. *Delivered* = gold document inside the delivered window; *r@12* = gold in the top-12 score map; *final r@12* = gold in the top-12 final order.

| Corpus | Lane | n | Delivered | r@12 | Final r@12 | Receipt |
|---|---|---:|---:|---:|---:|---|
| EnterpriseRAG-Bench, 947,531 chunks | enterprise docs | 470 | **0.668** | 0.681 | 0.683 | [v0.10.0 witness](benchmarks/dogfood/receipts/sweep_v0100_witness_947k_2026-09-29.json) |
| EnterpriseRAG-Bench, 100k carve | enterprise docs | 141 | **0.667** | 0.702 | 0.702 | [ladder](benchmarks/dogfood/erb/receipts/ladder_erb100k_beta_seed0_2026-09-04.json) |
| EnronQA v2 | email | 500 | **0.856** | 0.876 | 0.872 | [ladder](benchmarks/dogfood/enronqa/receipts/ladder_enronqa_v2_beta_seed0_2026-09-04.json) |
| EnronQA, padded | email | 500 | **0.792** | 0.808 | 0.816 | [ladder](benchmarks/dogfood/enronqa_padded/receipts/ladder_enronqa_padded_beta_seed0_2026-09-04.json) |
| LoCoMo | conversation memory | 2,378 | **0.435** | 0.486 | 0.487 | [ladder](benchmarks/dogfood/locomo/receipts/ladder_locomo_beta_seed0_2026-09-04.json) |
| MULoc | multi-doc localization | 680 | **0.443** | 0.484 | 0.488 | [ladder](benchmarks/dogfood/muloc/receipts/ladder_muloc_beta_seed0_2026-09-04.json) |
| FinanceBench | financial filings | 150 | **0.153** | 0.153 | 0.153 | [ladder](benchmarks/dogfood/financebench/receipts/ladder_financebench_beta_seed0_2026-09-04.json) |
| CodeRAG-Bench, solutions | code | 663 | **0.971** | 0.982 | 0.982 | [ladder](benchmarks/dogfood/coderag_solutions/receipts/ladder_coderag_solutions_beta_seed0_2026-09-04.json) |
| SWE-bench | code localization | 489 | **0.599** | 0.638 | 0.640 | [ladder](benchmarks/dogfood/swebench/receipts/ladder_swebench_beta_seed0_2026-09-04.json) |
| CosQA | code search | 500 | **0.286** | 0.362 | 0.358 | [ladder](benchmarks/dogfood/cosqa/receipts/ladder_cosqa_beta_seed0_2026-09-04.json) |
| CodeRAG-Bench, library docs | code docs | 709 | **0.212** | 0.227 | 0.227 | [ladder](benchmarks/dogfood/coderag_docs/receipts/ladder_coderag_docs_beta_seed0_2026-09-04.json) |

The 947k row is the v0.10.0 release witness (`a5a5dbef`). It exactly reproduces the v0.9.2 witness, which in turn exactly reproduced the frozen floor-12 reference. The other rows are the beta witness sweep at `21606a0` (2026-09-04, shipped `cymatix.toml`, `PYTHONHASHSEED=0`, `CYMATIX_DISABLE_LEARN=1`). v0.10.0 adds only opt-in delivery and migration features, and these rows have not been re-run on it. MULoc moves by a few needles between hash seeds (0.443–0.449 across receipts). The weak rows are real: FinanceBench, CosQA and library-docs retrieval are open problems, not tuned-away ones.

Release-by-release history (the 0.9.x default flips and their paired receipts), the modeled May 2026 token-economics table, older end-to-end runs, the sharded gap ([#275](https://github.com/mbachaud/Cymatix-Context/issues/275)), and the dense-off latency disclosure ([#374](https://github.com/mbachaud/Cymatix-Context/issues/374)) all live on [Benchmarks and Receipts](https://github.com/mbachaud/Cymatix-Context/wiki/Benchmarks-and-Receipts).

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
