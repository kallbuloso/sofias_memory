"""Public schemas for explicit memory deletion (SM-512)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import PipelineRunStatus


class ForgetRequest(BaseModel):
    """Forget request for one of three scopes: SOURCE, DATASET, or EVERYTHING.

    Scope is not a request field. It is derived from which fields are
    actually present in the payload (``source_id``, ``everything``, or an
    explicit ``dataset`` alone).
    """

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(
        default="main", min_length=1, description="Dataset slug (SOURCE/DATASET scope)."
    )
    source_id: UUID | None = Field(
        default=None,
        description="Present to scope this Forget to a single source within the dataset.",
    )
    everything: bool = Field(
        default=False,
        description=(
            'Destructive: forget every dataset\'s memory. Requires confirm="DELETE EVERYTHING".'
        ),
    )
    confirm: str | None = Field(
        default=None,
        description='Required, exact confirmation phrase for everything=true: "DELETE EVERYTHING".',
    )
    memory_only: bool = Field(
        default=False,
        description=(
            "Clear derived memory (chunks/entities/relations/summaries/graph) but "
            "keep the original source stored, so it can be re-cognified later."
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


class ForgetSourceResult(BaseModel):
    """Durable state of one SOURCE-scope forget run, as observed when responding.

    Every field is reconstructed from the persisted ``PipelineRun`` -- never
    from in-process memory. Business counters are populated only once the
    run has actually succeeded (``status="succeeded"``).
    """

    model_config = ConfigDict(extra="forbid")

    scope: Literal["source"] = "source"
    run_id: UUID = Field(
        description="Durable PipelineRun identifier. Poll GET /api/v1/runs/{run_id}."
    )
    status: PipelineRunStatus = Field(description="Current status of the underlying PipelineRun.")
    dataset_id: UUID | None = None
    source_id: UUID | None = None
    memory_only: bool | None = None
    source_status: str | None = None
    documents_deactivated: int | None = None
    chunks_deactivated: int | None = None
    summaries_deactivated: int | None = None
    entities_deactivated: int | None = None
    relations_deactivated: int | None = None
    entity_mentions_unprojected: int | None = None
    relation_evidence_unprojected: int | None = None
    graph_events_enqueued: int | None = None
    graph_events_processed: int | None = None
    storage_deleted: bool | None = None


# Preserve the SM-422 name for backward compatibility with existing callers/tests.
ForgetResult = ForgetSourceResult


class ForgetDatasetResult(BaseModel):
    """Durable state of one DATASET-scope forget run."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["dataset"] = "dataset"
    run_id: UUID = Field(
        description="Durable PipelineRun identifier. Poll GET /api/v1/runs/{run_id}."
    )
    status: PipelineRunStatus = Field(description="Current status of the underlying PipelineRun.")
    dataset_id: UUID | None = None
    memory_only: bool | None = None
    sources_affected: int | None = None
    sources_pending: int | None = None
    sources_deleted: int | None = None
    documents_deactivated: int | None = None
    chunks_deactivated: int | None = None
    summaries_deactivated: int | None = None
    entities_deactivated: int | None = None
    relations_deactivated: int | None = None
    entity_mentions_unprojected: int | None = None
    relation_evidence_unprojected: int | None = None
    graph_events_enqueued: int | None = None
    graph_events_processed: int | None = None
    storage_deleted: int | None = None
    storage_already_absent: int | None = None


class ForgetEverythingResult(BaseModel):
    """Durable state of one EVERYTHING-scope forget run."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["everything"] = "everything"
    run_id: UUID = Field(
        description="Durable PipelineRun identifier. Poll GET /api/v1/runs/{run_id}."
    )
    status: PipelineRunStatus = Field(description="Current status of the underlying PipelineRun.")
    datasets_affected: int | None = None
    sources_affected: int | None = None
    sources_pending: int | None = None
    sources_deleted: int | None = None
    documents_deactivated: int | None = None
    chunks_deactivated: int | None = None
    summaries_deactivated: int | None = None
    entities_deactivated: int | None = None
    relations_deactivated: int | None = None
    entity_mentions_unprojected: int | None = None
    relation_evidence_unprojected: int | None = None
    graph_events_enqueued: int | None = None
    graph_events_processed: int | None = None
    storage_deleted: int | None = None
    storage_already_absent: int | None = None


type ForgetResponseData = ForgetSourceResult | ForgetDatasetResult | ForgetEverythingResult
