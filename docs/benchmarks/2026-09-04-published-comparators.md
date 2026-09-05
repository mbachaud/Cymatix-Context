# Cymatix vs published retrieval systems on the same benchmarks (2026-09-04)

**Question.** After the beta witness sweep (`2026-09-04-beta-witness-sweep.md`) we hold
receipts on nine corpora that other people also publish retrieval numbers for. How does
cymatix-context's shipped, neural-free retriever compare with the systems those papers
report — BM25, dense embedders, hybrid stacks — and how comparable is each comparison?

**Method.** The cymatix column is recomputed from our own per-needle receipts in the
paper's metric form (document level, first gold gene, score-map rank; NDCG@10 with a single
relevant item; recall@k = first gold within k). The published column is copied from the
paper's own table by a research pass that was instructed to report only numbers read
verbatim and to write "not found" otherwise; every cell carries its source. **A published
number is comparable to ours only to the degree the setup column says**: corpus size,
retrieval unit, per-instance vs pinned snapshot, dedup, and query construction all differ
in places, and the house rule (`BASELINES.md`) still applies — receipts are compared within
their own row; this document is context, not a leaderboard.

Our matched foil is the calibration bridge: `benchmarks/dogfood/code/bm25_doc_foil.py` runs a
plain BM25 over exactly the files each bed ingested, so the distance between the paper's BM25
and our foil's BM25 is the distance between the paper's haystack and ours.

## 1. Cymatix numbers in the papers' metric forms

All from beta `21606a0`, shipped config, k = 12 delivery; rank axes deterministic under
`PYTHONHASHSEED=0`.

| bench (bed) | n | doc R@1 | R@5 | R@10 | R@12 | R@50 | NDCG@10 | MRR | delivered@12 | matched BM25 foil (NDCG@10 / R@12) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| ERB 947k (v09x bed) | 470 | 0.321 | 0.594 | **0.666** | 0.681 | 0.768 | 0.490 | 0.440 | 0.668 | — |
| ERB 100k carve | 141 | 0.355 | 0.596 | 0.695 | 0.702 | 0.801 | 0.513 | 0.460 | 0.667 | — |
| MULocBench 108k (pinned snapshots) | 680 | 0.159 | 0.346 | 0.457 | 0.484 | 0.649 | 0.292 | 0.251 | 0.443 | — |
| SWE-bench Verified (pinned snapshots) | 489 | 0.215 | 0.464 | 0.603 | 0.638 | 0.828 | **0.389** | 0.336 | 0.599 | 0.341 / 0.558 |
| RepoBench-R python cff / cfr (global rank, rec@10) | 400 / 400 | — | — | 0.290–0.315 (easy), 0.195–0.215 (hard) | — | — | — | — | — | in-harness BM25 0.525–0.530 (easy), 0.400–0.435 (hard) |
| CodeRAG solutions (humaneval + mbpp) | 663 | 0.300 | 0.816 | 0.970 | 0.982 | 1.000 | **0.624** | 0.516 | 0.971 | 0.986 / 1.000 |
| CodeRAG docs (ds1000 + odex) | 709 | 0.021 | 0.114 | 0.207 | 0.227 | 0.406 | **0.098** | 0.075 | 0.212 | 0.154 / 0.265 |
| CoIR CosQA (deduped, 6,271 docs) | 500 | 0.020 | 0.130 | 0.316 | 0.362 | 0.674 | **0.132** | 0.096 | 0.286 | 0.249 / 0.410 |
| LoCoMo + LoCoMo-Plus | 2,378 | 0.068 | 0.306 | 0.446 | 0.486 | 0.616 | 0.232 | 0.176 | 0.435 | — |
| EnronQA v2 (85k) | 500 | 0.250 | 0.664 | 0.840 | 0.876 | 0.956 | 0.522 | 0.430 | 0.856 | — |
| EnronQA padded (597k) | 500 | 0.276 | 0.672 | 0.792 | 0.808 | 0.902 | 0.518 | 0.438 | 0.792 | — |
| FinanceBench (page level) | 150 | 0.047 | 0.100 | 0.147 | 0.153 | 0.213 | 0.088 | 0.073 | 0.153 | — |

