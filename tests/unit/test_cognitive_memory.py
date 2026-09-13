from __future__ import annotations

import pytest

from sofias_memory.domain import (
    COGNITIVE_MEMORY_CONTENT_MAX_LENGTH,
    CognitiveMemoryOriginKind,
    CognitiveMemoryType,
    InvalidCognitiveMemoryConfidenceError,
    InvalidCognitiveMemoryContentError,
    InvalidCognitiveMemoryProvenanceError,
    normalize_cognitive_memory_content,
    validate_cognitive_memory_confidence,
    validate_cognitive_memory_external_ref,
    validate_cognitive_memory_provenance_origin_requirements,
    validate_cognitive_memory_source_system,
)

# --- memory type -------------------------------------------------------


def test_profile_and_semantic_are_valid_memory_types() -> None:
    assert CognitiveMemoryType("profile") is CognitiveMemoryType.PROFILE
    assert CognitiveMemoryType("semantic") is CognitiveMemoryType.SEMANTIC


@pytest.mark.parametrize("value", ["episodic", "procedural", "EPISODIC", "", "note"])
def test_episodic_and_procedural_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        CognitiveMemoryType(value)


# --- content normalization ----------------------------------------------


def test_content_normalizes_crlf_and_cr_to_lf() -> None:
    assert normalize_cognitive_memory_content("line1\r\nline2\rline3") == "line1\nline2\nline3"


def test_content_trims_edge_whitespace_only() -> None:
    assert normalize_cognitive_memory_content("  hello world  \n") == "hello world"


def test_content_preserves_internal_whitespace() -> None:
    assert normalize_cognitive_memory_content("a   b\n\nc") == "a   b\n\nc"


def test_content_empty_after_trim_is_rejected() -> None:
    with pytest.raises(InvalidCognitiveMemoryContentError):
        normalize_cognitive_memory_content("   \n\t  ")


def test_content_within_max_length_is_accepted() -> None:
    value = "a" * COGNITIVE_MEMORY_CONTENT_MAX_LENGTH
    assert normalize_cognitive_memory_content(value) == value


def test_content_over_max_length_is_rejected() -> None:
    with pytest.raises(InvalidCognitiveMemoryContentError):
        normalize_cognitive_memory_content("a" * (COGNITIVE_MEMORY_CONTENT_MAX_LENGTH + 1))


# --- confidence -----------------------------------------------------------


USER_ASSERTED = CognitiveMemoryOriginKind.USER_ASSERTED
INFERRED = CognitiveMemoryOriginKind.INFERRED


def test_confidence_none_is_allowed_for_non_inferred() -> None:
    assert validate_cognitive_memory_confidence(None, origin_kind=USER_ASSERTED) is None


def test_confidence_lower_bound() -> None:
    assert validate_cognitive_memory_confidence(0.0, origin_kind=INFERRED) == 0.0
    with pytest.raises(InvalidCognitiveMemoryConfidenceError):
        validate_cognitive_memory_confidence(-0.0001, origin_kind=INFERRED)


def test_confidence_upper_bound() -> None:
    assert validate_cognitive_memory_confidence(1.0, origin_kind=INFERRED) == 1.0
    with pytest.raises(InvalidCognitiveMemoryConfidenceError):
        validate_cognitive_memory_confidence(1.0001, origin_kind=INFERRED)


def test_inferred_requires_confidence() -> None:
    with pytest.raises(InvalidCognitiveMemoryConfidenceError):
        validate_cognitive_memory_confidence(None, origin_kind=INFERRED)


def test_user_asserted_never_synthesizes_confidence() -> None:
    # None stays None -- never coerced to 1.0.
    assert validate_cognitive_memory_confidence(None, origin_kind=USER_ASSERTED) is None


# --- source_system slug ----------------------------------------------------


@pytest.mark.parametrize("value", ["sofias-assistant", "a", "a-b-c", "abc123"])
def test_valid_source_system_slugs(value: str) -> None:
    assert validate_cognitive_memory_source_system(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "Sofias-Assistant", "-leading", "trailing-", "double--hyphen", "has space", "a" * 65],
)
def test_invalid_source_system_slugs_rejected(value: str) -> None:
    with pytest.raises(InvalidCognitiveMemoryProvenanceError):
        validate_cognitive_memory_source_system(value)


# --- external ref -----------------------------------------------------------


def test_external_ref_trims_and_accepts_1_to_255() -> None:
    assert validate_cognitive_memory_external_ref("  ref-1  ") == "ref-1"
    assert validate_cognitive_memory_external_ref("a" * 255) == "a" * 255


