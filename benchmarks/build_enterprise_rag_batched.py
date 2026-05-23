r"""Batched ingestion for EnterpriseRAG-Bench corpus.

Uses ``spacy.Language.pipe(batch_size=N)`` to amortize spaCy NER cost
across many strands — typically 3-5x throughput on small docs vs the
sequential ``tagger.pack()`` path used by build_fixture_matrix.

Trade-offs vs build_fixture_matrix sharded mode:
  + 3-5x faster on small JSON corpora (spacy.pipe batching)
  + Lower memory footprint (one process, streamed batches)
  + Simpler progress logging
  - Single process (no parallel shards) — but with batching, often still wins on net
  - Blob output only (no sharded mode)

The output matches what build_fixture_matrix produces in blob mode
(same Genome schema, same gene format), so existing /context queries
work against the resulting .db.

Usage:
  python benchmarks/build_enterprise_rag_batched.py \
      --in-dir F:/tmp/enterprise_rag_10k/sources \
      --out F:/Projects/helix-context/genomes/bench/matrix/enterprise_rag_10k_batched.db \
      --batch-size 64

  python benchmarks/build_enterprise_rag_batched.py \
      --in-dir F:/tmp/enterprise_rag_50k/sources \
      --out F:/Projects/helix-context/genomes/bench/matrix/enterprise_rag_50k_batched.db \
      --batch-size 96 --files-per-chunk 512
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE))

from helix_context.tagger import CpuTagger, _get_nlp
from helix_context.genome import Genome
from helix_context.codons import CodonChunker
from helix_context.schemas import Gene, PromoterTags, EpigeneticMarkers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bench.batched")


MIN_FILE_SIZE = 50
MAX_FILE_SIZE = 200_000


def collect_strands(in_dir: Path, chunker: CodonChunker,
                    max_files: int | None = None) -> list[tuple[str, str, int, bool]]:
    """Walk in_dir for .json files, chunk each, return flat list of
    ``(strand_content, source_id, sequence_index, is_fragment)`` tuples."""
    strands = []
    n_files = 0
    n_skipped = 0
    t0 = time.perf_counter()
    for fp in in_dir.rglob("*.json"):
        if fp.name.lower() == "agents.md":
            n_skipped += 1; continue
        try:
            sz = fp.stat().st_size
        except OSError:
            n_skipped += 1; continue
        if sz < MIN_FILE_SIZE or sz > MAX_FILE_SIZE:
            n_skipped += 1; continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            n_skipped += 1; continue
        for i, s in enumerate(chunker.chunk(content, content_type="code")):
            strands.append((s.content, str(fp), i, s.is_fragment))
        n_files += 1
        if max_files and n_files >= max_files:
            break
    elapsed = time.perf_counter() - t0
    log.info("collected %d strands from %d files (%d skipped) in %.1fs",
             len(strands), n_files, n_skipped, elapsed)
    return strands


def batched_pack_and_write(strands: list[tuple[str, str, int, bool]],
                            genome: Genome, tagger: CpuTagger,
                            batch_size: int = 64) -> dict:
    """Pack strands in batches using nlp.pipe, write each gene immediately."""
    nlp = _get_nlp()
    stats = {"genes": 0, "errors": 0, "t0": time.perf_counter()}

    # nlp.pipe streams docs as they're ready. We pair each doc with its
    # source tuple, then run the rest of the tagger.pack() steps inline.
    truncated_texts = (s[0][:50_000] for s in strands)
    doc_iter = nlp.pipe(truncated_texts, batch_size=batch_size)

    for (content, source_id, seq_i, is_frag), doc in zip(strands, doc_iter):
        try:
            # Mirrors tagger.pack() steps 1-7 with pre-computed doc.
            entities = tagger._extract_entities(doc, content)
            domains = tagger._extract_domains(doc, content, entities)
            filename_domains = tagger._extract_filename_domains(source_id)
            if filename_domains:
                seen = set(domains)
                prepend = [t for t in filename_domains if t not in seen]
                domains = prepend + domains
            key_values = tagger._extract_key_values(content)
            codons = tagger._extract_codons(doc, content, "code")
            complement = tagger._extract_complement(doc, content)
            intent = tagger._extract_intent(doc, content, "code")
            intent_class = tagger._classify_intent(intent)
            summary = tagger._extract_summary(doc, content)

            gene_id = Genome.make_gene_id(content)
            gene = Gene(
                gene_id=gene_id,
                content=content,
                complement=complement,
                codons=codons,
                promoter=PromoterTags(
                    domains=domains[:10],
                    entities=entities[:15],
                    intent=intent,
                    intent_class=intent_class,
                    summary=summary,
                    sequence_index=seq_i,
                ),
                epigenetics=EpigeneticMarkers(),
                key_values=key_values,
                source_id=source_id,
            )
            gene.is_fragment = is_frag
            genome.upsert_gene(gene)
            stats["genes"] += 1
        except Exception:
            stats["errors"] += 1
            if stats["errors"] < 5:
                log.exception("pack failed for %s", source_id)

        if stats["genes"] > 0 and stats["genes"] % 500 == 0:
            elapsed = time.perf_counter() - stats["t0"]
            log.info("  packed %d / %d strands in %.1fs (%.1f genes/s)",
                     stats["genes"], len(strands), elapsed,
                     stats["genes"] / max(elapsed, 0.001))

    stats["elapsed_s"] = time.perf_counter() - stats["t0"]
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in-dir", required=True, type=Path,
                        help="Source root (e.g. F:/tmp/enterprise_rag_10k/sources)")
    parser.add_argument("--out", required=True, type=Path,
                        help="Output .db path")
    parser.add_argument("--batch-size", type=int, default=64,
                        help="spacy.pipe batch_size (default 64)")
    parser.add_argument("--max-chars", type=int, default=4000,
                        help="CodonChunker max_chars_per_strand (default 4000). "
                             "Raise (e.g. 16000) for wider/whole-doc strands.")
    parser.add_argument("--max-files", type=int, default=None,
                        help="Cap file count (for fast smoke testing)")
    args = parser.parse_args()

    if not args.in_dir.is_dir():
        log.error("in-dir not found: %s", args.in_dir)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        log.info("removing existing %s", args.out)
        args.out.unlink()

    log.info("=== batched ingest ===")
    log.info("in-dir=%s out=%s batch=%d max_chars=%d",
             args.in_dir, args.out, args.batch_size, args.max_chars)

    log.info("[1/3] init genome + tagger...")
    t0 = time.perf_counter()
    genome = Genome(path=str(args.out), synonym_map={},
                    splade_enabled=True, entity_graph=True)
    tagger = CpuTagger()
    chunker = CodonChunker(max_chars_per_strand=args.max_chars)
    log.info("  init in %.1fs", time.perf_counter() - t0)

    log.info("[2/3] collect strands from %s...", args.in_dir)
    strands = collect_strands(args.in_dir, chunker, max_files=args.max_files)

    log.info("[3/3] batched pack + write (batch_size=%d)...", args.batch_size)
    stats = batched_pack_and_write(strands, genome, tagger, args.batch_size)

    final = genome.stats()
    try:
        hl = genome.conn.execute(
            "SELECT COUNT(*) FROM harmonic_links"
        ).fetchone()[0]
    except Exception:
        hl = -1
    genome.close()

    log.info("=" * 60)
    log.info("DONE in %.1fs (%.1f min)", stats["elapsed_s"], stats["elapsed_s"] / 60)
    log.info("  genes packed: %d (errors=%d)", stats["genes"], stats["errors"])
    log.info("  genome.stats: %d total, %d harmonic_links",
             final.get("total_genes", -1), hl)
    log.info("  net rate: %.1f genes/s", stats["genes"] / max(stats["elapsed_s"], 0.001))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
