"""Unit tests for the deterministic Skill resolution-text primitive
(SM-702, ADR-0013, Feature Contract SS 7)."""

from __future__ import annotations

from sofias_memory.domain import build_skill_resolution_text


def test_builds_name_description_with_no_tags() -> None:
    text = build_skill_resolution_text(name="deploy-app", description="Deploys the app.", tags=[])
    assert text == "deploy-app\nDeploys the app."


def test_appends_comma_joined_tags_line_when_present() -> None:
    text = build_skill_resolution_text(
        name="deploy-app",
        description="Deploys the app.",
        tags=["deployment", "ops"],
    )
    assert text == "deploy-app\nDeploys the app.\ndeployment, ops"


def test_omits_tags_line_entirely_when_tags_empty() -> None:
    text = build_skill_resolution_text(name="n", description="d", tags=())
    assert "\n" not in text or text.count("\n") == 1
    assert text == "n\nd"


def test_deterministic_for_same_inputs() -> None:
    kwargs = {"name": "n", "description": "d", "tags": ["b", "a"]}
    assert build_skill_resolution_text(**kwargs) == build_skill_resolution_text(**kwargs)
