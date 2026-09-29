# Packet token cost on the ERB 947k bed (2026-09-29)

What a delivered packet costs a downstream model, counted with a real
tokenizer on saved packets. No retrieval was re-run; the tool reads the
`context` string each answer model actually received.

Receipt: `benchmarks/dogfood/erb/receipts/packet_token_cost_947k_2026-09-29.json`
Tool: `benchmarks/dogfood/erb/packet_token_cost.py` (tiktoken 0.13.0,
`o200k_base` and `cl100k_base`), run at `a5a5dbef`.

## Result (500 official EnterpriseRAG questions, 947,531-document bed)

| Packet | Seats (mean) | Characters (median) | Tokens, o200k (mean / median / p90 / max) | Tokens, cl100k (mean) |
|---|---|---|---|---|
| Compressed, 12 seats, `expression_tokens = 7000` (shipped assembly) | 12.0 | 28,986 | **8,345** / 8,268 / 9,197 / 11,361 | 8,367 |
| Full text, 12 seats | 12.0 | 50,502 | **12,650** / 12,688 / 13,804 / 15,719 | 12,698 |
| Full text, 12 + up to 4 companions (`companion_chunks = 4`) | 15.9 (3.9 added) | 63,705 | **15,917** / 16,021 / 17,398 / 19,484 | 15,977 |

The decoder instruction adds about 215 tokens on top of each packet. The
answer model's question and wrapper prompt are not counted here.

## Reading it

- **Shipped defaults.** v0.10.0 ships compressed assembly
  (`full_text_delivery = false`, `expression_tokens = 7000`). Companions only
  attach under `full_text_delivery = true`, so the shipped default adds none.
  A full 12-seat packet costs about **8.3k tokens**, roughly 19% above the
  7,000-token budget. The budget is a characters-over-four estimate, and this
  corpus tokenizes at about 3.4 characters per token.
- **Seats under shipped defaults.** These captures were taken with the tier
  seat floor on, so every packet has 12 seats. Shipped defaults leave the floor
  off (#430). On this bed, the FOCUSED tier then cuts about 32% of queries to
  6 seats (152 of 470 in the #430 receipt). The shipped-default mean is
  therefore at or below the 12-seat figure. No receipt measures that mean
  directly: NO EVIDENCE for a single shipped-default average.
- **The 12+4 full-text profile.** This profile is documented opt-in and is not
  a default. It costs about **1.9x** a compressed 12-seat packet (+7.6k
  tokens). The four companions add about 3.3k tokens (+26%) over full-text 12.
  In the one historical paired Sol run, 12+4 answered 284/500 against 249/500
  for full-text 12 (`docs/research/2026-09-14-companion-release-settings.md`).
  That run is not a release score.

## Inputs (read-only)

| Arm | Capture directory | files_sha256 |
|---|---|---|
| default_compressed_12 | September 10 ERB answer capture (`.worktrees/erb-answers-20260910/contexts`, archived by the 2026-09-24 space pass to `H:\archives\space-pass-2026-09-24\...`) | see receipt |
| fulltext_12 | `benchmarks/dogfood/erb/receipts/packet-100k-sol-erb947k-20260912-01/contexts` (local, untracked) | see receipt |
| fulltext_12plus4_companions | `benchmarks/dogfood/erb/receipts/paired-companion-sol-erb947k-20260913-01/lexical16/contexts` (local, untracked) | see receipt |

The full-text arms reuse the compressed capture's 12 document ids and order,
with the complete stored bodies restored. Their retrieval config was recorded
in `packet-100k-sol-erb947k-20260912-01/effective-config.json`:
`expression_tokens = 7000`, `min_delivered_docs = 12`, legacy wire. The bed
identity is `11a05b91…` (947,531 documents, ingest_c 6). The companion arm's
own `full-text 12` control is byte-identical in its token counts to
`fulltext_12`.
