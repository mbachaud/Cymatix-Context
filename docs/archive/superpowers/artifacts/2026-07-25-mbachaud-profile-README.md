# Michael Bachaud

> I architect local-first AI systems and direct implementation agents through
> specifications, adversarial QA, instrumentation, and benchmark-gated iteration.

I don't hand a prompt to a model and ship what comes back. I run an adversarial
engineering loop and hold the authority at every gate:

```text
hypothesis → specification → implementation agents → instrumentation
  → benchmark → falsification → diagnosis → revised architecture
```

The receipts for that loop are in the issue trackers, not just the code:
documented null results, isolated host variance, frozen benchmark versions, and
issues I file against my own flagship features when the measurements falsify them.

## What's here

| Tier | Repo | What it is |
|------|------|-----------|
| 🚩 **Flagship** | [Cymatix Context](https://github.com/mbachaud/Cymatix-Context) | Local-first context/retrieval engine for LLM agents — no LLM call on the retrieval path. Shipped Python package; CLI + HTTP + MCP; ~3,400 tests; reproducible benchmark receipts. |
| 🔧 **Operational tooling** | [ScoreRift](https://github.com/mbachaud/scorerift) | Dual-layer audit: automated scoring + manual grading + reconciliation. The product isn't the score — it's watching the gap between them. |
| 🏗️ **Applied-architecture demos** | [DriftWatch](https://github.com/mbachaud/DriftWatch), [BookKeeper](https://github.com/mbachaud/BookKeeper) | Reference architectures — SOC 2 draft-generation assist, and a double-entry ledger core. Architecture samples, not certified/production products. |
| 🧪 **AI-development laboratory** | [BigEd](https://github.com/mbachaud/BigEd) | Explicitly *not a product* — a lab for local-first multi-agent orchestration on ~11 GB VRAM edge hardware, file-based handoffs, HITL gates. |
| ↗️ **External contribution** | [archestra-ai/archestra#4827](https://github.com/archestra-ai/archestra/pull/4827) *(merged)* | Tool-approval outcomes weren't surviving page reload; found, diagnosed, and fixed in someone else's unfamiliar architecture (+377/−45). Evidence the method transfers beyond my own stack. |
| 🔱 **Forks & benchmark fixtures** | headroom, AssetOpsBench, anker-solix, archestra-fork | Upstreams I build on or benchmark against. Labeled as forks, not pinned as my work. |

## How I split the work with models

**Human (me):** product thesis, constraints, architecture selection, acceptance
criteria, experiment design, risk decisions, QA and falsification authority.
**Models:** implementation, refactoring, draft documentation, test generation,
code-review lenses.

I don't claim code authorship I don't have — and I don't donate the architecture
authorship I do.