LoCoMo by category, doc R@5 / R@10: single-hop 0.396 / 0.593, temporal 0.406 / 0.550,
adversarial 0.341 / 0.516, multi-hop 0.320 / 0.452, commonsense 0.225 / 0.303,
cognitive 0.005 / 0.005.

## 2. Published comparators

### 2.1 CodeRAG-Bench — canonical-document retrieval, NDCG@10 (%)

Source: arXiv 2406.14497 v2 (26 Feb 2025), Table 3, cross-checked against v1 (identical
shared cells). Their pools: HumanEval and MBPP retrieve from their own solution sets (164 and
500 docs, one NL-problem-plus-solution document per task); DS-1000 (1,000 queries) and ODEX
(945) retrieve from the 34,003-page library-documentation store, averaging 1.4 and 1.2 gold
pages per query. Our bed holds all 1,128 programming solutions in one haystack (harder for
the solutions tasks), scores only the ds1000/odex rows that carry canonical docs (513 / 201),
and drops 3,032 pages under 50 bytes plus 9,128 duplicate pages.

| system | HumanEval | MBPP | DS-1000 | ODEX |
|---|---:|---:|---:|---:|
| BM25 (paper) | 100.0 | 98.6 | 5.2 | 6.7 |
| GIST-base / GIST-large | 98.0 / 100.0 | 98.0 / 98.9 | 12.0 / 13.6 | 12.1 / 28.0 |
| BGE-base / BGE-large | 99.7 / 98.0 | 98.0 / 99.0 | 10.8 / 8.9 | 22.0 / 11.5 |
| SFR-Embedding-Mistral (4096d) | 100.0 | 99.0 | 19.3 | 37.1 |
| Codesage-small / Jina-v2-code | 100.0 / 100.0 | 96.3 / 97.7 | 8.9 / 26.2 | 14.3 / 19.9 |
| OpenAI text-embedding-3-small / voyage-code-2 | 100.0 / 100.0 | 98.9 / 99.0 | 18.2 / 33.1 | 16.5 / 26.6 |
| **matched BM25 foil over our beds** | 99.8 | 98.2 | 16.8 | 11.8 |
| **cymatix beta, shipped config** | **62.7** | **62.3** | **10.4** | **8.5** |
| cymatix beta, tag lanes muted (diagnostic arm) | 83.8 | 88.4 | 12.9 | 9.8 |

Reading: on the two solutions tasks every published system, including BM25, is at 96–100
and cymatix's shipped config is at 62 — a top-1 loss the §4.2 diagnostic in the sweep doc
attributes to tag-plateau ties under RRF (muted: 87). On the two documentation tasks cymatix
sits above the paper's BM25 (10.4 / 8.5 vs 5.2 / 6.7) but below every dense embedder
(12–37); note our own BM25 foil already reaches 16.8 / 11.8 on our cleaned haystack, so about
half of that BM25 gap to the paper is haystack, not retriever. Efficiency context from their
Table 4: BM25 encodes a query in 0.15 ms with a 141 MB index; SFR-Mistral takes 316 ms and a
14.2 GB model — the tier cymatix competes in is the first one.

### 2.2 RepoBench-R — closed-pool ranking, acc@k (%)

Source: arXiv 2306.03091 v2, Table 2 (Python and Java, test set; v0 data). Their query is
the last 3 lines of in-file code; ours (the June harness) is the import statements plus the
last 30 lines. Their numbers are over 12,000 / 6,000 examples per split, ours over the 200
sampled per split that the June dumps carry. Same candidate pools, same gold definition.

| Python | easy XF-F @1 / @3 | easy XF-R @1 / @3 | hard XF-F @1 / @3 / @5 | hard XF-R @1 / @3 / @5 |
|---|---|---|---|---|
| Random (paper) | 15.7 / 47.0 | 15.6 / 46.9 | 6.4 / 19.3 / 32.1 | 6.4 / 19.4 / 32.2 |
| Jaccard (paper) | 20.8 / 53.3 | 24.3 / 54.7 | 10.0 / 25.9 / 39.9 | 11.4 / 26.0 / 40.3 |
| CodeBERT (paper) | 16.5 / 48.2 | 17.9 / 48.4 | 6.6 / 20.0 / 33.3 | 7.0 / 19.7 / 32.5 |
| UniXcoder (paper) | **25.9 / 59.7** | **29.4 / 61.9** | **17.7 / 39.0 / 53.5** | **20.1 / 41.0 / 54.9** |
| in-harness BM25 foil (n=200) | 14.0 / 49.0 | 20.0 / 56.0 | 10.0 / 25.5 / 39.0 | 13.5 / 32.0 / 46.5 |
| **cymatix beta shipped (n=200)** | 14.0 / 44.5 | 19.0 / 49.0 | 6.0 / 19.5 / 33.0 | 8.0 / 26.0 / 40.5 |

