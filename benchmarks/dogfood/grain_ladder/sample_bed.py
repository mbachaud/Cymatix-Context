"""Sample a growing bed every N seconds: genes, distinct files, bytes.

Appends one JSON line per sample to --out until --done-file appears.
Read-only connection; tolerates a busy writer.

Usage: python -P sample_bed.py --db X.db --out samples.jsonl --done-file rate.log
"""
import argparse
import json
import os
import sqlite3
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--done-file", required=True)
    ap.add_argument("--every", type=int, default=300)
    a = ap.parse_args()
    t0 = time.time()
    while True:
        for db in a.db:
            if not os.path.exists(db):
                continue
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
                genes, files = con.execute("select count(*), count(distinct source_id) from genes").fetchone()
                con.close()
            except sqlite3.Error as exc:
                genes = files = None
                print("sample failed:", exc)
            size = os.path.getsize(db) + (os.path.getsize(db + "-wal") if os.path.exists(db + "-wal") else 0)
            with open(a.out, "a", encoding="utf-8") as f:
                f.write(json.dumps({"db": db, "t_s": round(time.time() - t0), "ts": time.strftime("%H:%M:%S"),
                                    "genes": genes, "files": files, "bytes": size}) + "\n")
        if os.path.exists(a.done_file) and "DONE" in open(a.done_file, encoding="utf-8").read():
            return
        time.sleep(a.every)


if __name__ == "__main__":
    main()
