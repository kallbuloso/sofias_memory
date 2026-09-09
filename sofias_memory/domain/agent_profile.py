"""Shared Agent Profile field validation primitives.

ADR-0014 and the v0.5.0 Agent Management Feature Contract SS 4 require
every Agent-Profile-aware entry point (structured create, PATCH -- SM-802)
to validate ``display_name``/``description``/``instructions`` with exactly
these rules, so no endpoint may implement its own variant. This module has
no dependency on FastAPI, SQLAlchemy, or Neo4j.

Deliberately does not import from ``skill_revision_content.py``: that module
is explicitly SkillRevision domain code (ADR-0013), not a neutral
cross-feature primitive, even though ``normalize_newlines`` there happens to
be a pure string transform. Coupling Agent to it would tie Agent Profile
normalization to a module that may evolve for Skill-specific reasons having
nothing to do with Agent. :func:`normalize_agent_newlines` below is the
Agent-owned equivalent -- duplicated, not imported, and not shared back into
Skill either.

Ambiguity resolution note (SM-801): the Feature Contract states
``description`` as "1..1024 Unicode chars quando presente" with no trim and
no explicit nonblank-after-trim requirement, unlike ``display_name`` (which
explicitly orders a trim + nonblank-after-trim) and ``instructions`` (which
explicitly orders a nonblank-after-newline-normalization check). Per the
SM-801 ticket's own instruction to resolve real ambiguity conservatively --
preserving the contract rather than inventing a new policy -- ``description``
is validated as a pure length check on the value as supplied: a
whitespace-only string within 1..1024 characters is accepted, exactly as
literally written, rather than an unwritten nonblank rule being added here.
"""

from __future__ import annotations

AGENT_DISPLAY_NAME_MAX_LENGTH = 120
AGENT_DESCRIPTION_MAX_LENGTH = 1024
AGENT_INSTRUCTIONS_MAX_LENGTH = 65_536


class InvalidAgentProfileError(ValueError):
    """A caller-supplied Agent Profile field fails the shared contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Agent profile: {reason}")


def validate_agent_display_name(value: str | None) -> str | None:
    """``None`` -> ``None``. Otherwise trimmed; the trimmed value must be
    non-empty and at most :data:`AGENT_DISPLAY_NAME_MAX_LENGTH` characters
    (Feature Contract SS 4.1). ``display_name`` is a mutable human label,
    never identity -- trimming it is deliberate, unlike ``Agent.name``
    (:func:`~sofias_memory.domain.agent_name.validate_agent_name`), which
    never trims."""

    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise InvalidAgentProfileError("display_name must not be empty or whitespace-only")
    if len(stripped) > AGENT_DISPLAY_NAME_MAX_LENGTH:
        raise InvalidAgentProfileError(
            f"display_name must be at most {AGENT_DISPLAY_NAME_MAX_LENGTH} characters"
        )
    return stripped


def validate_agent_description(value: str | None) -> str | None:
    """``None`` -> ``None``. Otherwise 1..1024 Unicode characters, no other
    transformation (Feature Contract SS 4.2). No trim and no nonblank rule
    beyond the length bound is applied -- see the module docstring's
    ambiguity resolution note."""

    if value is None:
        return None
    if not (1 <= len(value) <= AGENT_DESCRIPTION_MAX_LENGTH):
        raise InvalidAgentProfileError(
            f"description must be 1..{AGENT_DESCRIPTION_MAX_LENGTH} characters when present"
        )
    return value


def normalize_agent_newlines(value: str) -> str:
    """``\\r\\n`` and bare ``\\r`` -> ``\\n``. No other transformation --
    significant whitespace (Markdown list indentation, code blocks, etc.) in
    ``instructions`` is never trimmed or otherwise altered (Feature Contract
    SS 4.3). Agent-owned: deliberately not imported from
    ``skill_revision_content.normalize_newlines`` -- see the module
    docstring."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def normalize_validate_agent_instructions(value: str | None) -> str | None:
    """``None`` -> ``None``. Otherwise newline-normalized (``\\r\\n``/``\\r``
    -> ``\\n``), then required to be 1..65536 characters with at least one
    non-whitespace character (Feature Contract SS 4.3). Never trims
    persisted meaningful whitespace beyond newline normalization."""

    if value is None:
        return None
    normalized = normalize_agent_newlines(value)
    if len(normalized) > AGENT_INSTRUCTIONS_MAX_LENGTH:
        raise InvalidAgentProfileError(
            f"instructions must be at most {AGENT_INSTRUCTIONS_MAX_LENGTH} characters"
        )
    if not normalized.strip():
        raise InvalidAgentProfileError(
            "instructions must contain at least one non-whitespace character"
        )
    return normalized
