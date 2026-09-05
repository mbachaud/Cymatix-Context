"""ARM-3 stage C -- one-pass corpus cache for the head-chunk lane probe.

Builds, in ONE read-only scan of `genes` plus one scan of the FTS docsize
shadow table:
  * rowid-ascending chunk order per source_id  -> pos, n_chunks, posclass
  * per-chunk n_chars
  * per-chunk promoter domains-hash and entities-hash (lane position-blindness)
  * per-rowid FTS5 doc length (sum of the per-column varints in
    genes_fts_docsize) and the corpus avgdl -> inputs for a bit-exact BM25
    replica

Writes a pickle to the scratchpad. Bed is opened READ-ONLY (mode=ro,
PRAGMA query_only). No writes, no indexes, no vacuum.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BED = "F:/tmp/erb_blob_v09x.db"
CACHE = Path(os.environ.get(
    "ARM3_CACHE",
    "C:/Users/max/AppData/Local/Temp/claude/"
    "F--Projects-cymatix-context--claude-worktrees-wave-1-semantic-ranking-5f1dab/"
    "c81df3a0-0a0c-4160-81ee-e68db3b02372/scratchpad",
))
CACHE.mkdir(parents=True, exist_ok=True)
OUT = CACHE / "arm3_corpus_cache.pkl"


def h8(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def decode_docsize(blob: bytes) -> int:
    """Sum the fts5 varint per-column sizes -> total doc length D."""
    total = 0
    val = 0
    shift_done = True
    i = 0
    n = len(blob)
    while i < n:
        val = 0
        while i < n:
            b = blob[i]
            i += 1
            val = (val << 7) | (b & 0x7F)
            if not (b & 0x80):
                break
        total += val
    return total


def main() -> int:
    t0 = time.perf_counter()
    con = sqlite3.connect("file:%s?mode=ro" % BED, uri=True)
    con.execute("PRAGMA query_only=1")
    cur = con.cursor()

    print("scan 1/2: genes ...", flush=True)
    gid_list = []
    rowids = []
    src_h = []
    nchars = []
    dom_h = []
    ent_h = []
    ent_n = []
    t = time.perf_counter()
    sql = ("SELECT rowid, gene_id, source_id, length(content), promoter "
           "FROM genes ORDER BY rowid")
    for rid, gid, sid, ln, prom in cur.execute(sql):
        rowids.append(rid)
        gid_list.append(gid)
        src_h.append(h8(sid or ""))
        nchars.append(ln or 0)
        dh = eh = 0
        en = 0
        if prom:
            try:
                p = json.loads(prom)
                ds = p.get("domains") or []
                es = p.get("entities") or []
                dh = h8("\u0000".join(map(str, ds)))
                eh = h8("\u0000".join(map(str, es)))
                en = len(es)
            except Exception:
                dh = h8(prom[:200])
        dom_h.append(dh)
        ent_h.append(eh)
        ent_n.append(en)
    print("  %d rows in %.1fs" % (len(gid_list), time.perf_counter() - t), flush=True)

    # chunk order: rowid ascending WITHIN a source (rows already rowid-ordered)
    order = {}
    for i, s in enumerate(src_h):
        order.setdefault(s, []).append(i)
    pos = [0] * len(gid_list)
    nch = [0] * len(gid_list)
    for s, idxs in order.items():
        n = len(idxs)
        for j, i in enumerate(idxs):
            pos[i] = j
            nch[i] = n
    print("  %d sources" % len(order), flush=True)

    print("scan 2/2: genes_fts_docsize ...", flush=True)
    t = time.perf_counter()
    dl_by_rowid = {}
    tot = 0
    for rid, sz in cur.execute("SELECT id, sz FROM genes_fts_docsize"):
        d = decode_docsize(sz)
        dl_by_rowid[rid] = d
        tot += d
    n_docs = len(dl_by_rowid)
    avgdl = tot / float(n_docs) if n_docs else 1.0
    print("  %d docsize rows, avgdl=%.6f in %.1fs" % (n_docs, avgdl, time.perf_counter() - t),
          flush=True)

    cache = {
        "bed": BED,
        "bed_bytes": os.path.getsize(BED),
        "gid": gid_list,
        "rowid": rowids,
        "src_h": src_h,
        "nchars": nchars,
        "dom_h": dom_h,
        "ent_h": ent_h,
        "ent_n": ent_n,
        "pos": pos,
        "n_chunks": nch,
        "dl_by_rowid": dl_by_rowid,
        "avgdl": avgdl,
        "n_fts_docs": n_docs,
        "built_s": round(time.perf_counter() - t0, 1),
    }
    with open(OUT, "wb") as fh:
        pickle.dump(cache, fh, protocol=4)
    print("wrote %s (%.1f MB) in %.1fs" % (OUT, OUT.stat().st_size / 1e6,
                                           time.perf_counter() - t0), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
