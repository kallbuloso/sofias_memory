"""Public request and response schemas for feedback recording."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FeedbackTargetType = Literal["answer", "reference"]
FeedbackScore = Literal[-1, 0, 1]
FEEDBACK_COMMENT_MAX_LENGTH = 4000


class FeedbackRequest(BaseModel):
    """Record feedback against a persisted recall answer or reference."""

    model_config = ConfigDict(extra="forbid")

    query_id: UUID = Field(description="The Recall query this feedback is about.")
    target_type: FeedbackTargetType = Field(
        description="'answer' for feedback on the generated answer, 'reference' for one specific "
        "retrieved reference."
    )
    target_id: UUID | None = Field(
        default=None,
        description=(
            "Required for target_type='reference' (the reference's id); omitted for 'answer'."
        ),
    )
    score: FeedbackScore = Field(description="-1 (negative), 0 (neutral), or 1 (positive).")
    comment: str | None = Field(
        default=None,
        max_length=FEEDBACK_COMMENT_MAX_LENGTH,
        description="Optional free-text comment.",
    )

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def validate_target_shape(self) -> FeedbackRequest:
        if self.target_type == "answer" and self.target_id is not None:
            raise ValueError("answer feedback must not include target_id")
        if self.target_type == "reference" and self.target_id is None:
            raise ValueError("reference feedback requires target_id")
        return self


class FeedbackResult(BaseModel):
    """Durable feedback record returned after persistence."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: UUID
    query_id: UUID
    target_type: str
    target_id: UUID | None
    score: int
    comment: str | None
    applied_at: datetime | None
    created_at: datetime
