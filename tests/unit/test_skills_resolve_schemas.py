"""Boundary tests for the SM-704 ``SkillResolveRequest``/``SkillResolveMatch``
schemas -- pure Pydantic validation, no PostgreSQL, no embedding provider."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sofias_memory.schemas.skills import SkillResolveRequest


def test_query_required() -> None:
    with pytest.raises(ValidationError):
        SkillResolveRequest()  # type: ignore[call-arg]


def test_query_whitespace_only_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillResolveRequest(query="   \n\t  ")


def test_query_empty_string_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillResolveRequest(query="")


def test_top_k_defaults_to_five() -> None:
    request = SkillResolveRequest(query="deploy")
    assert request.top_k == 5


def test_top_k_minimum_accepted() -> None:
    request = SkillResolveRequest(query="deploy", top_k=1)
    assert request.top_k == 1


def test_top_k_maximum_accepted() -> None:
    request = SkillResolveRequest(query="deploy", top_k=20)
    assert request.top_k == 20


def test_top_k_zero_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillResolveRequest(query="deploy", top_k=0)


def test_top_k_over_maximum_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillResolveRequest(query="deploy", top_k=21)


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillResolveRequest(query="deploy", status="active")  # type: ignore[call-arg]
