"""Shared Agent ``name`` validation primitive.

ADR-0014 and the v0.5.0 Agent Management Feature Contract SS 3.1 require
every Agent-aware entry point (structured create -- SM-802) to validate the
caller-supplied portable ``name`` with exactly this one rule, so no endpoint
may implement its own variant. This module has no dependency on FastAPI,
SQLAlchemy, or Neo4j.

Deliberately a separate module from ``skill_name.py`` (not a shared
import) even though the portable-subset regex is identical: Agent identity
and Skill identity are independent contracts that happen to share a format,
not the same concept -- coupling them would make a future divergence (e.g.
Agent allowing a longer name) require disentangling two call sites instead
of an isolated change.

Validation only, never normalization: an invalid ``name`` is rejected
outright, never silently lowercased, trimmed, or slugified into a valid one
(Feature Contract SS 3.1).
"""

from __future__ import annotations

import re

AGENT_NAME_MAX_LENGTH = 64
"""Maximum ``name`` length, in characters (portable subset)."""

_AGENT_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
"""Lowercase ``a-z0-9-``, 1..64 chars, no leading/trailing/doubled hyphen."""


class InvalidAgentNameError(ValueError):
    """A caller-supplied Agent ``name`` fails the shared portable contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Agent name: {reason}")


def validate_agent_name(value: str) -> str:
    """Validate an Agent ``name`` against the portable subset.

    Returns ``value`` unchanged when valid. Never lowercases, trims, or
    otherwise rewrites an invalid value into a valid one -- an invalid
    ``name`` always raises :class:`InvalidAgentNameError`.
    """

    if not value:
        raise InvalidAgentNameError("name must not be empty")
    if len(value) > AGENT_NAME_MAX_LENGTH:
        raise InvalidAgentNameError(f"name must be at most {AGENT_NAME_MAX_LENGTH} characters")
    if not _AGENT_NAME_PATTERN.fullmatch(value):
        raise InvalidAgentNameError(
            "name must contain only lowercase a-z, 0-9, and single hyphens, "
            "and must not start or end with a hyphen"
        )
    return value
