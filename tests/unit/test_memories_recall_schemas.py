"""Pydantic-level tests for the SM-1003 ``MemoryRecallRequest`` schema --
proves defaults, scope/type/top_k/as_of/min_relevance bounds, and that
normalization is delegated to the shared SM-1001 domain primitives."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sofias_memory.schemas.memories import MemoryRecallRequest


def make_request(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "query": "What interface color does the user prefer?",
        "scopes": ["global"],
    }
    base.update(overrides)
    return base


# --- defaults -----------------------------------------------------------


def test_default_memory_types_is_profile_and_semantic() -> None:
    request = MemoryRecallRequest(**make_request())  # type: ignore[arg-type]
    assert [t.value for t in request.memory_types] == ["profile", "semantic"]


def test_default_top_k_is_10() -> None:
    request = MemoryRecallRequest(**make_request())  # type: ignore[arg-type]
    assert request.top_k == 10


def test_default_include_superseded_is_false() -> None:
    request = MemoryRecallRequest(**make_request())  # type: ignore[arg-type]
    assert request.include_superseded is False


def test_default_min_relevance_is_none() -> None:
    request = MemoryRecallRequest(**make_request())  # type: ignore[arg-type]
    assert request.min_relevance is None


def test_default_as_of_is_close_to_now_utc() -> None:
    before = datetime.now(UTC)
    request = MemoryRecallRequest(**make_request())  # type: ignore[arg-type]
    after = datetime.now(UTC)
    assert before <= request.as_of <= after


# --- query ----------------------------------------------------------------


def test_query_is_normalized() -> None:
    request = MemoryRecallRequest(**make_request(query="  line1\r\nline2  "))  # type: ignore[arg-type]
    assert request.query == "line1\nline2"


def test_blank_query_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(query="   \n\t  "))  # type: ignore[arg-type]


def test_oversized_query_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(query="a" * 8193))  # type: ignore[arg-type]


# --- scopes -----------------------------------------------------------------


def test_scopes_required() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(query="x", scopes=[])  # type: ignore[arg-type]


def test_scopes_more_than_16_rejected() -> None:
    scopes = [f"project:s{i}" for i in range(17)]
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(scopes=scopes))  # type: ignore[arg-type]


def test_scopes_exactly_16_accepted() -> None:
    scopes = [f"project:s{i}" for i in range(16)]
    request = MemoryRecallRequest(**make_request(scopes=scopes))  # type: ignore[arg-type]
    assert len(request.scopes) == 16


@pytest.mark.parametrize("value", ["Global", "project:", "project:UPPER", "tenant:x", ""])
def test_invalid_scope_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(scopes=[value]))  # type: ignore[arg-type]


def test_duplicate_scopes_are_deduplicated() -> None:
    request = MemoryRecallRequest(
        **make_request(scopes=["global", "global", "project:alpha", "project:alpha"])
    )  # type: ignore[arg-type]
    assert request.scopes == ["global", "project:alpha"]


def test_global_never_auto_added_for_project_scope() -> None:
    request = MemoryRecallRequest(**make_request(scopes=["project:alpha"]))  # type: ignore[arg-type]
    assert request.scopes == ["project:alpha"]


# --- memory_types -------------------------------------------------------------


@pytest.mark.parametrize("value", ["episodic", "procedural", "skill", "EPISODIC"])
def test_invalid_memory_type_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(memory_types=[value]))  # type: ignore[arg-type]


def test_memory_types_can_be_restricted_to_one() -> None:
    request = MemoryRecallRequest(**make_request(memory_types=["profile"]))  # type: ignore[arg-type]
    assert [t.value for t in request.memory_types] == ["profile"]


# --- top_k --------------------------------------------------------------------


def test_top_k_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(top_k=0))  # type: ignore[arg-type]


def test_top_k_51_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(top_k=51))  # type: ignore[arg-type]


def test_top_k_boundaries_accepted() -> None:
    assert MemoryRecallRequest(**make_request(top_k=1)).top_k == 1  # type: ignore[arg-type]
    assert MemoryRecallRequest(**make_request(top_k=50)).top_k == 50  # type: ignore[arg-type]


# --- min_relevance --------------------------------------------------------------


def test_min_relevance_below_negative_one_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(min_relevance=-1.0001))  # type: ignore[arg-type]


def test_min_relevance_above_one_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(min_relevance=1.0001))  # type: ignore[arg-type]


def test_min_relevance_boundaries_accepted() -> None:
    assert MemoryRecallRequest(**make_request(min_relevance=-1.0)).min_relevance == -1.0  # type: ignore[arg-type]
    assert MemoryRecallRequest(**make_request(min_relevance=1.0)).min_relevance == 1.0  # type: ignore[arg-type]


# --- as_of ----------------------------------------------------------------------


def test_future_as_of_rejected() -> None:
    future = datetime.now(UTC) + timedelta(days=1)
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(as_of=future))  # type: ignore[arg-type]


def test_naive_as_of_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(as_of=datetime(2026, 1, 1)))  # noqa: DTZ001


def test_past_timezone_aware_as_of_normalized_to_utc() -> None:
    past = datetime(2020, 1, 1, tzinfo=UTC)
    request = MemoryRecallRequest(**make_request(as_of=past))  # type: ignore[arg-type]
    assert request.as_of == past


# --- extra fields forbidden ---------------------------------------------------


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(dataset_id="main"))  # type: ignore[arg-type]


def test_session_id_field_not_accepted() -> None:
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**make_request(session_id="abc"))  # type: ignore[arg-type]
