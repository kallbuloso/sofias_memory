"""Public schemas for the durable pipeline Runs read API (SM-508)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.schemas.common import JSONValue

RUN_PAGE_DEFAULT_LIMIT = 50
RUN_PAGE_MAX_LIMIT = 100


class RunSummaryResult(BaseModel):
    """Public, list-safe projection of one persisted pipeline run."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    pipeline_type: PipelineType
    dataset_id: UUID | None
    source_id: UUID | None
    status: PipelineRunStatus
    progress: float
    current_step: str | None
    attempt: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None
    metrics: dict[str, JSONValue]


class RunListResult(BaseModel):
    """Paginated run list."""

    model_config = ConfigDict(extra="forbid")

    items: list[RunSummaryResult]
    limit: int
    offset: int
    total: int


class RunStepErrorResult(BaseModel):
    """Structural whitelist for a step's public error (ADR-0009 SS B): only a
    stable ``code`` and a safe ``message`` are ever published, regardless of
    what else the persisted ``PipelineStep.error`` JSONB happens to contain."""

    model_config = ConfigDict(extra="forbid")

    code: str | None
    message: str | None


class RunStepResult(BaseModel):
    """Public projection of one persisted pipeline step."""

    model_config = ConfigDict(extra="forbid")

    step_id: UUID
    name: str
    ordinal: int
    status: PipelineStepStatus
    attempt: int
    metrics: dict[str, JSONValue]
    error: RunStepErrorResult | None
    started_at: datetime | None
    finished_at: datetime | None


class RunDetailResult(RunSummaryResult):
    """Run projection plus its durably persisted step plan."""

    steps: list[RunStepResult]