| Java | easy XF-F @1 / @3 | easy XF-R @1 / @3 | hard XF-F @1 / @3 / @5 | hard XF-R @1 / @3 / @5 |
|---|---|---|---|---|
| Random (paper) | 15.3 / 46.0 | 15.4 / 46.1 | 5.6 / 16.9 / 28.2 | 5.6 / 16.9 / 28.2 |
| Jaccard (paper) | 15.3 / 46.7 | 19.1 / 52.0 | 7.2 / 19.9 / 32.1 | 9.2 / 22.7 / 34.2 |
| UniXcoder (paper) | **17.3 / 50.5** | **24.2 / 57.8** | **10.8 / 26.4 / 39.7** | **15.1 / 33.4 / 46.2** |
| in-harness BM25 foil (n=200) | 16.0 / 45.5 | 16.5 / 52.0 | 6.5 / 19.0 / 35.5 | 13.5 / 28.0 / 40.5 |
| **cymatix beta shipped (n=200)** | 19.5 / 47.5 | 15.5 / 44.5 | 7.5 / 18.5 / 34.0 | 11.0 / 23.5 / 35.5 |

Reading: on both languages cymatix's pool ranking lands between the paper's random floor and
its Jaccard lexical baseline, and well under UniXcoder (the only semantic retriever that
clearly beats lexical here: +10 to +14 points at acc@5 on hard). With n = 200 per cell the
sampling error is about ±3 points at acc@1 and ±7 at acc@5, so "at random / at Jaccard" is
the honest resolution; a code-context query is simply not what the NL-tuned lanes were built
for, and nothing in v0.9.x targeted it.

### 2.3 CoIR CosQA — web query → Python function, NDCG@10 (%)

Source: arXiv 2407.02883 v3 (ACL 2025 main), Table 3, identical in the ACL camera-ready and
the public leaderboard; the BM25 row exists only from v2 onward. Their setup: the 500 test
queries (Bing web queries, ~6.6 words) against the released 20,604-id corpus; qrels credit
exactly one id per query. The released corpus is not deduplicated in practice — 20,604 ids
carry only 6,267 unique code texts and 426 of the 500 gold documents have byte-identical
twins under other ids, so a retriever that returns the twin scores a miss in CoIR. Our bed
collapses those twins (6,271 genes) and credits them, which is why our BM25 foil lands 11
points above the paper's BM25 on the same queries.

| system | CosQA NDCG@10 |
|---|---:|
| BM25 (paper, raw corpus) | 13.96 |
| Contriever 110M | 14.21 |
| BGE-M3 567M | 22.73 |
| UniXcoder 123M | 25.14 |
| OpenAI text-embedding-ada-002 | 28.88 |
| voyage-code-002 | 29.79 |
| GTE-base | 30.24 |
| E5-Mistral 7B | 31.27 |
| E5-base / BGE-base | 32.59 / 32.76 |
| leaderboard additions: CodeSage-large-v2 / SFR-Embedding-Code-2B | 32.73 / 36.31 |
| **matched BM25 foil over our deduped bed** | 24.9 |
| **cymatix beta, shipped config (deduped bed)** | **13.2** |
| cymatix beta, tag lanes muted (diagnostic) | 25.2 |

Reading: on a haystack about 11 points easier than the paper's, cymatix's shipped config
ties the paper's BM25 and trails every dense model by 9–23 points; like-for-like it is
roughly 11 points under BM25. The muted arm lands on our own foil (25) — the same level as
UniXcoder, still on the easier haystack. Six-word web queries are the worst case for
tag-lane plateaus: almost every query term is a shared tag.

### 2.4 Issue → file localization: SWE-bench, Loc-Bench, MULocBench

