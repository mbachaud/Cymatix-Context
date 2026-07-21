"""Canonical-digest determinism (council Amendment 2, implemented exactly).

For a fixed adapter version, pinned spaCy model version, and OKF spec
version, ingesting the same bundle yields a byte-identical canonical
digest across runs and platforms. The tests below:

- ingest each vendored sample bundle twice in SEPARATE PROCESSES with
  DIFFERENT PYTHONHASHSEED values and assert digest equality;
- assert digest stability across a clock advance;
- assert the wall-clock timestamp columns DIFFER between the two runs
  (documenting that they are outside the guarantee);
- assert float artifacts (embeddings) vary with config while the
  digest does not — excluded by construction, the digest is computed
  from the parsed bundle and never from the SQLite file or raw rows.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cymatix_context.okf import compute_bundle_digest, read_bundle
from cymatix_context.okf.digest import ADAPTER_VERSION, bundle_digest_payload

OKF_FIXTURES = Path(__file__).parent / "fixtures" / "okf"

# Full ingest in a child process: parse → digest → ingest into an
# in-memory store under the deterministic-ingest profile (no SEMA /
# dense / SPLADE model loads in the child; spaCy still runs), then
# report the digest plus a wall-clock column sample.
_INGEST_SCRIPT = """
import json, sys
from cymatix_context.config import (
    BudgetConfig, GenomeConfig, HelixConfig, RibosomeConfig,
)
from cymatix_context.context_manager import HelixContextManager
from cymatix_context.okf import ingest_bundle

cfg = HelixConfig(
    ribosome=RibosomeConfig(model="mock", timeout=5),
    budget=BudgetConfig(max_genes_per_turn=4),
    genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
)
cfg.ingestion.sema_embed_on_ingest = False
cfg.ingestion.dense_embed_on_ingest = False
cfg.ingestion.splade_enabled = False

