"""Shared Native Cognitive Memory content/confidence/provenance validation
primitives (ADR-0016, Feature Contract v0.7.0 Native Cognitive Memory).

Every Cognitive Memory entry point (SM-1002+) must validate content,
confidence, and provenance-by-origin with exactly these rules, so no
service/route may implement its own variant. This module has no dependency
on FastAPI, SQLAlchemy, or Neo4j.

Provenance-by-origin rules (Feature Contract SS 7.3) apply only to the full
provenance of a non-FORGOTTEN item. They are deliberately application-level
validation here, never a PostgreSQL CHECK constraint on
``memory_provenance``: a CHECK enforcing "turn_uuid or source_ref required"
would make the Forget-approved scrub (origin_kind + source_system
preserved, every external ref set to NULL) structurally impossible
(ADR-0016 SS 13, Feature Contract SS 16.2).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sofias_memory.domain.enums import CognitiveMemoryOriginKind

COGNITIVE_MEMORY_CONTENT_MAX_LENGTH = 16384
COGNITIVE_MEMORY_SOURCE_SYSTEM_MAX_LENGTH = 64
COGNITIVE_MEMORY_EXTERNAL_REF_MAX_LENGTH = 255

_SOURCE_SYSTEM_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class InvalidCognitiveMemoryContentError(ValueError):
    """Caller-supplied Cognitive Memory ``content`` fails the frozen contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Cognitive Memory content: {reason}")


class InvalidCognitiveMemoryConfidenceError(ValueError):
    """Caller-supplied Cognitive Memory ``confidence`` fails the frozen contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Cognitive Memory confidence: {reason}")


class InvalidCognitiveMemoryProvenanceError(ValueError):
    """Caller-supplied Cognitive Memory provenance fails the frozen contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Cognitive Memory provenance: {reason}")


class InvalidCognitiveMemoryTimestampError(ValueError):
    """A caller-supplied Cognitive Memory timestamp is not timezone-aware."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Cognitive Memory timestamp: {reason}")


class InvalidCognitiveMemoryValidityWindowError(ValueError):
    """``valid_from``/``valid_until`` fail the frozen ordering contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Cognitive Memory validity window: {reason}")


