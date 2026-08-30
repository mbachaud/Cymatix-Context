"""Before/after receipt for the tagger v2 entity-hygiene fix (issue #410).

Tags each file of a deterministic EnronQA sample twice through
``CpuTagger.pack()`` — arm v1 with the ``_is_noise_entity`` gate bypassed
(byte-faithful to the pre-#410 tagger: the gate is the only behavioral
change site) and arm v2 with the shipped gate — then diffs what
``entity_graph`` would hold: per-entity gene counts, the plumbing hubs
removed, multi-line entities, and how many genes' tag multiset changed
(the digest-invalidation evidence that forces the tagger-version bump).

Usage
-----
    python scripts/receipt_tagger_v2_entity_hygiene.py \\
        --corpus F:/Projects/enronqa/corpus --padding F:/Projects/enronqa/padding \\
        --sample 2000 --out benchmarks/dogfood/receipts/tagger_v2_entity_hygiene_2026-08-30.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

log = logging.getLogger("receipt_tagger_v2")


def _sample_files(roots: List[Path], n: int, seed: int) -> List[str]:
    """Deterministic sample spread across roots proportionally to size."""
    per_root: Dict[str, List[str]] = {}
    for root in roots:
        found: List[str] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                if fname.lower().endswith(".txt"):
                    found.append(os.path.join(dirpath, fname))
        per_root[str(root)] = sorted(found)
        log.info("  %-40s %7d files", root, len(found))
    total = sum(len(v) for v in per_root.values())
    if not total:
        raise SystemExit(f"no .txt files under {roots}")
    rng = random.Random(seed)
    picked: List[str] = []
    for name in sorted(per_root):
        files = per_root[name]
        share = max(1, round(n * len(files) / total))
        picked.extend(rng.sample(files, min(share, len(files))))
    return sorted(picked)[:n]


def _tag_arm(files: List[str], noise_gate_on: bool) -> Tuple[dict, List[dict]]:
    """Tag every chunk of every file; return aggregate + per-gene rows.

    ``noise_gate_on=False`` reproduces tagger v1 by bypassing
    ``_is_noise_entity`` — the only change site of the v2 fix.
    """
    from cymatix_context import tagger as tagger_mod
    from cymatix_context.codons import CodonChunker
    from cymatix_context.tagger import CpuTagger

    original_gate = tagger_mod._is_noise_entity
    if not noise_gate_on:
        tagger_mod._is_noise_entity = lambda text: False
    try:
        chunker = CodonChunker()
        tagger = CpuTagger()
        entity_rows: Counter = Counter()  # entity -> gene count (entity_graph shape)
        multiline_entities: Counter = Counter()
        genes: List[dict] = []
        errors = 0
        for fpath in files:
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                errors += 1
                log.warning("file read failed: %s", fpath, exc_info=True)
                continue
            try:
                strands = chunker.chunk(content, content_type="text")
                for i, strand in enumerate(strands):
                    gene = tagger.pack(
                        strand.content,
                        content_type="text",
                        source_id=fpath,
                        sequence_index=i,
                    )
                    # entity_graph inserts entities[:15], lowercased, distinct
                    graph_entities = {
                        e.lower() for e in gene.promoter.entities[:15]
                    }
                    entity_rows.update(graph_entities)
                    for e in gene.promoter.entities:
                        if "\n" in e or "\r" in e:
                            multiline_entities[e] += 1
                    tags = sorted(
                        [d.lower() for d in gene.promoter.domains]
                        + [e.lower() for e in gene.promoter.entities]
                    )
                    genes.append({
                        "gene_id": gene.gene_id,
                        "source": fpath,
                        "seq": i,
                        "tags": tags,
                        "entities": [e.lower() for e in gene.promoter.entities],
                    })
            except Exception:
                errors += 1
                log.warning("chunk/tag failed: %s", fpath, exc_info=True)
    finally:
        tagger_mod._is_noise_entity = original_gate

    aggregate = {
        "genes": len(genes),
        "errors": errors,
        "entity_graph_rows": sum(entity_rows.values()),
        "distinct_entities": len(entity_rows),
        "top25_hubs": dict(entity_rows.most_common(25)),
        "multiline_entity_kinds": len(multiline_entities),
        "multiline_entity_occurrences": sum(multiline_entities.values()),
        "multiline_examples": [k for k, _ in multiline_entities.most_common(5)],
        "_entity_rows": entity_rows,
    }
    return aggregate, genes


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--corpus", default=r"F:\Projects\enronqa\corpus")
    p.add_argument("--padding", default=r"F:\Projects\enronqa\padding")
    p.add_argument("--sample", type=int, default=2000)
    p.add_argument("--seed", type=int, default=20260830)
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)

    from cymatix_context.tagger import TAGGER_VERSION

    roots = [Path(args.corpus)]
    if args.padding and Path(args.padding).exists():
        roots.append(Path(args.padding))
    files = _sample_files(roots, args.sample, args.seed)
    log.info("sampled %d files (seed=%d)", len(files), args.seed)

    t0 = time.perf_counter()
    log.info("--- arm v1: noise gate bypassed (pre-#410 behavior) ---")
    v1, v1_genes = _tag_arm(files, noise_gate_on=False)
    log.info("--- arm v2: shipped entity hygiene ---")
    v2, v2_genes = _tag_arm(files, noise_gate_on=True)
    wall = time.perf_counter() - t0

    v1_rows: Counter = v1.pop("_entity_rows")
    v2_rows: Counter = v2.pop("_entity_rows")

    removed = {e: n for e, n in v1_rows.items() if e not in v2_rows}
    removed_top25 = dict(
        sorted(removed.items(), key=lambda kv: kv[1], reverse=True)[:25]
    )

    v1_by_id = {g["gene_id"]: g for g in v1_genes}
    v2_by_id = {g["gene_id"]: g for g in v2_genes}
    gene_ids_equal = set(v1_by_id) == set(v2_by_id)
    tags_changed = sum(
        1 for gid in v1_by_id
        if gid in v2_by_id and v1_by_id[gid]["tags"] != v2_by_id[gid]["tags"]
    )

    survival_checks = {
        e: {"v1_rows": v1_rows.get(e, 0), "v2_rows": v2_rows.get(e, 0)}
        for e in ("enron", "ect", "hou")
    }

    receipt = {
        "tool": "receipt_tagger_v2_entity_hygiene",
        "issue": "https://github.com/mbachaud/Cymatix-Context/issues/410",
        "stamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tagger_version_after": TAGGER_VERSION,
        "arms_note": (
            "v1 = current code with _is_noise_entity bypassed (the gate is "
            "the only behavioral change site of the v2 fix); v2 = shipped"
        ),
        "sample": {
            "roots": [str(r) for r in roots],
            "files": len(files),
            "seed": args.seed,
        },
        "wall_s": round(wall, 1),
        "v1": v1,
        "v2": v2,
        "delta": {
            "entity_graph_rows": v2["entity_graph_rows"] - v1["entity_graph_rows"],
            "entity_graph_rows_pct": round(
                100.0
                * (v2["entity_graph_rows"] - v1["entity_graph_rows"])
                / max(v1["entity_graph_rows"], 1),
                2,
            ),
            "distinct_entities_removed": len(removed),
            "removed_top25_by_rows": removed_top25,
            "gene_ids_equal": gene_ids_equal,
            "genes_with_changed_tag_multiset": tags_changed,
            "genes_total": len(v1_by_id),
            "survival_checks": survival_checks,
        },
        "comparability_note": (
            "gene_ids identical (content untouched) but tag multisets differ "
            "on the changed genes -> the ingest_equivalence_gate content "
            "digest changes: beds built at tagger_version 2 are not "
            "comparable to tagger_version 1 beds (BASELINES.md rule)"
        ),
    }

    out = json.dumps(receipt, indent=2, sort_keys=False)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out, encoding="utf-8")
        log.info("receipt -> %s", out_path)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
