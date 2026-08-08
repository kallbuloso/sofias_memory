from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sofias_memory.schemas.common import (
    ErrorBody,
    ErrorCode,
    ErrorEnvelope,
    ResponseMeta,
    SuccessEnvelope,
)


def test_response_meta_generates_utc_timezone_aware_timestamp() -> None:
    meta = ResponseMeta(request_id=uuid4())

    assert meta.timestamp.tzinfo is not None
    assert meta.timestamp.utcoffset() == UTC.utcoffset(meta.timestamp)


def test_success_envelope_serializes_data() -> None:
    request_id = uuid4()
    envelope = SuccessEnvelope[dict[str, str]](
        data={"status": "ok"},
        meta=ResponseMeta(request_id=request_id),
    )

    assert envelope.model_dump(mode="json")["data"] == {"status": "ok"}


def test_success_envelope_includes_request_id() -> None:
    request_id = uuid4()
    envelope = SuccessEnvelope[dict[str, str]](
        data={"status": "ok"},
        meta=ResponseMeta(request_id=request_id),
    )

    assert envelope.model_dump(mode="json")["meta"]["request_id"] == str(request_id)


def test_error_envelope_serializes_exact_contract() -> None:
    request_id = uuid4()
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=ErrorCode.INVALID_REQUEST,
            message="Invalid request.",
            details={"field": "name"},
            request_id=request_id,
        )
    )

    assert envelope.model_dump(mode="json") == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "Invalid request.",
            "details": {"field": "name"},
            "request_id": str(request_id),
        }
    }


def test_error_code_serializes_as_stable_string() -> None:
    body = ErrorBody(
        code=ErrorCode.MISSING_API_KEY,
        message="API key is required.",
        request_id=uuid4(),
    )

    assert body.model_dump(mode="json")["code"] == "MISSING_API_KEY"


def test_empty_details_work() -> None:
    body = ErrorBody(
        code=ErrorCode.INTERNAL_ERROR,
        message="Internal server error.",
        request_id=uuid4(),
    )

    assert body.details == {}
    assert body.model_dump(mode="json")["details"] == {}


def test_uuid_serializes_correctly() -> None:
    request_id = uuid4()
    meta = ResponseMeta(request_id=request_id)

    assert meta.model_dump(mode="json")["request_id"] == str(request_id)


def test_timestamp_serializes_as_utc_iso_8601() -> None:
    request_id = uuid4()
    timestamp = datetime(2026, 8, 5, 23, 0, tzinfo=UTC)
    meta = ResponseMeta(request_id=request_id, timestamp=timestamp)

    assert meta.model_dump(mode="json")["timestamp"] == "2026-08-05T23:00:00Z"


def test_response_meta_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        ResponseMeta(request_id=uuid4(), timestamp=datetime(2026, 8, 5, 23, 0))


def test_error_body_rejects_invalid_request_id() -> None:
    with pytest.raises(ValidationError):
        ErrorBody(
            code=ErrorCode.INVALID_REQUEST,
            message="Invalid request.",
            request_id="not-a-uuid",
        )


def test_error_body_rejects_non_json_safe_details() -> None:
    with pytest.raises(ValidationError):
        ErrorBody(
            code=ErrorCode.INVALID_REQUEST,
            message="Invalid request.",
            details={"bad": object()},
            request_id=uuid4(),
        )
