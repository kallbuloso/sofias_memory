"""Public schemas for explicit memory deletion."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import SourceStatus


class ForgetRequest(BaseModel):
    """Explicit synchronous source forget request."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1)
    source_id: UUID
    memory_only: bool = Field(default=False)
    wait: bool = Field(default=True)

    @field_validator("dataset")
    @classmethod
    def strip_dataset(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dataset must not be empty")
        return stripped


class ForgetResult(BaseModel):
    """Operational counts returned by synchronous source forget."""

    model_config = ConfigDict(extra="forbid")

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