mgr = HelixContextManager(cfg)
result = ingest_bundle(mgr, sys.argv[1])
row = mgr.genome.conn.execute(
    "SELECT MAX(last_seen), MAX(last_verified_at) FROM genes"
).fetchone()
print(json.dumps({
    "digest": result.digest,
    "last_seen": row[0],
    "last_verified_at": row[1],
    "links_captured": result.links_captured,
}))
"""

_READ_SCRIPT = """
import json, sys
from cymatix_context.okf import compute_bundle_digest, read_bundle
print(json.dumps({"digest": compute_bundle_digest(read_bundle(sys.argv[1]))}))
"""


def _run_child(script: str, bundle: Path, hashseed: str) -> dict:
    import os

    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hashseed
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", script, str(bundle)],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestSeparateProcessDeterminism:
    @pytest.mark.parametrize("bundle", ["crypto_bitcoin", "ga4"])
    def test_sample_bundle_ingest_digest_stable_across_hashseeds(self, bundle):
        pytest.importorskip("spacy")
        path = OKF_FIXTURES / bundle
        run_a = _run_child(_INGEST_SCRIPT, path, hashseed="0")
        run_b = _run_child(_INGEST_SCRIPT, path, hashseed="424242")

        assert run_a["digest"] == run_b["digest"]
        assert run_a["links_captured"] == run_b["links_captured"]
        # Wall-clock columns are OUTSIDE the guarantee — two ingest runs
        # stamp different times, and the digest must not see them.
        assert run_a["last_seen"] != run_b["last_seen"]

    @pytest.mark.parametrize("bundle", ["type_only", "degraded"])
    def test_synthetic_bundle_digest_stable_across_hashseeds(self, bundle):
        path = OKF_FIXTURES / bundle
        run_a = _run_child(_READ_SCRIPT, path, hashseed="1")
        run_b = _run_child(_READ_SCRIPT, path, hashseed="31337")
        assert run_a["digest"] == run_b["digest"]


class TestClockIndependence:
    def test_digest_stable_across_clock_advance(self):
        bundle = read_bundle(OKF_FIXTURES / "crypto_bitcoin")
        before = compute_bundle_digest(bundle)
        import time as _time

        real_time = _time.time

        with patch("time.time", lambda: real_time() + 86_400.0):
            advanced = compute_bundle_digest(
                read_bundle(OKF_FIXTURES / "crypto_bitcoin")
            )
        assert advanced == before


class TestFloatExclusion:
    def test_embeddings_vary_with_config_but_digest_does_not(self):
        pytest.importorskip("spacy")
        from cymatix_context.context_manager import HelixContextManager
        from cymatix_context.okf import ingest_bundle
        from tests.conftest import make_helix_config

        path = OKF_FIXTURES / "type_only"

        default_cfg = make_helix_config()
        mgr_default = HelixContextManager(default_cfg)
        res_default = ingest_bundle(mgr_default, path)

        det_cfg = make_helix_config()
        det_cfg.ingestion.sema_embed_on_ingest = False
        det_cfg.ingestion.dense_embed_on_ingest = False
        det_cfg.ingestion.splade_enabled = False
        mgr_det = HelixContextManager(det_cfg)
        res_det = ingest_bundle(mgr_det, path)

        assert res_default.digest == res_det.digest

        dense_default = mgr_default.genome.conn.execute(
            "SELECT embedding_dense_v2 FROM genes WHERE gene_id = ?",
            (res_default.gene_ids[0],),
        ).fetchone()[0]
        dense_det = mgr_det.genome.conn.execute(
            "SELECT embedding_dense_v2 FROM genes WHERE gene_id = ?",
            (res_det.gene_ids[0],),
        ).fetchone()[0]
        # Default config stores a float vector; the deterministic-ingest
        # profile stores NULL — and neither is visible to the digest.
        assert dense_default is not None
        assert dense_det is None


class TestPayloadShape:
    def test_payload_matches_amendment_2_fields(self):
        bundle = read_bundle(OKF_FIXTURES / "crypto_bitcoin")
        payload = bundle_digest_payload(bundle)

        assert payload["adapter_version"] == ADAPTER_VERSION
        assert payload["okf_spec_pin"] == "ee67a5ca"
        concept = payload["concepts"][0]
        assert set(concept) == {
            "concept_id",
            "gene_id",
            "content_hash",
            "type",
            "title",
            "description",
            "domains",
            "entities",
            "key_values",
        }
        assert concept["gene_id"] == concept["content_hash"][:16]
        assert concept["domains"] == sorted(concept["domains"])
        assert concept["key_values"] == sorted(concept["key_values"])
        # Digest entities are adapter-supplied (frontmatter-derivable);
        # tagger/spaCy entities merge into the store, not the digest.
        assert concept["entities"] == []

        ids = [c["concept_id"] for c in payload["concepts"]]
        assert ids == sorted(ids)
        assert payload["links"] == sorted(payload["links"])

    def test_link_edge_set_includes_dangling_targets(self):
        bundle = read_bundle(OKF_FIXTURES / "degraded")
        payload = bundle_digest_payload(bundle)
        assert ["dangling", "missing/concept"] in payload["links"]

    def test_digest_changes_when_content_changes(self, tmp_path):
        (tmp_path / "c.md").write_text(
            "---\ntype: X\n---\noriginal", encoding="utf-8"
        )
        d1 = compute_bundle_digest(read_bundle(tmp_path, bundle_id="b"))
        (tmp_path / "c.md").write_text(
            "---\ntype: X\n---\nmodified", encoding="utf-8"
        )
        d2 = compute_bundle_digest(read_bundle(tmp_path, bundle_id="b"))
        assert d1 != d2

    def test_digest_ignores_bundle_directory_location(self, tmp_path):
        # POSIX-relative paths only: the same bundle content in two
        # differently-named parent directories digests identically when
        # given the same bundle_id... and the bundle_id itself is not a
        # digest input either (it is host-local provenance).
        for name in ("loc_a", "loc_b"):
            d = tmp_path / name
            d.mkdir()
            (d / "c.md").write_text(
                "---\ntype: X\n---\nbody", encoding="utf-8"
            )
        d1 = compute_bundle_digest(read_bundle(tmp_path / "loc_a"))
        d2 = compute_bundle_digest(read_bundle(tmp_path / "loc_b"))
        assert d1 == d2
