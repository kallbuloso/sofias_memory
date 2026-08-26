"""Route-level contract for POST /api/v1/improve after the B5 migration (SM-511).

Mirrors ``test_cognify_routes.py`` (SM-510): the submission service, waiter
and PostgreSQL reads are substituted so the HTTP contract can be asserted
precisely; durable behavior is proven against real PostgreSQL/Neo4j in the
integration suite.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.improve import IMPROVE_RESULT_METRIC_KEY
from sofias_memory.services.pipeline_submission import (
    SubmissionOutcome,
    SubmissionTargets,
    worker_disabled_error,
)
from sofias_memory.services.pipeline_waiter import WaitOutcome

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"

DATASET_ID = UUID("33333333-3333-3333-3333-333333333333")
RUN_ID = UUID("55555555-5555-5555-5555-555555555555")

IMPROVE_MODULE = "sofias_memory.api.routes.improve"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@dataclass
class FakeRun:
    id: UUID = RUN_ID
    metrics: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


@dataclass
class Recorder:
    submits: list[dict[str, Any]] = field(default_factory=list)
    waits: list[UUID] = field(default_factory=list)
    outcome: SubmissionOutcome = field(
        default_factory=lambda: SubmissionOutcome(
            run_id=RUN_ID,
            pipeline_type=PipelineType.IMPROVE,
            dataset_id=DATASET_ID,
            source_id=None,
            status=PipelineRunStatus.QUEUED,
            created=True,
        )
    )
    wait_status: PipelineRunStatus = PipelineRunStatus.SUCCEEDED
    wait_timed_out: bool = False
    submit_error: Exception | None = None
    run: FakeRun = field(default_factory=FakeRun)


def install_fakes(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    class FakeSubmissionService:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def submit(
            self,
            *,
            pipeline_type: PipelineType,
            work_input: Mapping[str, Any],
            idempotency_key: str | None,
            prepare: Any,
        ) -> SubmissionOutcome:
            if recorder.submit_error is not None:
                raise recorder.submit_error
            targets = await prepare(FakeSubmissionUnitOfWork())
            recorder.submits.append(
                {
                    "pipeline_type": pipeline_type,
                    "work_input": dict(work_input),
                    "idempotency_key": idempotency_key,
                    "targets": targets,
                }
            )
            return recorder.outcome

    class FakeWaiter:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def wait_for_terminal(self, run_id: UUID, *, timeout_seconds: float) -> WaitOutcome:
            del timeout_seconds
            recorder.waits.append(run_id)
            return WaitOutcome(
                run_id=run_id,
                status=recorder.wait_status,
                timed_out=recorder.wait_timed_out,
            )

    class FakeUnitOfWork:
        def __init__(self, session_factory: Any) -> None:
            del session_factory

        async def __aenter__(self) -> FakeUnitOfWork:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        @property
        def pipeline_runs(self) -> Any:
            class _Runs:
                async def get_by_id(self, run_id: UUID) -> FakeRun | None:
                    return recorder.run if run_id == recorder.run.id else None

            return _Runs()

    monkeypatch.setattr(f"{IMPROVE_MODULE}.PipelineSubmissionService", FakeSubmissionService)
    monkeypatch.setattr(f"{IMPROVE_MODULE}.PipelineRunWaiter", FakeWaiter)
    monkeypatch.setattr(f"{IMPROVE_MODULE}.PostgresUnitOfWork", FakeUnitOfWork)


@dataclass
class FakeDataset:
    id: UUID = DATASET_ID
    status: Any = None


class FakeSubmissionUnitOfWork:
    @property
    def datasets(self) -> Any:
        class _Datasets:
            async def get_by_slug(self, slug: str) -> FakeDataset | None:
                if slug == "missing":
                    return None
                from sofias_memory.domain import DatasetStatus

                if slug == "inactive":
                    return FakeDataset(status=DatasetStatus.DELETING)
                return FakeDataset(status=DatasetStatus.ACTIVE)

        return _Datasets()


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def make_app() -> Any:
    return create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)


def succeeded_result_metrics() -> dict[str, Any]:
    result: dict[str, Any] = {
        "dataset_id": str(DATASET_ID),
        "generation": 1,
        "stages": ["feedback_weights", "entity_deduplication", "relation_embeddings"],
        "feedback_processed": 2,
        "feedback_applied": 1,
        "feedback_skipped": 1,
        "entities_updated": 1,
        "relations_updated": 0,
        "relations_embedded": 3,
        "entities_embedded": 2,
        "entity_duplicate_candidates": 1,
        "entities_merged": 1,
        "entity_mentions_reassigned": 1,
        "relations_rewired": 0,
        "relations_deactivated": 0,
        "relation_evidence_copied": 0,
        "document_summaries_rebuilt": 0,
        "dataset_summaries_rebuilt": 0,
        "summaries_deactivated": 0,
        "graph_relations_deactivated": 0,
        "graph_entities_importance_updated": 0,
        "graph_relations_importance_updated": 0,
        "graph_entities_missing": 0,
        "graph_entities_extra": 0,
        "graph_chunks_missing": 0,
        "graph_chunks_extra": 0,
        "graph_entity_mentions_missing": 0,
        "graph_entity_mentions_extra": 0,
        "graph_relations_missing": 0,
        "graph_relations_extra": 0,
        "graph_next_missing": 0,
        "graph_next_extra": 0,
        "graph_rebuilt": False,
        "graph_events_enqueued": 2,
        "graph_events_processed": 0,
    }
    return result


def succeeded_run() -> FakeRun:
    return FakeRun(metrics={IMPROVE_RESULT_METRIC_KEY: succeeded_result_metrics()})


async def post_improve(
    app: Any,
    body: Mapping[str, Any],
    *,
    idempotency_key: str | None = None,
) -> httpx.Response:
    headers = {API_KEY_HEADER: EXPECTED_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with build_client(app) as client:
        return await client.post("/api/v1/improve", headers=headers, json=dict(body))


# --- validation -----------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_dataset_is_resolved_by_the_preparation_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"dataset": "missing"})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Dataset does not exist."


@pytest.mark.asyncio
async def test_inactive_dataset_is_rejected_by_the_preparation_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"dataset": "inactive"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unsupported_stage_is_rejected_before_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"stages": ["not_a_real_stage"]})

    assert response.status_code == 422  # rejected by the pydantic Literal, before the route body
    assert recorder.submits == []


# --- wait=false / wait=true -------------------------------------------------


@pytest.mark.asyncio
async def test_wait_false_returns_202_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"wait": False})

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["run_id"] == str(RUN_ID)
    assert data["status"] == "queued"
    assert data["dataset_id"] is None
    assert data["stages"] is None
    assert recorder.waits == []


@pytest.mark.asyncio
async def test_wait_true_returns_the_persisted_result_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"wait": True})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "succeeded"
    assert data["dataset_id"] == str(DATASET_ID)
    assert data["entities_merged"] == 1
    assert data["relations_embedded"] == 3
    assert data["graph_events_enqueued"] == 2
    assert recorder.waits == [RUN_ID]


@pytest.mark.asyncio
async def test_wait_true_and_wait_false_share_one_submission_path_excluding_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)
    app = make_app()

    await post_improve(app, {"wait": True})
    await post_improve(app, {"wait": False})

    assert len(recorder.submits) == 2
    assert recorder.submits[0]["work_input"] == recorder.submits[1]["work_input"]
    assert "wait" not in recorder.submits[0]["work_input"]
    assert recorder.submits[0]["pipeline_type"] == PipelineType.IMPROVE
    assert recorder.submits[0]["targets"] == SubmissionTargets(
        dataset_id=DATASET_ID, source_id=None
    )


@pytest.mark.asyncio
async def test_stages_none_and_explicit_default_order_are_the_same_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)
    app = make_app()

    await post_improve(app, {"wait": True})
    await post_improve(
        app,
        {
            "wait": True,
            "stages": ["feedback_weights", "entity_deduplication", "relation_embeddings"],
        },
    )

    assert recorder.submits[0]["work_input"] == recorder.submits[1]["work_input"]


@pytest.mark.asyncio
async def test_custom_stage_order_is_preserved_in_the_work_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SM-511 MAJOR 1: request order is part of the durable work identity --
    never reordered to a canonical pipeline order."""

    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)

    await post_improve(
        make_app(), {"wait": True, "stages": ["relation_embeddings", "feedback_weights"]}
    )

    assert recorder.submits[0]["work_input"]["stages"] == [
        "relation_embeddings",
        "feedback_weights",
    ]


