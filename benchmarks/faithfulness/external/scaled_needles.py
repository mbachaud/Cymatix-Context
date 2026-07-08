"""Scaled synthetic needle set (24) — answers single-token in BOTH gemma-2-2b
and Qwen3-4B (verified), so the same set runs cross-model and the metric needs
no tokenizer-specific handling. All answers arbitrary given the question
(A must hallucinate, B must read from context). Egress: synthetic only.
"""

def _mk(nid, subj, verb, ans, doc):
    ctx = f"Redwood Inference {doc}: the {subj} {verb} {ans}."
    q = f"At Redwood Inference, the {subj} {verb}"
    while "  " in ctx:
        ctx = ctx.replace("  ", " ")
    while "  " in q:
        q = q.replace("  ", " ")
    return {"id": nid, "ctx": ctx, "q": q.rstrip(), "ans": ans}


SCALED_NEEDLES = [
    # animals — "is nicknamed the X"
    _mk("beacon", "Beacon monitoring bot is nicknamed the", "", "tiger", "field guide"),
    _mk("cascade", "Cascade ingestion daemon is nicknamed the", "", "eagle", "field guide"),
    _mk("atlas", "Atlas watchdog is nicknamed the", "", "shark", "field guide"),
    _mk("sentinel", "Sentinel scanner is nicknamed the", "", "wolf", "field guide"),
    # materials — "is built on X"
    _mk("vault", "Vault storage tier is built on", "", "quartz", "spec"),
    _mk("relay", "Relay index shard is built on", "", "granite", "spec"),
    _mk("forge", "Forge cache layer is built on", "", "copper", "spec"),
    _mk("nova", "Nova routing plane is built on", "", "marble", "spec"),
    _mk("delta", "Delta ledger store is built on", "", "bronze", "spec"),
    _mk("echo", "Echo queue buffer is built on", "", "slate", "spec"),
    # colors — "accent is X"
    _mk("pillar", "Pillar dashboard accent is", "", "violet", "UI kit"),
    _mk("mesh", "Mesh dashboard accent is", "", "turquoise", "UI kit"),
    _mk("pulse", "Pulse dashboard accent is", "", "amber", "UI kit"),
    _mk("grove", "Grove dashboard accent is", "", "crimson", "UI kit"),
    _mk("ember", "Ember dashboard accent is", "", "olive", "UI kit"),
    _mk("fern", "Fern dashboard accent is", "", "azure", "UI kit"),
    # fruits — "engine is codenamed X"
    _mk("prism", "Prism analytics engine is codenamed", "", "mango", "release notes"),
    _mk("orbit", "Orbit query engine is codenamed", "", "peach", "release notes"),
    _mk("halo", "Halo search engine is codenamed", "", "cherry", "release notes"),
    _mk("comet", "Comet stream engine is codenamed", "", "lemon", "release notes"),
    _mk("ridge", "Ridge batch engine is codenamed", "", "grape", "release notes"),
    _mk("spire", "Spire graph engine is codenamed", "", "plum", "release notes"),
    # elements — "array is powered by X"
    _mk("zephyr", "Zephyr compute array is powered by", "", "neon", "power log"),
    _mk("onyx", "Onyx render array is powered by", "", "helium", "power log"),
]
