"""Route-level contract for POST /api/v1/cognify after the B5 migration (SM-510).

The submission service, waiter and PostgreSQL reads are substituted here so
the HTTP contract (validation, 202 vs 200, idempotency pass-through, worker
gate, failure shape) can be asserted precisely; the durable behavior behind
them is proven against real PostgreSQL in
``tests/integration/test_cognify_async_postgres_integration.py``.
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
from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.cognify import COGNIFY_RESULT_METRIC_KEY
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
RUN_ID = UUID("44444444-4444-4444-4444-444444444444")

COGNIFY_MODULE = "sofias_memory.api.routes.cognify"


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
            pipeline_type=PipelineType.COGNIFY,
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

    monkeypatch.setattr(f"{COGNIFY_MODULE}.PipelineSubmissionService", FakeSubmissionService)
    monkeypatch.setattr(f"{COGNIFY_MODULE}.PipelineRunWaiter", FakeWaiter)
    monkeypatch.setattr(f"{COGNIFY_MODULE}.PostgresUnitOfWork", FakeUnitOfWork)


@dataclass
class FakeDataset:
    id: UUID = DATASET_ID


class FakeSubmissionUnitOfWork:
    @property
    def datasets(self) -> Any:
        class _Datasets:
            async def get_by_slug(self, slug: str) -> FakeDataset | None:
                return FakeDataset() if slug != "missing" else None

        return _Datasets()


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def make_app() -> Any:
    return create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)


def succeeded_run() -> FakeRun:
    return FakeRun(
        metrics={
            COGNIFY_RESULT_METRIC_KEY: {
                "dataset_id": str(DATASET_ID),
                "generation": 2,
                "sources_processed": 3,
                "chunks": 11,
                "entities": 5,
                "relations": 4,
            }
        }
    )


async def post_cognify(
    app: Any,
    body: Mapping[str, Any],
    *,
    idempotency_key: str | None = None,
) -> httpx.Response:
    headers = {API_KEY_HEADER: EXPECTED_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with build_client(app) as client:
        return await client.post("/api/v1/cognify", headers=headers, json=dict(body))


# --- validation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_with_explicit_source_ids_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"rebuild": True, "source_ids": [str(uuid4())]})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST
    # Rejected before anything durable happens.
    assert recorder.submits == []


@pytest.mark.asyncio
async def test_unknown_dataset_is_resolved_by_the_preparation_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"dataset": "missing"})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Dataset does not exist."


# --- wait=false / wait=true ---------------------------------------------------


@pytest.mark.asyncio
async def test_wait_false_returns_202_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"wait": False})

    assert response.status_code == 202
    assert response.json()["data"] == {
        "run_id": str(RUN_ID),
        "status": "queued",
        "dataset_id": None,
        "generation": None,
        "sources_processed": None,
        "chunks": None,
        "entities": None,
        "relations": None,
    }
    assert recorder.waits == []


@pytest.mark.asyncio
async def test_wait_true_returns_the_persisted_result_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"wait": True})

    assert response.status_code == 200
    assert response.json()["data"] == {
        "run_id": str(RUN_ID),
        "status": "succeeded",
        "dataset_id": str(DATASET_ID),
        "generation": 2,
        "sources_processed": 3,
        "chunks": 11,
        "entities": 5,
        "relations": 4,
    }
    assert recorder.waits == [RUN_ID]


@pytest.mark.asyncio
async def test_wait_true_and_wait_false_share_one_submission_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)
    app = make_app()

    await post_cognify(app, {"wait": True})
    await post_cognify(app, {"wait": False})

    assert len(recorder.submits) == 2
    # Identical durable work identity: only the response shape differed.
    assert recorder.submits[0]["work_input"] == recorder.submits[1]["work_input"]
    assert "wait" not in recorder.submits[0]["work_input"]
    assert recorder.submits[0]["pipeline_type"] == PipelineType.COGNIFY
    assert recorder.submits[0]["targets"] == SubmissionTargets(
        dataset_id=DATASET_ID, source_id=None
    )


@pytest.mark.asyncio
async def test_wait_true_times_out_into_202_without_inventing_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(wait_status=PipelineRunStatus.RUNNING, wait_timed_out=True)
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"wait": True})

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "running"
    assert response.json()["data"]["chunks"] is None


@pytest.mark.asyncio
async def test_wait_true_skips_waiting_on_an_already_terminal_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    recorder.outcome = SubmissionOutcome(
        run_id=RUN_ID,
        pipeline_type=PipelineType.COGNIFY,
        dataset_id=DATASET_ID,
        source_id=None,
        status=PipelineRunStatus.SUCCEEDED,
        created=False,
    )
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"wait": True}, idempotency_key="k-1")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "succeeded"
    assert recorder.waits == []


# --- idempotency / worker gate / failure --------------------------------------


@pytest.mark.asyncio
async def test_idempotency_key_header_reaches_the_submission_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)
    app = make_app()

    await post_cognify(app, {"wait": True}, idempotency_key="cognify-1")
    await post_cognify(app, {"wait": False}, idempotency_key="cognify-1")

    assert [item["idempotency_key"] for item in recorder.submits] == ["cognify-1", "cognify-1"]


@pytest.mark.asyncio
async def test_missing_idempotency_key_header_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(run=succeeded_run())
    install_fakes(monkeypatch, recorder)

    await post_cognify(make_app(), {"wait": True})

    assert recorder.submits[0]["idempotency_key"] is None


@pytest.mark.asyncio
async def test_worker_unavailable_is_surfaced_as_503(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(submit_error=worker_disabled_error())
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"wait": True})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.WORKER_DISABLED
    assert recorder.waits == []


@pytest.mark.asyncio
async def test_failed_run_returns_a_safe_error_derived_from_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(
        wait_status=PipelineRunStatus.FAILED,
        run=FakeRun(error_code="COGNIFY_DEPENDENCY_UNAVAILABLE"),
    )
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"wait": True})

    error = response.json()["error"]
    assert response.status_code == 500
    assert error["code"] == ErrorCode.INTERNAL_ERROR
    assert error["message"] == "Cognify run failed."
    assert error["details"] == {
        "run_id": str(RUN_ID),
        "status": "failed",
        "step_error_code": "COGNIFY_DEPENDENCY_UNAVAILABLE",
    }
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_cancelled_run_is_reported_without_business_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(wait_status=PipelineRunStatus.CANCELLED)
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"wait": True})

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "cancelled"
    assert response.json()["data"]["chunks"] is None


@pytest.mark.asyncio
async def test_succeeded_run_without_persisted_result_is_an_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(run=FakeRun(metrics={}))
    install_fakes(monkeypatch, recorder)

    response = await post_cognify(make_app(), {"wait": True})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == ErrorCode.INTERNAL_ERROR


# --- the route never executes B4 Cognify inline -------------------------------


def test_cognify_route_module_no_longer_references_the_b4_orchestration() -> None:
    from sofias_memory.api.routes import cognify as route_module
    from sofias_memory.services import cognify as service_module

    # The removed B4 entry points do not exist anywhere any more, so no route
    # (or anything else) could call them even by accident.
    assert not hasattr(service_module.CognifyService, "cognify")
    assert not hasattr(service_module.CognifyService, "_create_running_run")
    assert not hasattr(service_module.CognifyService, "_mark_run_succeeded")
    assert not hasattr(service_module.CognifyService, "_mark_run_failed")
    # The route builds no provider client and no CognifyService of its own.
    assert not hasattr(route_module, "CognifyService")
    assert not hasattr(route_module, "OpenAIEmbeddingClient")
    assert not hasattr(route_module, "GraphOutboxBatchProcessor")


# --- OpenAPI surface -----------------------------------------------------------


def test_cognify_openapi_surface_is_a_single_post_route() -> None:
    paths = cast(dict[str, dict[str, Any]], make_app().openapi()["paths"])
    cognify_paths = {path for path in paths if path.startswith("/api/v1/cognify")}

    assert cognify_paths == {"/api/v1/cognify"}
    assert set(paths["/api/v1/cognify"].keys()) == {"post"}


def test_cognify_request_schema_forbids_unknown_fields() -> None:
    schemas = cast(dict[str, dict[str, Any]], make_app().openapi()["components"]["schemas"])

    assert schemas["CognifyRequest"]["additionalProperties"] is False
    assert set(schemas["CognifyRequest"]["properties"]) == {
        "dataset",
        "source_ids",
        "rebuild",
        "wait",
    }


def test_cognify_result_schema_exposes_only_safe_public_fields() -> None:
    schemas = cast(dict[str, dict[str, Any]], make_app().openapi()["components"]["schemas"])

    assert set(schemas["CognifyResult"]["properties"]) == {
        "run_id",
        "status",
        "dataset_id",
        "generation",
        "sources_processed",
        "chunks",
        "entities",
        "relations",
    }


def test_cognify_declares_the_accepted_response(monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    paths = cast(dict[str, dict[str, Any]], make_app().openapi()["paths"])

    assert "202" in paths["/api/v1/cognify"]["post"]["responses"]
