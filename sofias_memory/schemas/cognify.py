"""Public schemas for cognify."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import PipelineRunStatus


class CognifyRequest(BaseModel):
    """Cognify request for pending, explicit, or rebuilt dataset sources."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1, description="Dataset slug to process.")
    source_ids: list[UUID] | None = Field(
        default=None,
        description=(
            "Process only these specific source ids. Omit to process all pending "
            "sources in the dataset. Mutually exclusive with rebuild=true."
        ),
    )
    rebuild: bool = Field(
        default=False,
        description=(
            "Reprocess the whole dataset onto a new generation instead of only "
            "pending sources. Always covers the whole dataset; rejects source_ids."
        ),
    )
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

    run_id: UUID = Field(
        description="Durable PipelineRun identifier. Poll GET /api/v1/runs/{run_id}."
    )
    status: PipelineRunStatus = Field(description="Current status of the underlying PipelineRun.")
    dataset_id: UUID | None = Field(default=None, description="Dataset this run processed.")
    generation: int | None = Field(
        default=None,
        description="Dataset generation activated by this run (only set after rebuild=true).",
    )
    sources_processed: int | None = Field(
        default=None, description="Number of sources processed by this run."
    )
    chunks: int | None = Field(default=None, description="Number of chunks created.")
    entities: int | None = Field(default=None, description="Number of entities extracted.")
    relations: int | None = Field(default=None, description="Number of relations extracted.")
