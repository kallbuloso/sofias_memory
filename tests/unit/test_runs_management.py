"""Unit coverage for the SM-508 Runs read API: schemas, service, routes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep
from sofias_memory.schemas.runs import (
    RunDetailResult,
    RunListResult,
    RunStepErrorResult,
    RunStepResult,
    RunSummaryResult,
)
from sofias_memory.services.runs import (
    RunService,
    RunUnitOfWork,
    UnitOfWorkFactory,
    run_step_error_result,
    run_summary_result,
)
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url=DATABASE_URL,
        neo4j_password=NEO4J_PASSWORD,
        llm_api_key=LLM_API_KEY,
        app_env="test",
        data_directory=tmp_path,
    )


def pipeline_run(
    *,
    run_id: UUID | None = None,
    pipeline_type: PipelineType = PipelineType.REMEMBER,
    dataset_id: UUID | None = None,
    source_id: UUID | None = None,
    session_id: UUID | None = None,
    status: PipelineRunStatus = PipelineRunStatus.QUEUED,
    progress: float = 0.0,
    current_step: str | None = None,
    attempt: int = 0,
    created_at: datetime = CREATED_AT,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    metrics: dict[str, object] | None = None,
) -> PipelineRun:
    return PipelineRun(
        id=run_id or uuid4(),
        pipeline_type=pipeline_type,
        dataset_id=dataset_id,
        source_id=source_id,
        session_id=session_id,
        status=status,
        idempotency_key=None,
        payload_hash="a" * 64,
        input={"secret": "never-public"},
        progress=progress,
        current_step=current_step,
        attempt=attempt,
        worker_id="worker-internal-1",
        heartbeat_at=created_at,
        config_fingerprint="b" * 64,
        error_code=error_code,
        error_message=error_message,
        metrics=metrics or {},
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        next_attempt_at=None,
        retry_of_run_id=None,
    )


def pipeline_step(
    *,
    step_id: UUID | None = None,
    run_id: UUID,
    name: str,
    ordinal: int,
    status: PipelineStepStatus = PipelineStepStatus.QUEUED,
    attempt: int = 0,
    metrics: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> PipelineStep:
    return PipelineStep(
        id=step_id or uuid4(),
        run_id=run_id,
        name=name,
        ordinal=ordinal,
        status=status,
        attempt=attempt,
        input_hash="c" * 64,
        output={"raw": "never-public"},
        metrics=metrics or {},
        error=error,
        started_at=started_at,
        finished_at=finished_at,
    )


class FakeStore:
    def __init__(self) -> None:
        self.runs: list[PipelineRun] = []
        self.steps: list[PipelineStep] = []


class FakePipelineRunRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, run_id: UUID) -> PipelineRun | None:
        return next((run for run in self._store.runs if run.id == run_id), None)

    def _filtered(
        self,
        *,
        statuses: list[PipelineRunStatus] | None,
        dataset_id: UUID | None,
        pipeline_type: PipelineType | None,
        session_id: UUID | None = None,
    ) -> list[PipelineRun]:
        runs = self._store.runs
        if statuses:
            runs = [run for run in runs if run.status in statuses]
        if dataset_id is not None:
            runs = [run for run in runs if run.dataset_id == dataset_id]
        if pipeline_type is not None:
            runs = [run for run in runs if run.pipeline_type == pipeline_type]
        if session_id is not None:
            runs = [run for run in runs if run.session_id == session_id]
        return sorted(runs, key=lambda run: (run.created_at, run.id), reverse=True)

    async def list_page(
        self,
        *,
        statuses: list[PipelineRunStatus] | None = None,
        dataset_id: UUID | None = None,
        pipeline_type: PipelineType | None = None,
        session_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PipelineRun]:
        ordered = self._filtered(
            statuses=statuses,
            dataset_id=dataset_id,
            pipeline_type=pipeline_type,
            session_id=session_id,
        )
        return ordered[offset : offset + limit]

    async def count_page(
        self,
        *,
        statuses: list[PipelineRunStatus] | None = None,
        dataset_id: UUID | None = None,
        pipeline_type: PipelineType | None = None,
        session_id: UUID | None = None,
    ) -> int:
        return len(
            self._filtered(
                statuses=statuses,
                dataset_id=dataset_id,
                pipeline_type=pipeline_type,
                session_id=session_id,
            )
        )


class FakePipelineStepRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_for_run(self, run_id: UUID) -> list[PipelineStep]:
        scoped = [step for step in self._store.steps if step.run_id == run_id]
        return sorted(scoped, key=lambda step: (step.ordinal, step.id))


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.pipeline_runs = FakePipelineRunRepository(store)
        self.pipeline_steps = FakePipelineStepRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


def service_for(store: FakeStore) -> RunService:
    def create_uow() -> RunUnitOfWork:
        return cast(RunUnitOfWork, FakeUnitOfWork(store))

    return RunService(unit_of_work_factory=cast(UnitOfWorkFactory, create_uow))


@pytest.mark.asyncio
async def test_list_runs_empty() -> None:
    result = await service_for(FakeStore()).list_runs(limit=50, offset=0)

    assert result == RunListResult(items=[], limit=50, offset=0, total=0)


@pytest.mark.asyncio
async def test_list_runs_paginates_and_reports_total() -> None:
    store = FakeStore()
    older = pipeline_run(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = pipeline_run(created_at=datetime(2026, 1, 2, tzinfo=UTC))
    store.runs.extend([older, newer])

    page = await service_for(store).list_runs(limit=1, offset=0)

    assert [item.run_id for item in page.items] == [newer.id]
    assert page.total == 2
    assert page.limit == 1
    assert page.offset == 0


@pytest.mark.asyncio
async def test_list_runs_orders_by_created_at_desc_then_id_desc() -> None:
    store = FakeStore()
    tied_time = datetime(2026, 1, 1, tzinfo=UTC)
    low_id = pipeline_run(run_id=UUID("10000000-0000-0000-0000-000000000001"), created_at=tied_time)
    high_id = pipeline_run(
        run_id=UUID("10000000-0000-0000-0000-000000000002"), created_at=tied_time
    )
    store.runs.extend([low_id, high_id])

    page = await service_for(store).list_runs(limit=50, offset=0)

    assert [item.run_id for item in page.items] == [high_id.id, low_id.id]


@pytest.mark.asyncio
async def test_list_runs_filters_by_single_status() -> None:
    store = FakeStore()
    queued = pipeline_run(status=PipelineRunStatus.QUEUED)
    running = pipeline_run(status=PipelineRunStatus.RUNNING)
    store.runs.extend([queued, running])

    page = await service_for(store).list_runs(
        limit=50, offset=0, statuses=[PipelineRunStatus.RUNNING]
    )

    assert [item.run_id for item in page.items] == [running.id]
    assert page.total == 1


@pytest.mark.asyncio
async def test_list_runs_filters_by_multiple_statuses() -> None:
    store = FakeStore()
    queued = pipeline_run(status=PipelineRunStatus.QUEUED)
    running = pipeline_run(status=PipelineRunStatus.RUNNING)
    succeeded = pipeline_run(status=PipelineRunStatus.SUCCEEDED)
    store.runs.extend([queued, running, succeeded])

    page = await service_for(store).list_runs(
        limit=50,
        offset=0,
        statuses=[PipelineRunStatus.QUEUED, PipelineRunStatus.RUNNING],
    )

    assert {item.run_id for item in page.items} == {queued.id, running.id}


@pytest.mark.asyncio
async def test_list_runs_filters_by_pipeline_type() -> None:
    store = FakeStore()
    remember = pipeline_run(pipeline_type=PipelineType.REMEMBER)
    cognify = pipeline_run(pipeline_type=PipelineType.COGNIFY)
    store.runs.extend([remember, cognify])

    page = await service_for(store).list_runs(
        limit=50, offset=0, pipeline_type=PipelineType.COGNIFY
    )

    assert [item.run_id for item in page.items] == [cognify.id]


@pytest.mark.asyncio
async def test_list_runs_filters_by_dataset_id() -> None:
    store = FakeStore()
    dataset_id = uuid4()
    matching = pipeline_run(dataset_id=dataset_id)
    other = pipeline_run(dataset_id=uuid4())
    store.runs.extend([matching, other])

    page = await service_for(store).list_runs(limit=50, offset=0, dataset_id=dataset_id)

    assert [item.run_id for item in page.items] == [matching.id]


@pytest.mark.asyncio
async def test_list_runs_filters_by_session_uuid() -> None:
    store = FakeStore()
    session_id = uuid4()
    matching = pipeline_run(session_id=session_id)
    other = pipeline_run(session_id=uuid4())
    unassociated = pipeline_run(session_id=None)
    store.runs.extend([matching, other, unassociated])

    page = await service_for(store).list_runs(limit=50, offset=0, session_id=session_id)

    assert [item.run_id for item in page.items] == [matching.id]
    assert page.items[0].session_uuid == session_id
    assert page.total == 1


@pytest.mark.asyncio
async def test_list_runs_session_uuid_valid_but_unused_returns_empty_not_404() -> None:
    store = FakeStore()
    store.runs.append(pipeline_run())

    page = await service_for(store).list_runs(limit=50, offset=0, session_id=uuid4())

    assert page.items == []
    assert page.total == 0


@pytest.mark.asyncio
async def test_list_runs_combines_filters_cumulatively() -> None:
    store = FakeStore()
    dataset_id = uuid4()
    match = pipeline_run(
        dataset_id=dataset_id,
        pipeline_type=PipelineType.COGNIFY,
        status=PipelineRunStatus.FAILED,
    )
    wrong_status = pipeline_run(
        dataset_id=dataset_id,
        pipeline_type=PipelineType.COGNIFY,
        status=PipelineRunStatus.SUCCEEDED,
    )
    wrong_dataset = pipeline_run(
        dataset_id=uuid4(),
        pipeline_type=PipelineType.COGNIFY,
        status=PipelineRunStatus.FAILED,
    )
    store.runs.extend([match, wrong_status, wrong_dataset])

    page = await service_for(store).list_runs(
        limit=50,
        offset=0,
        statuses=[PipelineRunStatus.FAILED],
        dataset_id=dataset_id,
        pipeline_type=PipelineType.COGNIFY,
    )

    assert [item.run_id for item in page.items] == [match.id]


@pytest.mark.asyncio
async def test_get_run_returns_detail_with_steps_in_ordinal_order() -> None:
    store = FakeStore()
    run = pipeline_run(status=PipelineRunStatus.RUNNING)
    store.runs.append(run)
    second = pipeline_step(run_id=run.id, name="embed", ordinal=1)
    first = pipeline_step(run_id=run.id, name="chunk", ordinal=0)
    store.steps.extend([second, first])

    detail = await service_for(store).get_run(run.id)

    assert [step.name for step in detail.steps] == ["chunk", "embed"]


@pytest.mark.asyncio
async def test_get_run_step_error_end_to_end_discards_extra_persisted_keys() -> None:
    store = FakeStore()
    run = pipeline_run(status=PipelineRunStatus.FAILED)
    store.runs.append(run)
    store.steps.append(
        pipeline_step(
            run_id=run.id,
            name="chunk",
            ordinal=0,
            status=PipelineStepStatus.FAILED,
            error={
                "code": "PROVIDER_ERROR",
                "message": "Safe message.",
                "traceback": "must-not-leak",
                "provider_response": {"secret": "must-not-leak"},
                "debug": "must-not-leak",
            },
        )
    )

    detail = await service_for(store).get_run(run.id)

    dumped_error = detail.steps[0].model_dump()["error"]
    assert dumped_error == {"code": "PROVIDER_ERROR", "message": "Safe message."}


@pytest.mark.asyncio
async def test_get_run_legacy_b4_run_has_zero_steps() -> None:
    store = FakeStore()
    run = pipeline_run(status=PipelineRunStatus.RUNNING, attempt=1)
    store.runs.append(run)

    detail = await service_for(store).get_run(run.id)

    assert detail.steps == []


@pytest.mark.asyncio
async def test_get_run_missing_raises_404() -> None:
    with pytest.raises(SofiasMemoryError) as error:
        await service_for(FakeStore()).get_run(uuid4())

    assert error.value.status_code == 404


@pytest.mark.parametrize(
    "status",
    [
        PipelineRunStatus.QUEUED,
        PipelineRunStatus.RUNNING,
        PipelineRunStatus.SUCCEEDED,
        PipelineRunStatus.FAILED,
        PipelineRunStatus.CANCELLING,
        PipelineRunStatus.CANCELLED,
    ],
)
@pytest.mark.asyncio
async def test_get_run_round_trips_every_lifecycle_status(status: PipelineRunStatus) -> None:
    store = FakeStore()
    run = pipeline_run(status=status)
    store.runs.append(run)

    detail = await service_for(store).get_run(run.id)

    assert detail.status == status


@pytest.mark.asyncio
async def test_get_run_exposes_error_code_and_message_for_failed_run() -> None:
    store = FakeStore()
    run = pipeline_run(
        status=PipelineRunStatus.FAILED,
        error_code="STEP_INPUT_DRIFT",
        error_message="Registered step plan no longer matches the registry.",
    )
    store.runs.append(run)

    detail = await service_for(store).get_run(run.id)

    assert detail.error_code == "STEP_INPUT_DRIFT"
    assert detail.error_message == "Registered step plan no longer matches the registry."


@pytest.mark.asyncio
async def test_get_run_exposes_metrics() -> None:
    store = FakeStore()
    run = pipeline_run(metrics={"chunks": 8, "entities": 17})
    store.runs.append(run)

    detail = await service_for(store).get_run(run.id)

    assert detail.metrics == {"chunks": 8, "entities": 17}


@pytest.mark.asyncio
async def test_get_run_timestamps_are_nullable_for_a_queued_run() -> None:
    store = FakeStore()
    run = pipeline_run(status=PipelineRunStatus.QUEUED)
    store.runs.append(run)

    detail = await service_for(store).get_run(run.id)

    assert detail.started_at is None
    assert detail.finished_at is None


@pytest.mark.asyncio
async def test_get_run_timestamps_are_populated_for_a_finished_run() -> None:
    store = FakeStore()
    started = datetime(2026, 1, 1, 1, tzinfo=UTC)
    finished = datetime(2026, 1, 1, 2, tzinfo=UTC)
    run = pipeline_run(status=PipelineRunStatus.SUCCEEDED, started_at=started, finished_at=finished)
    store.runs.append(run)

    detail = await service_for(store).get_run(run.id)

    assert detail.started_at == started
    assert detail.finished_at == finished


def test_run_summary_result_exposes_session_uuid_from_the_persisted_fk() -> None:
    session_id = uuid4()
    run = pipeline_run(session_id=session_id)

    assert run_summary_result(run).session_uuid == session_id


def test_run_summary_result_legacy_run_with_textual_input_session_id_is_null() -> None:
    """SM-605 SS 35/59: a pre-v0.3.0 historical run may carry a legacy
    textual `session_id` inside `input`, but `PipelineRun.session_id` (the
    only authoritative FK) is NULL for it -- no inference/backfill ever
    reinterprets the legacy payload as a first-class association."""

    run = pipeline_run(session_id=None)
    run.input = {"session_id": "legacy-text-session", "secret": "never-public"}

    assert run_summary_result(run).session_uuid is None


def test_run_summary_public_projection_does_not_expose_sensitive_run_fields() -> None:
    run = pipeline_run()
    result = RunSummaryResult(
        run_id=run.id,
        pipeline_type=run.pipeline_type,
        dataset_id=run.dataset_id,
        source_id=run.source_id,
        status=run.status,
        progress=run.progress,
        current_step=run.current_step,
        attempt=run.attempt,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_code=run.error_code,
        error_message=run.error_message,
        metrics={},
    )
    dumped = result.model_dump()

    sensitive_fields = {
        "input",
        "payload_hash",
        "idempotency_key",
        "config_fingerprint",
        "worker_id",
        "heartbeat_at",
        "next_attempt_at",
        "retry_of_run_id",
    }
    assert sensitive_fields.isdisjoint(dumped)


def test_run_step_public_projection_does_not_expose_sensitive_step_fields() -> None:
    result = RunStepResult(
        step_id=uuid4(),
        name="chunk",
        ordinal=0,
        status=PipelineStepStatus.SUCCEEDED,
        attempt=1,
        metrics={},
        error=None,
        started_at=None,
        finished_at=None,
    )
    dumped = result.model_dump()

    sensitive_fields = {"input_hash", "output", "run_id"}
    assert sensitive_fields.isdisjoint(dumped)


def test_run_detail_result_extends_summary_with_steps() -> None:
    assert issubclass(RunDetailResult, RunSummaryResult)
    assert "steps" in RunDetailResult.model_fields


def test_run_step_error_result_is_null_when_no_error_is_persisted() -> None:
    assert run_step_error_result(None) is None


def test_run_step_error_result_publishes_valid_code_and_message() -> None:
    result = run_step_error_result({"code": "PROVIDER_ERROR", "message": "Safe message."})

    assert result == RunStepErrorResult(code="PROVIDER_ERROR", message="Safe message.")


def test_run_step_error_result_discards_every_extra_key() -> None:
    persisted_error = {
        "code": "PROVIDER_ERROR",
        "message": "Safe message.",
        "traceback": "must-not-leak",
        "provider_response": {"secret": "must-not-leak"},
        "debug": "must-not-leak",
    }

    result = run_step_error_result(persisted_error)

    assert result is not None
    dumped = result.model_dump()
    assert dumped == {"code": "PROVIDER_ERROR", "message": "Safe message."}
    assert "traceback" not in dumped
    assert "provider_response" not in dumped
    assert "debug" not in dumped


def test_run_step_error_result_projects_non_string_code_and_message_as_none() -> None:
    result = run_step_error_result(
        {"code": {"nested": "must-not-leak"}, "message": ["must-not-leak"]}
    )

    assert result == RunStepErrorResult(code=None, message=None)


def test_run_step_error_result_projects_missing_keys_as_none() -> None:
    result = run_step_error_result({})

    assert result == RunStepErrorResult(code=None, message=None)


@pytest.mark.asyncio
async def test_runs_routes_return_envelope_require_api_key_and_reject_bad_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()

    class FakeRunService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def list_runs(
            self,
            *,
            limit: int,
            offset: int,
            statuses: list[PipelineRunStatus] | None = None,
            dataset_id: UUID | None = None,
            pipeline_type: PipelineType | None = None,
            session_id: UUID | None = None,
        ) -> RunListResult:
            assert (limit, offset) == (2, 1)
            return RunListResult(
                items=[run_summary(run_id)],
                limit=limit,
                offset=offset,
                total=3,
            )

        async def get_run(self, run_id: UUID) -> RunDetailResult:
            return run_detail(run_id)

    monkeypatch.setattr("sofias_memory.api.routes.runs.RunService", FakeRunService)
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_key = await client.get("/api/v1/runs")
        listed = await client.get(
            "/api/v1/runs?limit=2&offset=1",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        fetched = await client.get(
            f"/api/v1/runs/{run_id}",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        invalid_uuid = await client.get(
            "/api/v1/runs/not-a-uuid",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        limit_zero = await client.get(
            "/api/v1/runs?limit=0",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        limit_too_large = await client.get(
            "/api/v1/runs?limit=101",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        offset_negative = await client.get(
            "/api/v1/runs?offset=-1",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        invalid_status = await client.get(
            "/api/v1/runs?status=not-a-status",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        invalid_type = await client.get(
            "/api/v1/runs?type=not-a-type",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert missing_key.status_code == 401
    assert listed.status_code == 200
    assert listed.json()["data"]["limit"] == 2
    assert listed.json()["data"]["offset"] == 1
    assert listed.json()["data"]["total"] == 3
    assert "request_id" in listed.json()["meta"]
    assert fetched.status_code == 200
    assert fetched.json()["data"]["run_id"] == str(run_id)
    assert invalid_uuid.status_code == 422
    assert limit_zero.status_code == 422
    assert limit_too_large.status_code == 422
    assert offset_negative.status_code == 422
    assert invalid_status.status_code == 422
    assert invalid_type.status_code == 422


@pytest.mark.asyncio
async def test_runs_route_session_uuid_filter_composes_with_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    expected_session_id = uuid4()
    received: dict[str, object] = {}

    class FakeRunService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def list_runs(
            self,
            *,
            limit: int,
            offset: int,
            statuses: list[PipelineRunStatus] | None = None,
            dataset_id: UUID | None = None,
            pipeline_type: PipelineType | None = None,
            session_id: UUID | None = None,
        ) -> RunListResult:
            received["session_id"] = session_id
            received["statuses"] = statuses
            return RunListResult(
                items=[run_summary(run_id)],
                limit=limit,
                offset=offset,
                total=1,
            )

    monkeypatch.setattr("sofias_memory.api.routes.runs.RunService", FakeRunService)
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/runs?session_uuid={expected_session_id}&status=queued",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        malformed = await client.get(
            "/api/v1/runs?session_uuid=not-a-uuid",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert response.status_code == 200
    assert received["session_id"] == expected_session_id
    assert received["statuses"] == [PipelineRunStatus.QUEUED]
    assert malformed.status_code == 422


@pytest.mark.asyncio
async def test_get_run_route_returns_404_for_unknown_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sofias_memory.api.errors import SofiasMemoryError
    from sofias_memory.schemas.common import ErrorCode

    class NotFoundRunService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def get_run(self, run_id: UUID) -> RunDetailResult:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=404,
                message="Pipeline run does not exist.",
                details={"run_id": str(run_id)},
            )

    monkeypatch.setattr("sofias_memory.api.routes.runs.RunService", NotFoundRunService)
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/runs/{uuid4()}",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def run_summary(run_id: UUID) -> RunSummaryResult:
    return RunSummaryResult(
        run_id=run_id,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=None,
        source_id=None,
        status=PipelineRunStatus.QUEUED,
        progress=0.0,
        current_step=None,
        attempt=0,
        created_at=CREATED_AT,
        started_at=None,
        finished_at=None,
        error_code=None,
        error_message=None,
        metrics={},
    )


def run_detail(run_id: UUID) -> RunDetailResult:
    return RunDetailResult(**run_summary(run_id).model_dump(), steps=[])
