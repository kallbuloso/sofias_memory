from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

type JSONValue = str | int | float | bool | None | list[JSONValue] | dict[str, JSONValue]
type JSONDetails = dict[str, JSONValue]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    MISSING_API_KEY = "MISSING_API_KEY"
    INVALID_API_KEY = "INVALID_API_KEY"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ResponseMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID = Field(description="Effective request correlation identifier.")
    timestamp: datetime = Field(
        default_factory=utc_now,
        description="UTC timestamp for response construction.",
    )

    @field_validator("timestamp")
    @classmethod
    def require_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("timestamp")
    def serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class SuccessEnvelope[T](BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: T
    meta: ResponseMeta


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode = Field(description="Stable application error code.")
    message: str = Field(description="Safe public error message.")
    details: JSONDetails = Field(
        default_factory=dict,
        description="Sanitized JSON-safe error details.",
    )
    request_id: UUID = Field(description="Effective request correlation identifier.")


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
