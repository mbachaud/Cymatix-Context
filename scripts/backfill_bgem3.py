"""One-shot BGE-M3 re-embedding of all genes. Run once after Step 4 ships.

Usage: python scripts/backfill_bgem3.py [path/to/genome.db]
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cymatix_context.backends.bgem3_codec import BGEM3Codec
from cymatix_context.config import load_config

# Read genome path from helix.toml; CLI arg overrides if provided
_repo_root = Path(__file__).resolve().parents[1]
cfg = load_config()
_default_db = str(_repo_root / cfg.genome.path)
DB = sys.argv[1] if len(sys.argv) > 1 else _default_db
DIM = cfg.retrieval.dense_embedding_dim
BATCH = 100

codec = BGEM3Codec(dim=DIM)
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

try:
    cur.execute("ALTER TABLE genes ADD COLUMN embedding_dense TEXT")
    conn.commit()
    print("Added embedding_dense column")
except sqlite3.OperationalError:
    print("Column already exists")

rows = cur.execute(
    "SELECT gene_id, content FROM genes WHERE embedding_dense IS NULL"
).fetchall()
print(f"Re-embedding {len(rows)} genes at dim={DIM}...")

for i, row in enumerate(rows):
    vec = codec.encode((row["content"] or "")[:2000], task="passage")
    cur.execute(
        "UPDATE genes SET embedding_dense = ? WHERE gene_id = ?",
        (json.dumps(vec), row["gene_id"]),
    )
    if i % BATCH == 0:
        conn.commit()
        print(f"  {i}/{len(rows)}")

conn.commit()
print(f"Done. {len(rows)} genes re-embedded.")
