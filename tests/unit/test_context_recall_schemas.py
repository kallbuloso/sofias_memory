"""SM-CTX-006 Contextual Recall request/response schema rules (Contract v1
SS 38/39/49/50/53, handoff 14 SS 115/116/119-121)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from sofias_memory.schemas.context_recall import (
    CONTEXT_RECALL_MAX_EPISODES,
    CONTEXT_RECALL_MAX_EVIDENCE,
    CONTEXT_RECALL_MAX_MEMORIES,
    ContextRecallIntent,
    ContextRecallLimits,
    ContextRecallRequest,
)


def body(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "scope": "project:sofias-memory",
        "intent": "resume",
        "query": "Onde paramos?",
    }
    value.update(overrides)
    return {k: v for k, v in value.items() if v is not _MISSING}


_MISSING = object()


def error_types(error: ValidationError) -> set[str]:
    return {str(item["type"]) for item in error.errors()}


def test_minimal_request_resolves_defaults() -> None:
    request = ContextRecallRequest.model_validate(body())
    assert request.scope == "project:sofias-memory"
    assert request.context_key == "main"
    assert request.intent is ContextRecallIntent.RESUME
    assert request.datasets == []
    assert request.include_evidence is False
    assert request.limits == ContextRecallLimits()
    assert (request.limits.memories, request.limits.episodes, request.limits.evidence) == (8, 8, 12)


def test_explicit_context_key_is_kept_exactly() -> None:
    request = ContextRecallRequest.model_validate(body(context_key="feature:contextual-memory"))
    assert request.context_key == "feature:contextual-memory"


@pytest.mark.parametrize("context_key", ["Main", "feature a", "", "main "])
def test_invalid_context_key_grammar_is_rejected(context_key: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ContextRecallRequest.model_validate(body(context_key=context_key))
    assert error_types(excinfo.value) == {"invalid_context_key"}


def test_project_scope_accepted_and_global_rejected() -> None:
    assert ContextRecallRequest.model_validate(body(scope="project:a")).scope == "project:a"
    with pytest.raises(ValidationError) as excinfo:
        ContextRecallRequest.model_validate(body(scope="global"))
    assert error_types(excinfo.value) == {"invalid_scope"}


@pytest.mark.parametrize("query", [_MISSING, None, "", "   \r\n "])
def test_query_is_required_and_non_blank(query: object) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ContextRecallRequest.model_validate(body(query=query))
    assert not error_types(excinfo.value) & {"invalid_scope", "invalid_context_key"}


def test_query_is_normalized() -> None:
    request = ContextRecallRequest.model_validate(body(query="  Onde\r\nparamos?  "))
    assert request.query == "Onde\nparamos?"


@pytest.mark.parametrize("intent", ["resume", "context", "explain", "history"])
def test_all_contract_intents_parse(intent: str) -> None:
    # explain/history parse (they are Contract v1 values) and are rejected by
    # the service's partial-contract gate, never silently treated as context.
    assert ContextRecallRequest.model_validate(body(intent=intent)).intent.value == intent


def test_unknown_intent_and_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ContextRecallRequest.model_validate(body(intent="answer"))
    with pytest.raises(ValidationError):
        ContextRecallRequest.model_validate(body(include_session_context=True))
    with pytest.raises(ValidationError):
        ContextRecallRequest.model_validate(body(limits={"chunks": 3}))


def test_limits_are_positive_and_clamped_to_server_maximums() -> None:
    with pytest.raises(ValidationError):
        ContextRecallLimits(memories=0)
    effective = ContextRecallLimits(memories=10_000, episodes=999, evidence=10**9).effective()
    assert effective.memories == CONTEXT_RECALL_MAX_MEMORIES
    assert effective.episodes == CONTEXT_RECALL_MAX_EPISODES
    assert effective.evidence == CONTEXT_RECALL_MAX_EVIDENCE
    small = ContextRecallLimits(memories=2, episodes=3, evidence=1).effective()
    assert (small.memories, small.episodes, small.evidence) == (2, 3, 1)
