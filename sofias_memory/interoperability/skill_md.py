"""Standalone SKILL.md parsing/serialization (ADR-0013, Feature Contract
v0.4.0 Skills SS 12).

The Sofias Memory subset is deliberately smaller than the full Agent Skills
format: no bundles, no ``scripts/``/``references/``/``assets/``, and the
frontmatter accepts exactly six top-level fields (SS 12.1/12.9).

This module converges immediately to the same domain primitives the
structured API uses (``sofias_memory.domain``) -- it never re-implements a
second, parallel validation or canonicalization path. Parsing produces a
:class:`ParsedSkillDocument` wrapping an already-validated
:class:`~sofias_memory.domain.SkillRevisionContent`, ready for the exact
same ``create_skill_aggregate``/``create_skill_revision`` primitives the
structured create/create-revision paths call (SM-701/SM-702).

YAML parsing uses ``yaml.safe_load`` exclusively -- never ``yaml.load``,
``FullLoader``, or ``UnsafeLoader``, and never constructs arbitrary Python
objects from caller-supplied content (untrusted-ingestion discipline,
AGENTS.md SS19).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import yaml

from sofias_memory.domain import (
    SOFIAS_MEMORY_TAGS_METADATA_KEY,
    InvalidSkillNameError,
    InvalidSkillRevisionContentError,
    SkillRevisionContent,
    canonicalize_tags,
    normalize_newlines,
    validate_compatibility,
    validate_declared_tools,
    validate_description,
    validate_metadata,
    validate_procedure,
    validate_skill_name,
)

FRONTMATTER_DELIMITER = "---"

ALLOWED_TOP_LEVEL_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
"""Feature Contract SS 12.1/12.9 -- any other top-level field (``tags``,
``tools``, ``scripts``, ``references``, ``assets``, ``version``, ...) is
rejected outright, never silently dropped. ``metadata.version`` remains
allowed: it is nested inside ``metadata``, a plain string value."""


class InvalidSkillMarkdownError(ValueError):
    """A standalone ``SKILL.md`` document fails parsing or the portable
    subset's validation contract. The single exception type the
    import/export service layer needs to catch and translate to
    ``422 INVALID_REQUEST`` -- callers never need to know whether the
    underlying cause was a YAML syntax error, a structural frontmatter
    problem, or a domain field-validation failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid SKILL.md document: {reason}")


@dataclass(frozen=True, slots=True)
class ParsedSkillDocument:
    """An already-validated standalone ``SKILL.md`` document, converged to
    the exact same domain representation the structured API persists."""

    name: str
    content: SkillRevisionContent


def _split_frontmatter(normalized: str) -> tuple[str, str]:
    lines = normalized.split("\n")
    if not lines or lines[0] != FRONTMATTER_DELIMITER:
        raise InvalidSkillMarkdownError("document must start with a '---' frontmatter delimiter")

    closing_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index] == FRONTMATTER_DELIMITER:
            closing_index = index
            break
    if closing_index is None:
        raise InvalidSkillMarkdownError("document is missing the closing '---' delimiter")

    yaml_text = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1 :])
    return yaml_text, body


def _load_frontmatter_mapping(yaml_text: str) -> dict[str, object]:
    try:
        loaded = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise InvalidSkillMarkdownError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise InvalidSkillMarkdownError("frontmatter must be a YAML mapping")
    unknown = set(loaded) - ALLOWED_TOP_LEVEL_FIELDS
    if unknown:
        raise InvalidSkillMarkdownError(f"unknown top-level field(s): {sorted(unknown)}")
    return loaded


def _require_string(frontmatter: dict[str, object], field: str) -> str:
    value = frontmatter.get(field)
    if not isinstance(value, str):
        raise InvalidSkillMarkdownError(f"{field!r} must be a string")
    return value


def _optional_string(frontmatter: dict[str, object], field: str) -> str | None:
    if field not in frontmatter or frontmatter[field] is None:
        return None
    value = frontmatter[field]
    if not isinstance(value, str):
        raise InvalidSkillMarkdownError(f"{field!r} must be a string when present")
    return value


def _extract_metadata_mapping(frontmatter: dict[str, object]) -> dict[str, str]:
    raw = frontmatter.get("metadata")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidSkillMarkdownError("'metadata' must be a YAML mapping")
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise InvalidSkillMarkdownError("'metadata' must be a map of string keys to strings")
    return dict(raw)


