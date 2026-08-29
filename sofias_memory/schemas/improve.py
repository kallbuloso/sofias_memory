"""Public schemas for explicit memory improvement."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import PipelineRunStatus

ImproveStage = Literal[
    "feedback_weights",
    "entity_deduplication",
    "relation_embeddings",
    "summaries",
    "graph_reconciliation",
]


class ImproveRequest(BaseModel):
    """Durable improve request."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1, description="Dataset slug to improve.")
    stages: list[ImproveStage] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Which maintenance stages to run: feedback_weights, "
            "entity_deduplication, relation_embeddings, summaries, "
            "graph_reconciliation. Omit to run all stages."
        ),
    )
    wait: bool = Field(
        default=False,
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


class ImproveResult(BaseModel):
    """Durable state of one Improve run, as observed when responding.

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
    dataset_id: UUID | None = Field(default=None, description="Dataset this run improved.")
    generation: int | None = Field(
        default=None, description="Dataset generation this run acted on."
    )
    stages: list[str] | None = Field(default=None, description="Stages actually executed.")
    feedback_processed: int | None = None
    feedback_applied: int | None = None
    feedback_skipped: int | None = None
    entities_updated: int | None = None
    relations_updated: int | None = None
    relations_embedded: int | None = None
    entities_embedded: int | None = None
    entity_duplicate_candidates: int | None = None
    entities_merged: int | None = None
    entity_mentions_reassigned: int | None = None
    relations_rewired: int | None = None
    relations_deactivated: int | None = None
    relation_evidence_copied: int | None = None
    document_summaries_rebuilt: int | None = None
    dataset_summaries_rebuilt: int | None = None
    summaries_deactivated: int | None = None
    graph_relations_deactivated: int | None = None
    graph_entities_importance_updated: int | None = None
    graph_relations_importance_updated: int | None = None
    graph_entities_missing: int | None = None
    graph_entities_extra: int | None = None
    graph_chunks_missing: int | None = None
    graph_chunks_extra: int | None = None
    graph_entity_mentions_missing: int | None = None
    graph_entity_mentions_extra: int | None = None
    graph_relations_missing: int | None = None
    graph_relations_extra: int | None = None
    graph_next_missing: int | None = None
    graph_next_extra: int | None = None
    graph_rebuilt: bool | None = None
    graph_events_enqueued: int | None = None
    graph_events_processed: int | None = None
