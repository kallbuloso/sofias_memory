"""Schema tests for SM-CTX-001 Episodes (Contract v1 SS 10/17/20)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from sofias_memory.schemas.episodes import (
    EpisodeCreateRequest,
    EpisodeInvalidateRequest,
    EpisodeResult,
)


def body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope": "project:sofias-memory",
        "kind": "decision",
        "summary": "Episode foi escolhido como agregado first-class.",
        "occurred_at": "2026-11-17T21:00:00Z",
        "provenance": {"origin_kind": "user_asserted", "source_system": "sofias-assistant"},
    }
    payload.update(overrides)
    return payload


def error_types(exc: ValidationError) -> set[str]:
    return {str(error["type"]) for error in exc.errors()}


def test_minimal_create_request_defaults_context_key_to_none_not_main() -> None:
    request = EpisodeCreateRequest.model_validate(body())
    assert request.context_key is None
    assert request.situation is None and request.reason is None and request.outcome is None
    assert request.occurred_at == datetime(2026, 11, 17, 21, 0, tzinfo=UTC)


def test_full_create_request_normalizes_text() -> None:
    request = EpisodeCreateRequest.model_validate(
        body(
            context_key="feature:contextual-memory",
            summary="  line1\r\nline2  ",
            situation=" s ",
            reason="r",
            outcome="o",
            confidence=0.5,
            provenance={
                "origin_kind": "inferred",
                "source_system": " sofias-personal-assistant ",
                "conversation_ref": " conversation:42 ",
                "turn_ref": "turn:18",
            },
        )
    )
    assert request.summary == "line1\nline2"
    assert request.situation == "s"
    assert request.provenance.source_system == "sofias-personal-assistant"
    assert request.provenance.conversation_ref == "conversation:42"


@pytest.mark.parametrize(
    ("overrides", "expected_type"),
    [
        ({"scope": "global"}, "invalid_scope"),
        ({"scope": "project:UPPER"}, "invalid_scope"),
        ({"context_key": "Main"}, "invalid_context_key"),
        ({"context_key": ""}, "invalid_context_key"),
        ({"kind": "note"}, "invalid_episode_kind"),
        ({"kind": "Decision"}, "invalid_episode_kind"),
    ],
)
def test_specific_validation_error_types(overrides: dict[str, Any], expected_type: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        EpisodeCreateRequest.model_validate(body(**overrides))
    assert error_types(exc_info.value) == {expected_type}


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": "   "},
        {"situation": ""},
        {"reason": " \n "},
        {"outcome": "\t"},
        {"occurred_at": "2026-11-17T21:00:00"},
        {"occurred_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"provenance": {"origin_kind": "inferred", "source_system": "x"}},
        {"provenance": {"origin_kind": "user_asserted", "source_system": "  "}},
        {"provenance": {"origin_kind": "user_asserted", "source_system": "x", "turn_ref": " "}},
        {"provenance": {"origin_kind": "nope", "source_system": "x"}},
        {"provenance": {"origin_kind": "user_asserted", "source_system": "x", "turn_uuid": "x"}},
        {"support": {"mode": "all_required", "refs": []}},
        {"embedding": [0.1]},
        {"lifecycle": "active"},
    ],
)
def test_invalid_create_requests_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EpisodeCreateRequest.model_validate(body(**overrides))


@pytest.mark.parametrize("missing", ["scope", "kind", "summary", "occurred_at", "provenance"])
def test_required_create_fields(missing: str) -> None:
    payload = body()
    del payload[missing]
    with pytest.raises(ValidationError):
        EpisodeCreateRequest.model_validate(payload)


def test_invalidate_request_requires_non_blank_reason() -> None:
    assert EpisodeInvalidateRequest.model_validate({"reason": " wrong "}).reason == "wrong"
    for bad in ({}, {"reason": "  "}, {"reason": "x", "extra": 1}):
        with pytest.raises(ValidationError):
            EpisodeInvalidateRequest.model_validate(bad)


def test_episode_result_shape_never_exposes_embedding_or_ledger_values() -> None:
    fields = set(EpisodeResult.model_fields)
    assert fields == {
        "episode_id",
        "scope",
        "context_key",
        "kind",
        "summary",
        "situation",
        "reason",
        "outcome",
        "occurred_at",
        "lifecycle",
        "confidence",
        "provenance",
        "created_at",
        "invalidated_at",
        "forgotten_at",
    }