def normalize_newlines(value: str) -> str:
    """``\\r\\n`` and bare ``\\r`` -> ``\\n``. No other transformation here."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def normalize_cognitive_memory_content(value: str) -> str:
    """CRLF/CR -> LF, then trim edges only, then require 1..16384 Unicode
    characters (Feature Contract SS 6.1). Trimming both borders guarantees
    at least one non-whitespace character remains whenever the result is
    non-empty, so no separate "must contain non-whitespace" check is needed
    after this normalization.
    """

    normalized = normalize_newlines(value).strip()
    if not (1 <= len(normalized) <= COGNITIVE_MEMORY_CONTENT_MAX_LENGTH):
        raise InvalidCognitiveMemoryContentError(
            f"content must be 1..{COGNITIVE_MEMORY_CONTENT_MAX_LENGTH} Unicode "
            "characters after normalization"
        )
    return normalized


def validate_cognitive_memory_confidence(
    value: float | None, *, origin_kind: CognitiveMemoryOriginKind
) -> float | None:
    """``None`` is allowed except when ``origin_kind`` is ``INFERRED``, where
    it is required (Feature Contract SS 6.2/7.3). Never synthesizes a value
    (e.g. ``USER_ASSERTED`` never implies ``confidence=1.0``)."""

    if value is not None and not (0.0 <= value <= 1.0):
        raise InvalidCognitiveMemoryConfidenceError("confidence must be between 0.0 and 1.0")
    if value is None and origin_kind is CognitiveMemoryOriginKind.INFERRED:
        raise InvalidCognitiveMemoryConfidenceError(
            "confidence is required when origin_kind is 'inferred'"
        )
    return value


def validate_cognitive_memory_source_system(value: str) -> str:
    """1..64 chars, lowercase ``a-z0-9-``, no leading/trailing/doubled hyphen
    (Feature Contract SS 7.2). A system slug only -- never a resource
    reference."""

    if not (1 <= len(value) <= COGNITIVE_MEMORY_SOURCE_SYSTEM_MAX_LENGTH):
        raise InvalidCognitiveMemoryProvenanceError(
            f"source_system must be 1..{COGNITIVE_MEMORY_SOURCE_SYSTEM_MAX_LENGTH} characters"
        )
    if not _SOURCE_SYSTEM_PATTERN.fullmatch(value):
        raise InvalidCognitiveMemoryProvenanceError(
            "source_system must contain only lowercase a-z, 0-9, and single "
            "hyphens, and must not start or end with a hyphen"
        )
    return value


def validate_cognitive_memory_external_ref(value: str) -> str:
    """Opaque external reference (``confirmation_ref``/``source_ref``):
    trimmed, 1..255 characters, never a content blob (Feature Contract
    SS 7.2)."""

    trimmed = value.strip()
    if not (1 <= len(trimmed) <= COGNITIVE_MEMORY_EXTERNAL_REF_MAX_LENGTH):
        raise InvalidCognitiveMemoryProvenanceError(
            f"external reference must be 1..{COGNITIVE_MEMORY_EXTERNAL_REF_MAX_LENGTH} characters"
        )
    return trimmed


def normalize_cognitive_memory_timestamp(value: datetime) -> datetime:
    """Requires a timezone-aware value, then normalizes it to UTC (Feature
    Contract SS 7.2/general codebase UTC-timestamp convention). Used for
    ``valid_from``/``valid_until``/``observed_at`` -- never silently treats a
    naive timestamp as UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidCognitiveMemoryTimestampError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def validate_cognitive_memory_validity_window(
    valid_from: datetime | None, valid_until: datetime | None
) -> None:
    """``valid_from`` is inclusive, ``valid_until`` is exclusive; when both
    are present, ``valid_until`` must be strictly after ``valid_from``
    (Feature Contract SS 9, mirrored by the ``validity_window_ordered``
    PostgreSQL CHECK constraint)."""

    if valid_from is not None and valid_until is not None and valid_until <= valid_from:
        raise InvalidCognitiveMemoryValidityWindowError(
            "valid_until must be strictly after valid_from"
        )


def validate_cognitive_memory_provenance_origin_requirements(
    *,
    origin_kind: CognitiveMemoryOriginKind,
    turn_uuid: object | None,
    task_uuid: object | None,
    source_ref: str | None,
    observed_at: object | None,
) -> None:
    """Minimum required fields per ``origin_kind`` beyond ``source_system``
    (Feature Contract SS 7.3). Applies only to the full provenance of a
    non-FORGOTTEN item -- never re-applied to an already-scrubbed tombstone.
    """

    if origin_kind is CognitiveMemoryOriginKind.USER_ASSERTED:
        if turn_uuid is None and source_ref is None:
            raise InvalidCognitiveMemoryProvenanceError(
                "user_asserted provenance requires turn_uuid or source_ref"
            )
    elif origin_kind is CognitiveMemoryOriginKind.TOOL_OBSERVED:
        if source_ref is None or observed_at is None:
            raise InvalidCognitiveMemoryProvenanceError(
                "tool_observed provenance requires source_ref and observed_at"
            )
    elif origin_kind is CognitiveMemoryOriginKind.IMPORTED:
        if source_ref is None:
            raise InvalidCognitiveMemoryProvenanceError("imported provenance requires source_ref")
    elif origin_kind is CognitiveMemoryOriginKind.INFERRED:
        if turn_uuid is None and task_uuid is None and source_ref is None:
            raise InvalidCognitiveMemoryProvenanceError(
                "inferred provenance requires turn_uuid, task_uuid, or source_ref"
            )
    elif (
        origin_kind is CognitiveMemoryOriginKind.ASSISTANT_GENERATED
        and turn_uuid is None
        and task_uuid is None
        and source_ref is None
    ):
        raise InvalidCognitiveMemoryProvenanceError(
            "assistant_generated provenance requires turn_uuid, task_uuid, or source_ref"
        )
