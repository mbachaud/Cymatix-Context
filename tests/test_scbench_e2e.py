"""Non-scored Phase 0 qualification tests for the SCBench harness."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from tests.conftest import make_client, make_cymatix_config
from cymatix_context.config import (
    BudgetConfig,
    GenomeConfig,
    IngestionConfig,
    RetrievalConfig,
)
from cymatix_context.integrations.scbench.client import CymatixClient
from cymatix_context.integrations.scbench.config import (
    CampaignConfig,
    SourcePolicy,
)
from cymatix_context.integrations.scbench.coordinator import TreatmentCoordinator


QUALIFICATION_FIXTURE = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "scbench"
    / "fixtures"
    / "qualification"
)


class _TestClientTransport(httpx.BaseTransport):
    """Route the production HTTP client through an in-process FastAPI app."""

    def __init__(self, test_client) -> None:
        self.test_client = test_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.test_client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.read(),
        )
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )


def _campaign() -> CampaignConfig:
    return CampaignConfig.model_validate(
        {
            "campaign_id": "qualification",
            "cymatix_commit": "1" * 40,
            "scbench_commit": "2" * 40,
            "codex_version": "codex-cli 0.74.0",
            "codex_model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
            "problems": ["qualification"],
            "replicates": [
                {"id": 1, "order": ["control", "cymatix"]},
                {"id": 2, "order": ["cymatix", "control"]},
            ],
            "treatment": {},
            "source_policy": SourcePolicy.default().model_dump(mode="json"),
        }
    )


def _qualification_config(tmp_path: Path):
    return make_cymatix_config(
        budget=BudgetConfig(
            max_genes_per_turn=8,
            session_delivery_enabled=True,
        ),
        genome=GenomeConfig(
            path=str(tmp_path / "qualification.genome.db"),
            cold_start_threshold=0,
        ),
        ingestion=IngestionConfig(
            splade_enabled=False,
            dense_embed_on_ingest=False,
            sema_embed_on_ingest=False,
        ),
        retrieval=RetrievalConfig(dense_embedding_enabled=False),
    )


def test_closed_loop_remembers_prior_constraint_without_eval_data(tmp_path):
    """Removing conversation learning or source filtering loses the prior constraint."""
    workspace = tmp_path / "workspace"
    shutil.copytree(QUALIFICATION_FIXTURE, workspace)
    evaluation = workspace / "evaluation"
    evaluation.mkdir()
    (evaluation / "hidden.txt").write_text(
        "HIDDEN EVALUATION SECRET", encoding="utf-8"
    )
    (workspace / "evaluation.json").write_text(
        '{"hidden":"grader output"}', encoding="utf-8"
    )

    config = _qualification_config(tmp_path)
    with make_client(config) as app_client:
        client = CymatixClient(
            "http://testserver",
            timeout_s=20.0,
            transport=_TestClientTransport(app_client),
        )
        coordinator = TreatmentCoordinator(
            campaign=_campaign(),
            pair_id="qualification-r1",
            replicate=1,
            problem="qualification",
            workspace_root=workspace,
            receipt_root=tmp_path / "receipts",
            client=client,
            evaluation_roots=(evaluation,),
            warmup_enabled=True,
        )
        coordinator.initialize()
        assert coordinator.initial_sync is not None
        assert coordinator.initial_sync.ingested >= 2
        first = coordinator.before_checkpoint("Keep VALUE compatible")
        assert first.packet is None
        (workspace / "src" / "state.py").write_text(
            'VALUE = 1\nLAST_DECISION = "preserve VALUE"\n',
            encoding="utf-8",
        )
        (workspace / "src" / "accessor.py").write_text(
            "from .state import VALUE\n\ndef current_value(): return VALUE\n",
            encoding="utf-8",
        )
        (workspace / "README.md").unlink()
        sync = coordinator.after_checkpoint(
            "Keep VALUE compatible",
            "I preserved VALUE = 1",
        )
        assert sync.ingested >= 3
        assert "repo://qualification/README.md" in sync.deleted_source_ids
        coordinator.finish_checkpoint()

        prepared = coordinator.before_checkpoint("Add a new accessor")

        assert "VALUE" in prepared.prompt
        assert "evaluation.json" not in prepared.prompt
        assert "hidden" not in prepared.prompt.casefold()
        assert coordinator.warmup_elapsed_ms >= 0
        assert prepared.retrieval is not None
        replayed_prompt = (
            tmp_path / "receipts" / prepared.prompt_artifact.path
        ).read_bytes().decode("utf-8")
        assert replayed_prompt == prepared.prompt
        coordinator.close()

    assert (tmp_path / "qualification.genome.db").is_file()
