"""Unit tests for standalone SKILL.md parsing (SM-703, Feature Contract SS
12). No PostgreSQL, no embedding provider -- pure parsing/validation."""

from __future__ import annotations

import pytest

from sofias_memory.domain import PROCEDURE_MAX_LENGTH, SKILL_NAME_MAX_LENGTH
from sofias_memory.interoperability.skill_md import (
    InvalidSkillMarkdownError,
    parse_skill_markdown,
)


def doc(frontmatter: str, body: str = "Do the thing.") -> str:
    return f"---\n{frontmatter}\n---\n{body}"


def test_minimal_valid_document() -> None:
    parsed = parse_skill_markdown(doc("name: deploy-app\ndescription: Deploys the app."))
    assert parsed.name == "deploy-app"
    assert parsed.content.description == "Deploys the app."
    assert parsed.content.procedure == "Do the thing."
    assert parsed.content.license is None
    assert parsed.content.compatibility is None
    assert parsed.content.metadata == {}
    assert parsed.content.tags == []
    assert parsed.content.declared_tools == []


def test_all_optional_fields_present() -> None:
    frontmatter = (
        "name: deploy-app\n"
        "description: Deploys the app.\n"
        "license: MIT\n"
        "compatibility: bash>=4\n"
        "metadata:\n"
        "  author: example\n"
        '  sofias-memory.tags: \'["pdf","forms"]\'\n'
        "allowed-tools: Bash(git:*) Read"
    )
    parsed = parse_skill_markdown(doc(frontmatter))
    assert parsed.content.license == "MIT"
    assert parsed.content.compatibility == "bash>=4"
    assert parsed.content.metadata == {"author": "example"}
    assert parsed.content.tags == ["forms", "pdf"]
    assert parsed.content.declared_tools == ["Bash(git:*)", "Read"]


def test_crlf_document_is_normalized() -> None:
    raw = "---\r\nname: deploy-app\r\ndescription: d\r\n---\r\nline one\r\nline two"
    parsed = parse_skill_markdown(raw)
    assert parsed.content.procedure == "line one\nline two"


def test_unicode_content_is_preserved() -> None:
    parsed = parse_skill_markdown(doc("name: deploy-app\ndescription: Faz o deploy da aplicação."))
    assert parsed.content.description == "Faz o deploy da aplicação."


def test_metadata_must_be_string_map() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc("name: deploy-app\ndescription: d\nmetadata:\n  count: 1"))


def test_reserved_tags_key_is_extracted_and_removed_from_metadata() -> None:
    frontmatter = (
        'name: deploy-app\ndescription: d\nmetadata:\n  sofias-memory.tags: \'["b","a","a"]\''
    )
    parsed = parse_skill_markdown(doc(frontmatter))
    assert parsed.content.tags == ["a", "b"]
    assert "sofias-memory.tags" not in parsed.content.metadata
    assert parsed.content.metadata == {}


def test_reserved_tags_key_malformed_json_is_rejected() -> None:
    frontmatter = "name: deploy-app\ndescription: d\nmetadata:\n  sofias-memory.tags: not-json"
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc(frontmatter))


def test_reserved_tags_key_non_array_json_is_rejected() -> None:
    frontmatter = "name: deploy-app\ndescription: d\nmetadata:\n  sofias-memory.tags: '{\"a\":1}'"
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc(frontmatter))


def test_reserved_tags_key_array_with_non_string_is_rejected() -> None:
    frontmatter = "name: deploy-app\ndescription: d\nmetadata:\n  sofias-memory.tags: '[\"a\",1]'"
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc(frontmatter))


def test_tags_dedupe_and_sort() -> None:
    frontmatter = (
        'name: deploy-app\ndescription: d\nmetadata:\n  sofias-memory.tags: \'["z","a","z"]\''
    )
    parsed = parse_skill_markdown(doc(frontmatter))
    assert parsed.content.tags == ["a", "z"]


