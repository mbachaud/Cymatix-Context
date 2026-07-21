"""
SPLADE Backend — Learned sparse expansion for the knowledge store.

Bio analogue (legacy term: epigenetics):
    SPLADE is like an epigenetic mark that makes hidden genes visible.
    Where BM25 only sees exact words, SPLADE expands each chunk with
    semantically related terms at index time — making documents findable
    by meaning, not just surface text.

Implementation:
    Uses naver/splade-cocondenser-ensembledistil (or compatible) to
    produce sparse vocabulary-space weight vectors. These get stored
    in a splade_terms table (inverted index in SQLite) and queried
    via dot-product scoring at retrieval time.

Performance:
    ~5ms per chunk on CPU (single forward pass, no autoregressive generation).
    ~60s for 3,500 chunks (batch mode).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("helix.splade")

# Lazy-loaded model + tokenizer
_model = None
_tokenizer = None
_device = None

_SPECIAL_TOKENS = frozenset({"[CLS]", "[SEP]", "[PAD]", "[UNK]"})


def _ensure_loaded(model_name: str = "naver/splade-cocondenser-ensembledistil"):
    """Load SPLADE model on first use. Cached for process lifetime."""
    global _model, _tokenizer, _device

    if _model is not None:
        return

    import torch
    from transformers import AutoModelForMaskedLM, AutoTokenizer

    from cymatix_context.hardware import get_hardware

    _device = torch.device(get_hardware().device)
    _tokenizer = AutoTokenizer.from_pretrained(model_name)
    _model = AutoModelForMaskedLM.from_pretrained(model_name).to(_device)
    _model.eval()
    log.info("SPLADE model loaded: %s on %s", model_name, _device)


def encode(text: str, top_k: int = 128, model_name: str = "naver/splade-cocondenser-ensembledistil") -> Dict[str, float]:
    """
    Encode text into a sparse SPLADE vector.

    Returns {token: weight} dict with top_k non-zero entries.
    Each token is from the BERT vocabulary; weight indicates how
    strongly that term is associated with this content.
    """
    import torch

    _ensure_loaded(model_name)

    tokens = _tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=256,
        padding=False,
    ).to(_device)

    with torch.no_grad():
        output = _model(**tokens)

    # SPLADE activation: ReLU + log(1 + x) over MLM logits, max-pool over tokens
    logits = output.logits  # (1, seq_len, vocab_size)
    activated = torch.log1p(torch.relu(logits))  # sparse activation
    pooled = activated.max(dim=1).values.squeeze(0)  # (vocab_size,)

    # Extract top-k non-zero entries
    nonzero_count = int((pooled > 0).sum().item())
    if nonzero_count == 0:
        # torch.Tensor.topk(0) raises on some backends; also there's nothing
        # to return if the pooled activation is entirely zero.
        return {}
    top_values, top_indices = pooled.topk(min(top_k, nonzero_count))

    sparse: Dict[str, float] = {}
    for idx, val in zip(top_indices.cpu().tolist(), top_values.cpu().tolist()):
        if val > 0:
            token = _tokenizer.decode([idx]).strip()
            # Skip special tokens and single chars
            if len(token) > 1 and token not in _SPECIAL_TOKENS:
                sparse[token] = round(val, 4)

    return sparse


def encode_batch(
    texts: List[str],
    top_k: int = 128,
    batch_size: Optional[int] = None,
    model_name: str = "naver/splade-cocondenser-ensembledistil",
) -> List[Dict[str, float]]:
    """
    Batch-encode texts into SPLADE sparse vectors.
    More efficient than calling encode() in a loop due to batched forward passes.

    ``batch_size`` defaults to ``recommended_batch_size("splade")`` when unset,
    sized for the detected hardware (VRAM tier on CUDA/ROCm, system RAM on
    MPS/CPU). Pass an explicit value to override.
    """
    import torch

    _ensure_loaded(model_name)

    if batch_size is None:
        from cymatix_context.hardware import recommended_batch_size
        batch_size = recommended_batch_size("splade")

    results: List[Dict[str, float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        tokens = _tokenizer(
            batch,
            return_tensors="pt",
            truncation=True,
            max_length=256,
            padding=True,
        ).to(_device)

        with torch.no_grad():
            output = _model(**tokens)

        logits = output.logits  # (batch, seq_len, vocab_size)
        activated = torch.log1p(torch.relu(logits))
        pooled = activated.max(dim=1).values  # (batch, vocab_size)

        for j in range(pooled.size(0)):
            vec = pooled[j]
            nonzero_count = int((vec > 0).sum().item())
            if nonzero_count == 0:
                results.append({})
                continue
            top_values, top_indices = vec.topk(min(top_k, nonzero_count))

            sparse: Dict[str, float] = {}
            for idx, val in zip(top_indices.cpu().tolist(), top_values.cpu().tolist()):
                if val > 0:
                    token = _tokenizer.decode([idx]).strip()
                    if len(token) > 1 and token not in _SPECIAL_TOKENS:
                        sparse[token] = round(val, 4)
            results.append(sparse)

    return results


# ── SQLite integration helpers ────────────────────────────────────

def create_splade_table(conn) -> None:
    """Create the splade_terms inverted index table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS splade_terms (
            gene_id  TEXT NOT NULL,
            term     TEXT NOT NULL,
            weight   REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_splade_term
        ON splade_terms (term, weight DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_splade_gene
        ON splade_terms (gene_id)
    """)
    conn.commit()


def upsert_splade_terms(conn, gene_id: str, sparse: Dict[str, float]) -> None:
    """Store SPLADE sparse vector for a document (replaces existing entries)."""
    conn.execute("DELETE FROM splade_terms WHERE gene_id = ?", (gene_id,))
    if sparse:
        conn.executemany(
            "INSERT INTO splade_terms (gene_id, term, weight) VALUES (?, ?, ?)",
            [(gene_id, term, weight) for term, weight in sparse.items()],
        )
    conn.commit()


def query_splade(
    conn,
    query_sparse: Dict[str, float],
    limit: int = 50,
    min_score: float = 0.01,
) -> List[Tuple[str, float]]:
    """
    Query the SPLADE inverted index with a sparse query vector.

    Returns [(gene_id, score)] ranked by dot-product similarity.
    """
    if not query_sparse:
        return []

    # Single SQL computing the query-weighted dot product per document:
    # join the inverted index against the query vector (VALUES CTE) so
    # LIMIT and min_score apply to the TRUE score. The previous two-pass
    # version pre-filtered candidates by unweighted SUM(weight) term mass,
    # which could discard a high-weighted-score document in favor of one
    # with more raw mass but near-zero query overlap.
    values_clause = ",".join("(?, ?)" for _ in query_sparse)
    params: List[object] = []
    for term, qw in query_sparse.items():
        params.extend((term, qw))

    rows = conn.execute(
        f"WITH q(term, qw) AS (VALUES {values_clause}) "
        f"SELECT st.gene_id, SUM(st.weight * q.qw) AS score "
        f"FROM splade_terms st JOIN q ON st.term = q.term "
        f"GROUP BY st.gene_id "
        f"HAVING score > ? "
        f"ORDER BY score DESC "
        f"LIMIT ?",
        params + [min_score, limit],
    ).fetchall()

    return [(gene_id, score) for gene_id, score in rows]