def _extract_tags(metadata: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Consume and remove the reserved transport key (Feature Contract
    SS 12.6) -- it never reaches :func:`validate_metadata`, and never
    persists as part of semantic ``metadata``."""

    if SOFIAS_MEMORY_TAGS_METADATA_KEY not in metadata:
        return [], metadata

    raw_value = metadata[SOFIAS_MEMORY_TAGS_METADATA_KEY]
    try:
        parsed_value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise InvalidSkillMarkdownError(
            f"metadata[{SOFIAS_MEMORY_TAGS_METADATA_KEY!r}] must be a JSON array of strings"
        ) from exc
    if not isinstance(parsed_value, list) or not all(
        isinstance(item, str) for item in parsed_value
    ):
        raise InvalidSkillMarkdownError(
            f"metadata[{SOFIAS_MEMORY_TAGS_METADATA_KEY!r}] must be a JSON array of strings"
        )

    remaining = {
        key: value for key, value in metadata.items() if key != SOFIAS_MEMORY_TAGS_METADATA_KEY
    }
    return parsed_value, remaining


def _tokenize_allowed_tools(value: str | None) -> list[str]:
    if value is None:
        return []
    return value.split()


def parse_skill_markdown(raw_content: str) -> ParsedSkillDocument:
    """Parse a standalone ``SKILL.md`` document into an already-validated
    :class:`ParsedSkillDocument`. Raises :class:`InvalidSkillMarkdownError`
    for any structural or field-level problem -- never a raw
    ``yaml.YAMLError``, ``json.JSONDecodeError``, or domain exception."""

    normalized = normalize_newlines(raw_content)
    yaml_text, body = _split_frontmatter(normalized)
    frontmatter = _load_frontmatter_mapping(yaml_text)

    raw_metadata = _extract_metadata_mapping(frontmatter)
    raw_tags, remaining_metadata = _extract_tags(raw_metadata)
    declared_tools = _tokenize_allowed_tools(_optional_string(frontmatter, "allowed-tools"))

    try:
        name = validate_skill_name(_require_string(frontmatter, "name"))
        description = validate_description(_require_string(frontmatter, "description"))
        license_ = _optional_string(frontmatter, "license")
        compatibility = validate_compatibility(_optional_string(frontmatter, "compatibility"))
        metadata = validate_metadata(remaining_metadata)
        tags = canonicalize_tags(raw_tags)
        declared_tools = validate_declared_tools(declared_tools)
        procedure = validate_procedure(body)
    except (InvalidSkillNameError, InvalidSkillRevisionContentError) as exc:
        raise InvalidSkillMarkdownError(str(exc)) from exc

    content = SkillRevisionContent(
        description=description,
        procedure=procedure,
        license=license_,
        compatibility=compatibility,
        metadata=metadata,
        tags=tags,
        declared_tools=declared_tools,
    )
    return ParsedSkillDocument(name=name, content=content)


_YAML_DUMP_WIDTH = 1_000_000
"""Effectively disables PyYAML's automatic line-wrapping (default width 80)
-- a folded plain scalar re-parses to the same string, but export must
still be byte-identical across repeated calls without relying on that
folding round-tripping perfectly for arbitrary content."""


def _synthesize_export_metadata(metadata: dict[str, str], tags: list[str]) -> dict[str, str]:
    combined = dict(metadata)
    if tags:
        combined[SOFIAS_MEMORY_TAGS_METADATA_KEY] = json.dumps(
            tags, ensure_ascii=False, separators=(",", ":")
        )
    return dict(sorted(combined.items()))


def serialize_skill_markdown(*, name: str, content: SkillRevisionContent) -> str:
    """Deterministic canonical ``SKILL.md`` serialization of already-
    persisted semantic content. The same ``(name, content)`` always
    produces byte-identical output -- never a promise of byte-identity
    with whatever ``SKILL.md`` was originally imported (Feature Contract
    SS 12.5/12.7)."""

    frontmatter: dict[str, object] = {"name": name, "description": content.description}
    if content.license is not None:
        frontmatter["license"] = content.license
    if content.compatibility is not None:
        frontmatter["compatibility"] = content.compatibility
    metadata_out = _synthesize_export_metadata(content.metadata, content.tags)
    if metadata_out:
        frontmatter["metadata"] = metadata_out
    if content.declared_tools:
        frontmatter["allowed-tools"] = " ".join(content.declared_tools)

    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        line_break="\n",
        width=_YAML_DUMP_WIDTH,
    )
    return f"{FRONTMATTER_DELIMITER}\n{yaml_text}{FRONTMATTER_DELIMITER}\n{content.procedure}"