Three published setups, none identical to ours. Metric definitions matter here: the SWE-bench
paper reports BM25 recall of oracle files under a **token budget** (Avg = mean per-instance
recall; All = % instances with every gold file retrieved; Any = % with at least one), per
instance at its own base commit, Python files only, test files excluded, query =
`problem_statement` (arXiv 2310.06770 v3 §4.1 Table 3 and the official
`eval_retrieval.py`). LocAgent (arXiv 2503.09089 v2, Table 4) and MULocBench (arXiv
2509.25242 v1, Table 1) report **Acc@k = all gold files within the top k**, function-level
indexes collapsed to files. Ours is first-gold within k on pinned per-repo snapshots and
gene (chunk) units, so the like-for-like slice is the **single-gold-file subset**, where
first-gold and all-gold coincide.

| SWE-bench, BM25 (paper, full test 2,294, per-instance) | 13k tokens | 27k | 50k |
|---|---:|---:|---:|
| Avg recall | 29.58 | 44.41 | 51.06 |
| All gold files retrieved (%) | 26.09 | 39.83 | 45.90 |
| Any gold file retrieved (%) | 34.77 | 51.27 | 58.38 |

| SWE-bench Lite (274 of 300), file-level Acc@k, LocAgent Table 4 | @1 | @3 | @5 |
|---|---:|---:|---:|
| BM25 | 38.69 | 51.82 | 61.68 |
| E5-base-v2 / CodeRankEmbed (dense) | 49.64 / 52.55 | 74.45 / 77.74 | 80.29 / 84.67 |
| Agentless (Claude 3.5) | 72.63 | 79.20 | 79.56 |
| LocAgent (Claude 3.5) | 77.74 | 91.97 | 94.16 |
| **cymatix beta, SWE-bench Verified single-file subset (n = 420, pinned snapshots)** | **21.4** | **38.1** | **46.9** |
| matched BM25 foil, same subset | 18.6 | 33.3 | 42.1 |
| cymatix, any-gold within 12 genes, all 489 | 63.8 | | |

| MULocBench Python (842), file-level Acc@k, paper Table 1 | @1 | @5 |
|---|---:|---:|
| BM25 | 9.4 | 9.4 |
| Agentless (Claude 3.5 Haiku) | 12.2 | 23.9 |
| OpenHands (Claude 3.5 Haiku) | 24.1 | 33.5 |
| LocAgent (Claude 3.5 Haiku) | 28.5 | 35.2 |
| **cymatix beta, single-file subset (n = 349, pinned snapshots)** | **11.5** | **25.8** |
| cymatix, first gold, all 680 | 15.9 | 34.6 |

Reading: against per-instance BM25 on SWE-bench Lite, cymatix's file localization on
Verified is about half at every k (21 vs 39 at @1, 47 vs 62 at @5) — but so is our own BM25
foil on the same slice (19 / 42), which says most of that gap is the setup (pinned
near-neighbour snapshots, chunk units, every extension admitted, a different subset), not
the retriever; on our haystack cymatix beats plain BM25 by 3–5 points at every k. Against
the SWE-bench paper's own budgeted BM25, cymatix's any-gold-within-12-genes (63.8 %) is
above the 50k-token "Any" (58.4 %) on a subset that is easier and a snapshot that is worse,
so call it "same neighbourhood". Dense embedders (E5, CodeRankEmbed) and agentic localizers
are 20–45 points beyond either lexical system. On MULocBench cymatix's single-file Acc@5
(25.8) lands on the Agentless-with-Haiku row (23.9) and well above the paper's BM25 row
(9.4, printed identically at @1 and @5); the LLM-agent localizers (33–35) remain ahead.
Nothing here is a neural-free-vs-agent comparison at equal cost — the agents make dozens of
model calls per issue.

### 2.5 LoCoMo — conversational memory recall

Source: arXiv 2402.17753 v1, Table 3: RAG with the DRAGON dense retriever over dialog
turns, observations or session summaries, reader gpt-3.5-turbo-16k; recall@k over the
paper's 7,512 questions. No BM25 or other retriever is reported; LoCoMo-Plus (arXiv
2602.10715) reports **no retrieval recall at all** (its RAG baselines with OpenAI embeddings
score 23–30 % accuracy on the cognitive set vs 37–45 % on LoCoMo). Our bed is one gene per
turn plus the 401 cognitive cues; our 2,378-question set is the resolvable subset.

