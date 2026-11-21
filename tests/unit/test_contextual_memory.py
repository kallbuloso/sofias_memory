"""Unit tests for SM-CTX-001 Episode domain primitives (Contract v1
Contextual Project Memory SS 8-10/14-22)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from sofias_memory.domain import (
    CONTEXT_KEY_PATTERN,
    EPISODE_OCCURRED_AT_FUTURE_TOLERANCE,
    CognitiveMemoryType,
    EpisodeKind,
    EpisodeLifecycle,
    EpisodeTransition,
    InvalidContextKeyError,
    InvalidContextualScopeError,
    InvalidEpisodeKindError,
    InvalidEpisodeOccurredAtError,
    InvalidEpisodeTextError,
    build_episode_embedding_text,
    episode_forget_transition,
    episode_invalidate_transition,
    normalize_contextual_scope,
    normalize_episode_occurred_at,
    normalize_episode_text,
    parse_episode_kind,
    validate_context_key,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_episode_kinds_are_exactly_the_nine_v1_kinds() -> None:
    assert [kind.value for kind in EpisodeKind] == [
        "decision",
        "investigation",
        "experiment",
        "incident",
        "discovery",
        "change",
        "outcome",
        "learning",
        "milestone",
    ]


def test_episode_lifecycle_values_have_no_superseded_state() -> None:
    assert [state.value for state in EpisodeLifecycle] == ["active", "invalidated", "forgotten"]


def test_cognitive_memory_type_stays_profile_semantic_only() -> None:
    assert [memory_type.value for memory_type in CognitiveMemoryType] == ["profile", "semantic"]


@pytest.mark.parametrize("value", ["project:sofias-memory", "project:a", " project:x.y_z-1 "])
def test_scope_accepts_canonical_project_scope(value: str) -> None:
    assert normalize_contextual_scope(value) == value.strip()


@pytest.mark.parametrize(
    "value",
    ["global", " global ", "", "project:", "project:Upper", "project:-x", "tenant:x", "proj:x"],
)
def test_scope_rejects_global_and_malformed(value: str) -> None:
    with pytest.raises(InvalidContextualScopeError):
        normalize_contextual_scope(value)


@pytest.mark.parametrize(
    "value",
    ["main", "feature:contextual-memory", "release:0.8.0", "incident:retrieval-001", "a" * 128],
)
def test_context_key_accepts_contract_examples(value: str) -> None:
    assert validate_context_key(value) == value


@pytest.mark.parametrize(
    "value", ["", "Main", " main", "main ", "a b", "-main", ":x", "a" * 129, "feature/x", "ção"]
)
def test_context_key_rejects_invalid_values_without_rewriting(value: str) -> None:
    with pytest.raises(InvalidContextKeyError):
        validate_context_key(value)


def test_context_key_pattern_is_contract_grammar() -> None:
    assert CONTEXT_KEY_PATTERN.pattern == r"^[a-z0-9][a-z0-9._:-]{0,127}$"


def test_parse_episode_kind_exact_match_only() -> None:
    assert parse_episode_kind("decision") is EpisodeKind.DECISION
    assert parse_episode_kind(EpisodeKind.MILESTONE) is EpisodeKind.MILESTONE
    for bad in ("Decision", "note", "", 3, None):
        with pytest.raises(InvalidEpisodeKindError):
            parse_episode_kind(bad)


def test_episode_text_normalizes_newlines_and_trims_without_length_limit() -> None:
    assert normalize_episode_text("  a\r\nb\rc  ", field="summary") == "a\nb\nc"
    long_text = "x" * 200_000
    assert normalize_episode_text(long_text, field="summary") == long_text
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(InvalidEpisodeTextError):
            normalize_episode_text(blank, field="summary")


def test_occurred_at_requires_timezone_and_normalizes_to_utc() -> None:
    local = datetime(2026, 11, 18, 17, 0, tzinfo=timezone(timedelta(hours=-3)))
    normalized = normalize_episode_occurred_at(local, now=NOW)
    assert normalized == datetime(2026, 11, 18, 20, 0, tzinfo=UTC)
    assert normalized.utcoffset() == timedelta(0)
    with pytest.raises(InvalidEpisodeOccurredAtError):
        normalize_episode_occurred_at(datetime(2026, 11, 18, 20, 0), now=NOW)


def test_occurred_at_may_be_past_but_not_beyond_future_tolerance() -> None:
    assert normalize_episode_occurred_at(datetime(1999, 1, 1, tzinfo=UTC), now=NOW)
    assert normalize_episode_occurred_at(NOW + EPISODE_OCCURRED_AT_FUTURE_TOLERANCE, now=NOW)
    with pytest.raises(InvalidEpisodeOccurredAtError):
        normalize_episode_occurred_at(
            NOW + EPISODE_OCCURRED_AT_FUTURE_TOLERANCE + timedelta(seconds=1), now=NOW
        )


def test_canonical_embedding_text_has_approved_format_and_omits_absent_fields() -> None:
    assert build_episode_embedding_text(
        summary="Episode foi escolhido.",
        situation="Foram avaliadas duas estratégias.",
        reason="Lifecycle diferente.",
        outcome="MemoryItem permanece profile|semantic.",
    ) == (
        "Summary: Episode foi escolhido.\n"
        "Situation: Foram avaliadas duas estratégias.\n"
        "Reason: Lifecycle diferente.\n"
        "Outcome: MemoryItem permanece profile|semantic."
    )
    assert (
        build_episode_embedding_text(summary="S", situation=None, reason="R", outcome=None)
        == "Summary: S\nReason: R"
    )


def test_canonical_embedding_text_is_deterministic() -> None:
    kwargs = {"summary": "S", "situation": "T", "reason": None, "outcome": "O"}
    assert build_episode_embedding_text(**kwargs) == build_episode_embedding_text(**kwargs)  # type: ignore[arg-type]


def test_invalidate_transitions() -> None:
    assert episode_invalidate_transition(EpisodeLifecycle.ACTIVE) is EpisodeTransition.APPLY
    assert episode_invalidate_transition(EpisodeLifecycle.INVALIDATED) is EpisodeTransition.NO_OP
    assert episode_invalidate_transition(EpisodeLifecycle.FORGOTTEN) is EpisodeTransition.CONFLICT


def test_forget_transitions_forgotten_is_terminal() -> None:
    assert episode_forget_transition(EpisodeLifecycle.ACTIVE) is EpisodeTransition.APPLY
    assert episode_forget_transition(EpisodeLifecycle.INVALIDATED) is EpisodeTransition.APPLY
    assert episode_forget_transition(EpisodeLifecycle.FORGOTTEN) is EpisodeTransition.NO_OP
