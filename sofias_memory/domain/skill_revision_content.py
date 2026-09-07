"""Shared SkillRevision content validation and canonical hashing primitives.

ADR-0013 and the v0.4.0 Skills Feature Contract SS 4.2/6/12.6/12.7/12.8
require every SkillRevision-aware entry point (structured create, SKILL.md
import) to validate and canonicalize revision content with exactly these
rules, so no endpoint may implement its own variant. This module has no
dependency on FastAPI, SQLAlchemy, or Neo4j.

Two distinct concerns live here, kept in one module because they share the
same frozen field contract and must never drift apart:

1. Field validation/normalization (SS 4.2/12.8) -- applied once, at write
   time, and used for both storage and (for ``tags``) hashing.
2. Canonical representation and ``content_sha256`` (SS 12.7) -- a pure
   function of already-validated content, used only for hashing/safe-replay
   detection, never for storage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

DESCRIPTION_MAX_LENGTH = 1024
COMPATIBILITY_MAX_LENGTH = 500
PROCEDURE_MAX_LENGTH = 65_536

CONTENT_SHA256_PATTERN = "^[0-9a-f]{64}$"
"""Lowercase-only: matches :func:`hashlib.sha256(...).hexdigest()` output."""

SOFIAS_MEMORY_TAGS_METADATA_KEY = "sofias-memory.tags"
"""Reserved SKILL.md *transport* key (Feature Contract SS 12.6): the
JSON-array-encoded string a future SM-703 import/export uses to round-trip
``tags`` through ``metadata["sofias-memory.tags"]`` in the portable
``map<string,string>`` shape a SKILL.md frontmatter allows. It is never part
of the semantic, persisted ``metadata`` a SkillRevision actually stores --
``tags`` is the single source of truth for tag data. A future import step
must consume (parse and discard) this key before calling
:func:`validate_metadata`; :func:`validate_metadata` itself rejects it if
still present, so a caller can never accidentally persist two competing
representations of the same tags."""


class InvalidSkillRevisionContentError(ValueError):
    """Caller-supplied SkillRevision content fails the shared contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid SkillRevision content: {reason}")


def normalize_newlines(value: str) -> str:
    """``\\r\\n`` and bare ``\\r`` -> ``\\n``. No other transformation --
    significant Markdown whitespace (list indentation, code blocks) is never
    trimmed or otherwise altered (Feature Contract SS 12.8)."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def validate_description(value: str) -> str:
    """1..1024 Unicode characters, no other transformation."""

    if not (1 <= len(value) <= DESCRIPTION_MAX_LENGTH):
        raise InvalidSkillRevisionContentError(
            f"description must be 1..{DESCRIPTION_MAX_LENGTH} characters"
        )
    return value


def validate_compatibility(value: str | None) -> str | None:
    """``None`` -> ``None``. Otherwise 1..500 Unicode characters."""

    if value is None:
        return None
    if not (1 <= len(value) <= COMPATIBILITY_MAX_LENGTH):
        raise InvalidSkillRevisionContentError(
            f"compatibility must be 1..{COMPATIBILITY_MAX_LENGTH} characters when present"
        )
    return value


def validate_procedure(value: str) -> str:
    """Newline-normalize, then require 1..65536 characters with at least one
    non-whitespace character. Never trims/strips the persisted content --
    only ``\\r\\n``/``\\r`` -> ``\\n`` newline normalization is applied."""

    normalized = normalize_newlines(value)
    if len(normalized) > PROCEDURE_MAX_LENGTH:
        raise InvalidSkillRevisionContentError(
            f"procedure must be at most {PROCEDURE_MAX_LENGTH} characters"
        )
    if not normalized.strip():
        raise InvalidSkillRevisionContentError(
            "procedure must contain at least one non-whitespace character"
        )
    return normalized


def validate_metadata(value: dict[str, str] | None) -> dict[str, str]:
    """``None`` -> ``{}``. Otherwise every value must already be ``str`` --
    nested objects, arrays, numbers, and booleans are rejected, never
    coerced (Feature Contract SS 12.3: ``metadata`` is ``map<string,string>``
    on the entire public surface).

    Rejects :data:`SOFIAS_MEMORY_TAGS_METADATA_KEY` if present -- it is a
    SKILL.md transport-only key, never part of semantic persisted metadata.
    A future SM-703 import step must consume and remove it *before* calling
    this function; a structured caller must use ``tags=[...]`` directly and
    never smuggle it through ``metadata`` (Feature Contract SS 12.6)."""

    if value is None:
        return {}
    if SOFIAS_MEMORY_TAGS_METADATA_KEY in value:
        raise InvalidSkillRevisionContentError(
            f"metadata must not contain the reserved transport key "
            f"{SOFIAS_MEMORY_TAGS_METADATA_KEY!r}; use tags=[...] instead"
        )
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise InvalidSkillRevisionContentError(
                "metadata must be a map of string keys to string values"
            )
    return dict(value)


def canonicalize_tags(value: list[str] | None) -> list[str]:
    """``None`` -> ``[]``. Otherwise deduplicated and lexicographically
    sorted -- applied once, at write time, and used identically for storage,
    API responses, and ``content_sha256`` (Feature Contract SS 12.6)."""

    if value is None:
        return []
    for item in value:
        if not isinstance(item, str) or not item:
            raise InvalidSkillRevisionContentError("tags must be non-empty strings")
    return sorted(set(value))


def validate_declared_tools(value: list[str] | None) -> list[str]:
    """``None`` -> ``[]``. Otherwise validated but left in the order the
    caller supplied -- storage/API preserve write order; only the canonical
    hash representation sorts a copy (Feature Contract SS 12.2)."""

    if value is None:
        return []
    for item in value:
        if not isinstance(item, str) or not item:
            raise InvalidSkillRevisionContentError("declared_tools must be non-empty strings")
    return list(value)


@dataclass(frozen=True, slots=True)
class SkillRevisionContent:
    """Already-validated/normalized SkillRevision content, ready to persist
    or to canonicalize for hashing. Never partially valid -- construct only
    through the ``validate_*``/``canonicalize_*`` functions above."""

    description: str
    procedure: str
    license: str | None
    compatibility: str | None
    metadata: dict[str, str]
    tags: list[str]
    declared_tools: list[str]


def build_canonical_object(*, name: str, content: SkillRevisionContent) -> dict[str, object]:
    """The frozen eight-key canonical object used for ``content_sha256``
    (Feature Contract SS 12.7.1). ``declared_tools`` is sorted here as a
    *copy*, solely for hashing -- ``content.declared_tools`` (storage order)
    is never mutated."""

    return {
        "name": name,
        "description": content.description,
        "procedure": content.procedure,
        "license": content.license,
        "compatibility": content.compatibility,
        "metadata": content.metadata,
        "tags": content.tags,
        "declared_tools": sorted(content.declared_tools),
    }


def compute_content_sha256(*, name: str, content: SkillRevisionContent) -> str:
    """SHA-256 hex digest of the canonical UTF-8 JSON representation
    (``sort_keys=True``, compact stable separators) -- Feature Contract
    SS 12.7. Same semantic content with different dict insertion order,
    CRLF vs LF ``procedure``, or differently-ordered ``tags``/
    ``declared_tools`` input always produces the same digest."""

    canonical_object = build_canonical_object(name=name, content=content)
    canonical_json = json.dumps(
        canonical_object,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
