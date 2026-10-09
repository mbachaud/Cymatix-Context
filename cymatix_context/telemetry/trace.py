"""Opt-in packet trace (issue #493, Phase 1, schema v0).

A packet trace records what cymatix delivered for one query: ids, keyed
content hashes, ranks, scores, the know/miss verdict and a replay
fingerprint. Retrieval is deterministic, so the record is replayable
evidence, not just a log.

Levels (``[trace] level`` / ``CYMATIX_TRACE_LEVEL``):

* ``off``       nothing is recorded
* ``metadata``  ids, keyed hashes, ranks, scores, timings -- NO raw query
                and NO chunk text anywhere in the record
* ``full``      additionally the raw query and chunk text (local only)

Design notes:

* ``packet_id`` (a ULID) is minted for every request, traced or not, so a
  response can always be referred to later.
* Hashes of query/content are HMAC-SHA256 under a per-deployment secret
  stored next to the trace dir; a plain SHA-256 of a short query is
  reversible by dictionary.
* Writes happen on a background thread fed by a bounded queue. The hot
  path only builds a dict and calls ``put_nowait``; every failure is
  logged at warning and swallowed.
* Env > toml > default, resolved here at use time (same split as
  ``otel.resolve_telemetry_settings``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import logging
import os
import queue
import random
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger("cymatix.trace")

SCHEMA_VERSION = "0"
LEVELS = ("off", "metadata", "full")
PACKETS_FILE = "packets.jsonl"
KEY_FILE = ".trace_key"
_QUEUE_MAX = 10_000

# Bottom layer of the env > toml > default chain. Must equal
# config.TraceConfig's field defaults (config import avoided to keep this
# module dependency-free; tests cross-check).
_TRACE_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "level": "metadata",
    "path": "",
    "hash_chain": False,
    "sampler_ratio": 1.0,
    "rotate_bytes": 64 * 1024 * 1024,
}


# -- ULID ---------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_packet_id() -> str:
    """26-char ULID: 48-bit ms timestamp + 80 random bits, Crockford base32."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    value = (ts << 80) | secrets.randbits(80)
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


# -- W3C traceparent ----------------------------------------------------

_TP_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def parse_traceparent(value: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse a W3C ``traceparent`` header; None when absent or invalid."""
    if not value:
        return None
    m = _TP_RE.match(value.strip().lower())
    if not m:
        return None
    version, trace_id, span_id, flags = m.groups()
    if version == "ff" or set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None
    return {"trace_id": trace_id, "parent_span_id": span_id, "trace_flags": flags}


def stamp_span(packet_id: str) -> None:
    """Set ``cymatix.packet_id`` on the active OTel span, if one records.

    Metadata-level only: the id is the single attribute added.
    """
    try:
        from opentelemetry import trace as _ot  # optional dependency
        span = _ot.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute("cymatix.packet_id", packet_id)
    except Exception:
        log.debug("packet_id span stamp failed", exc_info=True)


# -- settings -----------------------------------------------------------


def _env_raw(name: str) -> Optional[str]:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return None
    return val.strip()


def resolve_trace_settings(config: Any = None) -> Dict[str, Any]:
    """Effective settings: CYMATIX_TRACE_* env > [trace] toml > default.

    ``config`` duck-types ``config.TraceConfig`` (None = pure defaults).
    ENABLED is on iff the env value is "1"; an unknown LEVEL is ignored
    with a warning.
    """
    def layer(attr: str) -> Any:
        return getattr(config, attr) if config is not None else _TRACE_DEFAULTS[attr]

    en = _env_raw("CYMATIX_TRACE_ENABLED")
    level = str(layer("level")).lower()
    lv = _env_raw("CYMATIX_TRACE_LEVEL")
    if lv is not None:
        level = lv.lower()
    if level not in LEVELS:
        log.warning("trace level %r is not one of %s -- using 'metadata'", level, LEVELS)
        level = "metadata"
    return {
        "enabled": (en == "1") if en is not None else bool(layer("enabled")),
        "level": level,
        "path": _env_raw("CYMATIX_TRACE_PATH") or str(layer("path")),
        "hash_chain": bool(layer("hash_chain")),
        "sampler_ratio": float(layer("sampler_ratio")),
        "rotate_bytes": int(layer("rotate_bytes")),
    }


# -- keyed hashing ------------------------------------------------------


class KeyedHasher:
    """HMAC-SHA256 under a per-deployment secret kept in ``<dir>/.trace_key``."""

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._key: Optional[bytes] = None
        self._lock = threading.Lock()

    def _load_key(self) -> bytes:
        with self._lock:
            if self._key is not None:
                return self._key
            path = self._dir / KEY_FILE
            self._dir.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(secrets.token_bytes(32))
            except FileExistsError:
                pass
            self._key = path.read_bytes()
            return self._key

    def hash(self, text: str) -> str:
        return hmac.new(self._load_key(), text.encode("utf-8", "replace"),
                        hashlib.sha256).hexdigest()


# -- event builder ------------------------------------------------------


def build_packet_event(
    *,
    packet_id: str,
    pipeline_request_id: Optional[str],
    session_id: Optional[str],
    query: str,
    chunks: Iterable[Dict[str, Any]],
    verdict: Dict[str, Any],
    timing_ms: Dict[str, float],
    replay: Dict[str, Any],
    level: str,
    hasher: KeyedHasher,
    traceparent: Optional[Dict[str, str]] = None,
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Assemble a schema-v0 ``packet`` event.

    Chunk inputs are dicts with ``gene_id`` and optionally ``text``,
    ``rank``, ``score``, ``source_kind``, ``lane_contribs``,
    ``ingested_at``. Unavailable fields are omitted, never invented. Raw
    ``query`` / chunk ``text`` are copied only at ``full``.
    """
    full = level == "full"
    out_chunks: List[Dict[str, Any]] = []
    for c in chunks:
        gid = c.get("gene_id")
        rec: Dict[str, Any] = {"chunk_id": gid, "doc_id": gid}
        text = c.get("text")
        if text is not None:
            rec["content_hash"] = hasher.hash(text)
        for k in ("rank", "score", "source_kind", "lane_contribs", "ingested_at"):
            v = c.get(k)
            if v is not None:
                rec[k] = round(v, 6) if k == "score" else v
        if full and text is not None:
            rec["text"] = text
        out_chunks.append(rec)

    ev: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": "packet",
        "packet_id": packet_id,
        "pipeline_request_id": pipeline_request_id or None,
        "session_id": session_id,
        "ts": round(ts if ts is not None else time.time(), 3),
        "level": level,
        "query_hash": hasher.hash(query),
        "query_len_terms": len(query.split()),
        "replay": replay,
        "chunks": out_chunks,
        "verdict": verdict,
        "timing_ms": timing_ms,
    }
    if traceparent:
        ev["trace"] = dict(traceparent)
    if full:
        ev["query"] = query
    return ev