@pytest.mark.asyncio
async def test_reordered_stage_lists_are_different_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)
    app = make_app()

    await post_improve(app, {"wait": True, "stages": ["relation_embeddings", "feedback_weights"]})
    await post_improve(app, {"wait": True, "stages": ["feedback_weights", "relation_embeddings"]})

    assert recorder.submits[0]["work_input"] != recorder.submits[1]["work_input"]


@pytest.mark.asyncio
async def test_duplicate_stages_preserve_first_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)

    await post_improve(
        make_app(),
        {
            "wait": True,
            "stages": ["relation_embeddings", "feedback_weights", "relation_embeddings"],
        },
    )

    assert recorder.submits[0]["work_input"]["stages"] == [
        "relation_embeddings",
        "feedback_weights",
    ]


@pytest.mark.asyncio
async def test_wait_true_times_out_into_202_without_inventing_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(wait_status=PipelineRunStatus.RUNNING, wait_timed_out=True)
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"wait": True})

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["status"] == "running"
    assert data["entities_merged"] is None


@pytest.mark.asyncio
async def test_wait_true_skips_waiting_on_an_already_terminal_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    recorder.outcome = SubmissionOutcome(
        run_id=RUN_ID,
        pipeline_type=PipelineType.IMPROVE,
        dataset_id=DATASET_ID,
        source_id=None,
        status=PipelineRunStatus.SUCCEEDED,
        created=False,
    )
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"wait": True}, idempotency_key="k-1")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "succeeded"
    assert recorder.waits == []


