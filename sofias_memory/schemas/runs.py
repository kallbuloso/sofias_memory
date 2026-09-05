"""Public schemas for the durable pipeline Runs read API (SM-508)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sofias_memory.domain import PipelineRunStatus, PipelineStepStatus, PipelineType
from sofias_memory.schemas.common import JSONValue

RUN_PAGE_DEFAULT_LIMIT = 50
RUN_PAGE_MAX_LIMIT = 100


class RunSummaryResult(BaseModel):
    """Public, list-safe projection of one persisted pipeline run."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID = Field(description="Durable, unique identifier for this run.")
    pipeline_type: PipelineType = Field(description="Which pipeline this run executes.")
    dataset_id: UUID | None = Field(description="Dataset this run targets, if any.")
    source_id: UUID | None = Field(description="Source this run targets, if any.")
    session_uuid: UUID | None = Field(
        default=None,
        description=(
            "The Session this run is first-class associated with, if any. A "
            "manual retry preserves the original run's association. A "
            "pre-v0.3.0 historical run is always null here, even if its "
            "legacy input payload contains a textual session_id."
        ),
    )
    status: PipelineRunStatus = Field(
        description="queued, running, succeeded, failed, cancelling, or cancelled."
    )
    progress: float = Field(description="Fraction of planned steps completed, from 0.0 to 1.0.")
    current_step: str | None = Field(description="Name of the step currently executing, if any.")
    attempt: int = Field(description="1 for the original run; incremented for each manual retry.")
    created_at: datetime = Field(description="When this run was durably created.")
    started_at: datetime | None = Field(description="When execution began, if it has.")
    finished_at: datetime | None = Field(
        description="When this run reached a terminal state, if it has."
    )
    error_code: str | None = Field(description="Stable error code if the run failed.")
    error_message: str | None = Field(description="Safe, public error message if the run failed.")
    metrics: dict[str, JSONValue] = Field(description="Pipeline-specific counters/results.")


class RunListResult(BaseModel):
    """Paginated run list."""

    model_config = ConfigDict(extra="forbid")

    items: list[RunSummaryResult]
    limit: int
    offset: int
    total: int


class RunStepErrorResult(BaseModel):
    """A step's public error: only a stable ``code`` and a safe ``message``
    are ever published, regardless of what else the persisted step error
    happens to contain internally."""

    model_config = ConfigDict(extra="forbid")

    code: str | None
    message: str | None


class RunStepResult(BaseModel):
    """Public projection of one persisted pipeline step."""

    model_config = ConfigDict(extra="forbid")

    step_id: UUID = Field(description="Durable, unique identifier for this step.")
    name: str = Field(description="Step name within the pipeline's fixed step plan.")
    ordinal: int = Field(description="Position of this step in the plan, starting at 0.")
    status: PipelineStepStatus = Field(description="This step's own lifecycle status.")
    attempt: int = Field(description="1 for the first attempt; incremented on step-level retry.")
    metrics: dict[str, JSONValue] = Field(description="Step-specific counters/results.")
    error: RunStepErrorResult | None = Field(description="Populated only if this step failed.")
    started_at: datetime | None = Field(description="When this step began executing, if it has.")
    finished_at: datetime | None = Field(
        description="When this step reached a terminal state, if it has."
    )


class RunDetailResult(RunSummaryResult):
    """Run projection plus its durably persisted step plan."""

    steps: list[RunStepResult]
