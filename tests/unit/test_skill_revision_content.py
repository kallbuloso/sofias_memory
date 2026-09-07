from __future__ import annotations

import json
import re

import pytest

from sofias_memory.domain import (
    COMPATIBILITY_MAX_LENGTH,
    CONTENT_SHA256_PATTERN,
    DESCRIPTION_MAX_LENGTH,
    PROCEDURE_MAX_LENGTH,
    SOFIAS_MEMORY_TAGS_METADATA_KEY,
    InvalidSkillRevisionContentError,
    SkillRevisionContent,
    build_canonical_object,
    canonicalize_tags,
    compute_content_sha256,
    normalize_newlines,
    validate_compatibility,
    validate_declared_tools,
    validate_description,
    validate_metadata,
    validate_procedure,
)

# --- description ------------------------------------------------------


def test_description_boundary_accepted() -> None:
    assert validate_description("x") == "x"
    long_description = "x" * DESCRIPTION_MAX_LENGTH
    assert validate_description(long_description) == long_description


def test_description_boundary_rejected() -> None:
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_description("")
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_description("x" * (DESCRIPTION_MAX_LENGTH + 1))


# --- compatibility ------------------------------------------------------


def test_compatibility_none_passthrough() -> None:
    assert validate_compatibility(None) is None


def test_compatibility_boundary_accepted() -> None:
    assert validate_compatibility("x") == "x"
    long_value = "x" * COMPATIBILITY_MAX_LENGTH
    assert validate_compatibility(long_value) == long_value


def test_compatibility_boundary_rejected() -> None:
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_compatibility("")
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_compatibility("x" * (COMPATIBILITY_MAX_LENGTH + 1))


# --- procedure ------------------------------------------------------


def test_procedure_single_non_whitespace_char_accepted() -> None:
    assert validate_procedure("x") == "x"


def test_procedure_only_whitespace_rejected() -> None:
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_procedure("   \n\t  ")


def test_procedure_max_length_accepted() -> None:
    value = "x" * PROCEDURE_MAX_LENGTH
    assert validate_procedure(value) == value


def test_procedure_over_max_length_rejected() -> None:
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_procedure("x" * (PROCEDURE_MAX_LENGTH + 1))


def test_procedure_crlf_normalized_to_lf() -> None:
    assert validate_procedure("line1\r\nline2\r\n") == "line1\nline2\n"


def test_procedure_cr_normalized_to_lf() -> None:
    assert validate_procedure("line1\rline2\r") == "line1\nline2\n"


def test_procedure_markdown_whitespace_preserved() -> None:
    markdown = "# Title\n\n- item one\n  - nested item\n\n```\ncode block\n```\n"
    assert validate_procedure(markdown) == markdown


def test_normalize_newlines_is_the_only_transformation() -> None:
    assert normalize_newlines("a\r\nb\rc\nd") == "a\nb\nc\nd"


# --- metadata ------------------------------------------------------


def test_metadata_none_defaults_to_empty_dict() -> None:
    assert validate_metadata(None) == {}


def test_metadata_accepts_string_values() -> None:
    assert validate_metadata({"a": "1", "b": "2"}) == {"a": "1", "b": "2"}


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1},
        {"a": True},
        {"a": None},
        {"a": ["nested"]},
        {"a": {"nested": "object"}},
    ],
)
def test_metadata_rejects_non_string_values(value: dict[str, object]) -> None:
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_metadata(value)  # type: ignore[arg-type]


def test_metadata_rejects_reserved_tags_transport_key() -> None:
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_metadata({SOFIAS_MEMORY_TAGS_METADATA_KEY: '["pdf"]'})


def test_metadata_rejects_reserved_tags_transport_key_alongside_other_keys() -> None:
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_metadata({"a": "1", SOFIAS_MEMORY_TAGS_METADATA_KEY: '["pdf"]'})


# --- tags ------------------------------------------------------


def test_tags_none_defaults_to_empty_list() -> None:
    assert canonicalize_tags(None) == []