# --- idempotency / worker gate / failure ------------------------------------


@pytest.mark.asyncio
async def test_idempotency_key_header_reaches_the_submission_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)
    app = make_app()

    await post_improve(app, {"wait": True}, idempotency_key="improve-1")
    await post_improve(app, {"wait": False}, idempotency_key="improve-1")

    assert [item["idempotency_key"] for item in recorder.submits] == ["improve-1", "improve-1"]


@pytest.mark.asyncio
async def test_missing_idempotency_key_header_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)

    await post_improve(make_app(), {"wait": True})

    assert recorder.submits[0]["idempotency_key"] is None


@pytest.mark.asyncio
async def test_reserved_idempotency_namespace_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from sofias_memory.services.pipeline_submission import reserved_idempotency_key_namespace_error

    recorder = Recorder(submit_error=reserved_idempotency_key_namespace_error())
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"wait": True}, idempotency_key="sys:internal")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.RESERVED_IDEMPOTENCY_KEY_NAMESPACE


@pytest.mark.asyncio
async def test_worker_unavailable_is_surfaced_as_503(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(submit_error=worker_disabled_error())
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"wait": True})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.WORKER_DISABLED
    assert recorder.waits == []


@pytest.mark.asyncio
async def test_failed_run_returns_a_safe_error_derived_from_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(
        wait_status=PipelineRunStatus.FAILED,
        run=FakeRun(error_code="IMPROVE_DEPENDENCY_UNAVAILABLE"),
    )
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"wait": True})

    error = response.json()["error"]
    assert response.status_code == 500
    assert error["code"] == ErrorCode.INTERNAL_ERROR
    assert error["message"] == "Improve run failed."
    assert error["details"] == {
        "run_id": str(RUN_ID),
        "status": "failed",
        "step_error_code": "IMPROVE_DEPENDENCY_UNAVAILABLE",
    }
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_cancelled_run_is_reported_without_business_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(wait_status=PipelineRunStatus.CANCELLED)
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"wait": True})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "cancelled"
    assert data["entities_merged"] is None


@pytest.mark.asyncio
async def test_succeeded_run_without_persisted_result_is_an_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=FakeRun(metrics={}))
    install_fakes(monkeypatch, recorder)

    response = await post_improve(make_app(), {"wait": True})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == ErrorCode.INTERNAL_ERROR


# --- the route never executes B4 Improve inline -----------------------------


def test_improve_route_module_no_longer_references_the_b4_orchestration() -> None:
    from sofias_memory.api.routes import improve as route_module

    assert not hasattr(route_module, "ImproveService")
    assert not hasattr(route_module, "OpenAIEmbeddingClient")
    assert not hasattr(route_module, "GraphOutboxBatchProcessor")
    assert not hasattr(route_module, "GraphMaintenanceService")
    assert not hasattr(route_module, "GraphReconciliationService")
    assert not hasattr(route_module, "SummaryRebuildService")


def test_improve_service_module_no_longer_owns_run_lifecycle() -> None:
    from sofias_memory.services import improve as service_module

    assert not hasattr(service_module, "ImproveService")


# --- registry -----------------------------------------------------------------


def test_default_registry_contains_all_five_pipelines() -> None:
    from sofias_memory.domain import PipelineType
    from sofias_memory.pipelines.registry import build_default_pipeline_registry

    registry = build_default_pipeline_registry()

    assert registry.get(PipelineType.REMEMBER) is not None
    assert registry.get(PipelineType.COGNIFY) is not None
    assert registry.get(PipelineType.IMPROVE) is not None
    assert registry.get(PipelineType.FORGET) is not None
    assert registry.get(PipelineType.DATASET_DELETE) is not None
    assert len(registry) == 5


# --- OpenAPI surface ------------------------------------------------------


def test_improve_openapi_surface_is_a_single_post_route() -> None:
    paths = cast(dict[str, dict[str, Any]], make_app().openapi()["paths"])
    improve_paths = {path for path in paths if path.startswith("/api/v1/improve")}

    assert improve_paths == {"/api/v1/improve"}
    assert set(paths["/api/v1/improve"].keys()) == {"post"}


def test_improve_request_schema_forbids_unknown_fields() -> None:
    schemas = cast(dict[str, dict[str, Any]], make_app().openapi()["components"]["schemas"])

    assert schemas["ImproveRequest"]["additionalProperties"] is False
    assert set(schemas["ImproveRequest"]["properties"]) == {"dataset", "stages", "wait"}


def test_improve_declares_the_accepted_response() -> None:
    paths = cast(dict[str, dict[str, Any]], make_app().openapi()["paths"])

    assert "202" in paths["/api/v1/improve"]["post"]["responses"]
