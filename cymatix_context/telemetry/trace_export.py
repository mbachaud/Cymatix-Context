"""Phase 5 of issue #493 -- shareable export of packet traces.

``export_traces`` turns local ``metadata``-level packet traces into a JSONL
bundle that can leave the machine:

* ``full``-level records are refused (they carry raw query / chunk text).
* Every keyed-hash field is re-keyed: ``h -> HMAC(export_key, h)`` with a
  fresh random export key per export that is never stored. Joins inside
  one export survive; two exports of the same source cannot be correlated;
  nothing verifies under the deployment key (``<trace dir>/.trace_key``).
  ``session_id`` is re-keyed the same way.
* ``prev_hash`` is dropped (the hash chain is meaningless after re-keying)
  and the manifest says so.
* Raw text fields are stripped if present.
* The finished output is scanned for secrets (``secret_scan``); any finding
  aborts the export and leaves no output file.

Replay note: replay (a later phase) runs on the un-exported LOCAL trace,
taking a separate query file matched by ``query_hash`` under the deployment
key. An exported ``query_hash`` is deliberately not matchable that way
without the deployment-key holder re-deriving it from the local trace.

Parquet is optional: written next to the JSONL only when ``pyarrow`` is
importable (``pip install cymatix-context[trace-export]``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .trace import SCHEMA_VERSION

EXPORT_FILE = "packets.jsonl"
MANIFEST_FILE = "manifest.json"
DEFAULT_CONSENT = "not_shareable"

REHASHED_FIELDS = ["query_hash", "chunks[].content_hash", "session_id"]
STRIPPED_FIELDS = ["query", "chunks[].text"]
DROPPED_FIELDS = ["prev_hash"]


class ExportRefused(ValueError):
    """The source contains records that must not be exported."""


class SecretFound(RuntimeError):
    """The export output matched a secret pattern."""

    def __init__(self, findings: List[Dict[str, Any]]):
        self.findings = findings
        kinds = sorted({f["kind"] for f in findings})
        super().__init__(
            f"secret scan found {len(findings)} finding(s) ({', '.join(kinds)}); "
            "export aborted and no output written"
        )


_TLDS = "com|net|org|io|dev|co|ai|app|cloud|local|internal|corp|lan|edu|gov|info|biz|xyz"
_PATTERNS = {
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "generic_api_key": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\s*[=:]\s*['\"]?[A-Za-z0-9_\-/+]{8,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "ipv4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    "url": re.compile(r"(?i)\b(?:https?|ftp|ssh)://[^\s\"']+"),
    "hostname": re.compile(rf"(?i)\b(?:[a-z0-9-]+\.)+(?:{_TLDS})\b"),
}


def secret_scan(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Scan a text file; return ``[{kind, line, match}]`` (match is truncated)."""
    findings: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            for kind, rx in _PATTERNS.items():
                for m in rx.finditer(line):
                    findings.append({"kind": kind, "line": n, "match": m.group(0)[:12] + "..."})
    return findings


def _source_files(src: Path) -> List[Path]:
    if src.is_file():
        return [src]
    if not src.is_dir():
        raise FileNotFoundError(f"trace input not found: {src}")
    files = sorted(src.glob("packets-*.jsonl")) + sorted(src.glob("packets.jsonl"))
    if not files:
        raise FileNotFoundError(f"no packets*.jsonl under {src}")
    return files


def _rekey(key: bytes, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return hmac.new(key, value.encode("utf-8", "replace"), hashlib.sha256).hexdigest()


def _transform(rec: Dict[str, Any], key: bytes) -> Dict[str, Any]:
    out = {k: v for k, v in rec.items() if k not in ("prev_hash", "query")}
    for field in ("query_hash", "session_id"):
        if out.get(field) is not None:
            out[field] = _rekey(key, out[field])
    chunks = []
    for c in rec.get("chunks") or []:
        c = {k: v for k, v in c.items() if k != "text"}
        if c.get("content_hash") is not None:
            c["content_hash"] = _rekey(key, c["content_hash"])
        chunks.append(c)
    out["chunks"] = chunks
    return out


def _write_parquet(records: List[Dict[str, Any]], dest: Path) -> Optional[str]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return None
    nested = ("replay", "chunks", "verdict", "timing_ms", "trace")
    rows = [{k: (json.dumps(v, sort_keys=True) if k in nested else v)
             for k, v in r.items()} for r in records]
    keys = sorted({k for r in rows for k in r})
    pq.write_table(pa.table({k: [r.get(k) for r in rows] for k in keys}), str(dest))
    return dest.name


def export_traces(src: Union[str, Path], out_dir: Union[str, Path]) -> Dict[str, Any]:
    """Export ``src`` (trace dir or JSONL file) to ``out_dir``; return the manifest."""
    src = Path(src)
    out_dir = Path(out_dir)
    files = _source_files(src)
    key = secrets.token_bytes(32)  # fresh per export, never stored
    records: List[Dict[str, Any]] = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("level") != "metadata":
                    raise ExportRefused(
                        f"{f.name}:{n} is a full-level record; only metadata-level "
                        "traces can be exported")
                records.append(_transform(rec, key))

    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / EXPORT_FILE
    tmp = out_dir / (EXPORT_FILE + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        findings = secret_scan(tmp)
        if findings:
            raise SecretFound(findings)
        tmp.replace(final)
    finally:
        if tmp.exists():
            tmp.unlink()

    parquet = _write_parquet(records, out_dir / "packets.parquet")
    manifest: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_count": len(records),
        "source_files": [f.name for f in files],
        "rehashed_fields": REHASHED_FIELDS,
        "stripped_fields": STRIPPED_FIELDS,
        "dropped_fields": DROPPED_FIELDS,
        "notes": ["hash chain dropped: not meaningful after re-keying",
                  "hashes re-keyed under a one-time export key that is not stored"],
        "outputs": [EXPORT_FILE] + ([parquet] if parquet else []),
        "parquet": "written" if parquet else "skipped (pyarrow not installed)",
        "consent": DEFAULT_CONSENT,
    }
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
