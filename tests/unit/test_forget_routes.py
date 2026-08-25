"""Route-level contract for POST /api/v1/forget after the B5 migration (SM-512).

Mirrors ``test_cognify_routes.py``/``test_improve_routes.py``: the
submission service, waiter and PostgreSQL reads are substituted so the HTTP
contract can be asserted precisely; durable behavior is proven against real
PostgreSQL/Neo4j/filesystem in the integration suite.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.forget import FORGET_RESULT_METRIC_KEY
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

DATASET_ID = UUID("77777777-7777-7777-7777-777777777777")
SOURCE_ID = UUID("88888888-8888-8888-8888-888888888888")
RUN_ID = UUID("99999999-9999-9999-9999-999999999999")

FORGET_MODULE = "sofias_memory.api.routes.forget"


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
            pipeline_type=PipelineType.FORGET,
            dataset_id=DATASET_ID,
            source_id=SOURCE_ID,
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
                run_id=run_id, status=recorder.wait_status, timed_out=recorder.wait_timed_out
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

    monkeypatch.setattr(f"{FORGET_MODULE}.PipelineSubmissionService", FakeSubmissionService)
    monkeypatch.setattr(f"{FORGET_MODULE}.PipelineRunWaiter", FakeWaiter)
    monkeypatch.setattr(f"{FORGET_MODULE}.PostgresUnitOfWork", FakeUnitOfWork)


@dataclass
class FakeDataset:
    id: UUID = DATASET_ID
    status: Any = DatasetStatus.ACTIVE


@dataclass
class FakeSource:
    id: UUID = SOURCE_ID
    dataset_id: UUID = DATASET_ID


class FakeSubmissionUnitOfWork:
    @property
    def datasets(self) -> Any:
        class _Datasets:
            async def get_by_slug(self, slug: str) -> FakeDataset | None:
                if slug == "missing":
                    return None
                return FakeDataset()

        return _Datasets()

    @property
    def sources(self) -> Any:
        class _Sources:
            async def get_by_id(self, source_id: UUID) -> FakeSource | None:
                if source_id == SOURCE_ID:
                    return FakeSource()
                return None

        return _Sources()


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def make_app() -> Any:
    return create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)


def succeeded_source_metrics() -> dict[str, Any]:
    return {
        "dataset_id": str(DATASET_ID),
        "source_id": str(SOURCE_ID),
        "memory_only": False,
        "source_status": "deleted",
        "documents_deactivated": 1,
        "chunks_deactivated": 2,
        "summaries_deactivated": 1,
        "entities_deactivated": 0,
        "relations_deactivated": 0,
        "entity_mentions_unprojected": 3,
        "relation_evidence_unprojected": 1,
        "graph_events_enqueued": 4,
        "graph_events_processed": 4,
        "storage_deleted": True,
    }


def succeeded_run() -> FakeRun:
    return FakeRun(metrics={FORGET_RESULT_METRIC_KEY: succeeded_source_metrics()})


async def post_forget(
    app: Any, body: Mapping[str, Any], *, idempotency_key: str | None = None
) -> httpx.Response:
    headers = {API_KEY_HEADER: EXPECTED_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with build_client(app) as client:
        return await client.post("/api/v1/forget", headers=headers, json=dict(body))


# --- scope validation --------------------------------------------------------


@pytest.mark.asyncio
async def test_source_scope_is_derived_from_source_id(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    await post_forget(make_app(), {"source_id": str(SOURCE_ID), "wait": False})

    assert recorder.submits[0]["work_input"]["scope"] == "source"
    assert recorder.submits[0]["work_input"]["source_id"] == str(SOURCE_ID)
    assert "wait" not in recorder.submits[0]["work_input"]


@pytest.mark.asyncio
async def test_dataset_scope_requires_explicit_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"wait": False})

    assert response.status_code == 400
    assert recorder.submits == []


@pytest.mark.asyncio
async def test_dataset_scope_is_derived_from_explicit_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    await post_forget(make_app(), {"dataset": "main", "wait": False})

    assert recorder.submits[0]["work_input"]["scope"] == "dataset"
    assert recorder.submits[0]["work_input"]["dataset"] == "main"


@pytest.mark.asyncio
async def test_everything_requires_exact_confirm_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"everything": True, "wait": False})

    assert response.status_code == 400
    assert recorder.submits == []


@pytest.mark.asyncio
async def test_everything_scope_submits_with_no_dataset_or_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(
        outcome=SubmissionOutcome(
            run_id=RUN_ID,
            pipeline_type=PipelineType.FORGET,
            dataset_id=None,
            source_id=None,
            status=PipelineRunStatus.QUEUED,
            created=True,
        )
    )
    install_fakes(monkeypatch, recorder)

    await post_forget(
        make_app(), {"everything": True, "confirm": "DELETE EVERYTHING", "wait": False}
    )

    assert recorder.submits[0]["work_input"] == {"scope": "everything"}
    assert recorder.submits[0]["targets"] == SubmissionTargets(dataset_id=None, source_id=None)


@pytest.mark.asyncio
async def test_unknown_source_is_resolved_by_the_preparation_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"source_id": str(uuid4()), "wait": False})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unknown_dataset_is_resolved_by_the_preparation_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"dataset": "missing", "wait": False})

    assert response.status_code == 404


# --- wait=false / wait=true --------------------------------------------------


@pytest.mark.asyncio
async def test_wait_false_returns_202_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"source_id": str(SOURCE_ID), "wait": False})

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["scope"] == "source"
    assert data["status"] == "queued"
    assert recorder.waits == []


@pytest.mark.asyncio
async def test_wait_true_returns_the_persisted_result_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"source_id": str(SOURCE_ID), "wait": True})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "succeeded"
    assert data["storage_deleted"] is True
    assert recorder.waits == [RUN_ID]


@pytest.mark.asyncio
async def test_wait_true_and_wait_false_share_one_submission_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)
    app = make_app()

    await post_forget(app, {"source_id": str(SOURCE_ID), "wait": True})
    await post_forget(app, {"source_id": str(SOURCE_ID), "wait": False})

    assert recorder.submits[0]["work_input"] == recorder.submits[1]["work_input"]


@pytest.mark.asyncio
async def test_wait_true_times_out_into_202(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(wait_status=PipelineRunStatus.RUNNING, wait_timed_out=True)
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"source_id": str(SOURCE_ID), "wait": True})

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "running"


# --- idempotency / worker gate / failure --------------------------------------


@pytest.mark.asyncio
async def test_idempotency_key_header_reaches_the_submission_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)
    app = make_app()

    await post_forget(app, {"source_id": str(SOURCE_ID), "wait": True}, idempotency_key="forget-1")

    assert recorder.submits[0]["idempotency_key"] == "forget-1"


@pytest.mark.asyncio
async def test_worker_unavailable_is_surfaced_as_503(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(submit_error=worker_disabled_error())
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"source_id": str(SOURCE_ID), "wait": True})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.WORKER_DISABLED


@pytest.mark.asyncio
async def test_target_conflict_failure_is_surfaced_as_409(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(
        wait_status=PipelineRunStatus.FAILED,
        run=FakeRun(error_code="FORGET_TARGET_CONFLICT"),
    )
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"source_id": str(SOURCE_ID), "wait": True})

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_other_failure_is_surfaced_as_500(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(
        wait_status=PipelineRunStatus.FAILED,
        run=FakeRun(error_code="FORGET_DEPENDENCY_UNAVAILABLE"),
    )
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"source_id": str(SOURCE_ID), "wait": True})

    assert response.status_code == 500
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_cancelled_run_is_reported_without_business_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(wait_status=PipelineRunStatus.CANCELLED)
    install_fakes(monkeypatch, recorder)

    response = await post_forget(make_app(), {"source_id": str(SOURCE_ID), "wait": True})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "cancelled"
    assert data["storage_deleted"] is None


# --- the route never executes B4 Forget inline -------------------------------


def test_forget_route_module_no_longer_references_the_b4_orchestration() -> None:
    from sofias_memory.api.routes import forget as route_module

    assert not hasattr(route_module, "ForgetService")
    assert not hasattr(route_module, "Neo4jProjection")
    assert not hasattr(route_module, "GraphOutboxProcessor")
    assert not hasattr(route_module, "GraphOutboxBatchProcessor")


def test_forget_service_module_no_longer_owns_run_lifecycle() -> None:
    from sofias_memory.services import forget as service_module

    assert not hasattr(service_module, "ForgetService")


# --- registry -----------------------------------------------------------------


def test_default_registry_contains_cognify_improve_and_forget() -> None:
    from sofias_memory.pipelines.registry import build_default_pipeline_registry

    registry = build_default_pipeline_registry()

    assert registry.get(PipelineType.COGNIFY) is not None
    assert registry.get(PipelineType.IMPROVE) is not None
    assert registry.get(PipelineType.FORGET) is not None
    assert len(registry) == 3


def test_remember_is_still_unregistered() -> None:
    from sofias_memory.pipelines.registry import (
        UnknownPipelineTypeError,
        build_default_pipeline_registry,
    )

    with pytest.raises(UnknownPipelineTypeError):
        build_default_pipeline_registry().get(PipelineType.REMEMBER)


# --- OpenAPI surface ------------------------------------------------------


def test_forget_openapi_surface_is_a_single_post_route() -> None:
    paths = cast(dict[str, dict[str, Any]], make_app().openapi()["paths"])
    forget_paths = {path for path in paths if path.startswith("/api/v1/forget")}

    assert forget_paths == {"/api/v1/forget"}
    assert set(paths["/api/v1/forget"].keys()) == {"post"}


def test_forget_request_schema_forbids_unknown_fields() -> None:
    schemas = cast(dict[str, dict[str, Any]], make_app().openapi()["components"]["schemas"])

    assert schemas["ForgetRequest"]["additionalProperties"] is False
    assert set(schemas["ForgetRequest"]["properties"]) == {
        "dataset",
        "source_id",
        "everything",
        "confirm",
        "memory_only",
        "wait",
    }


def test_forget_declares_the_accepted_response() -> None:
    paths = cast(dict[str, dict[str, Any]], make_app().openapi()["paths"])

    assert "202" in paths["/api/v1/forget"]["post"]["responses"]
