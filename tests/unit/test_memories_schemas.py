"""Pydantic-level tests for the SM-1002 public Cognitive Memory Create
schema -- proves normalization/validation is delegated to the SM-1001
domain primitives, never re-implemented, and that cross-field rules
(confidence-by-origin, provenance-by-origin, validity window ordering) are
enforced before a service/route is ever reached."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sofias_memory.schemas.memories import MemoryCreateRequest, MemoryProvenanceCreateRequest


def make_provenance(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "origin_kind": "user_asserted",
        "source_system": "sofias-assistant",
        "turn_uuid": str(uuid4()),
    }
    base.update(overrides)
    return base


def make_request(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "memory_type": "profile",
        "scope": "global",
        "content": "Prefers teal interfaces.",
        "provenance": make_provenance(),
    }
    base.update(overrides)
    return base


# --- memory_type / scope / content ------------------------------------------


def test_valid_create_request_profile() -> None:
    request = MemoryCreateRequest(**make_request())  # type: ignore[arg-type]
    assert request.memory_type.value == "profile"
    assert request.scope == "global"
    assert request.content == "Prefers teal interfaces."


def test_valid_create_request_semantic() -> None:
    request = MemoryCreateRequest(
        **make_request(
            memory_type="semantic",
            content="The user's project deploys on EasyPanel.",
            provenance=make_provenance(origin_kind="assistant_generated", task_uuid=str(uuid4())),
        )
    )  # type: ignore[arg-type]
    assert request.memory_type.value == "semantic"


@pytest.mark.parametrize("value", ["episodic", "procedural", "PROFILE", ""])
def test_invalid_memory_type_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(**make_request(memory_type=value))  # type: ignore[arg-type]


def test_scope_is_normalized_via_domain_primitive() -> None:
    request = MemoryCreateRequest(**make_request(scope="  global  "))  # type: ignore[arg-type]
    assert request.scope == "global"


@pytest.mark.parametrize("value", ["Global", "project:", "project:UPPER", "tenant:x"])
def test_invalid_scope_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(**make_request(scope=value))  # type: ignore[arg-type]


def test_content_is_normalized_via_domain_primitive() -> None:
    request = MemoryCreateRequest(**make_request(content="  line1\r\nline2  "))  # type: ignore[arg-type]
    assert request.content == "line1\nline2"


def test_blank_content_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(**make_request(content="   \n\t  "))  # type: ignore[arg-type]


# --- confidence --------------------------------------------------------------


def test_confidence_none_allowed_for_user_asserted() -> None:
    request = MemoryCreateRequest(**make_request())  # type: ignore[arg-type]
    assert request.confidence is None


def test_confidence_out_of_bounds_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(**make_request(confidence=1.5))  # type: ignore[arg-type]


def test_inferred_without_confidence_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(
            **make_request(
                provenance=make_provenance(origin_kind="inferred", source_ref="ref-1"),
            )
        )  # type: ignore[arg-type]


def test_inferred_with_confidence_accepted() -> None:
    request = MemoryCreateRequest(
        **make_request(
            confidence=0.8,
            provenance=make_provenance(origin_kind="inferred", source_ref="ref-1"),
        )
    )  # type: ignore[arg-type]
    assert request.confidence == 0.8


def test_user_asserted_never_synthesizes_confidence() -> None:
    request = MemoryCreateRequest(**make_request())  # type: ignore[arg-type]
    assert request.confidence is None


# --- provenance origin requirements ------------------------------------------


def test_user_asserted_without_turn_or_source_ref_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(**make_request(provenance=make_provenance(turn_uuid=None)))  # type: ignore[arg-type]


def test_tool_observed_requires_source_ref_and_observed_at() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(
            **make_request(provenance=make_provenance(origin_kind="tool_observed", turn_uuid=None))
        )  # type: ignore[arg-type]
    request = MemoryCreateRequest(
        **make_request(
            provenance=make_provenance(
                origin_kind="tool_observed",
                turn_uuid=None,
                source_ref="ref-1",
                observed_at="2026-01-01T00:00:00Z",
            )
        )
    )  # type: ignore[arg-type]
    assert request.provenance.origin_kind.value == "tool_observed"


def test_source_system_invalid_slug_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(**make_request(provenance=make_provenance(source_system="Not-A-Slug")))  # type: ignore[arg-type]


def test_confirmation_ref_and_source_ref_are_trimmed() -> None:
    request = MemoryCreateRequest(
        **make_request(
            provenance=make_provenance(confirmation_ref="  conf-1  ", source_ref="  src-1  ")
        )
    )  # type: ignore[arg-type]
    assert request.provenance.confirmation_ref == "conf-1"
    assert request.provenance.source_ref == "src-1"


# --- timestamps ----------------------------------------------------------------


def test_naive_valid_from_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(
            **make_request(valid_from=datetime(2026, 1, 1))  # noqa: DTZ001
        )  # type: ignore[arg-type]


def test_timezone_aware_valid_from_is_normalized_to_utc() -> None:
    plus_two = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    request = MemoryCreateRequest(**make_request(valid_from=plus_two))  # type: ignore[arg-type]
    assert request.valid_from == datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def test_valid_until_must_be_strictly_after_valid_from() -> None:
    same = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        MemoryCreateRequest(**make_request(valid_from=same, valid_until=same))  # type: ignore[arg-type]


def test_valid_window_ordered_is_accepted() -> None:
    request = MemoryCreateRequest(
        **make_request(
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_until=datetime(2026, 2, 1, tzinfo=UTC),
        )
    )  # type: ignore[arg-type]
    assert request.valid_until is not None


# --- extra fields forbidden ---------------------------------------------------


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(**make_request(unexpected="nope"))  # type: ignore[arg-type]


def test_unknown_provenance_field_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryProvenanceCreateRequest(**make_provenance(unexpected="nope"))  # type: ignore[arg-type]
