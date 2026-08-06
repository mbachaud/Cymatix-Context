"""needles_dogfood.py — the dogfood needle set over the four owned repos.

Replaces the SIKE needle table for shippable runs. The old set drew gold from
six repos, 51 of whose 64 gold files lived in private siblings — which is why
it could not be handed to a collaborator. Every gold file here lives in
``cymatix-context``, ``driftwatch``, ``scorerift`` or ``MaxExpressKit``, so
the matching gold pack is redistributable as a unit.

Each needle pins a **specific value** that a retrieval system either surfaces
or does not: a port, a threshold, a default, a version. Prose questions make
poor needles because grading them needs a judge; a number does not.

``gold_source`` is **not hand-written** — it is derived from the corpus by
``verify_needles.py``, which scans the four repos for files literally
containing the expected value and writes the resolved list back. Hand-listed
gold paths rot silently as files move; derived ones fail loudly.

Fields:
    name        stable id, used in result JSONs
    query       the natural-language question put to retrieval
    expected    the canonical answer string
    accept      alternate renderings a consumer might emit (e.g. "0.15", ".15")
    anchor      the literal searched for when deriving gold_source; defaults
                to ``expected`` but can differ where the answer is a word and
                the on-disk token is punctuated
    repos       which repos may contribute gold (guards against a generic
                value like "12" matching half the corpus)
"""

from __future__ import annotations

NEEDLES: list[dict] = [
    # ── cymatix-context ────────────────────────────────────────────────
    {
        "name": "cymatix_proxy_port",
        "query": "What port does the Cymatix proxy server listen on?",
        "expected": "11437",
        "accept": ["11437"],
        "anchor": "localhost:11437",
        "repos": ["cymatix-context"],
    },
    {
        "name": "cymatix_expression_tokens",
        "query": "What is the default expression_tokens budget in Cymatix?",
        "expected": "7000",
        "accept": ["7000", "7,000", "~7K"],
        "anchor": "expression_tokens = 7000",
        "repos": ["cymatix-context"],
    },
    {
        "name": "cymatix_cymatics_bins",
        "query": "How many bins does the Cymatix cymatics spectrum use?",
        "expected": "256",
        "accept": ["256"],
        "anchor": "256 bin",
        "repos": ["cymatix-context"],
    },
    {
        "name": "cymatix_fusion_default",
        "query": "What is the default retrieval fusion_mode in Cymatix?",
        "expected": "rrf",
        "accept": ["rrf", "RRF", "Reciprocal Rank Fusion"],
        "anchor": 'fusion_mode: str = "rrf"',
        "repos": ["cymatix-context"],
    },
    {
        "name": "cymatix_know_emit_floor",
        "query": "What is the emit_floor for the Cymatix know confidence gate?",
        "expected": "0.45",
        "accept": ["0.45", ".45"],
        "anchor": "emit_floor      = 0.45",
        "repos": ["cymatix-context"],
    },
    {
        "name": "cymatix_max_genes",
        "query": "How many genes per turn does Cymatix deliver by default?",
        "expected": "12",
        "accept": ["12"],
        "anchor": "max_genes_per_turn = 12",
        "repos": ["cymatix-context"],
    },
    {
        "name": "cymatix_genome_path",
        "query": "What is the default Cymatix knowledge store path?",
        "expected": "genomes/main/genome.db",
        "accept": ["genomes/main/genome.db", "genomes\\main\\genome.db"],
        "repos": ["cymatix-context"],
        "anchor": 'path = "genomes/main/genome.db"',
    },
    {
        "name": "cymatix_splice_aggressiveness",
        "query": "What is the default splice_aggressiveness in Cymatix?",
        "expected": "0.3",
        "accept": ["0.3", ".3"],
        "anchor": "splice_aggressiveness = 0.3",
        "repos": ["cymatix-context"],
    },
    # ── scorerift ──────────────────────────────────────────────────────
    {
        "name": "scorerift_divergence_threshold",
        "query": "What is the divergence threshold that triggers alerts in ScoreRift?",
        "expected": "0.15",
        "accept": ["0.15", ".15"],
        "anchor": "> 0.15",
        "repos": ["scorerift"],
    },
    {
        "name": "scorerift_confidence_gate",
        "query": "What auto-confidence level must a ScoreRift dimension exceed before divergence can fire?",
        "expected": "50%",
        "accept": ["50%", "0.5", "50 percent"],
        "anchor": "50%",
        "repos": ["scorerift"],
    },
    {
        "name": "scorerift_import_hygiene_confidence",
        "query": "What confidence does ScoreRift assign the import_hygiene dimension?",
        "expected": "85%",
        "accept": ["85%", "0.85"],
        "anchor": "import_hygiene",
        "repos": ["scorerift"],
    },
    # ── driftwatch ─────────────────────────────────────────────────────
    {
        "name": "driftwatch_version",
        "query": "What version is DriftWatch currently at?",
        "expected": "v0.2.0",
        "accept": ["v0.2.0", "0.2.0"],
        "repos": ["driftwatch"],
    },
    {
        "name": "driftwatch_default_ollama_model",
        "query": "What is the default Ollama model tag in DriftWatch?",
        "expected": "qwen3:8b",
        "accept": ["qwen3:8b"],
        "repos": ["driftwatch"],
    },
    {
        "name": "driftwatch_default_inference_server",
        "query": "Which inference server is the recommended default for DriftWatch nodes?",
        "expected": "vLLM",
        "accept": ["vLLM", "vllm"],
        "anchor": "vLLM",
        "repos": ["driftwatch"],
    },
    {
        "name": "driftwatch_default_secret",
        "query": "What password does the DriftWatch docker-compose fall back to if DB_PASSWORD is unset?",
        "expected": "changeme",
        "accept": ["changeme"],
        "repos": ["driftwatch"],
    },
    # ── MaxExpressKit ──────────────────────────────────────────────────
    {
        "name": "mek_guardrails",
        "query": "What are the three guardrails MaxExpressKit ships for Claude Code?",
        "expected": "compliance, drift, ledger",
        "accept": ["compliance, drift, ledger", "compliance", "drift", "ledger"],
        "anchor": "compliance, drift, ledger",
        "repos": ["MaxExpressKit"],
    },
    {
        "name": "mek_source_apps",
        "query": "Which three applications was MaxExpressKit distilled from?",
        "expected": "CosmicTasha, ScoreRift, BookKeeper",
        "accept": ["CosmicTasha", "ScoreRift", "BookKeeper"],
        "anchor": "CosmicTasha",
        "repos": ["MaxExpressKit"],
    },
    {
        "name": "mek_marketplace_name",
        "query": "What marketplace name does the MaxExpressKit plugin install under?",
        "expected": "mek-marketplace",
        "accept": ["mek-marketplace"],
        "repos": ["MaxExpressKit"],
    },
]
