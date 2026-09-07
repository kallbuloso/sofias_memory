"""Boundary tests proving the public Skill schemas (SM-702) reuse the SM-701
domain contract exactly -- no limit is re-implemented ad hoc at the API
layer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sofias_memory.domain.skill_revision_content import SOFIAS_MEMORY_TAGS_METADATA_KEY
from sofias_memory.schemas.skills import (
    SkillCreateRequest,
    SkillRevisionCreateRequest,
    SkillUpdateRequest,
)


def _base_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "name": "deploy-app",
        "description": "d",
        "procedure": "p",
    }
    values.update(overrides)
    return values


def test_name_at_max_length_is_accepted() -> None:
    request = SkillCreateRequest(**_base_kwargs(name="a" * 64))
    assert len(request.name) == 64


def test_name_over_max_length_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(name="a" * 65))


def test_description_at_max_length_is_accepted() -> None:
    request = SkillCreateRequest(**_base_kwargs(description="d" * 1024))
    assert len(request.description) == 1024


def test_description_over_max_length_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(description="d" * 1025))


def test_compatibility_at_max_length_is_accepted() -> None:
    request = SkillCreateRequest(**_base_kwargs(compatibility="c" * 500))
    assert request.compatibility is not None
    assert len(request.compatibility) == 500


def test_compatibility_over_max_length_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(compatibility="c" * 501))


def test_compatibility_empty_string_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(compatibility=""))


def test_compatibility_none_is_accepted() -> None:
    request = SkillCreateRequest(**_base_kwargs(compatibility=None))
    assert request.compatibility is None


def test_procedure_at_max_length_is_accepted() -> None:
    request = SkillCreateRequest(**_base_kwargs(procedure="p" * 65536))
    assert len(request.procedure) == 65536


def test_procedure_over_max_length_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(procedure="p" * 65537))


def test_procedure_whitespace_only_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(procedure="   \n\t  "))


def test_metadata_rejects_reserved_tags_transport_key() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(metadata={SOFIAS_MEMORY_TAGS_METADATA_KEY: '["a"]'}))


def test_metadata_rejects_non_string_value() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(metadata={"key": {"nested": "value"}}))


def test_tags_are_deduplicated_and_sorted() -> None:
    request = SkillCreateRequest(**_base_kwargs(tags=["b", "a", "a"]))
    assert request.tags == ["a", "b"]


def test_tags_reject_empty_string_entry() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(tags=["a", ""]))


def test_declared_tools_preserve_supplied_order() -> None:
    request = SkillCreateRequest(**_base_kwargs(declared_tools=["z-tool", "a-tool"]))
    assert request.declared_tools == ["z-tool", "a-tool"]


def test_declared_tools_reject_empty_string_entry() -> None:
    with pytest.raises(ValidationError):
        SkillCreateRequest(**_base_kwargs(declared_tools=["tool", ""]))


def test_skill_revision_create_request_rejects_name_field() -> None:
    with pytest.raises(ValidationError):
        SkillRevisionCreateRequest(description="d", procedure="p", name="not-allowed")


def test_skill_revision_create_request_accepts_content_fields() -> None:
    request = SkillRevisionCreateRequest(description="d", procedure="p")
    assert request.description == "d"


def test_skill_update_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SkillUpdateRequest(current_revision=1, name="not-allowed")  # type: ignore[call-arg]


def test_skill_update_request_rejects_revision_below_one() -> None:
    with pytest.raises(ValidationError):
        SkillUpdateRequest(current_revision=0)
