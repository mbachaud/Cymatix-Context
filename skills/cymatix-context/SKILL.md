---
name: cymatix-context
description: Use Cymatix for architectural, historical, semantic, or cross-file repository context; verify exact code locally and fall back cleanly when Cymatix is unavailable.
---

# Cymatix Context

Use Cymatix as an evidence-discovery layer. It can narrow repository exploration, but it never replaces verification against the current working tree.

## Workflow

1. Call `cymatix_context` first for architectural, historical, semantic, or cross-file questions where compressed context can narrow the search.
2. Call `cymatix_context_packet` when freshness, authority, or edit safety matters.
3. Read local files for exact current code, line references, quoted text, and every edit. Treat Cymatix retrieval as evidence discovery, not source authority.
4. Call `cymatix_ingest` only for meaningful, attributable additions; do not ingest transient thoughts or raw tool output.
5. Call `cymatix_sessions_list` for participant awareness. Session presence does not prove what another participant is currently doing.
6. If a Cymatix call fails while `cymatix_health` remains available, call it to classify backend state. If the adapter or tool list is absent, disabled, or disconnected, continue with native tools.
7. Call `cymatix_announce` once after successful MCP registration when it is exposed. Announcement supplies a display label and does not alter retrieval. If registration failed, continue without it.

## Operating boundaries

- Direct MCP retrieval does not require the model proxy.
- A healthy separately started server does not require the tray.
- Use an explicit `caller_model_class` only when the current model class is known; use `model` only as a downstream budget/model-family hint.
- Never infer retrieval policy from the announcement model label.
- Cymatix may improve a task, but it must not block native repository exploration.