def test_external_ref_rejects_empty_or_too_long() -> None:
    with pytest.raises(InvalidCognitiveMemoryProvenanceError):
        validate_cognitive_memory_external_ref("   ")
    with pytest.raises(InvalidCognitiveMemoryProvenanceError):
        validate_cognitive_memory_external_ref("a" * 256)


# --- provenance origin requirements -----------------------------------------


def test_user_asserted_requires_turn_uuid_or_source_ref() -> None:
    validate_cognitive_memory_provenance_origin_requirements(
        origin_kind=CognitiveMemoryOriginKind.USER_ASSERTED,
        turn_uuid="turn-1",
        task_uuid=None,
        source_ref=None,
        observed_at=None,
    )
    validate_cognitive_memory_provenance_origin_requirements(
        origin_kind=CognitiveMemoryOriginKind.USER_ASSERTED,
        turn_uuid=None,
        task_uuid=None,
        source_ref="ref-1",
        observed_at=None,
    )
    with pytest.raises(InvalidCognitiveMemoryProvenanceError):
        validate_cognitive_memory_provenance_origin_requirements(
            origin_kind=CognitiveMemoryOriginKind.USER_ASSERTED,
            turn_uuid=None,
            task_uuid=None,
            source_ref=None,
            observed_at=None,
        )


def test_tool_observed_requires_source_ref_and_observed_at() -> None:
    validate_cognitive_memory_provenance_origin_requirements(
        origin_kind=CognitiveMemoryOriginKind.TOOL_OBSERVED,
        turn_uuid=None,
        task_uuid=None,
        source_ref="ref-1",
        observed_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(InvalidCognitiveMemoryProvenanceError):
        validate_cognitive_memory_provenance_origin_requirements(
            origin_kind=CognitiveMemoryOriginKind.TOOL_OBSERVED,
            turn_uuid=None,
            task_uuid=None,
            source_ref="ref-1",
            observed_at=None,
        )
    with pytest.raises(InvalidCognitiveMemoryProvenanceError):
        validate_cognitive_memory_provenance_origin_requirements(
            origin_kind=CognitiveMemoryOriginKind.TOOL_OBSERVED,
            turn_uuid=None,
            task_uuid=None,
            source_ref=None,
            observed_at="2026-01-01T00:00:00Z",
        )


def test_imported_requires_source_ref() -> None:
    validate_cognitive_memory_provenance_origin_requirements(
        origin_kind=CognitiveMemoryOriginKind.IMPORTED,
        turn_uuid=None,
        task_uuid=None,
        source_ref="ref-1",
        observed_at=None,
    )
    with pytest.raises(InvalidCognitiveMemoryProvenanceError):
        validate_cognitive_memory_provenance_origin_requirements(
            origin_kind=CognitiveMemoryOriginKind.IMPORTED,
            turn_uuid=None,
            task_uuid=None,
            source_ref=None,
            observed_at=None,
        )


def test_inferred_requires_turn_task_or_source_ref() -> None:
    for kwargs in (
        {"turn_uuid": "t", "task_uuid": None, "source_ref": None},
        {"turn_uuid": None, "task_uuid": "task", "source_ref": None},
        {"turn_uuid": None, "task_uuid": None, "source_ref": "ref"},
    ):
        validate_cognitive_memory_provenance_origin_requirements(
            origin_kind=CognitiveMemoryOriginKind.INFERRED,
            observed_at=None,
            **kwargs,
        )
    with pytest.raises(InvalidCognitiveMemoryProvenanceError):
        validate_cognitive_memory_provenance_origin_requirements(
            origin_kind=CognitiveMemoryOriginKind.INFERRED,
            turn_uuid=None,
            task_uuid=None,
            source_ref=None,
            observed_at=None,
        )


def test_assistant_generated_requires_turn_task_or_source_ref() -> None:
    validate_cognitive_memory_provenance_origin_requirements(
        origin_kind=CognitiveMemoryOriginKind.ASSISTANT_GENERATED,
        turn_uuid=None,
        task_uuid="task-1",
        source_ref=None,
        observed_at=None,
    )
    with pytest.raises(InvalidCognitiveMemoryProvenanceError):
        validate_cognitive_memory_provenance_origin_requirements(
            origin_kind=CognitiveMemoryOriginKind.ASSISTANT_GENERATED,
            turn_uuid=None,
            task_uuid=None,
            source_ref=None,
            observed_at=None,
        )
