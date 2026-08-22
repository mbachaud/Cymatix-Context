"""Capture the C1 rank-hash goldens on the UNPATCHED tree (issue #339 Phase 0.4).

Produces two committed baselines under ``tests/golden/``:

* ``golden_v090_context.json`` - the shape of the ``/context`` response on the
  seeded 7-document fixture: a recursive key manifest over the whole response
  tree (dotted paths, list elements collapsed to ``[]``) plus the pipeline's
  ``delivery`` block verbatim. The manifest is the mechanical form of the
  "zero new fields" claim; the delivery dict is the only block the C1 change
  can touch and it is fully deterministic for this fixture.
* ``golden_v090_worker_samples.json`` - the sorted key manifest of every
  success-path sample dict that ``benchmarks/dogfood/run_server_scaling.py``
  writes, plus the invariant fields of the first sample. ``latency_ms`` is
  excluded by name because it is wall-clock.

**Run with ``PYTHONHASHSEED=0``** so the recorded response is reproducible
across Python processes, matching the convention of
``_capture_pre_stage5_responses.py``. Regeneration command, from the worktree
root:

    PYTHONHASHSEED=0 python -m tests.golden._capture_c1_rank_hash_goldens

Both output paths are committed to git as the baseline. The consuming test is
``tests/test_bench_rank_hash_inert.py``: its leg A cases diff the gate-off
behaviour against these files, and its leg B cases prove the gate-on behaviour
adds exactly the enumerated keys and nothing else.

**The baseline is generated on the UNPATCHED v0.9.0 tree**
(``55162c6b4a84070a29b9c02237714b07c1193b38``), before the first source edit of
the C1 branch, with ``CYMATIX_BENCH_RANK_HASH`` scrubbed from the environment.
That is what makes the inert claim checkable by a reader who was not present:
the reference comes from a tree that does not contain the change.

This module is also imported by the test file, so both legs replay the SAME
stub server and the SAME canned ``/context`` envelope and cannot drift apart.
The stub is pure stdlib ``http.server``: no bed, no encoders, no uvicorn, so
every leg runs on any laptop.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


BENCH_RANK_HASH_ENV = "CYMATIX_BENCH_RANK_HASH"


def _preflight() -> None:
    """Runtime guards for a capture run.

    Called from ``main()`` rather than at import, because
    ``tests/test_bench_rank_hash_inert.py`` imports this module for the shared
    stub and the shared canned envelope: an import-time ``SystemExit`` would
    make the whole suite uncollectable for any operator who happens to have the
    gate exported. The guards still fire on every actual capture, which is what
    they exist to protect.
    """
    # Hash-seed determinism guard, same idiom as
    # _capture_pre_stage5_responses.py.
    if os.environ.get("PYTHONHASHSEED") not in ("0",):
        print(
            "WARNING: PYTHONHASHSEED is not '0'. Re-run as: "
            "PYTHONHASHSEED=0 python -m "
            "tests.golden._capture_c1_rank_hash_goldens",
            file=sys.stderr,
        )
    # A baseline captured with the instrument switched on would not be a
    # baseline. Refuse rather than record a contaminated golden.
    if os.environ.get(BENCH_RANK_HASH_ENV) is not None:
        raise SystemExit(
            f"refusing to capture a golden with {BENCH_RANK_HASH_ENV} set "
            f"(value {os.environ[BENCH_RANK_HASH_ENV]!r}); scrub it and re-run"
        )


GOLDEN_DIR = Path(__file__).resolve().parent
REPO_ROOT = GOLDEN_DIR.parent.parent
CONTEXT_GOLDEN_PATH = GOLDEN_DIR / "golden_v090_context.json"
WORKER_GOLDEN_PATH = GOLDEN_DIR / "golden_v090_worker_samples.json"
HARNESS_PATH = REPO_ROOT / "benchmarks" / "dogfood" / "run_server_scaling.py"

TREE = "v0.9.0"
COMMIT = "55162c6b4a84070a29b9c02237714b07c1193b38"
GENERATED_BY = "tests/golden/_capture_c1_rank_hash_goldens.py"

# Cloned from tests/test_packet_delivery_visibility.py: starts with a wh-word,
# under 15 words, no arithmetic operators, so the rule classifier calls it
# ``factual`` and the assembly cap of 5 is the first constraint to bind.
FACTUAL_QUERY = "what is the checkpoint interval value"
SEEDED_GENE_COUNT = 7


def _seed_topic_genes(genome, n: int) -> list[str]:
    """Seed ``n`` near-identical documents on one topic.

    Cloned from ``tests/test_packet_delivery_visibility.py`` so the fixture is
    the shipped template's fixture. Same domains and entities on every document
    keeps retrieval scores nearly uniform, so the score-ratio tier stays BROAD
    and the classifier cap binds first.
    """
    from tests.conftest import make_gene

    ids = []
    for i in range(n):
        gene = make_gene(
            (
                "The checkpoint interval value controls the flush cadence "
                f"of the knowledge store writer. Note {i}: same topic, "
                "distinct document."
            ),
            domains=["checkpoint", "interval"],
            entities=["value"],
            gene_id=f"ckptgene{i:08d}",
        )
        genome.upsert_gene(gene)
        ids.append(gene.gene_id)
    return ids


# ---------------------------------------------------------------- canned ----
#
# The frozen /context envelope the harness legs replay. Shape per
# benchmarks/dogfood/run_server_scaling.py:184-188: /context returns a
# Continue-compatible one-element LIST, expressed gene ids ride
# response[0].agent.citations[*].gene_id in expressed order, and the pipeline's
# delivery-visibility block rides response[0].delivery.

DELIVERED_IDS = [
    "ckptgene00000000",
    "ckptgene00000001",
    "ckptgene00000002",
    "ckptgene00000003",
    "ckptgene00000004",
]

#: The lossy variant drops this delivered id from the citations list, the way
#: routes_context.py:363-366 drops a gene id with no store row.
DROPPED_ID = "ckptgene00000001"

_DELIVERY = {
    "pool_size": 7,
    "delivered_count": 5,
    "delivered_gene_ids": list(DELIVERED_IDS),
    "cap_binding": "assembly_cap",
}


def _envelope(citation_ids: list[str], delivery: dict | None) -> list:
    resp: dict[str, Any] = {
        "name": "cymatix-context",
        "description": "canned C1 stub envelope",
        "content": "seeded checkpoint interval documents",
        "agent": {
            "citations": [
                {"gene_id": gid, "score": round(1.0 - 0.01 * i, 4)}
                for i, gid in enumerate(citation_ids)
            ],
        },
    }
    if delivery is not None:
        resp["delivery"] = delivery
    return [resp]


#: Baseline: every delivered id is cited, in delivered order.
CANNED_CONTEXT_RESPONSE = _envelope(list(DELIVERED_IDS), dict(_DELIVERY))

#: Lossy: one delivered id has no citation row. Still an order-preserved
#: subsequence, so citations_dropped == 1 and citations_subsequence is True.
CANNED_CONTEXT_RESPONSE_LOSSY = _envelope(
    [g for g in DELIVERED_IDS if g != DROPPED_ID], dict(_DELIVERY)
)

#: Reordered: nothing dropped, but two ids are transposed, so the citations
#: list is NOT an order-preserved subsequence of delivered_gene_ids. A drop
#: count alone cannot express this, which is why citations_subsequence exists.
CANNED_CONTEXT_RESPONSE_REORDERED = _envelope(
    [
        "ckptgene00000000",
        "ckptgene00000002",
        "ckptgene00000001",
        "ckptgene00000003",
        "ckptgene00000004",
    ],
    dict(_DELIVERY),
)

#: Abstain shape: no delivery block at all. Shipped behaviour, documented
#: in-tree at routes_context.py:324-325.
CANNED_CONTEXT_RESPONSE_NO_DELIVERY = _envelope(list(DELIVERED_IDS), None)


# ------------------------------------------------------------- stub http ----


class _StubHandler(BaseHTTPRequestHandler):
    """Answers POST /context with whatever the server was configured with."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        # The worker's FIRST POST is its untimed warmup, and a dead warmup is a
        # dead worker (rc 4). So the warmup is always answered 200 and any
        # configured error status applies from the first TIMED query onward.
        with self.server.stub_lock:
            self.server.stub_posts += 1
            first = self.server.stub_posts == 1
        status = 200 if first else getattr(self.server, "stub_status", 200)
        if status != 200:
            self.send_response(status)
            body = json.dumps({"detail": "stub error"}).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(getattr(self.server, "stub_response")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence the stdlib access log
        return


@contextlib.contextmanager
def serve_stub(response, status: int = 200) -> Iterator[str]:
    """Serve ``response`` from a loopback stdlib HTTP server on a free port.

    Yields the base url the harness worker should be pointed at. Stdlib only:
    no cymatix import, no bed, no encoder, no uvicorn. ``status`` other than
    200 applies from the first TIMED query onward; the worker's untimed warmup
    is always answered 200 so the error-path leg reaches the sample loop.
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    httpd.stub_response = response
    httpd.stub_status = status
    httpd.stub_posts = 0
    httpd.stub_lock = threading.Lock()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ----------------------------------------------------------- the harness ----


def load_harness():
    """Import ``benchmarks/dogfood/run_server_scaling.py`` by path.

    The benchmarks tree is not a package, so a file-location import keeps this
    working with zero packaging change.
    """
    spec = importlib.util.spec_from_file_location(
        "_c1_run_server_scaling", HARNESS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


NEEDLES = [
    {"name": "needle_a", "query": "what is the checkpoint interval value"},
    {"name": "needle_b", "query": "what does the flush cadence control"},
]

DEFAULT_GOLD = {
    "needle_a": ["ckptgene00000000"],
    "needle_b": ["ckptgene00000004"],
}


def run_worker(
    tmp_path: Path,
    base_url: str,
    env: dict | None = None,
    *,
    k: int = 12,
    rounds: int = 1,
    limit: int = 2,
    gold: dict | None = None,
) -> list[dict]:
    """Drive the UNMODIFIED ``worker_main`` against a stub and return samples.

    Writes a two-needle ``needles_resolved``-shaped file and a gold map,
    pre-creates the ``go`` barrier file so the worker never waits, builds the
    ``argparse.Namespace`` the harness expects, and calls ``worker_main``
    in-process. ``env`` entries are applied around the call and restored after,
    so a caller can flip the gate without leaking it into the session.
    """
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    resolved_path = tmp_path / "needles_resolved.json"
    gold_path = tmp_path / "gold_by_needle.json"
    out_path = tmp_path / "worker_out.json"
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    (sync_dir / "go").write_text("go", encoding="utf-8")

    resolved_path.write_text(
        json.dumps({"needles": NEEDLES}, indent=2), encoding="utf-8"
    )
    gold_path.write_text(
        json.dumps(gold if gold is not None else DEFAULT_GOLD, indent=2),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        resolved=str(resolved_path),
        gold=str(gold_path),
        limit=limit,
        k=k,
        rounds=rounds,
        base_url=base_url,
        session_id="c1-capture",
        worker_idx=0,
        worker_out=str(out_path),
        sync_dir=str(sync_dir),
        request_timeout=30.0,
        warmup_timeout=30.0,
    )

    harness = load_harness()
    previous: dict[str, str | None] = {}
    for key, value in (env or {}).items():
        previous[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        rc = harness.worker_main(args)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if rc != 0:
        raise RuntimeError(f"worker_main returned {rc}")
    return json.loads(out_path.read_text(encoding="utf-8"))["samples"]


# ---------------------------------------------------------- key manifest ----


def key_manifest(obj: Any, prefix: str = "") -> list[str]:
    """Recursive sorted dotted key manifest over a JSON tree.

    List elements collapse to a single ``[]`` segment so the manifest describes
    the SHAPE and not the length: ``agent.citations[].gene_id``. Returned
    sorted and deduplicated, so it is stable across runs and independent of
    dict iteration order.
    """
    paths: set[str] = set()

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key in node:
                child = f"{path}.{key}" if path else str(key)
                paths.add(child)
                walk(node[key], child)
        elif isinstance(node, list):
            for item in node:
                walk(item, f"{path}[]")

    walk(obj, prefix)
    return sorted(paths)


# ------------------------------------------------------------------ main ----


def capture_context_golden() -> dict:
    """Run the seeded fixture through the real ``/context`` route."""
    from cymatix_context.config import BudgetConfig
    from tests.conftest import make_client, make_cymatix_config

    client = make_client(
        make_cymatix_config(budget=BudgetConfig(max_genes_per_turn=12))
    )
    _seed_topic_genes(client.app.state.cymatix.genome, SEEDED_GENE_COUNT)
    resp = client.post("/context", json={"query": FACTUAL_QUERY})
    if resp.status_code != 200:
        raise RuntimeError(f"/context returned {resp.status_code}")
    body = resp.json()[0]
    return {
        "tree": TREE,
        "commit": COMMIT,
        "generated_by": GENERATED_BY,
        "query": FACTUAL_QUERY,
        "seeded_genes": SEEDED_GENE_COUNT,
        "key_manifest": key_manifest(body),
        "delivery": body.get("delivery"),
    }


def capture_worker_golden(tmp_root: Path) -> dict:
    """Drive the unpatched ``worker_main`` against the canned stub."""
    with serve_stub(CANNED_CONTEXT_RESPONSE) as base_url:
        samples = run_worker(tmp_root / "worker", base_url)
    success = [s for s in samples if "latency_ms" in s]
    if not success:
        raise RuntimeError("no success-path samples captured")
    keys: set[str] = set()
    for sample in success:
        keys.update(sample)
    first = success[0]
    return {
        "tree": TREE,
        "commit": COMMIT,
        "generated_by": GENERATED_BY,
        "sample_keys": sorted(keys),
        "invariant_fields": {
            k: v for k, v in sorted(first.items()) if k != "latency_ms"
        },
    }


def main() -> int:
    import tempfile

    _preflight()
    context_golden = capture_context_golden()
    CONTEXT_GOLDEN_PATH.write_text(
        json.dumps(context_golden, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {CONTEXT_GOLDEN_PATH}")

    with tempfile.TemporaryDirectory() as tmp:
        worker_golden = capture_worker_golden(Path(tmp))
    WORKER_GOLDEN_PATH.write_text(
        json.dumps(worker_golden, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {WORKER_GOLDEN_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
