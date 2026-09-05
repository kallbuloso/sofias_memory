from __future__ import annotations

import pytest

from sofias_memory.domain import (
    SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH,
    InvalidSessionEntryExternalIdError,
    normalize_session_entry_external_id,
)


def test_normalize_external_id_none_returns_none() -> None:
    assert normalize_session_entry_external_id(None) is None


def test_normalize_external_id_trims_surrounding_whitespace() -> None:
    assert normalize_session_entry_external_id(" abc ") == "abc"


def test_normalize_external_id_preserves_case() -> None:
    assert normalize_session_entry_external_id("Caller-Stable-ID") == "Caller-Stable-ID"


def test_normalize_external_id_does_not_slugify_or_lowercase() -> None:
    value = "  MiXeD / value  "
    assert normalize_session_entry_external_id(value) == "MiXeD / value"


def test_normalize_external_id_rejects_empty_string() -> None:
    with pytest.raises(InvalidSessionEntryExternalIdError):
        normalize_session_entry_external_id("")


def test_normalize_external_id_rejects_whitespace_only() -> None:
    with pytest.raises(InvalidSessionEntryExternalIdError):
        normalize_session_entry_external_id("   ")


def test_normalize_external_id_accepts_exactly_max_length_after_trim() -> None:
    value = "a" * SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH
    assert normalize_session_entry_external_id(f"  {value}  ") == value
    assert len(value) == 255


def test_normalize_external_id_rejects_over_max_length_after_trim() -> None:
    value = "a" * (SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH + 1)
    with pytest.raises(InvalidSessionEntryExternalIdError):
        normalize_session_entry_external_id(value)


def test_invalid_external_id_error_message_contains_reason() -> None:
    with pytest.raises(InvalidSessionEntryExternalIdError, match="empty or whitespace-only"):
        normalize_session_entry_external_id("")


def test_external_id_constant_is_independent_of_session_id_constant() -> None:
    from sofias_memory.domain import SESSION_ID_MAX_LENGTH

    # Same numeric bound today by coincidence, but a distinct named constant
    # -- SM-603 must not couple the two concepts together.
    assert SESSION_ENTRY_EXTERNAL_ID_MAX_LENGTH == SESSION_ID_MAX_LENGTH == 255
