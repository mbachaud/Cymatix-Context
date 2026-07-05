r"""sike_bed_ingest.py -- inject the SIKE curated-needle gold docs into a
distractor-bed genome copy (issue #221 bed-sweep, chain stage S2 helper).

WHY
---
``benchmarks/bench_needle.py`` does NOT self-ingest its needles. It queries
``/context`` and scores whether each needle's ``gold_source`` file shows up in
the delivered ``<GENE src="...">`` set (and whether the block body carries the
answer). So for a fixed SIKE question set swept across arbitrary DISTRACTOR
beds, the gold source *files* must be present in the bed we serve -- otherwise
every needle is a guaranteed miss and the sweep measures nothing.

This helper reads the canonical needle list straight out of
``benchmarks/bench_needle.py`` (``NEEDLES``), collects every distinct
``gold_source`` path substring, resolves each to a real file on disk under a
set of project roots (default ``F:\Projects``), and ingests those files into
the target bed copy with ``source_id`` set to the on-disk path -- exactly the
provenance shape ``helix ingest`` uses (``metadata={"path":..,
"source_id":..}``, see helix_context/cli/cmd_ingest.py) so bench_needle's
bidirectional-substring gold match fires.

Ingest goes through a read-write ``KnowledgeStore`` opened directly on the bed
copy (no server round-trip): faster, and it matches how the beds were built
(scripts/build_bench_genomes.py). content_type is inferred from the extension
(code vs text) the same way the CLI does (#224), so code golds get the code
chunker path.

IDEMPOTENT / RESUME-SAFE
------------------------
``KnowledgeStore.upsert_doc`` is keyed on a deterministic gene_id derived from
content, so re-running against a bed that already has the golds is a no-op
rewrite (no duplication). A ``--probe-only`` mode reports how many gold
source files are already resolvable + already present without writing.

CLI
---
  python scripts/bench_chain/sike_bed_ingest.py --genome <bed.db> \
      [--roots F:\Projects] [--json] [--probe-only]

Exit codes: 0 ok (>=1 gold file ingested or already present), 2 hard failure
(no needle list, no resolvable gold files, genome open failure). Stdlib + repo
imports only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Repo root is scripts/bench_chain/ -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# benchmarks/ must be importable to read the canonical NEEDLES list.
_BENCH_DIR = _REPO_ROOT / "benchmarks"
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))


# Extension -> content_type, mirroring cli/cmd_ingest._content_type_for (#224).
_CODE_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".c",
    ".h", ".cc", ".cpp", ".hpp", ".hh", ".cs", ".rb", ".php", ".lua",
    ".scala", ".kt", ".kts", ".swift", ".sh", ".bash", ".sql", ".m", ".mm",
    ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".bat", ".ps1",
})
_MAX_FILE_BYTES = 400_000  # generous: SIKE golds are small config/doc files.


def _content_type_for(path: Path) -> str:
    return "code" if path.suffix.lower() in _CODE_EXTS else "text"


def _load_gold_sources() -> list[str]:
    """Return the distinct ``gold_source`` path substrings across all needles.

    Reads ``benchmarks/bench_needle.py``'s NEEDLES so the sweep never drifts
    from the harness's own gold list. We DELIBERATELY skip pure-directory
    entries (e.g. ``helix-context/docs``) and the bench answer-key doc -- a
    directory can't be ingested as a single file and the answer-key would
    inflate recall circularly (the harness's own DO-NOT-ADD rule).
    """
    try:
        import bench_needle  # type: ignore
    except Exception as exc:  # pragma: no cover - reported by caller
        raise RuntimeError(
            "could not import benchmarks/bench_needle.py to read NEEDLES: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    needles = getattr(bench_needle, "NEEDLES", None)
    if not needles:
        raise RuntimeError("bench_needle.NEEDLES is empty or missing")

    seen: set[str] = set()
    out: list[str] = []
    for nd in needles:
        for gs in nd.get("gold_source", []) or []:
            g = str(gs).replace("\\", "/").strip()
            if not g or g in seen:
                continue
            # Skip bare-directory gold hints (no file extension in the last
            # segment) -- not individually ingestable.
            last = g.rsplit("/", 1)[-1]
            if "." not in last:
                continue
            # Never ingest the bench answer-key (circular recall).
            if "benchmarks/benchmarks.md" in g.lower():
                continue
            seen.add(g)
            out.append(g)
    return out


def _resolve(gold_substr: str, roots: list[Path]) -> Path | None:
    """Resolve a project-relative gold substring to a real file on disk.

    ``gold_source`` entries are project-relative like
    ``helix-context/helix.toml`` or ``Education/CLAUDE.md``. Try each root as a
    prefix; the sibling repos (Education, BookKeeper, two-brain-audit, ...)
    live directly under a projects root on the rig, and the substring already
    carries the repo dir as its first segment.
    """
    rel = gold_substr.replace("/", os.sep)
    for root in roots:
        cand = root / rel
        if cand.is_file():
            return cand
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--genome", required=True,
                    help="Path to the bed genome copy to ingest golds into.")
    ap.add_argument(
        "--roots", default=os.environ.get("SIKE_GOLD_ROOTS", r"F:\Projects"),
        help="os.pathsep-separated project roots to resolve gold paths under "
             r"(default F:\Projects).",
    )
    ap.add_argument("--json", action="store_true",
                    help="Emit a machine-readable JSON summary on stdout.")
    ap.add_argument("--probe-only", action="store_true",
                    help="Report resolvable/present golds; do not write.")
    args = ap.parse_args(argv)

    roots = [Path(r) for r in str(args.roots).split(os.pathsep) if r.strip()]
    summary: dict = {
        "genome": args.genome,
        "roots": [str(r) for r in roots],
        "gold_sources_total": 0,
        "resolved_files": 0,
        "unresolved": [],
        "ingested_files": 0,
        "genes_written": 0,
        "already_present_files": 0,
        "errors": [],
    }

    # 1. Collect gold source substrings from the canonical needle list.
    try:
        gold_sources = _load_gold_sources()
    except Exception as exc:
        summary["errors"].append(str(exc))
        _emit(summary, args.json)
        return 2
    summary["gold_sources_total"] = len(gold_sources)

    # 2. Resolve each to a real file (de-dup by resolved path).
    resolved: dict[str, Path] = {}
    for gs in gold_sources:
        f = _resolve(gs, roots)
        if f is None:
            summary["unresolved"].append(gs)
        else:
            resolved[str(f)] = f
    summary["resolved_files"] = len(resolved)

    if not resolved:
        summary["errors"].append(
            "no gold source files resolvable under roots "
            f"{summary['roots']}; bench_needle would score 0/N on this bed"
        )
        _emit(summary, args.json)
        return 2

    if args.probe_only:
        _emit(summary, args.json)
        return 0

    # 3. Open the bed copy read-write and ingest each gold file.
    try:
        from helix_context.knowledge_store import KnowledgeStore
        from helix_context.codons import CodonChunker
        from helix_context.tagger import CpuTagger
    except Exception as exc:
        summary["errors"].append(
            f"repo import failed: {type(exc).__name__}: {exc}"
        )
        _emit(summary, args.json)
        return 2

    try:
        ks = KnowledgeStore(path=args.genome)
    except Exception as exc:
        summary["errors"].append(
            f"could not open genome {args.genome}: "
            f"{type(exc).__name__}: {exc}"
        )
        _emit(summary, args.json)
        return 2

    chunker = CodonChunker()
    tagger = CpuTagger()

    try:
        for src_path, f in sorted(resolved.items()):
            try:
                size = f.stat().st_size
                if size == 0 or size > _MAX_FILE_BYTES:
                    summary["errors"].append(
                        f"skip {src_path} (size {size} out of bounds)"
                    )
                    continue
                content = f.read_text(encoding="utf-8", errors="replace")
                if not content.strip():
                    continue
                ct = _content_type_for(f)
                # Detect prior presence: is any gene already carrying this
                # source_id? (resume-safe accounting; ingest itself is a
                # deterministic-id no-op rewrite regardless.)
                try:
                    row = ks.conn.execute(
                        "SELECT COUNT(*) FROM genes WHERE source_id = ?",
                        (src_path,),
                    ).fetchone()
                    if row and int(row[0]) > 0:
                        summary["already_present_files"] += 1
                except Exception:
                    pass

                strands = chunker.chunk(content, content_type=ct)
                n_written = 0
                for i, strand in enumerate(strands):
                    gene = tagger.pack(
                        strand.content,
                        content_type=ct,
                        source_id=src_path,
                        sequence_index=i,
                    )
                    gene.is_fragment = strand.is_fragment
                    ks.upsert_doc(gene, apply_gate=False)
                    n_written += 1
                summary["ingested_files"] += 1
                summary["genes_written"] += n_written
            except Exception as exc:
                summary["errors"].append(
                    f"ingest {src_path} failed: {type(exc).__name__}: {exc}"
                )
    finally:
        try:
            ks.close()
        except Exception:
            pass

    _emit(summary, args.json)
    # Success if we ingested at least one gold file (or all were already
    # present). Hard failure only if nothing usable happened.
    if summary["ingested_files"] > 0 or summary["already_present_files"] > 0:
        return 0
    return 2


def _emit(summary: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, indent=2))
        return
    print("=" * 60)
    print("SIKE gold ingest -> {}".format(summary["genome"]))
    print("  gold sources in needle list : {}".format(
        summary["gold_sources_total"]))
    print("  resolved to files on disk   : {}".format(
        summary["resolved_files"]))
    print("  ingested files              : {}".format(
        summary["ingested_files"]))
    print("  genes written               : {}".format(
        summary["genes_written"]))
    print("  already present (resume)    : {}".format(
        summary["already_present_files"]))
    if summary["unresolved"]:
        print("  UNRESOLVED ({}) -- not on disk under roots:".format(
            len(summary["unresolved"])))
        for u in summary["unresolved"][:20]:
            print("      {}".format(u))
    if summary["errors"]:
        print("  errors ({}):".format(len(summary["errors"])))
        for e in summary["errors"][:20]:
            print("      {}".format(e))


if __name__ == "__main__":
    raise SystemExit(main())
