"""Public schemas for remember ingest (SM-513)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sofias_memory.domain import PipelineRunStatus
from sofias_memory.schemas.common import JSONValue


class RememberTextRequest(BaseModel):
    """Direct text remember request."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1)
    content: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1)
    metadata: dict[str, JSONValue] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, min_length=1)
    mode: str = Field(default="ingest")
    wait: bool = Field(default=True)
    force: bool = Field(default=False)

    @field_validator("dataset", "name", "session_id")
    @classmethod
    def strip_non_content_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be empty")
        return stripped


class RememberUrlRequest(BaseModel):
    """Single HTTPS URL remember request."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="main", min_length=1)
    url: str = Field(min_length=1, max_length=2048)
    metadata: dict[str, JSONValue] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, min_length=1)
    mode: str = Field(default="ingest")
    wait: bool = Field(default=True)
    force: bool = Field(default=False)

    @field_validator("dataset", "url", "session_id")
    @classmethod
    def strip_url_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be empty")
        return stripped


class RememberTextResult(BaseModel):
    """Result of a remember run (text/file/url; ``mode=ingest``/``full``).

    Business fields are optional/``None`` when the run has not yet reached
    ``succeeded`` (SM-513, matching Cognify/Improve/Forget's B5 pattern) --
    only ``run_id``/``status`` are guaranteed for ``queued``/``running``/
    ``cancelling``/``cancelled``."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: PipelineRunStatus
    dataset_id: UUID | None = None
    source_id: UUID | None = None
    document_id: UUID | None = None
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    chunks: int | None = None
    entities: int | None = None
    relations: int | None = None
    deduplicated: bool | None = None


__all__ = [
    "RememberTextRequest",
    "RememberTextResult",
    "RememberUrlRequest",
]
