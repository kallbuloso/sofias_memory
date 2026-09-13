"""Pydantic-level tests for the SM-1004 ``MemorySupersedeRequest`` schema --
proves it shares the exact same content/confidence/validity/provenance
validation as Create (via the shared ``_MemoryContentPayloadFields`` base)
while never accepting a caller-supplied ``memory_type``/``scope``, since
both are always inherited from the target old item."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sofias_memory.schemas.memories import MemorySupersedeRequest


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
        "content": "The user now prefers dark mode.",
        "provenance": make_provenance(),
    }
    base.update(overrides)
    return base


# --- content ------------------------------------------------------------------


def test_valid_supersede_request() -> None:
    request = MemorySupersedeRequest(**make_request())  # type: ignore[arg-type]
    assert request.content == "The user now prefers dark mode."


def test_content_is_normalized_via_domain_primitive() -> None:
    request = MemorySupersedeRequest(**make_request(content="  line1\r\nline2  "))  # type: ignore[arg-type]
    assert request.content == "line1\nline2"


def test_blank_content_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(**make_request(content="   \n\t  "))  # type: ignore[arg-type]


# --- memory_type / scope are never accepted -----------------------------------


def test_memory_type_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(**make_request(memory_type="profile"))  # type: ignore[arg-type]


def test_scope_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(**make_request(scope="global"))  # type: ignore[arg-type]


# --- confidence -----------------------------------------------------------------


def test_confidence_none_allowed_for_user_asserted() -> None:
    request = MemorySupersedeRequest(**make_request())  # type: ignore[arg-type]
    assert request.confidence is None


def test_confidence_out_of_bounds_rejected() -> None:
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(**make_request(confidence=1.5))  # type: ignore[arg-type]


def test_inferred_without_confidence_rejected() -> None:
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(
            **make_request(provenance=make_provenance(origin_kind="inferred", source_ref="ref-1"))
        )  # type: ignore[arg-type]


def test_inferred_with_confidence_accepted() -> None:
    request = MemorySupersedeRequest(
        **make_request(
            confidence=0.8,
            provenance=make_provenance(origin_kind="inferred", source_ref="ref-1"),
        )
    )  # type: ignore[arg-type]
    assert request.confidence == 0.8


# --- provenance origin requirements ------------------------------------------


def test_user_asserted_without_turn_or_source_ref_rejected() -> None:
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(**make_request(provenance=make_provenance(turn_uuid=None)))  # type: ignore[arg-type]


def test_source_system_invalid_slug_rejected() -> None:
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(
            **make_request(provenance=make_provenance(source_system="Not-A-Slug"))
        )  # type: ignore[arg-type]


# --- validity window -----------------------------------------------------------


def test_valid_until_must_be_strictly_after_valid_from() -> None:
    same = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(**make_request(valid_from=same, valid_until=same))  # type: ignore[arg-type]


def test_naive_valid_from_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(
            **make_request(valid_from=datetime(2026, 1, 1))  # noqa: DTZ001
        )  # type: ignore[arg-type]


# --- extra fields forbidden ---------------------------------------------------


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(ValidationError):
        MemorySupersedeRequest(**make_request(unexpected="nope"))  # type: ignore[arg-type]
