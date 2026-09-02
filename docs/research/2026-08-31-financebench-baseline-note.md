# FinanceBench baseline — the doc-routing blackout (2026-08-31)

Receipt: `benchmarks/dogfood/financebench/receipts/ladder_financebench_baseline_2026-08-31.json`
(n=150, k=12, page-level gold, 73,759-gene tagger-v2 bed, cold-gated, p50 829 ms).

| type | delivered | r@12 | pool-absent |
|---|---:|---:|---:|
| novel-generated | 17/50 (.34) | .34 | 29 |
| domain-relevant | 6/50 (.12) | .12 | 42 |
| metrics-generated | **0/50** | .000 | 47 |
| overall | 23/150 (.153) | .153 | 118/127 misses |

Mechanism (differs from every other bed): a metrics query splits into a
DOC-IDENTITY half ("3M", "FY2018" — matching every page of the company's
filing, fragmented against the `3M_2018_10K` header token by tokenizer
separators) and a CONCEPT half ("capital expenditure", cash-flow statement —
matching the same page of ALL 368 filings). Single-field flattened FTS
cannot express the intersection, so fused mass never concentrates and gold
is pool-absent 93% of the time. This is the fielded-evidence-cards case in
its purest form: a doc-identity/title leg × a body/claims leg. The
`key_values` claims cell (design memo Stage 0) and a doc-identity column are
the natural first interventions; FinanceBench becomes the sharpest gate for
both. Caveats: pypdf table extraction is rough; per-page chunking splits
statements; both recorded as bed-design factors, not excuses — real
deployments face the same.
