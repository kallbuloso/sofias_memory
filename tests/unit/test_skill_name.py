from __future__ import annotations

import pytest

from sofias_memory.domain import (
    SKILL_NAME_MAX_LENGTH,
    InvalidSkillNameError,
    validate_skill_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "a",
        "pdf",
        "pdf-tools",
        "tool2",
        "a-b-c",
        "a" * SKILL_NAME_MAX_LENGTH,
    ],
)
def test_valid_names_are_returned_unchanged(name: str) -> None:
    assert validate_skill_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "PDF",
        "Pdf-Tools",
        "-pdf",
        "pdf-",
        "pdf--tools",
        "pdf tools",
        "pdf_tools",
        "a" * (SKILL_NAME_MAX_LENGTH + 1),
    ],
)
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(InvalidSkillNameError):
        validate_skill_name(name)


def test_invalid_name_is_never_silently_rewritten() -> None:
    # An uppercase name must be rejected, never lowercased for the caller.
    with pytest.raises(InvalidSkillNameError):
        validate_skill_name("PDF-Tools")

    # A name with surrounding whitespace must be rejected, never trimmed.
    with pytest.raises(InvalidSkillNameError):
        validate_skill_name("  pdf-tools  ")