| dialog-turn units | R@5 single | multi | temporal | open-domain / commonsense | adversarial | overall |
|---|---:|---:|---:|---:|---:|---:|
| DRAGON (paper, k = 5) | 66.2 | 34.4 | 89.2 | 38.5 | 45.7 | 58.8 |
| DRAGON (paper, k = 10) | 72.8 | 47.4 (printed "247.4") | 97.3 | 53.8 | 54.3 | 67.5 |
| **cymatix beta, R@5** | 39.6 | 32.0 | 40.6 | 22.5 | 34.1 | 30.6 |
| **cymatix beta, R@10** | **59.3** | **45.2** | **55.0** | **30.3** | **51.6** | **44.6** |
| cymatix, cognitive (LoCoMo-Plus, n = 401), R@10 | | | | | | 0.5 |

Reading: a dense retriever tuned for dialogue beats the lexical stack by 23 points at R@10
overall, and by 42 on temporal questions (dates and relative time are where lexical recall
fails). Multi-hop is close (45 vs 47) and adversarial within 3. The cognitive class has no
published retrieval number to compare against; our 0.5 % is consistent with the paper's
observation that embedding RAG loses 12–16 accuracy points on it.

### 2.6 EnronQA — global email retrieval

Source: arXiv 2505.00263 v1, Table 4: retrieval over the full 103,638-email corpus, unit =
whole email, k = 5, on the benchmark's test questions. Our bed is the 73,772-email QA subset
(one email ≈ 1.15 genes) and our 500 questions are 250 original/rephrased pairs.

| system | R@5 | R@10 | R@12 |
|---|---:|---:|---:|
| BM25, Pyserini (paper) | **87.5** | — | — |
| ColBERTv2 (paper) | 54.1 | — | — |
| **cymatix beta, v2 bed (85k)** | 66.4 | 84.0 | 87.6 |
| cymatix beta, padded bed (597k, 500k distractor emails) | 67.2 | 79.2 | 80.8 |

Reading: the paper's headline is that plain BM25 dominates a late-interaction dense model
on email by 33 points; cymatix at k = 5 sits between them (66 vs 88 / 54) and only reaches
the paper's BM25@5 at k = 12. The same tag-plateau mechanism §4.2 of the sweep doc
measured on code is the obvious suspect for the R@5 shortfall and is untested on this bed.

### 2.7 FinanceBench — no retrieval metric exists to compare

arXiv 2311.11944 v1 publishes answer accuracy only (Table 2, n = 150): GPT-4-Turbo
shared vector store 19 % correct, single vector store per filing 50 %, long context 79 %,
oracle evidence pages 85 %; no chunk size, k, or page recall is stated. Our page-level
R@10 of 14.7 % over a shared store of all 361 filings is the retrieval regime behind their
19 % row, but the two numbers are different quantities and are not compared.

### 2.8 Onyx EnterpriseRAG-Bench (ERB) — the cleanest comparison

Source: arXiv 2605.05253 v1, Tables 6–7: document recall@10 over 511,962 documents on the
470 questions that carry gold document ids — the same questions, corpus and n as our
947k bed (our bed chunks the documents into 947,531 genes). Their recall is the fraction
of gold documents retrieved; ours is first gold within 10, so on multi-gold questions ours
is an upper bound.

| question type (n) | BM25, OpenSearch | vector, text-embedding-3-large | bash agent, GPT-5.4 | **cymatix beta, 947k, R@10** | cymatix delivered@12 |
|---|---:|---:|---:|---:|---:|
| overall (470) | **68.4** | 46.0 | 55.8 | **66.6** | 66.8 |
| basic (175) | 77.7 | 48.6 | 61.7 | 66.3 | 65.7 |
| semantic (125) | 43.2 | 24.8 | 29.6 | 34.4 | 34.4 |
| intra-document reasoning (40) | 90.0 | 57.5 | 75.0 | 82.5 | 85.0 |
| project related (40) | 65.5 | 49.2 | 43.2 | 100.0 | 100.0 |
| constrained (30) | 85.0 | 83.3 | 78.3 | 93.3 | 96.7 |
| conflicting info (20) | 82.5 | 55.0 | 72.5 | 90.0 | 90.0 |
| completeness (20) | 46.5 | 33.4 | 59.0 | 85.0 | 85.0 |
| miscellaneous (20) | 90.0 | 75.0 | 100.0 | 90.0 | 90.0 |

