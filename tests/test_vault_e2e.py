"""End-to-end: ingest → export → trace → pin → prune cycle, all via VaultManager."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from cymatix_context.config import HelixConfig, VaultConfig, VaultTracesConfig
from cymatix_context.genome import Genome
from cymatix_context.schemas import ChromatinState
from cymatix_context.vault import VaultManager
from tests.conftest import make_gene


@pytest.fixture
def vm(tmp_path: Path):
    cfg = HelixConfig()
    cfg.vault = VaultConfig(
        enabled=True, path=str(tmp_path / "vault"),
        party_id="", fan_out_threshold=5000, redact_body=False,
        stale_threshold=0.5,
        traces=VaultTracesConfig(
            enabled=True, retention_hours=48,
            max_retention_hours_hard=720, max_count=10000,
            rollup_enabled=True, rollup_shard="hour",
            prune_interval_minutes=60, trigger_only=False,
        ),
    )
    genome = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
    vault_root = Path(cfg.vault.path)
    manager = VaultManager(config=cfg, genome=genome)
    manager.start()
    yield manager, genome, vault_root
    manager.stop()
    genome.close()


def test_full_cycle(vm):
    manager, genome, vault_root = vm

    # 1. Ingest 5 genes
    for i in range(5):
        g = make_gene(f"content-{i}", domains=["e2e"], chromatin=ChromatinState.EUCHROMATIN)
        g.source_id = f"e{i}.py"
        genome.upsert_gene(g)

    # 2. Full export
    stats = manager.full_export()
    assert stats["genes_exported"] == 5

    # 3. Files on disk
    files = list((vault_root / "genes" / "e2e").glob("*.md"))
    assert len(files) == 5

    # 4. README written
    assert (vault_root / "README.md").exists()
    readme = (vault_root / "README.md").read_text()
    assert "v1.1" in readme

    # 5. Frontmatter has both computed and authored placeholders
    sample = files[0].read_text()
    rest = sample[len("---\n"):]
    end = rest.index("---\n")
    fm = yaml.safe_load(rest[:end])
    assert "gene_id" in fm
    assert fm["operator_notes"] == ""
    assert fm["pinned"] is False

    # 6. Trace export
    trace = manager.trace_export(
        request_id="e2e-01",
        trigger_reason="manual",
        total_latency_ms=42,
        health_status="aligned",
        stage_timing_ms={"extract": 1, "rerank": 41},
        fingerprint_route="",
        foveated_ranks="",
        final_genes=[],
    )
    assert trace.exists()
    assert "_exp" in trace.name

    # 7. Pin (move file to _traces-pinned/, strip _exp suffix)
    pinned_dir = vault_root / "_traces-pinned"
    pinned_dir.mkdir(exist_ok=True, mode=0o700)
    import re
    new_name = re.sub(r"_exp\d+\.md$", ".md", trace.name)
    pinned_path = pinned_dir / new_name
    trace.replace(pinned_path)
    assert pinned_path.exists()

    # 8. Run prune cycle — pinned trace must survive (mtime is fresh)
    results = manager.run_prune_cycle()
    assert results["traces"]["pruned_count"] == 0
    assert results["traces"]["force_pruned_count"] == 0
    assert pinned_path.exists()

    # 9. Status method reflects state
    s = manager.status()
    assert s["enabled"] is True
    assert s["exported_gene_count"] == 5
