"""Shared Skill ``name`` validation primitive.

ADR-0013 and the v0.4.0 Skills Feature Contract SS 3.1 require every
Skill-aware entry point (structured create, SKILL.md import) to validate the
caller-supplied portable ``name`` with exactly this one rule, so no endpoint
may implement its own variant. This module has no dependency on FastAPI,
SQLAlchemy, or Neo4j.

Unlike ``normalize_session_id``, this is validation only, never
normalization: an invalid ``name`` is rejected outright, never silently
lowercased, trimmed, or slugified into a valid one (Feature Contract SS 3.1:
"não transformar em slug... não trimar/transformar uma identidade inválida
para fazê-la válida silenciosamente").
"""

from __future__ import annotations

import re

SKILL_NAME_MAX_LENGTH = 64
"""Maximum ``name`` length, in characters (portable Agent Skills subset)."""

_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
"""Lowercase ``a-z0-9-``, 1..64 chars, no leading/trailing/doubled hyphen."""


class InvalidSkillNameError(ValueError):
    """A caller-supplied Skill ``name`` fails the shared portable contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Skill name: {reason}")


def validate_skill_name(value: str) -> str:
    """Validate a Skill ``name`` against the portable Agent Skills subset.

    Returns ``value`` unchanged when valid. Never lowercases, trims, or
    otherwise rewrites an invalid value into a valid one -- an invalid
    ``name`` always raises :class:`InvalidSkillNameError`.
    """

    if not value:
        raise InvalidSkillNameError("name must not be empty")
    if len(value) > SKILL_NAME_MAX_LENGTH:
        raise InvalidSkillNameError(f"name must be at most {SKILL_NAME_MAX_LENGTH} characters")
    if not _SKILL_NAME_PATTERN.fullmatch(value):
        raise InvalidSkillNameError(
            "name must contain only lowercase a-z, 0-9, and single hyphens, "
            "and must not start or end with a hyphen"
        )
    return value
