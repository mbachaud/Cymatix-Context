# Faithfulness experiment — does the model *causally use* helix's injected context?

**Date:** 2026-07-06 · **Tool:** Neuronpedia (hosted Qwen3‑27B SAE features / J‑lens) · **Feeds:** #239 (know/miss recalibration)
**Status:** runbook (manual, browser-driven) — the re-aimed successor to the abandoned "splice meaning-loss" experiment.

## Why this experiment (and why not the first one)

The original idea was to use Neuronpedia's J‑lens to check whether helix's *splice* step
destroys meaning (gold document delivered but answer string absent). Investigation on
2026‑07‑06 dissolved that premise: of 23 such needles, **17 were a bench-metric artifact**
(`body_has_answer` under-counts because the citation→body parser fails closed to empty
bodies under the legibility-off probe — the answer was in the delivered content all along),
and the remaining 6 were plain retrieval misses. **Zero were semantic meaning-loss.** J‑lens
tracks whether a *concept* is "on the model's mind"; a lost port number isn't a concept, so
that experiment would have measured nothing.

The **valid** interpretability question — the one J‑lens is actually built for — is
**faithfulness**: when the model answers, is it *using helix's injected evidence*, or
pattern-matching from its own parametric memory? This is the mechanistic ground-truth we
need to recalibrate the know/miss contract (#239, currently an anti-signal at AUC 0.35–0.44).

## Design — with/without-context activation control

For a needle whose answer is a **project-internal fact the base model cannot know**, run two
conditions through Neuronpedia's hosted Qwen3‑27B and compare which interpretable features
fire on the answer:

- **Condition A (parametric baseline):** paste the **question only**.
- **Condition B (helix-injected):** paste the **answer-bearing helix context snippet + the
  question**.

**Hypothesis / success criterion:** for project-internal facts, the answer concept fires in
**B but not A**. That is the causal signature — the model had no parametric basis (A), and
helix's context supplied it (B). If it fires in A too, the model already knew it (helix added
nothing for that fact). If it fires in *neither* B, helix delivered the token but not in a
form the model can use — a real splice/format problem worth a separate look.

The cleanest needles are **helix-internal** (ports, counts, custom library choices) — the base
model provably can't know them, so A is a true negative baseline.

## Worked needles (ready to paste)

### 1. `helix_headroom_port` — flagship (answer: **8787**)

Base Qwen3 cannot know Helix's Headroom proxy port. If it surfaces 8787 only under B, that is
unambiguous causal use.

**Condition A (paste this):**
```
What is the default port for the Headroom proxy that Helix can route through?
```

**Condition B (paste this):**
```
claude_base_url: str = ""   # Proxy URL (e.g. Headroom at http://127.0.0.1:8787); "" = direct

What is the default port for the Headroom proxy that Helix can route through?
```

**Look for:** on the token `8787` (Condition B), inspect the top-activating SAE features and
the J‑space patterns. Does a "port / network address" feature fire, tied to the injected line?
In Condition A, does the model surface any `8787`-related activation at all (expected: no)?

### 2. `cosmictasha_postgres_version` — clean value-in-config (answer: **16**)

**Condition A:**
```
What major version of PostgreSQL does CosmicTasha use in production?
```

**Condition B:**
```
# PostgreSQL
postgres:
  image: postgres:16-alpine
  restart: unless-stopped

What major version of PostgreSQL does CosmicTasha use in production?
```

**Look for:** whether the "PostgreSQL / version" concept features fire and bind to the
`16` in `postgres:16-alpine` under B, versus A where the model must guess.

## Navigating Neuronpedia

1. Open the Qwen3 model on Neuronpedia (the shared J‑lens link is one view of it:
   `neuronpedia.org/qwen3.6-27b/jlens`). The model must be an **open** hosted model — this
   works for Qwen3 / Gemma (our *local* ollama rungs), **not** Claude/Sonnet (closed, no SAEs).
2. Use the text-input / activation-testing surface (labelled "Search", "Test Text", or the
   J‑lens input depending on the current UI): paste **Condition A**, run it, and note the
   top-firing features on the last tokens / the answer region.
3. Paste **Condition B**, run it, and compare. The diff (B minus A) on the answer concept is
   the faithfulness signal.
4. If the J‑lens "DirectedModulation" control is available, suppress the feature(s) that lit
   up on the injected snippet and re-run B — if the answer collapses, that is *causal* (not
   just correlational) use.

## Honest scope & caveats

- **Open models only.** Measures the local rung (Qwen3/Gemma). Claude is closed. Since
  retrieval is model-independent, faithfulness studied on the open rung is a reasonable proxy
  for the context helix hands any model — but it is not a direct Claude measurement.
- **Concept vs value answers.** J‑lens/SAE features track *concepts*. Arbitrary values (ports,
  counts) have no clean concept feature — track the *surrounding* concept instead ("port",
  "PostgreSQL version") and whether it binds to the injected value.
- **The metric bound.** `content_has_answer` (token match on delivered content) is an *upper*
  bound on deliverability and can false-positive: e.g. `mek_source_apps` answer "three" also
  matches "**Three** guardrails" (wrong referent). `body_has_answer` is a *lower* bound
  (parser under-count). Truth is between them — this experiment probes whether the model
  actually *uses* the delivered token, which cuts through that ambiguity.
- **Diagnostic, not a CI metric.** Per-needle attribution is expensive and manual. Use it to
  *explain* (does the model use our context?), then encode the lesson cheaply; do not put it in
  the bench loop.

## Rigorous stretch (local, automatable)

The web UI gives the correlational version. For the causal proof, run the **matching
unquantized Qwen3 checkpoint** locally via **nnsight + SAELens** against the Neuronpedia SAE:
feed Condition A/B, take the answer logit, and **ablate the evidence features** — if the
answer probability collapses only when helix's injected features are ablated, helix context is
causally load-bearing. That harness can reuse our needle set and the `/context` capture
(`scripts/bench_chain/s3_fts_depth_sweep.py` machinery) and would slot beside the s3 driver.

## Appendix — other captured answer-present needles

`biged_default_model` (qwen3 — note: the delivered chunk near the token is model-swap config,
so the *assertion* "default = qwen3" is weaker; a caution case), `biged_db_tables` (34, in a
`tables=34` metric line), `mek_source_apps` (three — the referent-ambiguity case above),
`helix_subpackages_count` (16). Payloads captured under the session scratchpad
(`faith_payloads.json`).
