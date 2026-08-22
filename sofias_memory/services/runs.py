"""Read-only service for the durable pipeline Runs API (SM-508).

Observes PostgreSQL-authoritative ``pipeline_runs``/``pipeline_steps`` state
only: no commits, no row locking, no pipeline execution, no registry-based
step reconstruction, no Neo4j. See ADR-0009 SS B/SS 508 and AGENTS.md SS 17.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.models import PipelineRun, PipelineStep
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode, JSONValue
from sofias_memory.schemas.runs import (
    RunDetailResult,
    RunListResult,
    RunStepErrorResult,
    RunStepResult,
    RunSummaryResult,
)


class PipelineRunRepositoryForRuns(Protocol):
    async def get_by_id(self, run_id: UUID) -> PipelineRun | None: ...

    async def list_page(
        self,
        *,
        statuses: list[PipelineRunStatus] | None = None,
        dataset_id: UUID | None = None,
        pipeline_type: PipelineType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PipelineRun]: ...

    async def count_page(
        self,
        *,
        statuses: list[PipelineRunStatus] | None = None,
        dataset_id: UUID | None = None,
        pipeline_type: PipelineType | None = None,
    ) -> int: ...


class PipelineStepRepositoryForRuns(Protocol):
    async def list_for_run(self, run_id: UUID) -> list[PipelineStep]: ...


class RunUnitOfWork(Protocol):
    pipeline_runs: PipelineRunRepositoryForRuns
    pipeline_steps: PipelineStepRepositoryForRuns

    async def __aenter__(self) -> RunUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


type UnitOfWorkFactory = Callable[[], RunUnitOfWork]


class RunService:
    """Read-only observer over durable pipeline run/step state."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def list_runs(
        self,
        *,
        limit: int,
        offset: int,
        statuses: list[PipelineRunStatus] | None = None,
        dataset_id: UUID | None = None,
        pipeline_type: PipelineType | None = None,
    ) -> RunListResult:
        async with self._unit_of_work_factory() as uow:
            runs = await uow.pipeline_runs.list_page(
                statuses=statuses,
                dataset_id=dataset_id,
                pipeline_type=pipeline_type,
                limit=limit,
                offset=offset,
            )
            total = await uow.pipeline_runs.count_page(
                statuses=statuses,
                dataset_id=dataset_id,
                pipeline_type=pipeline_type,
            )
            return RunListResult(
                items=[run_summary_result(run) for run in runs],
                limit=limit,
                offset=offset,
                total=total,
            )

    async def get_run(self, run_id: UUID) -> RunDetailResult:
        async with self._unit_of_work_factory() as uow:
            run = await uow.pipeline_runs.get_by_id(run_id)
            if run is None:
                raise run_not_found_error(run_id)
            steps = await uow.pipeline_steps.list_for_run(run_id)
            return run_detail_result(run, steps)


def run_summary_result(run: PipelineRun) -> RunSummaryResult:
    return RunSummaryResult(
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
        metrics=cast(dict[str, JSONValue], run.metrics),
    )


def run_step_result(step: PipelineStep) -> RunStepResult:
    return RunStepResult(
        step_id=step.id,
        name=step.name,
        ordinal=step.ordinal,
        status=step.status,
        attempt=step.attempt,
        metrics=cast(dict[str, JSONValue], step.metrics),
        error=run_step_error_result(step.error),
        started_at=step.started_at,
        finished_at=step.finished_at,
    )


def run_step_error_result(error: dict[str, object] | None) -> RunStepErrorResult | None:
    """Structural whitelist projection (ADR-0009 SS B, SM-508 finding fix):
    reads only ``code``/``message`` out of the persisted JSONB and discards
    every other key, so a future or historical extra key (traceback, raw
    provider payload, debug detail) can never reach the public API. A
    non-string value for either key is projected as ``None`` rather than
    leaked verbatim."""

    if error is None:
        return None
    code = error.get("code")
    message = error.get("message")
    return RunStepErrorResult(
        code=code if isinstance(code, str) else None,
        message=message if isinstance(message, str) else None,
    )


def run_detail_result(run: PipelineRun, steps: list[PipelineStep]) -> RunDetailResult:
    summary = run_summary_result(run)
    return RunDetailResult(
        **summary.model_dump(),
        steps=[run_step_result(step) for step in steps],
    )


def run_not_found_error(run_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.NOT_FOUND,
        message="Pipeline run does not exist.",
        details={"run_id": str(run_id)},
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> RunUnitOfWork:
        return cast(RunUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
