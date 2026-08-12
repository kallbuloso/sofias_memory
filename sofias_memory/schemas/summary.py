"""Structured document summary output."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocumentSummaryOutput(BaseModel):
    """Validated retrieval summary aggregated from ordered chunk summaries."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("summary must not be blank")
        return stripped
