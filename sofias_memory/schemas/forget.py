"""Public schemas for explicit memory deletion."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import SourceStatus


class ForgetRequest(BaseModel):
    """Explicit synchronous forget request for one of three scopes.

    Scope is not a request field. It is derived by the service from which
    fields were actually present in the payload (``source_id``, ``everything``,
    or an explicit ``dataset``), so the wire-compatible SOURCE contract from
    SM-422 keeps working unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1)
    source_id: UUID | None = Field(default=None)
    everything: bool = Field(default=False)
    confirm: str | None = Field(default=None)
    memory_only: bool = Field(default=False)
    wait: bool = Field(default=True)

    @field_validator("dataset")
    @classmethod
    def strip_dataset(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dataset must not be empty")
        return stripped


class ForgetSourceResult(BaseModel):
    """Operational counts returned by synchronous source forget."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["source"] = "source"
    run_id: UUID
    status: Literal["succeeded"]
    dataset_id: UUID
    source_id: UUID
    memory_only: bool
    source_status: SourceStatus
    documents_deactivated: int
    chunks_deactivated: int
    summaries_deactivated: int
    entities_deactivated: int
    relations_deactivated: int
    entity_mentions_unprojected: int
    relation_evidence_unprojected: int
    graph_events_enqueued: int
    graph_events_processed: int
    storage_deleted: bool


# Preserve the SM-422 name for backward compatibility with existing callers/tests.
ForgetResult = ForgetSourceResult


class ForgetDatasetResult(BaseModel):
    """Operational counts returned by synchronous dataset-scoped forget."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["dataset"] = "dataset"
    run_id: UUID
    status: Literal["succeeded"]
    dataset_id: UUID
    memory_only: bool
    sources_affected: int
    sources_pending: int
    sources_deleted: int
    documents_deactivated: int
    chunks_deactivated: int
    summaries_deactivated: int
    entities_deactivated: int
    relations_deactivated: int
    entity_mentions_unprojected: int
    relation_evidence_unprojected: int
    graph_events_enqueued: int
    graph_events_processed: int
    storage_deleted: int
    storage_already_absent: int


class ForgetEverythingResult(BaseModel):
    """Operational counts returned by synchronous everything forget."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["everything"] = "everything"
    run_id: UUID
    status: Literal["succeeded"]
    datasets_affected: int
    sources_affected: int
    sources_pending: int
    sources_deleted: int
    documents_deactivated: int
    chunks_deactivated: int
    summaries_deactivated: int
    entities_deactivated: int
    relations_deactivated: int
    entity_mentions_unprojected: int
    relation_evidence_unprojected: int
    graph_events_enqueued: int
    graph_events_processed: int
    storage_deleted: int
    storage_already_absent: int


type ForgetResponseData = ForgetSourceResult | ForgetDatasetResult | ForgetEverythingResult
