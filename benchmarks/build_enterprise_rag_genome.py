r"""Build a helix genome from the EnterpriseRAG-Bench corpus.

Adapts ``scripts/build_bench_genomes.py`` to point at
``F:/Projects/EnterpriseRAG-Bench-main/generated_data/sources/`` —
500K JSON docs spanning 9 sources (Slack, Gmail, Linear, …).

Modes:
  --mode blob       single .db ingested from all sources
  --mode sharded    one shard per source (9 shards + main routing db)
  --mode smoke      blob with just `--smoke-sources` (default confluence+fireflies, ~15K files)

Each JSON doc is ingested as helix code-content (.json is in CODE_EXTS).
File-name root ``agents.md`` (scaffolding metadata) is skipped — those
files describe content rules, not actual corpus content.

Usage:
  # smoke (~5 min)
  python benchmarks/build_enterprise_rag_genome.py --mode smoke \
      --out F:/Projects/helix-context/genomes/bench/enterprise_rag_smoke.db

  # full blob (~1-2 hr est)
  python benchmarks/build_enterprise_rag_genome.py --mode blob \
      --out F:/Projects/helix-context/genomes/bench/enterprise_rag_blob.db

  # sharded (one .genome.db per source + main routing)
  python benchmarks/build_enterprise_rag_genome.py --mode sharded \
      --out-dir F:/Projects/helix-context/genomes/bench/enterprise_rag_sharded
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# helix_context lives in the worktree; benchmarks/ is its sibling
WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE))

from helix_context.tagger import CpuTagger
from helix_context.genome import Genome
from helix_context.codons import CodonChunker


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bench.enterprise_rag.build")


CORPUS_ROOT = Path(r"F:/Projects/EnterpriseRAG-Bench-main/generated_data/sources")
ALL_SOURCES = ("confluence", "fireflies", "github", "gmail", "google_drive",
               "hubspot", "jira", "linear", "slack")

INGEST_EXTS = {".json"}      # corpus is JSON only — keep it tight
MAX_FILE_SIZE = 200_000      # matches build_bench_genomes — 200KB
MIN_FILE_SIZE = 50           # skip tiny artifact files
SKIP_FILENAMES = {"agents.md"}   # source-scaffolding metadata, not corpus content


def ingest_tree(
    root: Path, genome: Genome, tagger: CpuTagger, chunker: CodonChunker,
    stats: dict, source_label: str,
) -> None:
    """Walk ``root`` and ingest matching files."""
    for dirpath, dirnames, filenames in os.walk(root):
        # No SKIP_DIRS at this layer — the corpus is flat-by-design.
        for fname in filenames:
            if fname.lower() in SKIP_FILENAMES:
                stats["skipped_scaffolding"] += 1
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in INGEST_EXTS:
                stats["skipped_ext"] += 1
                continue

            fpath = os.path.join(dirpath, fname)
            try:
                size = os.path.getsize(fpath)
            except OSError:
                continue
            if size < MIN_FILE_SIZE:
                stats["skipped_small"] += 1
                continue
            if size > MAX_FILE_SIZE:
                stats["skipped_large"] += 1
                continue

            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except Exception:
                stats["errors"] += 1
                continue

            strands = chunker.chunk(content, content_type="code")
            for i, strand in enumerate(strands):
                try:
                    gene = tagger.pack(
                        strand.content,
                        content_type="code",
                        source_id=fpath,
                        sequence_index=i,
                    )
                    gene.is_fragment = strand.is_fragment
                    genome.upsert_gene(gene)
                    stats["genes"] += 1
                except Exception:
                    stats["errors"] += 1

            stats["files"] += 1
            per_src = stats.setdefault("per_source", {}).setdefault(
                source_label, {"files": 0, "genes": 0},
            )
            per_src["files"] += 1

            if stats["files"] % 500 == 0:
                elapsed = time.perf_counter() - stats["t0"]
                rate = stats["files"] / max(elapsed, 0.001)
                log.info(
                    "[%d files, %d genes] %.1f files/s | %s | %s",
                    stats["files"], stats["genes"], rate,
                    source_label, os.path.basename(dirpath)[:40],
                )


def build_blob(db_path: Path, sources: list[str]) -> dict:
    """Ingest ``sources`` into a single .db file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        log.info("Removing existing %s", db_path)
        db_path.unlink()

    log.info("Opening fresh genome at %s", db_path)
    genome = Genome(
        path=str(db_path), synonym_map={},
        splade_enabled=True, entity_graph=True,
    )
    tagger = CpuTagger()
    chunker = CodonChunker()

    stats = {
        "files": 0, "genes": 0, "errors": 0,
        "skipped_ext": 0, "skipped_small": 0,
        "skipped_large": 0, "skipped_scaffolding": 0,
        "t0": time.perf_counter(),
    }

    for src in sources:
        root = CORPUS_ROOT / src
        if not root.exists():
            log.warning("Source missing: %s", root)
            continue
        log.info("=== Ingesting source: %s ===", src)
        before = stats["files"]
        ingest_tree(root, genome, tagger, chunker, stats, src)
        log.info("  done %s: +%d files", src, stats["files"] - before)

    elapsed = time.perf_counter() - stats["t0"]
    stats["elapsed_s"] = elapsed
    genome_stats = genome.stats()
    stats["total_genes_final"] = int(genome_stats.get("total_genes", -1))
    try:
        hl = genome.conn.execute(
            "SELECT COUNT(*) AS n FROM harmonic_links"
        ).fetchone()
        stats["harmonic_links"] = int(hl["n"]) if hl else 0
    except Exception:
        stats["harmonic_links"] = -1

    genome.close()

    log.info("=" * 60)
    log.info("DONE %s in %.1fs (%.1f min)", db_path.name, elapsed, elapsed / 60)
    log.info(
        "  files: %d ingested, skipped_ext=%d skipped_small=%d "
        "skipped_large=%d scaffolding=%d errors=%d",
        stats["files"], stats["skipped_ext"], stats["skipped_small"],
        stats["skipped_large"], stats["skipped_scaffolding"], stats["errors"],
    )
    log.info(
        "  genes: %d ingested (%d in db); harmonic_links=%d",
        stats["genes"], stats["total_genes_final"], stats["harmonic_links"],
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode", choices=["blob", "sharded", "smoke"], required=True,
        help="Fixture mode",
    )
    parser.add_argument(
        "--out", type=Path,
        help="Output .db path (blob/smoke modes)",
    )
    parser.add_argument(
        "--out-dir", type=Path,
        help="Output directory (sharded mode; creates main.genome.db "
             "+ per-source <name>.genome.db inside)",
    )
    parser.add_argument(
        "--smoke-sources", default="confluence,fireflies",
        help="Comma-separated sources for --mode smoke "
             "(default: confluence,fireflies)",
    )
    parser.add_argument(
        "--only", default=None,
        help="Comma-separated sources for --mode blob "
             "(default: all 9)",
    )
    args = parser.parse_args()

    if args.mode == "smoke":
        if args.out is None:
            log.error("--mode smoke requires --out")
            return 2
        sources = [s.strip() for s in args.smoke_sources.split(",")]
        log.info("MODE=smoke sources=%s out=%s", sources, args.out)
        build_blob(args.out, sources)
        return 0

    if args.mode == "blob":
        if args.out is None:
            log.error("--mode blob requires --out")
            return 2
        sources = list(ALL_SOURCES)
        if args.only:
            wanted = [s.strip() for s in args.only.split(",")]
            sources = [s for s in sources if s in wanted]
        log.info("MODE=blob sources=%s out=%s", sources, args.out)
        build_blob(args.out, sources)
        return 0

    if args.mode == "sharded":
        log.error("sharded mode not yet implemented — planned next iteration")
        return 2

    log.error("unknown mode: %s", args.mode)
    return 2


if __name__ == "__main__":
    sys.exit(main())
