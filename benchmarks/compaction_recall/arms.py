"""Arms: what each strategy would put in front of the post-compaction agent.

Text arms are fixed per event:

* ``today``   — what Claude Code actually re-injected: the compaction summary,
  the preserved messages and the re-attached files / plan / skills
* ``summary`` — the summary alone
* ``plan``    — the plan-file attachment alone (the "keep a current plan" doc)
* ``handoff`` — the last HANDOFF-style markdown written before the boundary

Retrieval arms index the pre-compaction history as chunks and answer the
needle's masked query with k chunks:

* ``bm25``    — SQLite FTS5 BM25, the lexical floor
* ``dense``   — BGE-M3 dense cosine, the semantic comparator
* ``cymatix`` — the shipped pipeline (``build_context``), scored on the
  ``expressed_context`` it would inject, after budget and truncation
"""

from __future__ import annotations
# bench-pin: cover-index autobuild
# Bench drivers never let a background covering-index build start during a
# measured run (it perturbs latency; results are unaffected). Set before any
# store opens. Drivers with an explicit prebuild path set their own value.
import os as _bench_os
import sys as _bench_sys
_bench_os.environ["CYMATIX_COVER_INDEX_AUTOBUILD"] = "0"
print('cover-index autobuild pinned off (bench driver)', file=_bench_sys.stderr)

import gc
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .transcript import Event

CHUNK_CHARS = 1200


@dataclass
class Delivered:
    text: str
    n_chunks: int

    @property
    def n_tokens(self) -> int:
        return len(self.text) // 4


# ── text arms ──────────────────────────────────────────────────────────────

def today_text(ev: Event) -> str:
    parts = [ev.summary] + [u.text for u in ev.preserved] + [u.text for u in ev.post_attachments]
    return "\n\n".join(p for p in parts if p)


def summary_text(ev: Event) -> str:
    return ev.summary


def plan_text(ev: Event) -> str:
    return ev.plan_text


_HANDOFF_WRITE = re.compile(r"\Afile_path: [^\n]*handoff[^\n]*\.md\s*$", re.IGNORECASE | re.MULTILINE)


def handoff_text(ev: Event) -> str:
    for u in reversed(ev.pre):
        if u.role == "tool_use" and u.text.startswith("Write\n"):
            body = u.text[len("Write\n"):]
            if _HANDOFF_WRITE.search(body.split("\n", 1)[0]):
                return body
    return ""


TEXT_ARMS = {"today": today_text, "summary": summary_text, "plan": plan_text,
             "handoff": handoff_text}


# ── chunking ───────────────────────────────────────────────────────────────

