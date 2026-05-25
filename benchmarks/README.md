# Benchmarks

This directory holds Helix Context's benchmark harness — the scripts that
measure retrieval correctness, compression ratio, cache hit-rate, latency,
and end-to-end agent answer quality against frozen genome fixtures. It has
~40 scripts and no single entry point; this README orients a reader and
points at the pieces that matter. Most benches assume a running Helix
server (`helix-server`, port 11437) and read from `genomes/`.

## Flagship: `bench_claude_matrix.py`

The matrix runner is the headline bench. It runs **50 needles** across the
**6 frozen genome fixtures** (`small`, `medium`, `large`, `xl`, plus
`medium-sharded` and `xl-sharded`), answering each needle with a real
`claude -p` subprocess (Haiku by default) wired to the helix-context MCP.
Per needle it also calls `/context` directly for a retrieval-only metric,
then scores the agent answer in `{-1, 0, +1}`.

```bash
python benchmarks/bench_claude_matrix.py                       # full matrix
python benchmarks/bench_claude_matrix.py --only small,medium   # subset of fixtures
python benchmarks/bench_claude_matrix.py --skip xl-sharded     # exclude a fixture
python benchmarks/bench_claude_matrix.py --model sonnet        # swap the answering model
python benchmarks/bench_claude_matrix.py --max-usd 0.20        # per-question budget cap
python benchmarks/bench_claude_matrix.py --external-server     # use a server you started
```

By default the harness manages the uvicorn server itself; pass
`--external-server` to drive a server you started manually (required when
setting `HELIX_USE_SHARDS=1` by hand).

> **This bench spends real money.** Each needle spawns a `claude -p`
> subprocess against the Anthropic API. A full single-model arm costs
> roughly **$19**. Use `--only` / `--skip` and `--max-usd` to bound spend
> while iterating.

Results are written, no-overwrite, to a per-run directory:
`benchmarks/results/claude_matrix_<UTC-timestamp>/` — one JSONL per
fixture plus `summary.json` and `run.log`.

## Supporting pieces

- **`bench_orchestrator.py`** — manages the uvicorn lifecycle and
  fixture hot-swap across the 6-fixture matrix. Same-mode switches
  (blob→blob, sharded→sharded) use an atomic `POST /admin/swap-db`;
  cross-mode switches (blob↔sharded) require a full uvicorn restart with
  `HELIX_USE_SHARDS` toggled, since sharding mode is fixed at
  store-construction time. Callers just say "switch to fixture X". Also
  usable as a standalone CLI to loop other benches over the matrix.
- **`scripts/build_fixture_matrix.py`** — builds the genome fixtures
  (monolithic blobs and sharded variants) per
  `docs/benchmarks/GENOME_FIXTURE_MATRIX.md`.
- **`scripts/freeze_matrix_manifest.py`** — writes the `frozen.json`
  manifest the matrix runner reads to locate each fixture's `.db`.
- **Fixtures** live under `genomes/bench/matrix/` (blobs) and
  `genomes/bench/matrix-sharded/` (sharded). They are not checked in;
  build them with the two scripts above.

## The "needle" concept

A **needle** is a single fact-lookup query plus two match lists:

- `gold_source` — case-insensitive path substrings. A needle is
  `gold_delivered` when **any** entry matches a delivered citation
  source. Retrieval is scored on whether a gold doc was delivered, not
  on the answer text.
- `accept` — answer substrings used to score the agent's answer
  independently of retrieval.

Needles carry multiple valid `gold_source` entries because a fact often
lives in several legitimately-correct files. The curation rationale, the
per-needle table, and the rules for adding a needle are in
[`docs/benchmarks/MULTI_VALID_GOLD.md`](../docs/benchmarks/MULTI_VALID_GOLD.md).

## Other benches

The directory also holds many single-purpose benches — compression
ratio, cache hit-rate, latency, RAG-vs-Helix token comparison,
multi-needle recall, and others. They are mostly self-documenting via
their module docstrings; read the top of any `bench_*.py` for its
purpose and usage. Background and methodology notes live in
[`docs/benchmarks/`](../docs/benchmarks/).