def test_allowed_tools_mapping_preserves_order() -> None:
    parsed = parse_skill_markdown(
        doc("name: deploy-app\ndescription: d\nallowed-tools: Read Bash(git:*) Write")
    )
    assert parsed.content.declared_tools == ["Read", "Bash(git:*)", "Write"]


def test_allowed_tools_absent_yields_empty_list() -> None:
    parsed = parse_skill_markdown(doc("name: deploy-app\ndescription: d"))
    assert parsed.content.declared_tools == []


def test_unknown_top_level_field_is_rejected() -> None:
    for field in ("tags", "tools", "scripts", "references", "assets", "version"):
        with pytest.raises(InvalidSkillMarkdownError):
            parse_skill_markdown(doc(f"name: deploy-app\ndescription: d\n{field}: x"))


def test_metadata_version_is_allowed_because_nested() -> None:
    parsed = parse_skill_markdown(
        doc("name: deploy-app\ndescription: d\nmetadata:\n  version: '1'")
    )
    assert parsed.content.metadata == {"version": "1"}


def test_malformed_yaml_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc("name: deploy-app\n  bad: [unterminated"))


def test_unsafe_python_tag_is_rejected_as_invalid_skill_markdown_not_leaked() -> None:
    """``yaml.safe_load`` refuses to construct a ``!!python/...`` tag and
    raises ``yaml.constructor.ConstructorError`` (a ``yaml.YAMLError``
    subclass) -- the parser must translate that into the one public
    ``InvalidSkillMarkdownError`` the service/route layer already knows how
    to map to 422, never let the raw YAML exception type escape. No Python
    object is ever constructed from untrusted content."""

    frontmatter = (
        "name: unsafe-skill\n"
        "description: Unsafe YAML test\n"
        "metadata:\n"
        '  exploit: !!python/object/apply:os.system ["echo should-not-run"]'
    )
    with pytest.raises(InvalidSkillMarkdownError) as exc_info:
        parse_skill_markdown(doc(frontmatter, body="Do nothing."))
    assert "python" in exc_info.value.reason.lower()


def test_missing_opening_delimiter_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown("name: deploy-app\ndescription: d\n---\nbody")


def test_missing_closing_delimiter_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown("---\nname: deploy-app\ndescription: d\nbody")


def test_frontmatter_not_a_mapping_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown("---\njust a scalar\n---\nbody")


def test_empty_frontmatter_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown("---\n---\nbody")


def test_empty_body_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown("---\nname: deploy-app\ndescription: d\n---\n")


def test_whitespace_only_body_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown("---\nname: deploy-app\ndescription: d\n---\n   \n  ")


def test_missing_name_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc("description: d"))


def test_missing_description_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc("name: deploy-app"))


def test_invalid_name_charset_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc("name: Deploy_App\ndescription: d"))


def test_name_at_max_length_is_accepted() -> None:
    name = "a" * SKILL_NAME_MAX_LENGTH
    parsed = parse_skill_markdown(doc(f"name: {name}\ndescription: d"))
    assert parsed.name == name


def test_name_over_max_length_is_rejected() -> None:
    name = "a" * (SKILL_NAME_MAX_LENGTH + 1)
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc(f"name: {name}\ndescription: d"))


def test_procedure_over_max_length_is_rejected() -> None:
    body = "p" * (PROCEDURE_MAX_LENGTH + 1)
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc("name: deploy-app\ndescription: d", body=body))


def test_description_too_long_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc(f"name: deploy-app\ndescription: {'d' * 1025}"))


def test_compatibility_too_long_is_rejected() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc(f"name: deploy-app\ndescription: d\ncompatibility: {'c' * 501}"))


def test_name_must_be_a_string() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc("name: 123\ndescription: d"))


def test_allowed_tools_must_be_a_string() -> None:
    with pytest.raises(InvalidSkillMarkdownError):
        parse_skill_markdown(doc("name: deploy-app\ndescription: d\nallowed-tools:\n  - Read"))