# -- replay fingerprint -------------------------------------------------


def config_hash(config: Any) -> str:
    """sha256 of the loaded runtime config (secrets redacted)."""
    try:
        dumped = dataclasses.asdict(config)
        if dumped.get("server", {}).get("admin_token"):
            dumped["server"]["admin_token"] = "<redacted>"
        blob = json.dumps(dumped, sort_keys=True, default=str)
    except Exception:
        log.debug("config hash failed", exc_info=True)
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _index_fingerprint(genome: Any) -> Dict[str, Any]:
    """doc count + max rowid, hashed. One COUNT(*) per refresh, cached by caller."""
    conn = getattr(genome, "read_conn", None) or genome.conn
    count, max_rowid = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM genes"
    ).fetchone()
    return {
        "index_fingerprint": hashlib.sha256(f"{count}:{max_rowid}".encode()).hexdigest()[:32],
        "doc_count": int(count),
    }


def _git_sha() -> Optional[str]:
    try:
        from ..server.routes_admin import _engine_commit
        return _engine_commit()
    except Exception:
        return None


# -- writer -------------------------------------------------------------


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


class TraceWriter:
    """Append-only JSONL writer on a background thread."""

    def __init__(self, directory: Path, *, hash_chain: bool, rotate_bytes: int):
        self._dir = Path(directory)
        self._chain = hash_chain
        self._rotate = max(1, int(rotate_bytes))
        self._prev: Optional[str] = None
        self._q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread = threading.Thread(target=self._run, name="cymatix-trace-writer", daemon=True)
        self._thread.start()

    def submit(self, record: Dict[str, Any]) -> None:
        try:
            self._q.put_nowait(record)
        except queue.Full:
            log.warning("trace queue full -- dropping packet %s", record.get("packet_id"))

    def close(self, timeout: float = 5.0) -> None:
        try:
            self._q.put(None, timeout=1)
        except queue.Full:
            return
        self._thread.join(timeout)

    def _resume_chain(self, path: Path) -> None:
        if self._chain and self._prev is None and path.exists():
            try:
                last = ""
                with open(path, "r", encoding="utf-8") as fh:
                    for last in fh:
                        pass
                if last.strip():
                    self._prev = _line_hash(last.rstrip("\n"))
            except OSError:
                log.warning("trace chain resume failed", exc_info=True)

    def _run(self) -> None:
        while True:
            rec = self._q.get()
            if rec is None:
                return
            try:
                self._write(rec)
            except Exception:
                log.warning("trace write failed", exc_info=True)

    def _write(self, rec: Dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / PACKETS_FILE
        if self._chain:
            self._resume_chain(path)
            rec = {**rec, "prev_hash": self._prev}
        line = json.dumps(rec, separators=(",", ":"), default=str)
        if path.exists() and path.stat().st_size >= self._rotate:
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            n = 0
            while True:
                dest = self._dir / f"packets-{stamp}{'-%d' % n if n else ''}.jsonl"
                if not dest.exists():
                    break
                n += 1
            os.replace(path, dest)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self._chain:
            self._prev = _line_hash(line)


def verify_chain(path: Path) -> bool:
    """True iff every line's ``prev_hash`` is the sha256 of the line before."""
    prev_line: Optional[str] = None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line:
                continue
            if prev_line is not None:
                if json.loads(line).get("prev_hash") != _line_hash(prev_line):
                    return False
            prev_line = line
    return True


# -- tracer (runtime facade) -------------------------------------------


class PacketTracer:
    """Owns settings, hasher, writer and the reader helpers."""

    def __init__(self, settings: Dict[str, Any], *, default_dir: Optional[Path] = None):
        self.settings = dict(settings)
        self.level = self.settings["level"]
        self.enabled = bool(self.settings["enabled"]) and self.level != "off"
        self.dir = Path(self.settings["path"]) if self.settings["path"] else (
            Path(default_dir) if default_dir is not None else Path("genomes/main/traces")
        )
        self.writer: Optional[TraceWriter] = None
        self.hasher: Optional[KeyedHasher] = None
        self._replay_cache: Optional[Dict[str, Any]] = None
        self._replay_at = 0.0
        if self.enabled:
            self.hasher = KeyedHasher(self.dir)
            self.writer = TraceWriter(
                self.dir, hash_chain=bool(self.settings["hash_chain"]),
                rotate_bytes=int(self.settings["rotate_bytes"]),
            )

    @classmethod
    def from_config(cls, config: Any, genome_path: Optional[str] = None) -> "PacketTracer":
        settings = resolve_trace_settings(getattr(config, "trace", None))
        default_dir = None
        if genome_path and genome_path != ":memory:":
            default_dir = Path(genome_path).resolve().parent / "traces"
        return cls(settings, default_dir=default_dir)

    def sampled(self) -> bool:
        if not self.enabled:
            return False
        ratio = self.settings["sampler_ratio"]
        return ratio >= 1.0 or random.random() < ratio

    def replay_info(self, genome: Any, config: Any, ttl_s: float = 60.0) -> Dict[str, Any]:
        """Cached replay block; recomputed at most once per ``ttl_s``."""
        now = time.monotonic()
        if self._replay_cache is None or now - self._replay_at > ttl_s:
            info: Dict[str, Any] = {}
            try:
                info.update(_index_fingerprint(genome))
            except Exception:
                log.debug("index fingerprint failed", exc_info=True)
                info.update({"index_fingerprint": None})
            try:
                from .. import __version__
            except Exception:
                __version__ = None
            info.update({
                "config_hash": config_hash(config),
                "cymatix_version": __version__,
                "git_sha": _git_sha(),
            })
            self._replay_cache, self._replay_at = info, now
        return dict(self._replay_cache)

    def emit(self, *, genome: Any, config: Any, **event_kwargs: Any) -> None:
        """Build + enqueue a packet event. Never raises."""
        if not self.enabled or self.writer is None:
            return
        try:
            ev = build_packet_event(
                replay=self.replay_info(genome, config), level=self.level,
                hasher=self.hasher, **event_kwargs,
            )
            self.writer.submit(ev)
        except Exception:
            log.warning("packet trace emit failed", exc_info=True)

    # -- reader ----------------------------------------------------------

    def _files_newest_first(self) -> List[Path]:
        if not self.dir.exists():
            return []
        rotated = sorted(self.dir.glob("packets-*.jsonl"), reverse=True)
        live = self.dir / PACKETS_FILE
        return ([live] if live.exists() else []) + rotated

    def _iter_newest_first(self) -> Iterable[Dict[str, Any]]:
        for f in self._files_newest_first():
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                log.warning("trace read failed: %s", f, exc_info=True)
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def get(self, packet_id: str) -> Optional[Dict[str, Any]]:
        for rec in self._iter_newest_first():
            if rec.get("packet_id") == packet_id:
                return rec
        return None

    def recent(self, session_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        out: List[Dict[str, Any]] = []
        for rec in self._iter_newest_first():
            if session_id is not None and rec.get("session_id") != session_id:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
