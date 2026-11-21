"""Schema tests for SM-CTX-002 ContextCheckpoints: input/result separation,
required current_state, default context_key, no Evidence fields."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointCreateRequest,
    ContextCheckpointInvalidateRequest,
    ContextCheckpointResult,
)


def body(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "scope": "project:sofias-memory",
        "current_state": {"text": "Estado atual."},
        "provenance": {"origin_kind": "user_asserted", "source_system": "manual"},
    }
    value.update(overrides)
    return value


def error_types(error: ValidationError) -> set[str]:
    return {item["type"] for item in error.errors()}


def test_minimal_create_defaults_context_key_to_main_and_empty_collections() -> None:
    request = ContextCheckpointCreateRequest.model_validate(body())
    assert request.context_key == "main"
    assert request.objective is None and request.next_action is None
    assert request.approved_decisions == [] and request.blockers == []


@pytest.mark.parametrize("current_state", [None, "missing"])
def test_create_requires_current_state(current_state: object) -> None:
    value = body()
    if current_state == "missing":
        del value["current_state"]
    else:
        value["current_state"] = current_state
    with pytest.raises(ValidationError):
        ContextCheckpointCreateRequest.model_validate(value)


def test_create_requires_provenance_and_scope() -> None:
    for field in ("provenance", "scope"):
        value = body()
        del value[field]
        with pytest.raises(ValidationError):
            ContextCheckpointCreateRequest.model_validate(value)


@pytest.mark.parametrize("scope", ["global", "project:", "Project:x", "proj:x", "project:A"])
def test_invalid_scopes_raise_invalid_scope(scope: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ContextCheckpointCreateRequest.model_validate(body(scope=scope))
    assert "invalid_scope" in error_types(caught.value)


@pytest.mark.parametrize("context_key", ["Main", "a b", "a" * 129, ""])
def test_invalid_context_keys_raise_invalid_context_key(context_key: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ContextCheckpointCreateRequest.model_validate(body(context_key=context_key))
    assert "invalid_context_key" in error_types(caught.value)


def test_blank_statement_text_is_rejected_everywhere() -> None:
    for field, value in (
        ("current_state", {"text": "  "}),
        ("objective", {"text": ""}),
        ("next_action", {"text": "\n"}),
        ("pending_work", [{"text": "ok"}, {"text": " "}]),
    ):
        with pytest.raises(ValidationError):
            ContextCheckpointCreateRequest.model_validate(body(**{field: value}))


def test_server_generated_and_future_fields_are_forbidden() -> None:
    for extra in (
        {"checkpoint_id": str(uuid4())},
        {"revision": 1},
        {"lifecycle": "active"},
        {"current_state": {"text": "x", "statement_id": str(uuid4())}},
        {"current_state": {"text": "x", "support": {"mode": "all_required"}}},
        {"expected_current_checkpoint_id": str(uuid4())},
    ):
        with pytest.raises(ValidationError):
            ContextCheckpointCreateRequest.model_validate(body(**extra))


def test_collections_preserve_order() -> None:
    request = ContextCheckpointCreateRequest.model_validate(
        body(open_items=[{"text": "3"}, {"text": "1"}, {"text": "2"}])
    )
    assert [item.text for item in request.open_items] == ["3", "1", "2"]


def test_invalidate_requires_non_blank_reason() -> None:
    assert ContextCheckpointInvalidateRequest.model_validate({"reason": " r "}).reason == "r"
    for value in ({}, {"reason": " "}, {"reason": None}):
        with pytest.raises(ValidationError):
            ContextCheckpointInvalidateRequest.model_validate(value)


def test_result_accepts_forgotten_tombstone_without_current_state() -> None:
    result = ContextCheckpointResult.model_validate(
        {
            "checkpoint_id": str(uuid4()),
            "scope": "project:sofias-memory",
            "context_key": "main",
            "revision": 2,
            "lifecycle": "forgotten",
            "objective": None,
            "current_state": None,
            "approved_decisions": [],
            "pending_work": [],
            "open_items": [],
            "blockers": [],
            "next_action": None,
            "provenance": {
                "origin_kind": "user_asserted",
                "source_system": "manual",
                "conversation_ref": None,
                "turn_ref": None,
            },
            "created_at": datetime.now(UTC).isoformat(),
            "superseded_at": None,
            "superseded_by": None,
            "invalidated_at": None,
            "forgotten_at": datetime.now(UTC).isoformat(),
        }
    )
    assert result.current_state is None
    assert "invalidation_reason" not in ContextCheckpointResult.model_fields
