# Invalid qualification attempt: treatment agent overrides

The non-scored control arm completed all four checkpoints and passed terminal
verification. The Cymatix arm then stopped before its first model call because
the harness supplied treatment-only configuration as top-level OmegaConf
overrides. SCBench only applies custom fields from a loaded agent YAML through
explicit `agent.*` overrides, so the sentinel campaign-manifest path remained
in effect and setup failed closed.

The retained receipts and logs are the evidence of record. The harness now
prefixes `campaign_manifest`, `pair_id`, `replicate`, and `receipt_root` with
`agent.`, guarded by a command-construction regression test.
