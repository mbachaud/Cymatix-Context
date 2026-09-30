# Companion delivery release settings

Status: implementation is opt-in (`full_text_delivery=false`, 7,000 expression
tokens). The proposed final defaults below await the paired production gate;
the version update and default promotion are separate release steps.

The generalized companion rule uses question text and complete stored chunks
from already selected sources. It reads no EnterpriseRAG questions, answers,
gold mappings, or original source files. BM25 uses k1=1.2 and b=0.75, discounts
the first primary source rank by `1 + 0.25 * (rank - 1)`, and discounts each
accepted same-source addition by `1 + additions_already_selected`.

## Historical run versus production

The saved paired run used 12 frozen primary chunks plus up to four companions,
100,000 evidence characters, full stored bodies, and Sol medium for both answers
and judgments. Its runtime policy is in
`benchmarks/dogfood/erb/receipts/paired-companion-sol-erb947k-20260913-01/resolved-policy.json`.
The source retrieval configuration was loaded and recorded separately in
`benchmarks/dogfood/erb/receipts/packet-100k-sol-erb947k-20260912-01/effective-config.json`;
that receipt is a later load of the historical config, not an original retrieval
runtime dump. The saved primary packets were reused without new retrieval.

The historical result was 249/500 versus 284/500, 40 gains and five losses,
on the official 500-question objects with SHA-256
`cdc93d51fa410e2c62bfe3b7ab855afab69bbd582d93e3c1095cd99b0b4bc2f7`.
Launch SHA: `9f232aa8928be41ef1e9cf3918ede5ad90dcd46c`, 222 commits behind
origin/master at preflight. Bed: `.worktrees/erb-answers-20260910/genome-retry1.db`,
24,595,505,152 bytes, identity
`11a05b915f8c6d5597edfa1fb57bfa2ba104ec74006a2b48ad04263de998bda6`.
These are internal Sol rubric results from one paired realization, concentrated
in previously reviewed development cases. They are not a score for this release.

| Setting | Proposed production profile | Historical experiment |
|---|---|---|
| Primary assembly cap | 12, configurable per request | 12 frozen chunks |
| Full stored body delivery | `budget.full_text_delivery=true` | true |
| Maximum supplements | `budget.companion_chunks=4` | 4 |
| Serialized evidence character cap | `budget.context_max_chars=100000` | 100000 |
| Expression token estimate budget | `budget.expression_tokens=25000` | offline character cap |
| Ribosome token estimate allowance | 3000 | separate answer/judge prompts |
| Tier-seat floor | false, gated opt-in | true in reloaded source config |
| Wire format | legacy | legacy |
| Harmonic batching | false, gated opt-in | false |

Primary retrieval remains subject to existing score, model-class and tier cuts;
12 is a maximum, not a promise of 12 documents. The new defaults therefore do
not reproduce the historical packets exactly. The tier-floor, canonical-wire,
and harmonic-batching migrations remain opt-in pending their separate gates.

Stored sequence numbers replace source-file character offsets when breaking
companion score ties. Normal hot-document and party-attribution restrictions
also apply. Existing databases build a source index once on writable startup;
requests use indexed source lookup. Sharded stores resolve owning shards through
their fingerprint index. A missing source identity cannot produce companions.

## Delivery and compatibility

Full-text delivery never truncates a stored body. It preserves the primary
assembly order, appends supplements, and skips additions that do not fit. If
primary text alone exceeds a hard budget, complete trailing primary items yield
to the limit. Explicit small token budgets still apply; estimates are not a
provider tokenizer guarantee. Session elision remains active; use
`ignore_delivered=true` when full repeated evidence is required.

The expressed-context character cap includes its evidence wrappers, not the
separate decoder prompt. Structured packets count their serialized packet JSON;
outer HTTP metadata is separate. Structured packets expose additions in the
`companions` array after the primary `verified` and `stale_risk` groups. Each
companion retains its freshness status and source identity. Its relevance score
is zero rather than borrowing a primary retrieval score. Consumers should read
companions alongside primary items and respect each item's freshness status.

To restore compressed assembly, set:

```toml
[budget]
full_text_delivery = false
companion_chunks = 0
expression_tokens = 7000
```

No new generative model dependency is introduced. The release does not select
Sol for users; Sol was the external evaluator used in the experiment.

## Worktree disposition

The clean `issues-417-430-431` stack supplied the historical retrieval code and
adds opt-in migrations plus provenance-bound gate tools. The packet replay and
paired judge runners are research tools, not production dependencies. Separate
structured-ingestion, latency, and alternative retrieval worktrees are excluded.
Config-validation and test-dependency fixes are already in v0.9.2. Issue #453's
measurement-gate follow-up is not resolved by companion delivery.
