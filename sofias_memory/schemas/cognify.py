"""Public schemas for cognify."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import PipelineRunStatus


class CognifyRequest(BaseModel):
    """Cognify request for pending, explicit, or rebuilt dataset sources."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1)
    source_ids: list[UUID] | None = Field(default=None)
    rebuild: bool = Field(default=False)
    wait: bool = Field(
        default=True,
        description=(
            "Wait for the durable run to reach a terminal state before responding. "
            "Not part of the work identity: the same request with wait=true and "
            "wait=false under one Idempotency-Key resolves to the same run."
        ),
    )

    @field_validator("dataset")
    @classmethod
    def strip_dataset(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dataset must not be empty")
        return stripped


class CognifyResult(BaseModel):
    """Durable state of one Cognify run, as observed when responding.

    Every field is reconstructed from the persisted ``PipelineRun`` -- never
    from in-process memory. The business counters are populated only once the
    run has actually succeeded (``status="succeeded"``); for an accepted run
    that has not reached a terminal state yet, and for a cancelled one, only
    ``run_id``/``status`` are known and the rest stay ``null``.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: PipelineRunStatus
    dataset_id: UUID | None = None
    generation: int | None = None
    sources_processed: int | None = None
    chunks: int | None = None
    entities: int | None = None
    relations: int | None = None
