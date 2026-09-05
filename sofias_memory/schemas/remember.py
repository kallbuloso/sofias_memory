"""Public schemas for remember ingest (SM-513)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import PipelineRunStatus, normalize_session_id
from sofias_memory.schemas.common import JSONValue

SESSION_ID_DESCRIPTION = (
    "Optional caller-supplied Session external key. When present, the "
    "Session is resolved or lazily created (rejected if archived) and the "
    "resulting PipelineRun is associated with it -- this is a first-class "
    "association, not mere correlation metadata."
)


class RememberTextRequest(BaseModel):
    """Direct text remember request."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(
        default="main",
        min_length=1,
        description="Target dataset slug. Created automatically only if it is 'main'.",
    )
    content: str = Field(
        min_length=1,
        description="Raw text content to store.",
        examples=["Project Aurora uses component Nimbus."],
    )
    name: str | None = Field(
        default=None, min_length=1, description="Optional human-readable name for this source."
    )
    metadata: dict[str, JSONValue] = Field(
        default_factory=dict,
        description="Arbitrary caller-supplied metadata stored with the source.",
    )
    session_id: str | None = Field(default=None, description=SESSION_ID_DESCRIPTION)
    mode: str = Field(
        default="ingest",
        description=(
            "'ingest' stores the content as-is for a later Cognify run. 'full' also "
            "chunks, embeds, and extracts entities/relations immediately."
        ),
    )
    wait: bool = Field(
        default=True,
        description=(
            "If true, wait for this run to reach a terminal state (up to the "
            "configured timeout) before responding. If false, return as soon as the "
            "run is durably queued."
        ),
    )
    force: bool = Field(
        default=False,
        description="Re-process even if identical content was already remembered for this dataset.",
    )

    @field_validator("dataset", "name")
    @classmethod
    def strip_non_content_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be empty")
        return stripped

    @field_validator("session_id")
    @classmethod
    def normalize_session_id_field(cls, value: str | None) -> str | None:
        return normalize_session_id(value)


class RememberUrlRequest(BaseModel):
    """Single HTTPS URL remember request."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(
        default="main",
        min_length=1,
        description="Target dataset slug. Created automatically only if it is 'main'.",
    )
    url: str = Field(
        min_length=1,
        max_length=2048,
        description=(
            "A single HTTPS URL to fetch and remember. Loopback, link-local, "
            "private-network, and cloud-metadata addresses are rejected."
        ),
    )
    metadata: dict[str, JSONValue] = Field(
        default_factory=dict,
        description="Arbitrary caller-supplied metadata stored with the source.",
    )
    session_id: str | None = Field(default=None, description=SESSION_ID_DESCRIPTION)
    mode: str = Field(
        default="ingest",
        description=(
            "'ingest' stores the fetched content as-is for a later Cognify run. "
            "'full' also chunks, embeds, and extracts entities/relations immediately."
        ),
    )
    wait: bool = Field(
        default=True,
        description=(
            "If true, wait for this run to reach a terminal state (up to the "
            "configured timeout) before responding. If false, return as soon as the "
            "run is durably queued."
        ),
    )
    force: bool = Field(
        default=False,
        description="Re-process even if identical content was already remembered for this dataset.",
    )

    @field_validator("dataset", "url")
    @classmethod
    def strip_url_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be empty")
        return stripped

    @field_validator("session_id")
    @classmethod
    def normalize_session_id_field(cls, value: str | None) -> str | None:
        return normalize_session_id(value)


class RememberTextResult(BaseModel):
    """Result of a remember run (text/file/url; ``mode=ingest``/``full``).

    Business fields are optional/``None`` when the run has not yet reached
    ``succeeded`` -- only ``run_id``/``status`` are guaranteed for
    ``queued``/``running``/``cancelling``/``cancelled``."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID = Field(
        description="Durable PipelineRun identifier. Poll GET /api/v1/runs/{run_id}."
    )
    status: PipelineRunStatus = Field(description="Current status of the underlying PipelineRun.")
    dataset_id: UUID | None = Field(
        default=None, description="Target dataset id. Present once the run has resolved it."
    )
    source_id: UUID | None = Field(
        default=None, description="Persisted source id. Present only once the run succeeds."
    )
    document_id: UUID | None = Field(
        default=None,
        description="Normalized document id (mode=full only). Present only once the run succeeds.",
    )
    content_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        description="SHA-256 of the normalized content, used for de-duplication.",
    )
    chunks: int | None = Field(
        default=None, description="Number of chunks created (mode=full only)."
    )
    entities: int | None = Field(
        default=None, description="Number of entities extracted (mode=full only)."
    )
    relations: int | None = Field(
        default=None, description="Number of relations extracted (mode=full only)."
    )
    deduplicated: bool | None = Field(
        default=None,
        description="True if this content was already known and no new processing was needed.",
    )
    session_uuid: UUID | None = Field(
        default=None,
        description=(
            "The Session this run is first-class associated with, if any. Null "
            "when no session_id was supplied, or for a pre-v0.3.0 historical "
            "run even if its legacy payload contains a textual session_id."
        ),
    )


__all__ = [
    "RememberTextRequest",
    "RememberTextResult",
    "RememberUrlRequest",
]
