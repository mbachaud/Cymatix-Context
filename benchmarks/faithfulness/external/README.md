# External faithfulness runners (#239 campaign)

One-shot research runners from the 2026-07-07 faithfulness campaign, imported
2026-07-08 for durability (they previously lived only in `F:/Projects/np-graph/`
and in ephemeral Claude session scratchpads). **None of these run in the repo's
test surface or CI**, and most do not run in the helix venv at all.

## Two environments — do not mix

The campaign deliberately split across two dependency stacks that cannot
coexist (circuit-tracer pins conflict with the helix model stack):

- **np-graph venv** (`F:/Projects/np-graph/venv/`) — `circuit-tracer`,
  transformer-lens, CUDA torch. Windows pins that matter: `pandas==2.2.3
  numpy==2.1.3 pyarrow==21` (newer heap-corrupt on import) and always run
  `python -X utf8` (the `◆` in legibility headers breaks cp1252 JSON writes).
- **helix env** — the repo's normal environment. Scripts marked *helix* below
  import `helix_context` and/or the sibling modules in `benchmarks/faithfulness/`
  and were originally executed from a session scratchpad with copies of
  `needles_239*.py` / `located_n1000.py` on `sys.path`.

Several scripts hardcode absolute paths (`f:/Projects/helix-context/...`,
`f:/Projects/np-graph/...`). They are archived as run, not productionized —
expect to adjust paths before rerunning.

## Scripts

| script | env | role |
|---|---|---|
| `faith_local.py` | np-graph | local self-hosted `gen_graph` monkeypatched into the validated scoring; reruns the §1 ideal-context pilot on gemma-2-2b → `full_pilot_results.json` |
| `dump_expressed.py` | helix | §2 stage 1 — ingest the 6-fact bed, dump `build_context()` `expressed_context` per needle → `expressed_contexts.json` |
| `faith_local_realhelix.py` | np-graph | §2 stage 2 — graph the dumped `expressed_context` prompts on gemma-2-2b → `realhelix_local_results.json` |
| `build_bed_239.py` | helix | §3 stage 1 — 48-needle graded-distractor bed: real-file ingest, know-features + confidence + `answer_survived` → `needles_239_stage1.json` |
| `faith_239.py` | np-graph | §3 stage 2 — Qwen3-4B causal-use graphs (`--ids` subset + resume) → `needles_239_faith.json` |
| `refit_239.py` | helix | §3 stage 3 — shipped-vs-refit logistic comparison against the causal-use label |
| `build_bed_239b.py` | helix | §4 stage 1 — 72-needle 3-cell delivery-balanced bed (`--abstain` toggles the ratio-gate) → `needles_239b_stage1*.json` |
| `faith_239b.py` | np-graph | §4 stage 2 — competition-aware graphs (scores gold *and* competitor) → `needles_239b_faith.json` |
| `refit_239b.py` | helix | §4 stage 3 — balanced-bed refit + the imputed-vs-measured circularity check |
| `spike_239b_answerpresence.py` | helix | answer-presence spike — span-level scorers vs causal-use on the §4 bed → `spike_239b_results.json` |
| `b1_analysis.py` | helix | B1 operating-point sweep — recomputes know-confidence under candidate betas/floors against the §3+§4 beds (graph-free) |
| `scaled_needles.py` | either (data) | §6 scale-N 24-needle set — answers verified single-token in BOTH gemma-2-2b and Qwen3-4B tokenizers |
| `tok_probe.py` | np-graph | tokenizer probe backing that verification (single-token-with-leading-space pool for Qwen3-4B) |
| `faith_scaled.py` | np-graph | §6 scale-N runner, model-parametrized → `scaled_results_qwen3_4b.json` |

The §6 invocation as run (RTX 3080 Ti 12 GB, bf16, `lazy_encoder`,
`offload="cpu"`, ~65 s/graph):

```bash
python -X utf8 faith_scaled.py --model Qwen/Qwen3-4B \
    --transcoders mwhanna/qwen3-4b-transcoders --needles scaled --mfn 1024 --bs 24
```

Shared metric code (validated, lives in the parent directory and IS importable
from the helix env): `benchmarks/faithfulness/faithfulness_circuit_tracer.py`,
`needle_faithfulness_experiment.py`, `real_helix_faithfulness.py`,
`needles_239.py`, `needles_239b.py`.

Result artifacts land in
[`benchmarks/results/faithfulness/`](../../results/faithfulness/README.md);
the interpreting docs are listed there.