def test_tags_deduplicated_and_sorted() -> None:
    assert canonicalize_tags(["pdf", "forms", "pdf"]) == ["forms", "pdf"]


def test_tags_rejects_empty_string() -> None:
    with pytest.raises(InvalidSkillRevisionContentError):
        canonicalize_tags([""])


# --- declared_tools ------------------------------------------------------


def test_declared_tools_none_defaults_to_empty_list() -> None:
    assert validate_declared_tools(None) == []


def test_declared_tools_preserves_given_order() -> None:
    assert validate_declared_tools(["zeta", "alpha"]) == ["zeta", "alpha"]


def test_declared_tools_rejects_empty_string() -> None:
    with pytest.raises(InvalidSkillRevisionContentError):
        validate_declared_tools([""])


# --- canonical hash ------------------------------------------------------


def _content(**overrides: object) -> SkillRevisionContent:
    base = {
        "description": "A description.",
        "procedure": "Do the thing.",
        "license": None,
        "compatibility": None,
        "metadata": {},
        "tags": [],
        "declared_tools": [],
    }
    base.update(overrides)
    return SkillRevisionContent(**base)  # type: ignore[arg-type]


def test_content_sha256_format() -> None:
    digest = compute_content_sha256(name="pdf-tools", content=_content())
    assert re.fullmatch(CONTENT_SHA256_PATTERN, digest)


def test_content_sha256_stable_under_metadata_key_order() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(metadata={"a": "1", "b": "2"}))
    b = compute_content_sha256(name="pdf-tools", content=_content(metadata={"b": "2", "a": "1"}))
    assert a == b


def test_content_sha256_stable_under_crlf_vs_lf() -> None:
    a = compute_content_sha256(
        name="pdf-tools", content=_content(procedure=normalize_newlines("line1\r\nline2"))
    )
    b = compute_content_sha256(
        name="pdf-tools", content=_content(procedure=normalize_newlines("line1\nline2"))
    )
    assert a == b


def test_content_sha256_stable_under_tags_reordered_and_deduplicated() -> None:
    a = compute_content_sha256(
        name="pdf-tools", content=_content(tags=canonicalize_tags(["pdf", "forms"]))
    )
    b = compute_content_sha256(
        name="pdf-tools",
        content=_content(tags=canonicalize_tags(["forms", "pdf", "forms"])),
    )
    assert a == b


def test_content_sha256_stable_under_declared_tools_reordered() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(declared_tools=["a", "b"]))
    b = compute_content_sha256(name="pdf-tools", content=_content(declared_tools=["b", "a"]))
    assert a == b


def test_content_sha256_stable_under_omitted_vs_neutral_metadata() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(metadata=validate_metadata(None)))
    b = compute_content_sha256(name="pdf-tools", content=_content(metadata={}))
    assert a == b


def test_content_sha256_stable_under_omitted_vs_neutral_tags() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(tags=canonicalize_tags(None)))
    b = compute_content_sha256(name="pdf-tools", content=_content(tags=[]))
    assert a == b


def test_content_sha256_stable_under_omitted_vs_neutral_declared_tools() -> None:
    a = compute_content_sha256(
        name="pdf-tools", content=_content(declared_tools=validate_declared_tools(None))
    )
    b = compute_content_sha256(name="pdf-tools", content=_content(declared_tools=[]))
    assert a == b


def test_content_sha256_stable_under_omitted_vs_neutral_license() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(license=None))
    b = compute_content_sha256(name="pdf-tools", content=_content(license=None))
    assert a == b


def test_content_sha256_stable_under_omitted_vs_neutral_compatibility() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(compatibility=None))
    b = compute_content_sha256(name="pdf-tools", content=_content(compatibility=None))
    assert a == b


def test_content_sha256_changes_when_description_changes() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(description="one"))
    b = compute_content_sha256(name="pdf-tools", content=_content(description="two"))
    assert a != b


def test_content_sha256_changes_when_procedure_changes() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(procedure="one"))
    b = compute_content_sha256(name="pdf-tools", content=_content(procedure="two"))
    assert a != b


