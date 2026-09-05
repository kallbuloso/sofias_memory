from __future__ import annotations

import pytest

from sofias_memory.domain import SESSION_ID_MAX_LENGTH, InvalidSessionIdError, normalize_session_id


def test_normalize_session_id_none_returns_none() -> None:
    assert normalize_session_id(None) is None


def test_normalize_session_id_trims_surrounding_whitespace() -> None:
    assert normalize_session_id(" abc ") == "abc"


def test_normalize_session_id_preserves_case() -> None:
    assert normalize_session_id("Sofias-Assistant:Conversation:98231") == (
        "Sofias-Assistant:Conversation:98231"
    )


def test_normalize_session_id_does_not_slugify_or_lowercase() -> None:
    value = "  MiXeD Case / with spaces  "
    assert normalize_session_id(value) == "MiXeD Case / with spaces"


def test_normalize_session_id_rejects_empty_string() -> None:
    with pytest.raises(InvalidSessionIdError):
        normalize_session_id("")


def test_normalize_session_id_rejects_whitespace_only() -> None:
    with pytest.raises(InvalidSessionIdError):
        normalize_session_id("   ")


def test_normalize_session_id_accepts_exactly_max_length_after_trim() -> None:
    value = "a" * SESSION_ID_MAX_LENGTH
    assert normalize_session_id(f"  {value}  ") == value
    assert len(value) == SESSION_ID_MAX_LENGTH


def test_normalize_session_id_rejects_over_max_length_after_trim() -> None:
    value = "a" * (SESSION_ID_MAX_LENGTH + 1)
    with pytest.raises(InvalidSessionIdError):
        normalize_session_id(value)


def test_normalize_session_id_max_length_is_enforced_after_trim_not_before() -> None:
    padded = " " + ("a" * SESSION_ID_MAX_LENGTH) + " "
    assert len(padded) > SESSION_ID_MAX_LENGTH
    assert normalize_session_id(padded) == "a" * SESSION_ID_MAX_LENGTH


def test_invalid_session_id_error_message_contains_reason() -> None:
    with pytest.raises(InvalidSessionIdError, match="empty or whitespace-only"):
        normalize_session_id("")
