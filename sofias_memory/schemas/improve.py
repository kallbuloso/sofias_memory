"""Public schemas for explicit memory improvement."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

ImproveStage = Literal[
    "feedback_weights",
    "entity_deduplication",
    "relation_embeddings",
    "summaries",
    "graph_reconciliation",
]


class ImproveRequest(BaseModel):
    """Explicit synchronous improve request."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1)
    stages: list[ImproveStage] | None = Field(default=None, min_length=1)
    wait: bool = Field(default=False)

    @field_validator("dataset")
    @classmethod
    def strip_dataset(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dataset must not be empty")
        return stripped


class ImproveResult(BaseModel):
    """Operational counts returned by Improve v1."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: Literal["succeeded"]
    dataset_id: UUID
    generation: int
    stages: list[str]
    feedback_processed: int
    feedback_applied: int
    feedback_skipped: int
    entities_updated: int
    relations_updated: int
    relations_embedded: int
    entities_embedded: int
    entity_duplicate_candidates: int
    graph_events_enqueued: int
    graph_events_processed: int