def test_content_sha256_changes_when_metadata_changes() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(metadata={"a": "1"}))
    b = compute_content_sha256(name="pdf-tools", content=_content(metadata={"a": "2"}))
    assert a != b


def test_content_sha256_changes_when_tags_change() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(tags=["a"]))
    b = compute_content_sha256(name="pdf-tools", content=_content(tags=["b"]))
    assert a != b


def test_content_sha256_changes_when_declared_tools_change() -> None:
    a = compute_content_sha256(name="pdf-tools", content=_content(declared_tools=["a"]))
    b = compute_content_sha256(name="pdf-tools", content=_content(declared_tools=["b"]))
    assert a != b


def test_content_sha256_scoped_hash_may_collide_across_different_skills() -> None:
    # Uniqueness of content_sha256 is enforced only within one Skill
    # (UNIQUE(skill_id, content_sha256)) -- the hash function itself has no
    # opinion on cross-Skill collisions, and two different Skills with
    # identical content legitimately produce the same digest.
    a = compute_content_sha256(name="skill-a", content=_content())
    b = compute_content_sha256(name="skill-a", content=_content())
    assert a == b


def test_build_canonical_object_has_exactly_eight_frozen_keys() -> None:
    canonical = build_canonical_object(name="pdf-tools", content=_content())
    assert set(canonical) == {
        "name",
        "description",
        "procedure",
        "license",
        "compatibility",
        "metadata",
        "tags",
        "declared_tools",
    }


def test_build_canonical_object_sorts_declared_tools_without_mutating_content() -> None:
    content = _content(declared_tools=["zeta", "alpha"])
    canonical = build_canonical_object(name="pdf-tools", content=content)

    assert canonical["declared_tools"] == ["alpha", "zeta"]
    assert content.declared_tools == ["zeta", "alpha"]


# --- cross-surface hash proof (structured vs. simulated SKILL.md import) ---


def _simulate_import_normalization(
    raw_metadata: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Stand-in for the SM-703 import step's consume-then-strip contract
    (Feature Contract SS 12.6) -- no YAML/SKILL.md parser here, just the
    same "extract reserved key, remove it, feed the rest through the
    shared primitives" shape a real parser will use. Never itself exported
    from ``sofias_memory.domain`` -- SM-701 builds no import mode."""

    remaining = dict(raw_metadata)
    raw_tags = remaining.pop(SOFIAS_MEMORY_TAGS_METADATA_KEY, None)
    tags = json.loads(raw_tags) if raw_tags is not None else None
    return validate_metadata(remaining), canonicalize_tags(tags)


def test_structured_and_simulated_import_produce_the_same_canonical_hash() -> None:
    # Structured API surface: caller uses tags=[...] directly.
    structured_metadata = validate_metadata({})
    structured_tags = canonicalize_tags(["pdf", "forms"])
    structured_hash = compute_content_sha256(
        name="pdf-tools",
        content=_content(metadata=structured_metadata, tags=structured_tags),
    )

    # SKILL.md transport surface: tags travel inside metadata under the
    # reserved key, as a JSON-array-encoded string -- consumed and removed
    # before it ever reaches validate_metadata/persistence.
    imported_metadata, imported_tags = _simulate_import_normalization(
        {SOFIAS_MEMORY_TAGS_METADATA_KEY: json.dumps(["pdf", "forms"])}
    )
    imported_hash = compute_content_sha256(
        name="pdf-tools",
        content=_content(metadata=imported_metadata, tags=imported_tags),
    )

    assert imported_metadata == {}
    assert imported_tags == ["forms", "pdf"]
    assert structured_metadata == imported_metadata
    assert structured_tags == imported_tags
    assert structured_hash == imported_hash


def test_reserved_key_never_survives_import_normalization_into_semantic_metadata() -> None:
    metadata, _tags = _simulate_import_normalization(
        {"a": "1", SOFIAS_MEMORY_TAGS_METADATA_KEY: json.dumps(["pdf"])}
    )

    assert SOFIAS_MEMORY_TAGS_METADATA_KEY not in metadata
    assert metadata == {"a": "1"}
