from __future__ import annotations

import pytest

from sofias_memory.domain import InvalidCognitiveMemoryScopeError, normalize_cognitive_memory_scope


def test_global_scope_is_valid() -> None:
    assert normalize_cognitive_memory_scope("global") == "global"


def test_global_scope_trims_edge_whitespace() -> None:
    assert normalize_cognitive_memory_scope("  global  ") == "global"


@pytest.mark.parametrize(
    "scope",
    [
        "project:x",
        "project:my-project",
        "project:my.project_1",
        "project:9abc",
        "project:" + "a" * 128,
    ],
)
def test_valid_project_scopes(scope: str) -> None:
    assert normalize_cognitive_memory_scope(scope) == scope


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "   ",
        "Global",
        "GLOBAL",
        "project:",
        "project:Foo",
        "project:_leading-underscore-not-alnum-first",
        "project:has space",
        "project:has\ttab",
        "project:" + "a" * 129,
        "project:UPPER",
        "not-a-known-scope",
        "globalx",
        "project",
        "project:foo bar",
    ],
)
def test_invalid_scopes_are_rejected_not_rewritten(scope: str) -> None:
    with pytest.raises(InvalidCognitiveMemoryScopeError):
        normalize_cognitive_memory_scope(scope)


def test_invalid_scope_is_never_silently_lowercased() -> None:
    with pytest.raises(InvalidCognitiveMemoryScopeError):
        normalize_cognitive_memory_scope("Project:Foo")


def test_matching_is_exact_no_prefix_or_hierarchy() -> None:
    # A scope must never be treated as a prefix/hierarchy match.
    with pytest.raises(InvalidCognitiveMemoryScopeError):
        normalize_cognitive_memory_scope("project:foo/bar")
