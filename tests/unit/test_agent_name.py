from __future__ import annotations

import pytest

from sofias_memory.domain import (
    AGENT_NAME_MAX_LENGTH,
    InvalidAgentNameError,
    validate_agent_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "a",
        "agent",
        "research-agent",
        "tool2",
        "a-b-c",
        "a" * AGENT_NAME_MAX_LENGTH,
    ],
)
def test_valid_names_are_returned_unchanged(name: str) -> None:
    assert validate_agent_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Agent",
        "Research-Agent",
        "-agent",
        "agent-",
        "agent--worker",
        "agent worker",
        "agent_worker",
        "a" * (AGENT_NAME_MAX_LENGTH + 1),
    ],
)
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(InvalidAgentNameError):
        validate_agent_name(name)


def test_invalid_name_is_never_silently_rewritten() -> None:
    # An uppercase name must be rejected, never lowercased for the caller.
    with pytest.raises(InvalidAgentNameError):
        validate_agent_name("Agent")

    # A name with surrounding whitespace must be rejected, never trimmed.
    with pytest.raises(InvalidAgentNameError):
        validate_agent_name("  agent  ")