def chunk_text(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Split on lines, then on whitespace; a token is only cut if it alone exceeds max."""
    pieces: list[str] = []
    for line in text.split("\n"):
        while len(line) > max_chars:
            cut = line.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            pieces.append(line[:cut])
            line = line[cut:].lstrip(" ")
        pieces.append(line)
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + 1 + len(p) > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur.strip():
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


class _Dedup:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def fresh(self, chunks: list[str]) -> list[str]:
        out = []
        for c in chunks:
            h = hashlib.sha1(c.encode("utf-8", "replace")).hexdigest()
            if h not in self._seen:
                self._seen.add(h)
                out.append(c)
        return out


# ── retrieval arms ─────────────────────────────────────────────────────────

_WORD = re.compile(r"[A-Za-z0-9]+")
MAX_QUERY_TERMS = 64


def query_terms(query: str) -> list[str]:
    """Unique lowercase terms, nearest-to-the-use first, capped."""
    seen: dict[str, None] = {}
    for w in reversed(_WORD.findall(query)):
        w = w.lower()
        if len(w) >= 2 and w != "mask":
            seen[w] = None
        if len(seen) >= MAX_QUERY_TERMS:
            break
    return list(seen)


class BM25Arm:
    name = "bm25"

    def reset(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE VIRTUAL TABLE c USING fts5(text, tokenize='unicode61')")
        self.dedup = _Dedup()
        self.size = 0

    def add(self, chunks: list[str]) -> None:
        rows = [(c,) for c in self.dedup.fresh(chunks)]
        self.db.executemany("INSERT INTO c(text) VALUES (?)", rows)
        self.size += len(rows)

    def query(self, query: str, k: int) -> Delivered:
        terms = query_terms(query)
        if not terms:
            return Delivered("", 0)
        match = " OR ".join(f'"{t}"' for t in terms)
        rows = self.db.execute(
            "SELECT text FROM c WHERE c MATCH ? ORDER BY bm25(c) LIMIT ?", (match, k)
        ).fetchall()
        return Delivered("\n\n".join(r[0] for r in rows), len(rows))

    def describe(self) -> dict:
        return {"engine": f"sqlite {sqlite3.sqlite_version} fts5 bm25 unicode61",
                "max_query_terms": MAX_QUERY_TERMS}


class DenseArm:
    name = "dense"
    MODEL = "BAAI/bge-m3"
    MAX_SEQ = 512

    def __init__(self) -> None:
        self._model = None

    def _load(self):
        if self._model is None:
            import torch
            from sentence_transformers import SentenceTransformer
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model = SentenceTransformer(self.MODEL, device=device)
            self._model.max_seq_length = self.MAX_SEQ
            if device == "cuda":
                self._model.half()
            self.device = device
        return self._model

    def reset(self) -> None:
        import numpy as np
        self._load()
        self.texts: list[str] = []
        self.vecs = np.zeros((0, 1024), dtype="float32")
        self.dedup = _Dedup()
        self.size = 0

    def add(self, chunks: list[str]) -> None:
        import numpy as np
        fresh = self.dedup.fresh(chunks)
        if not fresh:
            return
        v = self._load().encode(fresh, batch_size=32, normalize_embeddings=True,
                                convert_to_numpy=True, show_progress_bar=False)
        self.vecs = np.vstack([self.vecs, v.astype("float32")])
        self.texts.extend(fresh)
        self.size = len(self.texts)

    def query(self, query: str, k: int) -> Delivered:
        import numpy as np
        if not self.texts:
            return Delivered("", 0)
        q = self._load().encode([query], normalize_embeddings=True, convert_to_numpy=True,
                                show_progress_bar=False)[0].astype("float32")
        sims = self.vecs @ q
        top = np.argsort(-sims, kind="stable")[:k]
        return Delivered("\n\n".join(self.texts[i] for i in top), len(top))

    def describe(self) -> dict:
        return {"model": self.MODEL, "max_seq_length": self.MAX_SEQ,
                "device": getattr(self, "device", None), "similarity": "cosine (normalized dot)"}


class CymatixArm:
    """The shipped pipeline on a fresh store per event chain."""

    name = "cymatix"

    def __init__(self, config_path: str | None = None, root: str | None = None) -> None:
        self.config_path = config_path
        self.root = root or tempfile.mkdtemp(prefix="compaction_recall_")
        self.manager = None
        self._n = 0
        self.config_snapshot: dict | None = None

    def _close(self) -> None:
        if self.manager is not None:
            close = getattr(getattr(self.manager, "genome", None), "close", None)
            if callable(close):
                close()
            self.manager = None
            gc.collect()

    def reset(self) -> None:
        self._close()
        self._n += 1
        gdir = os.path.join(self.root, f"g{self._n}")
        shutil.rmtree(gdir, ignore_errors=True)
        os.makedirs(gdir, exist_ok=True)
        os.environ["CYMATIX_GENOME_PATH"] = os.path.join(gdir, "genome.db")
        os.environ["CYMATIX_DISABLE_LEARN"] = "1"
        if self.config_path:
            os.environ["CYMATIX_CONFIG"] = self.config_path
        from cymatix_context.config import load_config
        from cymatix_context.context_manager import CymatixContextManager
        cfg = load_config()
        cfg.genome.path = os.path.join(gdir, "genome.db")
        if self.config_snapshot is None:
            self.config_snapshot = _config_dump(cfg)
        self.manager = CymatixContextManager(cfg)
        self.dedup = _Dedup()
        self.size = 0

    def add(self, chunks: list[str]) -> None:
        for c in self.dedup.fresh(chunks):
            self.manager.ingest(c, content_type="text",
                                metadata={"path": f"transcript/chunk_{self.size}"})
            self.size += 1

    def query(self, query: str, k: int) -> Delivered:
        win = self.manager.build_context(query, read_only=True, ignore_delivered=True,
                                         max_genes=k)
        return Delivered(win.expressed_context or "", len(win.expressed_gene_ids or []))

    def describe(self) -> dict:
        # load_config() resolution order: CYMATIX_CONFIG, then ./cymatix.toml, then defaults.
        path = self.config_path or os.environ.get("CYMATIX_CONFIG") or "cymatix.toml"
        resolved = os.path.abspath(path) if os.path.exists(path) else "built-in defaults"
        sha = (hashlib.sha256(Path(path).read_bytes()).hexdigest()
               if os.path.exists(path) else None)
        return {"config_resolved": resolved, "config_sha256": sha,
                "loaded_config": self.config_snapshot}

    def close(self) -> None:
        self._close()
        shutil.rmtree(self.root, ignore_errors=True)


def _config_dump(cfg) -> dict:
    """The config as loaded at runtime (dataclass tree), secrets not expected here."""
    import dataclasses
    if dataclasses.is_dataclass(cfg):
        return dataclasses.asdict(cfg)
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump()
    return {"repr": repr(cfg)}
