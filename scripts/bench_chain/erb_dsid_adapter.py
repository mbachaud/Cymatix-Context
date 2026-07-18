r"""erb_dsid_adapter.py -- PURE transformer: delivered gene_ids -> ERB dsids.

This is a bench-side adapter (issue: durable per-gene document identity). It keeps
the ERB-specific ``dsid_*`` vocabulary OUT of helix core: core only knows the raw
``source_id`` (via ``ContextItem.document_id`` / ``context_packet.document_identity``).
This script maps those source_ids back to the official EnterpriseRAG-Bench
``expected_doc_ids`` space (``dsid_...``) for scoring.

It does NOT run retrieval (the ~15h re-retrieval is not approved). Inputs:

  (i)  a per-question *delivered map* JSONL, one row ``{"question_id": ...,
       "gene_ids": [...]}`` (what helix actually delivered per question);
  (ii) gene_id -> source_id resolved READ-ONLY from the blob DB
       (``SELECT gene_id, source_id FROM genes WHERE gene_id IN (...)``,
       opened ``mode=ro``); brief access -- the blob is shared;
  (iii) ``uuid_index.json`` (ERB ``generated_data/uuid_index.json``): a
       ``{dsid: rel}`` map of ~512k entries. Inverted ONCE to ``{rel: dsid}``
       and cached to a sidecar keyed by (mtime, size, sha256) so repeat runs
       skip the ~58MB rebuild.

Normalization reuses ``_rel_after_sources`` from ``erb500k_scored`` (NOT
reimplemented). Lookup: ``inverted.get(_rel_after_sources(source_id))``; on
miss, retry once with the first path component stripped (mirrors the gold-index
stripped-variant in ``erb500k_scored.make_gold_index``, lines 152-154).

Output: JSONL rows ``{"question_id": qid, "document_ids": [dsid, ...]}`` --
dedup preserving delivery order; unresolved source_ids are dropped and COUNTED,
with a miss-rate diagnostic printed to stderr. This shape drops straight into
the existing ``--document-ids-jsonl`` hook in ``erb_official_answers_export.py``
(``load_document_ids_hook``) unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

# Reuse the canonical source-path normalizer -- do NOT reimplement (design B).
from scripts.bench_chain.erb500k_scored import _rel_after_sources


# ---------------------------------------------------------------------------
# Inverted uuid_index (rel -> dsid), with a mtime/size/sha256-keyed sidecar
# ---------------------------------------------------------------------------

def _source_key(path: Path) -> dict:
    """Cache key for the uuid_index source file: mtime, size, and sha256."""
    st = path.stat()
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"mtime": st.st_mtime, "size": st.st_size, "sha256": h.hexdigest()}


def _sidecar_path(uuid_index_path: Path) -> Path:
    return uuid_index_path.with_suffix(uuid_index_path.suffix + ".inverted.json")


def build_inverted_index(
    uuid_index_path: str | os.PathLike,
    cache_path: str | os.PathLike | None = None,
    *,
    use_cache: bool = True,
) -> dict[str, str]:
    """Return ``{rel: dsid}`` inverted from ``uuid_index.json`` (``{dsid: rel}``).

    Cached to a sidecar keyed by (mtime, size, sha256) of the source file so a
    repeat run skips the ~58MB parse+invert. ``uuid_index.json`` is read with
    ``encoding="utf-8"``.
    """
    uuid_index_path = Path(uuid_index_path)
    sidecar = Path(cache_path) if cache_path is not None else _sidecar_path(uuid_index_path)
    key = _source_key(uuid_index_path)

    if use_cache and sidecar.is_file():
        try:
            with open(sidecar, encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("key") == key and isinstance(blob.get("inverted"), dict):
                return blob["inverted"]
        except Exception:
            pass  # corrupt / stale sidecar -> rebuild

    with open(uuid_index_path, encoding="utf-8") as fh:
        forward = json.load(fh)  # {dsid: rel}

    inverted: dict[str, str] = {}
    for dsid, rel in forward.items():
        if not rel:
            continue
        # First writer wins on a rel collision (matches make_gold_index's
        # setdefault posture); deterministic given a stable source ordering.
        inverted.setdefault(str(rel), str(dsid))

    if use_cache:
        try:
            tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"key": key, "inverted": inverted}, fh)
            os.replace(tmp, sidecar)
        except Exception:
            pass  # cache write is best-effort; never fail the run over it

    return inverted


# ---------------------------------------------------------------------------
# source_id -> dsid
# ---------------------------------------------------------------------------

def source_id_to_dsid(source_id: str | None, inverted: dict[str, str]) -> str | None:
    """Resolve a raw ``source_id`` to its ERB ``dsid`` via the inverted index.

    Primary: ``inverted.get(_rel_after_sources(source_id))``. On miss, retry
    once with the first path component of the rel stripped (mirrors the
    stripped-variant key in erb500k_scored.make_gold_index:152-154).
    """
    if not source_id:
        return None
    rel = _rel_after_sources(source_id)
    if not rel:
        return None
    dsid = inverted.get(rel)
    if dsid is not None:
        return dsid
    slash = rel.find("/")
    if slash > 0:
        return inverted.get(rel[slash + 1:])
    return None


# ---------------------------------------------------------------------------
# gene_id -> source_id (read-only blob access)
# ---------------------------------------------------------------------------

def resolve_gene_source_ids(
    gene_ids: list[str], blob_db_path: str | os.PathLike
) -> dict[str, str | None]:
    """Read-only ``{gene_id: source_id}`` for the given gene_ids from the blob.

    Opens the shared blob DB via ``file:...?mode=ro`` and keeps the query brief.
    Batches the ``IN (...)`` to stay under SQLite's variable limit.
    """
    out: dict[str, str | None] = {}
    unique = list(dict.fromkeys(g for g in gene_ids if g))
    if not unique:
        return out
    uri = f"file:{Path(blob_db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        for i in range(0, len(unique), 500):
            batch = unique[i:i + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT gene_id, source_id FROM genes WHERE gene_id IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                out[r["gene_id"]] = r["source_id"]
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# Pure row transform (testable without a blob or a 58MB index)
# ---------------------------------------------------------------------------

def transform_rows(
    delivered_rows: list[dict],
    gene_source_map: dict[str, str | None],
    inverted: dict[str, str],
) -> tuple[list[dict], dict]:
    """Map delivered gene_ids -> dsids per question.

    Returns ``(out_rows, stats)`` where each out row is
    ``{"question_id": qid, "document_ids": [dsid, ...]}`` (dedup, delivery
    order preserved). ``stats`` reports resolved/unresolved counts for the
    miss-rate diagnostic. Rows whose gene_ids all fail to resolve still emit
    with an empty ``document_ids`` list.
    """
    out_rows: list[dict] = []
    total = 0
    resolved = 0
    for row in delivered_rows:
        qid = row.get("question_id")
        gene_ids = row.get("gene_ids") or []
        dsids: list[str] = []
        seen: set[str] = set()
        for gid in gene_ids:
            total += 1
            sid = gene_source_map.get(gid)
            dsid = source_id_to_dsid(sid, inverted)
            if dsid is None:
                continue
            resolved += 1
            if dsid not in seen:
                seen.add(dsid)
                dsids.append(dsid)
        out_rows.append({"question_id": qid, "document_ids": dsids})
    unresolved = total - resolved
    stats = {
        "gene_ids_total": total,
        "gene_ids_resolved": resolved,
        "gene_ids_unresolved": unresolved,
        "miss_rate": (unresolved / total) if total else 0.0,
    }
    return out_rows, stats


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"[FATAL] {path}:{i}: invalid JSON: {exc}", file=sys.stderr)
                raise SystemExit(1)
    return rows


def run(
    delivered_map_path: str | os.PathLike,
    blob_db_path: str | os.PathLike,
    uuid_index_path: str | os.PathLike,
    out_path: str | os.PathLike,
    *,
    cache_path: str | os.PathLike | None = None,
) -> dict:
    delivered_rows = _load_jsonl(Path(delivered_map_path))
    all_gene_ids = [g for row in delivered_rows for g in (row.get("gene_ids") or [])]

    inverted = build_inverted_index(uuid_index_path, cache_path)
    gene_source_map = resolve_gene_source_ids(all_gene_ids, blob_db_path)

    out_rows, stats = transform_rows(delivered_rows, gene_source_map, inverted)

    out_path = Path(out_path)
    with open(out_path, "w", encoding="utf-8") as fh:
        for row in out_rows:
            fh.write(json.dumps(row) + "\n")

    print(
        f"[erb_dsid_adapter] questions={len(out_rows)} "
        f"gene_ids={stats['gene_ids_total']} resolved={stats['gene_ids_resolved']} "
        f"unresolved={stats['gene_ids_unresolved']} "
        f"miss_rate={stats['miss_rate']:.3f} -> {out_path}",
        file=sys.stderr,
    )
    return stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "PURE transformer: delivered gene_ids -> ERB dsids. Emits the "
            "{question_id, document_ids} JSONL consumed by "
            "erb_official_answers_export.py --document-ids-jsonl."
        )
    )
    p.add_argument("--delivered-map", type=Path, required=True,
                   help="JSONL of {question_id, gene_ids:[...]} delivered per question")
    p.add_argument("--blob-db", type=Path, required=True,
                   help="Blob genome DB (opened read-only) for gene_id->source_id")
    p.add_argument("--uuid-index", type=Path, required=True,
                   help="ERB generated_data/uuid_index.json ({dsid: rel})")
    p.add_argument("--out", type=Path, required=True,
                   help="Output JSONL of {question_id, document_ids:[dsid...]}")
    p.add_argument("--cache-path", type=Path, default=None,
                   help="Override sidecar path for the inverted index cache")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run(
        args.delivered_map,
        args.blob_db,
        args.uuid_index,
        args.out,
        cache_path=args.cache_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
