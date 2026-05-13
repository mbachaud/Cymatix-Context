"""
DeBERTa Compressor Backend — Drop-in replacement for OllamaBackend.

Replaces the two most expensive compressor operations:
    re_rank  — cross-encoder scoring (query, gene_summary) -> relevance
    splice   — binary classification (query, fragment) -> keep/drop

PACK and REPLICATE still use the Ollama backend (they need generation).

This backend loads two fine-tuned DeBERTa-v3-small models:
    training/models/rerank/   — cross-encoder for re-ranking
    training/models/splice/   — binary classifier for splice decisions

Usage:
    from helix_context.deberta_backend import DeBERTaRibosome

    compressor = DeBERTaRibosome(
        rerank_model_path="training/models/rerank",
        splice_model_path="training/models/splice",
        ollama_fallback=OllamaBackend(),  # for pack/replicate
    )
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import torch

from .schemas import Gene, NLRelation

log = logging.getLogger("helix.ribosome.deberta")


class DeBERTaRibosome:
    """
    Hybrid compressor: DeBERTa for re_rank + splice, Ollama for pack + persist.

    Drop-in compatible with helix_context.ribosome.Ribosome — same method
    signatures for re_rank() and splice(). Pack/replicate delegate to the
    Ollama-backed Compressor passed at init.
    """

    def __init__(
        self,
        rerank_model_path: str = "training/models/rerank",
        splice_model_path: str = "training/models/splice",
        nli_model_path: str = "training/models/nli",
        ollama_ribosome=None,
        device: Optional[str] = None,
        splice_threshold: float = 0.5,
        nli_splice_bonus: float = 0.15,
        nli_splice_penalty: float = 0.15,
        rerank_pretrained: str = "",
    ):
        from transformers import AutoTokenizer, AutoModelForSequenceClassification

        # Device resolution: defer to the hardware module's singleton when
        # the caller passes None. Explicit "auto" (legacy callers) goes
        # through the same path so detection is centralized; explicit
        # device strings ("cpu", "cuda:0", ...) are honored as-is.
        if device is None or device == "auto":
            from helix_context.hardware import get_hardware
            self._device = torch.device(get_hardware().device)
        else:
            self._device = torch.device(device)

        # Use pretrained cross-encoder if specified, else use local fine-tuned model
        rerank_source = rerank_pretrained if rerank_pretrained else rerank_model_path
        self._rerank_pretrained = bool(rerank_pretrained)
        log.info("Loading rerank model from %s (pretrained=%s)", rerank_source, self._rerank_pretrained)
        self._rerank_tokenizer = AutoTokenizer.from_pretrained(rerank_source)
        self._rerank_model = AutoModelForSequenceClassification.from_pretrained(
            rerank_source
        ).to(self._device)
        self._rerank_model.train(False)

        log.info("Loading DeBERTa splice model from %s", splice_model_path)
        self._splice_tokenizer = AutoTokenizer.from_pretrained(splice_model_path)
        self._splice_model = AutoModelForSequenceClassification.from_pretrained(
            splice_model_path
        ).to(self._device)
        self._splice_model.train(False)

        self.splice_threshold = splice_threshold
        self.nli_splice_bonus = nli_splice_bonus
        self.nli_splice_penalty = nli_splice_penalty
        self._ollama = ollama_ribosome

        # NLI model — lazy-loaded (optional, may not exist yet)
        self._nli = None
        self._nli_model_path = nli_model_path

        log.info("DeBERTa ribosome ready on %s", self._device)

    # ── Re-rank ────────────────────────────────────────────────────────

    def re_rank(self, query: str, candidates: List[Gene], k: int = 5) -> List[Gene]:
        """Score candidate documents by relevance using the cross-encoder."""
        if not candidates:
            return []
        if len(candidates) <= k:
            return candidates

        t0 = time.perf_counter()

        from helix_context.hardware import recommended_batch_size
        batch_size = recommended_batch_size("rerank")

        # Build text pairs
        texts_a = []
        texts_b = []
        for g in candidates:
            texts_a.append(query)
            summary = g.promoter.summary
            domains = ", ".join(g.promoter.domains)
            texts_b.append(f"{summary} [{domains}]" if domains else summary)

        # Score in chunks sized by the hardware-recommended batch size so
        # large candidate pools don't blow up VRAM. Chunked tokenize +
        # forward, then concatenate the per-chunk score lists.
        scores: List[float] = []
        with torch.no_grad():
            for i in range(0, len(texts_a), batch_size):
                chunk_a = texts_a[i : i + batch_size]
                chunk_b = texts_b[i : i + batch_size]
                encodings = self._rerank_tokenizer(
                    chunk_a,
                    chunk_b,
                    truncation=True,
                    max_length=256,
                    padding=True,
                    return_tensors="pt",
                ).to(self._device)
                outputs = self._rerank_model(**encodings)
                chunk_scores = outputs.logits.squeeze(-1)
                chunk_scores = torch.clamp(chunk_scores, 0.0, 1.0).cpu().tolist()
                if isinstance(chunk_scores, float):
                    chunk_scores = [chunk_scores]
                scores.extend(chunk_scores)

        # For pretrained cross-encoders (e.g. MS MARCO), skip position bonus —
        # they produce calibrated relevance scores that don't need retrieval-order bias.
        # For custom fine-tuned models, blend with retrieval position bonus.
        if self._rerank_pretrained:
            scored = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
        else:
            n = len(candidates)
            blended = []
            for i, (score, gene) in enumerate(zip(scores, candidates)):
                position_bonus = 0.3 * (1.0 - i / max(n, 1))
                blended.append((score + position_bonus, gene))
            scored = sorted(blended, key=lambda x: x[0], reverse=True)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "DeBERTa re_rank: %d candidates → top %d in %.1fms",
            len(candidates), k, elapsed_ms,
        )

        return [g for _, g in scored[:k]]

    # ── Splice ─────────────────────────────────────────────────────────

    def splice(
        self,
        query: str,
        genes: List[Gene],
        min_codons_kept: int = 2,
    ) -> Dict[str, str]:
        """Classify each fragment as keep/drop using the binary classifier."""
        if not genes:
            return {}

        t0 = time.perf_counter()
        result: Dict[str, str] = {}

        # Batch all (query, fragment) pairs across all documents
        all_pairs_a = []
        all_pairs_b = []
        pair_index = []  # (gene_idx, codon_idx) for reconstruction

        for gi, g in enumerate(genes):
            for ci, codon in enumerate(g.codons):
                all_pairs_a.append(query)
                all_pairs_b.append(codon)
                pair_index.append((gi, ci))

        if not all_pairs_a:
            return {g.gene_id: g.complement or g.content[:500] for g in genes}

        from helix_context.hardware import recommended_batch_size
        batch_size = recommended_batch_size("splice")

        # Predict in chunks sized by the hardware-recommended batch size.
        probs: List[float] = []
        with torch.no_grad():
            for i in range(0, len(all_pairs_a), batch_size):
                chunk_a = all_pairs_a[i : i + batch_size]
                chunk_b = all_pairs_b[i : i + batch_size]
                encodings = self._splice_tokenizer(
                    chunk_a,
                    chunk_b,
                    truncation=True,
                    max_length=128,
                    padding=True,
                    return_tensors="pt",
                ).to(self._device)
                outputs = self._splice_model(**encodings)
                logits = outputs.logits.squeeze(-1)
                chunk_probs = torch.sigmoid(logits).cpu().tolist()
                if isinstance(chunk_probs, float):
                    chunk_probs = [chunk_probs]
                probs.extend(chunk_probs)

        # Extract query keywords for content-match preservation
        query_lower = query.lower().split()
        query_keywords = {w.strip("?.,!;:'\"()[]{}") for w in query_lower if len(w) > 2}

        # Reconstruct per-document decisions
        gene_keep: Dict[int, List[int]] = {i: [] for i in range(len(genes))}
        for (gi, ci), prob in zip(pair_index, probs):
            # Always keep fragments that contain query terms (content-match preservation)
            codon_lower = genes[gi].codons[ci].lower() if ci < len(genes[gi].codons) else ""
            content_match = any(kw in codon_lower for kw in query_keywords)
            if prob >= self.splice_threshold or content_match:
                gene_keep[gi].append(ci)

        # Build spliced text
        for gi, g in enumerate(genes):
            kept_indices = gene_keep[gi]

            # Empty splice guard
            if not kept_indices and g.codons:
                kept_indices = list(range(min(min_codons_kept, len(g.codons))))
                log.info("Empty splice for gene %s, keeping first %d codons", g.gene_id, len(kept_indices))

            if kept_indices:
                kept = [g.codons[i] for i in kept_indices if i < len(g.codons)]
                result[g.gene_id] = " | ".join(kept) if kept else (g.complement or g.content[:500])
            else:
                result[g.gene_id] = g.complement or g.content[:500]

        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "DeBERTa splice: %d genes, %d codons in %.1fms",
            len(genes), len(all_pairs_a), elapsed_ms,
        )

        return result

    # ── NLI Classification ───────────────────────────────────────────────

    def _load_nli(self):
        """Lazy-load the NLI classifier on first use."""
        if self._nli is not None:
            return self._nli
        try:
            from .nli_backend import NLIClassifier
            self._nli = NLIClassifier(
                model_path=self._nli_model_path,
                device=str(self._device),
            )
        except Exception:
            log.warning("NLI model not available at %s", self._nli_model_path, exc_info=True)
            self._nli = None
        return self._nli

    def classify_relations(self, genes: List[Gene]) -> Dict:
        """Classify NLI relations between retrieved documents. Returns relation graph."""
        nli = self._load_nli()
        if nli is None:
            return {}
        return nli.build_relation_graph(genes)

    # ── Delegated to Ollama ────────────────────────────────────────────

    def pack(self, content: str, content_type: str = "text") -> Gene:
        """Delegate to Ollama compressor (needs generation)."""
        if self._ollama is None:
            raise RuntimeError("DeBERTa ribosome requires an Ollama fallback for pack()")
        return self._ollama.encode(content, content_type)

    def replicate(self, query: str, response: str) -> Gene:
        """Delegate to Ollama compressor (needs generation)."""
        if self._ollama is None:
            raise RuntimeError("DeBERTa ribosome requires an Ollama fallback for replicate()")
        return self._ollama.persist(query, response)
