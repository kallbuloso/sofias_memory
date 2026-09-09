from __future__ import annotations

import pytest

from sofias_memory.domain import (
    AGENT_DESCRIPTION_MAX_LENGTH,
    AGENT_DISPLAY_NAME_MAX_LENGTH,
    AGENT_INSTRUCTIONS_MAX_LENGTH,
    InvalidAgentProfileError,
    normalize_validate_agent_instructions,
    validate_agent_description,
    validate_agent_display_name,
)

# --- display_name -----------------------------------------------------------


def test_display_name_none_is_none() -> None:
    assert validate_agent_display_name(None) is None


def test_display_name_is_trimmed() -> None:
    assert validate_agent_display_name("  Research Agent  ") == "Research Agent"


def test_display_name_whitespace_only_is_invalid() -> None:
    with pytest.raises(InvalidAgentProfileError):
        validate_agent_display_name("   ")


def test_display_name_at_max_length_is_valid() -> None:
    value = "a" * AGENT_DISPLAY_NAME_MAX_LENGTH
    assert validate_agent_display_name(value) == value


def test_display_name_over_max_length_is_invalid() -> None:
    with pytest.raises(InvalidAgentProfileError):
        validate_agent_display_name("a" * (AGENT_DISPLAY_NAME_MAX_LENGTH + 1))


def test_display_name_over_max_length_after_trim_is_invalid() -> None:
    # Padding is added only outside the max length, so trimming must occur
    # before the length check for this case to correctly still be invalid.
    padded = " " + ("a" * AGENT_DISPLAY_NAME_MAX_LENGTH) + "b "
    with pytest.raises(InvalidAgentProfileError):
        validate_agent_display_name(padded)


# --- description --------------------------------------------------------------


def test_description_none_is_none() -> None:
    assert validate_agent_description(None) is None


def test_description_unchanged_no_trim() -> None:
    # No trim is applied to description (Feature Contract SS 4.2 orders none).
    value = "  padded description  "
    assert validate_agent_description(value) == value


def test_description_min_length_one_char_is_valid() -> None:
    assert validate_agent_description("x") == "x"


def test_description_empty_string_is_invalid() -> None:
    with pytest.raises(InvalidAgentProfileError):
        validate_agent_description("")


def test_description_at_max_length_is_valid() -> None:
    value = "a" * AGENT_DESCRIPTION_MAX_LENGTH
    assert validate_agent_description(value) == value


def test_description_over_max_length_is_invalid() -> None:
    with pytest.raises(InvalidAgentProfileError):
        validate_agent_description("a" * (AGENT_DESCRIPTION_MAX_LENGTH + 1))


def test_description_whitespace_only_within_bounds_is_accepted() -> None:
    # Deliberate: the Feature Contract states only a length bound for
    # description, with no explicit nonblank requirement (unlike
    # display_name and instructions) -- see the module docstring's
    # ambiguity resolution note in sofias_memory.domain.agent_profile.
    assert validate_agent_description("   ") == "   "


# --- instructions --------------------------------------------------------------


def test_instructions_none_is_none() -> None:
    assert normalize_validate_agent_instructions(None) is None


def test_instructions_unchanged_when_already_normalized() -> None:
    assert normalize_validate_agent_instructions("run this") == "run this"


def test_instructions_crlf_normalized_to_lf() -> None:
    assert normalize_validate_agent_instructions("line 1\r\nline 2") == "line 1\nline 2"


def test_instructions_bare_cr_normalized_to_lf() -> None:
    assert normalize_validate_agent_instructions("line 1\rline 2") == "line 1\nline 2"


def test_instructions_indentation_preserved() -> None:
    value = "line 1\r\n  indented\rline 3"
    assert normalize_validate_agent_instructions(value) == "line 1\n  indented\nline 3"


def test_instructions_whitespace_only_is_invalid() -> None:
    with pytest.raises(InvalidAgentProfileError):
        normalize_validate_agent_instructions("   \n\t ")


def test_instructions_at_max_length_is_valid() -> None:
    value = "a" * AGENT_INSTRUCTIONS_MAX_LENGTH
    assert normalize_validate_agent_instructions(value) == value


def test_instructions_over_max_length_is_invalid() -> None:
    with pytest.raises(InvalidAgentProfileError):
        normalize_validate_agent_instructions("a" * (AGENT_INSTRUCTIONS_MAX_LENGTH + 1))


def test_instructions_over_max_length_after_normalization_is_invalid() -> None:
    # CRLF -> LF shrinks length by one per pair, so start from a value whose
    # normalized length -- not raw length -- exceeds the max.
    value = ("a" * AGENT_INSTRUCTIONS_MAX_LENGTH) + "b"
    with pytest.raises(InvalidAgentProfileError):
        normalize_validate_agent_instructions(value)
