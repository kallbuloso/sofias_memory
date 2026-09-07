"""Unit tests for standalone SKILL.md serialization/export (SM-703, Feature
Contract SS 12.5/12.7). No PostgreSQL, no embedding provider -- pure
serialization and its round-trip through the parser."""

from __future__ import annotations

from sofias_memory.domain import SkillRevisionContent
from sofias_memory.interoperability.skill_md import (
    parse_skill_markdown,
    serialize_skill_markdown,
)


def minimal_content(**overrides: object) -> SkillRevisionContent:
    base: dict[str, object] = {
        "description": "d",
        "procedure": "Do the thing.",
        "license": None,
        "compatibility": None,
        "metadata": {},
        "tags": [],
        "declared_tools": [],
    }
    base.update(overrides)
    return SkillRevisionContent(**base)  # type: ignore[arg-type]


def test_same_content_produces_byte_identical_export_every_time() -> None:
    content = minimal_content(
        description="Deploys the app.",
        license="MIT",
        compatibility="bash>=4",
        metadata={"author": "example"},
        tags=["forms", "pdf"],
        declared_tools=["Read", "Bash(git:*)"],
    )
    first = serialize_skill_markdown(name="deploy-app", content=content)
    second = serialize_skill_markdown(name="deploy-app", content=content)
    assert first == second


def test_optional_fields_omitted_when_absent() -> None:
    content = minimal_content()
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    assert "license:" not in exported
    assert "compatibility:" not in exported
    assert "metadata:" not in exported
    assert "allowed-tools:" not in exported


def test_metadata_omitted_when_empty_and_tags_empty() -> None:
    content = minimal_content(metadata={}, tags=[])
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    assert "metadata:" not in exported


def test_metadata_present_when_only_tags_non_empty() -> None:
    content = minimal_content(tags=["pdf"])
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    assert "metadata:" in exported
    assert "sofias-memory.tags:" in exported


def test_metadata_keys_sorted() -> None:
    content = minimal_content(metadata={"zeta": "1", "alpha": "2"}, tags=["pdf"])
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    metadata_block = exported.split("metadata:")[1].split("---")[0]
    alpha_index = metadata_block.index("alpha")
    tags_index = metadata_block.index("sofias-memory.tags")
    zeta_index = metadata_block.index("zeta")
    assert alpha_index < tags_index < zeta_index


def test_reserved_tags_synthesized_from_persisted_tags() -> None:
    content = minimal_content(tags=["forms", "pdf"])
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    assert 'sofias-memory.tags: \'["forms","pdf"]\'' in exported


def test_declared_tools_joined_with_single_space() -> None:
    content = minimal_content(declared_tools=["Bash(git:*)", "Read"])
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    assert "allowed-tools: Bash(git:*) Read" in exported


def test_declared_tools_omitted_when_empty() -> None:
    content = minimal_content(declared_tools=[])
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    assert "allowed-tools:" not in exported


def test_unicode_round_trips() -> None:
    content = minimal_content(description="Faz o deploy da aplicação.")
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    assert "Faz o deploy da aplicação." in exported
    reparsed = parse_skill_markdown(exported)
    assert reparsed.content.description == "Faz o deploy da aplicação."


def test_markdown_whitespace_preserved_in_procedure() -> None:
    procedure = "1. Step one\n   - nested bullet\n\n```\ncode block\n```"
    content = minimal_content(procedure=procedure)
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    reparsed = parse_skill_markdown(exported)
    assert reparsed.content.procedure == procedure


def test_tag_like_metadata_value_round_trips_as_plain_string_not_a_yaml_tag() -> None:
    """A value that merely *contains* ``!!python``-looking text must be
    quoted/escaped by the safe dumper and come back as the identical
    string on reparse -- never interpreted as an actual YAML tag
    directive (which ``safe_load`` would refuse to construct anyway)."""

    content = minimal_content(metadata={"note": "!!python/object:os.system"}, tags=["pdf"])
    exported = serialize_skill_markdown(name="deploy-app", content=content)
    reparsed = parse_skill_markdown(exported)
    assert reparsed.content.metadata["note"] == "!!python/object:os.system"


def test_parse_export_round_trip_is_semantically_equivalent() -> None:
    original = (
        "---\n"
        "name: deploy-app\n"
        "description: Deploys the app.\n"
        "license: MIT\n"
        "compatibility: bash>=4\n"
        "metadata:\n"
        "  author: example\n"
        '  sofias-memory.tags: \'["forms","pdf"]\'\n'
        "allowed-tools: Bash(git:*) Read\n"
        "---\n"
        "Do the thing."
    )
    parsed = parse_skill_markdown(original)
    exported = serialize_skill_markdown(name=parsed.name, content=parsed.content)
    reparsed = parse_skill_markdown(exported)

    assert reparsed.name == parsed.name
    assert reparsed.content == parsed.content


def test_export_is_deterministic_regardless_of_original_key_order() -> None:
    doc_a = "---\nname: deploy-app\ndescription: d\nlicense: MIT\ncompatibility: bash>=4\n---\nbody"
    doc_b = "---\nname: deploy-app\ncompatibility: bash>=4\nlicense: MIT\ndescription: d\n---\nbody"
    parsed_a = parse_skill_markdown(doc_a)
    parsed_b = parse_skill_markdown(doc_b)
    exported_a = serialize_skill_markdown(name=parsed_a.name, content=parsed_a.content)
    exported_b = serialize_skill_markdown(name=parsed_b.name, content=parsed_b.content)
    assert exported_a == exported_b