Reading: overall cymatix (66.6) is 1.8 points under the paper's BM25 and 21 above its
OpenAI-embedding vector search, with a very different shape: it wins project-related
(+34), completeness (+38), constrained (+8), conflicting (+8) — the classes where its tag,
filename and entity lanes carry structure — and loses basic (−11) and semantic (−9), the
classes where plain lexical matching of the question's own words is the whole game. That
basic/semantic deficit is the same signature as the code beds: the fused lanes displace
the top lexical hit. Also from the paper: no hybrid or reranked system is reported, and its
scaling blog shows BM25 recall@10 sliding 85 → 56 % from 5k to 510k documents on a
different question set, which frames why a 947k-gene number is the one to cite.

## 4. Summary

| where cymatix stands (shipped, neural-free, beta) | evidence |
|---|---|
| **at BM25 level** on enterprise prose (ERB 66.6 vs 68.4), with a better class profile on structured questions and a worse one on plain lexical ones | §2.8 |
| **above BM25 by 3–5 points** on issue → file localization over the same haystack (SWE-bench Verified); half of per-instance BM25 in the papers' own setting, which our foil shows is mostly setup | §2.4 |
| **below BM25 by 10–35 points** on code-context and web-query code search (RepoBench-R, CosQA, CodeRAG solutions) and on documentation retrieval (CodeRAG docs), with a measured mechanism (tag-lane plateaus under RRF) that recovers most of it in diagnostic arms | §2.1–2.3, sweep doc §4.2 |
| **below a dialogue-tuned dense retriever by 23 points** at R@10 on LoCoMo, 42 on temporal questions | §2.5 |
| **between BM25 and ColBERTv2** on email at k = 5, at BM25's level by k = 12 | §2.6 |
| **below every dense embedder** on documentation and web-query code search by 9–25 points, at a cost tier (0.15 ms query encode, no model) shared only with BM25 | §2.1, §2.3 |

The shortest honest sentence: cymatix's default retriever is a BM25-class system whose
extra lanes pay off on structured enterprise questions and lose on short lexical queries;
nothing in the published tables is beaten by it except vector search on ERB, and the dense
code retrievers are a different tier it does not compete in today.

## 3. Comparability ledger

| bench | what matches | what does not | how much it matters |
|---|---|---|---|
| ERB | same corpus, same questions (the house n=470 is the paper's basis), document-level recall | k=10 vs our 12 (we report R@10 too); their pipeline chunking vs our gene chunking | small — the cleanest comparison we have |
| SWE-bench Verified | same instances, same gold files, issue text as query | papers retrieve per instance at its own base commit; we pin one snapshot per repo (477/489 needles localize on a near-neighbour tree) and admit only the builder's extensions; papers measure by token budget, we by k | medium — direction reliable, magnitude ±several points |
| MULocBench | same issues, file-level gold | pinned snapshots (exact-commit n=70 only); regex chunker; paper baselines may retrieve at the issue commit | medium |
| RepoBench-R | same dumps, same candidate pools, same acc@k | our query = imports + last 30 lines (the harness's construction), not necessarily the paper's | small on B (pool rank), C (global rank) is our own measure |
| CodeRAG-Bench | same corpora and query sets, NDCG@10 over canonical docs | our docs bed drops 3,032 pages < 50 bytes and collapses 9,128 duplicates; solutions corpus identical | small for solutions, medium for docs |
| CoIR CosQA | same 500 test queries, same gold | our bed is the 6,271-unique-snippet haystack, the paper's is the raw 20,604-id corpus (70 % byte duplicates) — a retriever returning a twin id is a miss in CoIR and a hit here | large — treat as an easier haystack |
| LoCoMo | same questions, same conversations | papers report QA F1 after retrieval far more often than retrieval recall; retrieval unit (turn vs session) differs | large unless a recall number exists |
| EnronQA | same corpus and questions | per-user mailboxes vs our whole-corpus bed; paraphrase split | medium |
| FinanceBench | same questions | the paper reports answer accuracy under retrieval configurations, not page recall | not comparable on retrieval |
