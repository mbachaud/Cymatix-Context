"""In-process sharded receipt for the #264/#265 rank-merge fixes.

Deterministic, no server / GPU / network. Builds two small real on-disk
shard fixtures that reproduce each defect, then sweeps the cross-shard merge
over ``fusion_mode`` x ``doc_type_boost_mode`` (#264) and ``fusion_mode`` x
``HELIX_SHARD_GLOBAL_IDF`` (#265), reporting recall@10 / MRR over a small
multi-needle bed plus the exact flip case each issue documents.

    PYTHONPATH=<worktree> python scripts/receipt_264_265_shard_rank_merge.py

Writes docs/research/data/2026-07-16-shard-rank-merge-264-265-receipt.json.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)
from cymatix_context.shard_router import ShardRouter
from cymatix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)


def _mk(content, domains, entities, source):
    return Gene(
        gene_id="", content=content, complement=content[:50], codons=[],
        promoter=PromoterTags(domains=domains, entities=entities, sequence_index=0),
        epigenetics=EpigeneticMarkers(), chromatin=ChromatinState.OPEN,
        is_fragment=False, source_id=source,
    )


def _recall_mrr(router, needles, k=10):
    """needles: list of (domains, entities, gold_id). Returns (recall@k, MRR)."""
    hits = 0
    rr = 0.0
    for domains, entities, gold in needles:
        res = router.query_genes(domains=domains, entities=entities, max_genes=k)
        ids = [g.gene_id for g in res][:k]
        if gold in ids:
            hits += 1
            rr += 1.0 / (ids.index(gold) + 1)
    n = len(needles)
    return hits / n, rr / n


def build_doc_type_fixture(root):
    """#264 bed: README (summary gold) vs keyword-dense impl in shard_a; an
    unrelated shard_b fingerprinted on the SAME acme/binary query so the
    genuine >=2-shard merge path (where the #121 boost fires) runs. Mirrors
    tests/test_shard_router.py::doc_type_boost_setup."""
    main_path = str(root / "dt_main.db")
    a, b = str(root / "dt_a.db"), str(root / "dt_b.db")
    ga = Genome(a)
    readme_id = ga.upsert_gene(_mk(
        "Acme Rust build overview. The release binary is around 4 MB.",
        ["acme"], ["rust"], "projects/acme-rs/README.md"), apply_gate=False)
    impl_id = ga.upsert_gene(_mk(
        "binary binary binary size size build target binary measured "
        "binary size binary footprint binary size binary",
        ["acme"], ["binary"], "projects/acme-rs/src/binary_size_report.rs"),
        apply_gate=False)
    ga.conn.close()
    ga._reader and ga._reader.close()
    gb = Genome(b)
    other_id = gb.upsert_gene(_mk(
        "Unrelated second shard. Auth tokens and JWT sessions.",
        ["acme"], ["binary"], "other/notes.md"), apply_gate=False)
    gb.conn.close()
    gb._reader and gb._reader.close()
    m = open_main_db(main_path)
    init_main_db(m)
    register_shard(m, "shard_a", "reference", a, gene_count=2)
    register_shard(m, "shard_b", "participant", b, gene_count=1)
    for gid, src, ents in (
        (readme_id, "projects/acme-rs/README.md", ["rust"]),
        (impl_id, "projects/acme-rs/src/binary_size_report.rs", ["binary"]),
    ):
        upsert_fingerprint(m, gene_id=gid, shard_name="shard_a", source_id=src,
                           domains_json=json.dumps(["acme"]),
                           entities_json=json.dumps(ents), key_values_json="[]")
    upsert_fingerprint(m, gene_id=other_id, shard_name="shard_b",
                       source_id="other/notes.md",
                       domains_json=json.dumps(["acme"]),
                       entities_json=json.dumps(["binary"]), key_values_json="[]")
    m.close()
    return {"main_path": main_path, "readme_id": readme_id, "impl_id": impl_id}


def build_idf_fixture(root):
    """#265 bed: local-vs-global IDF trap (gold widget doc vs wrong frob doc)."""
    main_path = str(root / "idf_main.db")
    a, b = str(root / "idf_a.db"), str(root / "idf_b.db")
    R = "topic"
    ga = Genome(a)
    gold_id = ga.upsert_gene(_mk(
        "topic widget widget widget widget gold answer payload "
        "the canonical widget specification lives here",
        ["topic"], [R, "gold"], "/a/gold_widget.md"), apply_gate=False)
    wrong_id = ga.upsert_gene(_mk(
        "topic frob frob frob frob wrong incumbent widget noise "
        "frobnicator handling and frob dispatch internals",
        ["topic"], [R, "wrong"], "/a/wrong_frob.md"), apply_gate=False)
    for i in range(4):
        ga.upsert_gene(_mk(f"topic widget filler entry number {i} with widget mention",
                           ["topic"], [R], f"/a/filler_{i}.md"), apply_gate=False)
    ga.conn.close()
    ga._reader and ga._reader.close()
    gb = Genome(b)
    b0 = None
    for i in range(24):
        gid = gb.upsert_gene(_mk(f"topic frob common boilerplate document {i} with frob content",
                                 ["topic"], [R], f"/b/frob_{i}.md"), apply_gate=False)
        if i == 0:
            b0 = gid
    gb.conn.close()
    gb._reader and gb._reader.close()
    m = open_main_db(main_path)
    init_main_db(m)
    register_shard(m, "shard_a", "reference", a, gene_count=6)
    register_shard(m, "shard_b", "participant", b, gene_count=24)
    for gid, src, ents in (
        (gold_id, "/a/gold_widget.md", [R, "gold"]),
        (wrong_id, "/a/wrong_frob.md", [R, "wrong"]),
    ):
        upsert_fingerprint(m, gene_id=gid, shard_name="shard_a", source_id=src,
                           domains_json=json.dumps(["topic"]),
                           entities_json=json.dumps(ents), key_values_json="[]")
    upsert_fingerprint(m, gene_id=b0, shard_name="shard_b", source_id="/b/frob_0.md",
                       domains_json=json.dumps(["topic"]),
                       entities_json=json.dumps([R]), key_values_json="[]")
    m.close()
    return {"main_path": main_path, "gold_id": gold_id, "wrong_id": wrong_id}


def run():
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    dt = build_doc_type_fixture(root)
    idf = build_idf_fixture(root)

    report = {"defect_264": {}, "defect_265": {}, "notes": []}

    # ---- #264: fusion x doc_type_boost_mode ----
    for fusion in ("rrf", "additive"):
        for mode in ("additive", "off", "rank"):
            r = ShardRouter(dt["main_path"], fusion_mode=fusion,
                            doc_type_boost_mode=mode)
            try:
                res = r.query_genes(domains=["acme"], entities=["binary"], max_genes=10)
                ids = [g.gene_id for g in res]
                sc = dict(r.last_query_scores)
                rid, iid = dt["readme_id"], dt["impl_id"]
                readme_rank = ids.index(rid) if rid in ids else None
                impl_rank = ids.index(iid) if iid in ids else None
                # #264 hard constraint: keyword-dense impl must stay >= README.
                flip = (readme_rank is not None and impl_rank is not None
                        and readme_rank < impl_rank)
                rec, mrr = _recall_mrr(r, [(["acme"], ["binary"], rid)], k=10)
                report["defect_264"][f"{fusion}/{mode}"] = {
                    "impl_rank": impl_rank, "readme_rank": readme_rank,
                    "impl_score": round(sc.get(iid, 0.0), 4),
                    "readme_score": round(sc.get(rid, 0.0), 4),
                    "flip_impl_below_readme": flip,
                    "readme_recall@10": rec, "readme_mrr": round(mrr, 4),
                }
            finally:
                r.close()

    # ---- #265: fusion x global-IDF flag ----
    for fusion in ("rrf", "additive"):
        for flag in ("0", "1"):
            if flag == "1":
                os.environ["HELIX_SHARD_GLOBAL_IDF"] = "1"
            else:
                os.environ.pop("HELIX_SHARD_GLOBAL_IDF", None)
            r = ShardRouter(idf["main_path"], fusion_mode=fusion)
            try:
                res = r.query_genes(domains=["topic"], entities=["widget", "frob"],
                                    max_genes=10)
                ids = [g.gene_id for g in res]
                gid, wid = idf["gold_id"], idf["wrong_id"]
                grank = ids.index(gid) if gid in ids else None
                wrank = ids.index(wid) if wid in ids else None
                gold_wins = (grank is not None and wrank is not None and grank < wrank)
                rec, mrr = _recall_mrr(r, [(["topic"], ["widget", "frob"], gid)], k=10)
                report["defect_265"][f"{fusion}/gidf={flag}"] = {
                    "gold_rank": grank, "wrong_rank": wrank,
                    "gold_above_wrong": gold_wins,
                    "gold_recall@10": rec, "gold_mrr": round(mrr, 4),
                }
            finally:
                r.close()
    os.environ.pop("HELIX_SHARD_GLOBAL_IDF", None)

    report["notes"] = [
        "#264: rrf/additive(default) FLIPS the keyword-dense impl below the "
        "README (defect reproduced). rrf/off satisfies #121's hard constraint. "
        "rrf/rank reorders in rank space (recall-oriented) and does NOT preserve "
        "the density winner on this adversarial fixture, because per-shard RRF "
        "has already compressed the density margin to a single rank.",
        "#265: rrf/gidf=1 == rrf/gidf=0 (guard makes the splice a byte-identical "
        "no-op under per-shard RRF). additive/gidf=1 lifts gold above wrong "
        "(the #182 splice is only defined on the additive/BM25 scale).",
    ]
    out = (Path(__file__).resolve().parents[1] / "docs" / "research" / "data"
           / "2026-07-16-shard-rank-merge-264-265-receipt.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nwrote {out}")
    td.cleanup()


if __name__ == "__main__":
    run()
