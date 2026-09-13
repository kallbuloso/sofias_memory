from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from sofias_memory.domain import (
    COGNITIVE_MEMORY_CONTENT_MAX_LENGTH,
    COGNITIVE_MEMORY_RECALL_QUERY_MAX_LENGTH,
    CognitiveMemoryOriginKind,
    CognitiveMemoryType,
    InvalidCognitiveMemoryConfidenceError,
    InvalidCognitiveMemoryContentError,
    InvalidCognitiveMemoryProvenanceError,
    InvalidCognitiveMemoryTimestampError,
    InvalidCognitiveMemoryValidityWindowError,
    normalize_cognitive_memory_content,
    normalize_cognitive_memory_recall_query,
    normalize_cognitive_memory_timestamp,
    validate_cognitive_memory_confidence,
    validate_cognitive_memory_external_ref,
    validate_cognitive_memory_provenance_origin_requirements,
    validate_cognitive_memory_source_system,
    validate_cognitive_memory_validity_window,
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


# --- timestamp normalization -------------------------------------------------


def test_timestamp_normalizes_non_utc_offset_to_utc() -> None:
    plus_two = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    normalized = normalize_cognitive_memory_timestamp(plus_two)
    assert normalized == datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    assert normalized.tzinfo is UTC


def test_timestamp_already_utc_is_unchanged() -> None:
    value = datetime(2026, 1, 1, tzinfo=UTC)
    assert normalize_cognitive_memory_timestamp(value) == value


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(InvalidCognitiveMemoryTimestampError):
        normalize_cognitive_memory_timestamp(datetime(2026, 1, 1))  # noqa: DTZ001


# --- validity window ----------------------------------------------------------


def test_validity_window_accepts_none_none() -> None:
    validate_cognitive_memory_validity_window(None, None)


def test_validity_window_accepts_one_sided_bounds() -> None:
    validate_cognitive_memory_validity_window(datetime(2026, 1, 1, tzinfo=UTC), None)
    validate_cognitive_memory_validity_window(None, datetime(2026, 1, 1, tzinfo=UTC))


def test_validity_window_accepts_until_strictly_after_from() -> None:
    validate_cognitive_memory_validity_window(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    )


def test_validity_window_rejects_until_equal_to_from() -> None:
    same = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(InvalidCognitiveMemoryValidityWindowError):
        validate_cognitive_memory_validity_window(same, same)


def test_validity_window_rejects_until_before_from() -> None:
    with pytest.raises(InvalidCognitiveMemoryValidityWindowError):
        validate_cognitive_memory_validity_window(
            datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
        )


# --- recall query normalization -----------------------------------------


def test_recall_query_normalizes_crlf_and_cr_to_lf() -> None:
    assert normalize_cognitive_memory_recall_query("q1\r\nq2\rq3") == "q1\nq2\nq3"


def test_recall_query_trims_edge_whitespace_only() -> None:
    assert normalize_cognitive_memory_recall_query("  what does X do?  ") == "what does X do?"


def test_recall_query_empty_after_trim_is_rejected() -> None:
    with pytest.raises(InvalidCognitiveMemoryContentError):
        normalize_cognitive_memory_recall_query("   \n\t  ")


def test_recall_query_within_max_length_is_accepted() -> None:
    value = "a" * COGNITIVE_MEMORY_RECALL_QUERY_MAX_LENGTH
    assert normalize_cognitive_memory_recall_query(value) == value


def test_recall_query_over_max_length_is_rejected() -> None:
    oversized = "a" * (COGNITIVE_MEMORY_RECALL_QUERY_MAX_LENGTH + 1)
    with pytest.raises(InvalidCognitiveMemoryContentError):
        normalize_cognitive_memory_recall_query(oversized)


def test_recall_query_max_length_differs_from_content_max_length() -> None:
    assert COGNITIVE_MEMORY_RECALL_QUERY_MAX_LENGTH != COGNITIVE_MEMORY_CONTENT_MAX_LENGTH
