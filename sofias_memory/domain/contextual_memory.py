"""Contextual Project Memory Episode primitives (SM-CTX-001, ADR-0017,
Contract v1 Contextual Project Memory SS 8-10/14-22).

An Episode is historical experience -- *what happened* -- never current
semantic truth (that stays in Cognitive Memory / ``MemoryItem``). This
module has no dependency on FastAPI, SQLAlchemy, or Neo4j.

Project Scope validation reuses the Cognitive Memory grammar exactly
(Contract SS 8) and additionally rejects ``global``: every contextual
resource belongs to a ``project:<key>`` namespace. Project scope is a
contextual namespace, never a tenant/owner/ACL boundary.

No public length limits are introduced here beyond the approved grammars
(handoff 09 SS 18): text fields must only be non-blank after newline
normalization and edge trimming. The global request body limit is the
operational boundary.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sofias_memory.domain.cognitive_memory import normalize_newlines
from sofias_memory.domain.cognitive_memory_scope import (
    GLOBAL_SCOPE,
    InvalidCognitiveMemoryScopeError,
    normalize_cognitive_memory_scope,
)
from sofias_memory.domain.enums import (
    ContextCheckpointLifecycle,
    ContextStatementType,
    EpisodeKind,
    EpisodeLifecycle,
)

CONTEXT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
"""Contract v1 SS 9.1: 1..128 ASCII lowercase chars, ``a-z0-9._:-``, first
char a lowercase letter or digit, exact comparison, no whitespace."""

DEFAULT_CONTEXT_KEY = "main"
"""Contract v1 SS 9.2: omitted ``context_key`` resolves to ``main`` for
ContextCheckpoints (never for Episodes, where omission means project-wide)."""

SCALAR_CONTEXT_STATEMENT_TYPES = frozenset(
    {
        ContextStatementType.OBJECTIVE,
        ContextStatementType.CURRENT_STATE,
        ContextStatementType.NEXT_ACTION,
    }
)
"""At most one statement of each of these types per checkpoint (handoff 10 SS 18)."""

CONTEXT_STREAM_LOCK_DOMAIN = "sofias-memory.context-stream.create-lock.v1"
"""Domain separator of :func:``context_stream_lock_key``. Changing it changes
every derived lock key, so it is versioned and must never be edited in place."""

EPISODE_OCCURRED_AT_FUTURE_TOLERANCE = timedelta(minutes=5)
"""Contract v1 SS 65 leaves the future-timestamp tolerance to the
implementation: ``occurred_at`` may be in the past, and anything beyond a
small clock-skew tolerance in the future is rejected."""


class InvalidContextualScopeError(ValueError):
    """Caller-supplied contextual ``scope`` is not a canonical ``project:<key>``."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid contextual scope: {reason}")


class InvalidContextKeyError(ValueError):
    """Caller-supplied ``context_key`` fails the Contract v1 SS 9.1 grammar."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid context_key: {reason}")


class InvalidEpisodeKindError(ValueError):
    """Caller-supplied Episode ``kind`` is not one of the nine v1 kinds."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Episode kind: {reason}")


class InvalidEpisodeTextError(ValueError):
    """A caller-supplied Episode/provenance text field is blank."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Episode text: {reason}")


class InvalidEpisodeOccurredAtError(ValueError):
    """``occurred_at`` is naive or too far in the future."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Episode occurred_at: {reason}")


def normalize_contextual_scope(value: str) -> str:
    """The Cognitive Memory scope grammar (trim edges only, never rewrite),
    then reject ``global``: contextual resources are project-scoped only."""

    try:
        normalized = normalize_cognitive_memory_scope(value)
    except InvalidCognitiveMemoryScopeError as error:
        raise InvalidContextualScopeError(error.reason) from error
    if normalized == GLOBAL_SCOPE:
        raise InvalidContextualScopeError("contextual resources require 'project:<key>' scope")
    return normalized


def validate_context_key(value: str) -> str:
    """Validate-only, never rewrite: an uppercase or whitespace-padded value
    is rejected outright (Contract v1 SS 9.1: comparison is exact)."""

    if not CONTEXT_KEY_PATTERN.fullmatch(value):
        raise InvalidContextKeyError(
            "context_key must be 1..128 characters of lowercase a-z, 0-9, '.', '_', ':', '-', "
            "starting with a lowercase letter or digit"
        )
    return value


def parse_episode_kind(value: object) -> EpisodeKind:
    """Exact match against the nine approved v1 kinds."""

    if isinstance(value, EpisodeKind):
        return value
    if isinstance(value, str):
        try:
            return EpisodeKind(value)
        except ValueError:
            pass
    allowed = ", ".join(kind.value for kind in EpisodeKind)
    raise InvalidEpisodeKindError(f"kind must be one of: {allowed}")


def normalize_episode_text(value: str, *, field: str) -> str:
    """CRLF/CR -> LF, trim edges, require non-blank. No length limit."""

    normalized = normalize_newlines(value).strip()
    if not normalized:
        raise InvalidEpisodeTextError(f"{field} must not be blank")
    return normalized


def normalize_episode_occurred_at(value: datetime, *, now: datetime) -> datetime:
    """Timezone-aware required, normalized to UTC; rejected when beyond
    :data:`EPISODE_OCCURRED_AT_FUTURE_TOLERANCE` in the future."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidEpisodeOccurredAtError("occurred_at must be timezone-aware")
    normalized = value.astimezone(UTC)
    if normalized > now + EPISODE_OCCURRED_AT_FUTURE_TOLERANCE:
        raise InvalidEpisodeOccurredAtError("occurred_at must not be in the future")
    return normalized


def build_episode_embedding_text(
    *,
    summary: str,
    situation: str | None,
    reason: str | None,
    outcome: str | None,
) -> str:
    """Deterministic canonical embedding text, reconstructible from the
    Episode alone (Architecture SS 13, handoff 09 SS 20). Absent fields are
    omitted. Never includes ids, scope, context_key, timestamps, provenance,
    confidence, lifecycle, or the idempotency key."""

    lines = [f"Summary: {summary}"]
    if situation is not None:
        lines.append(f"Situation: {situation}")
    if reason is not None:
        lines.append(f"Reason: {reason}")
    if outcome is not None:
        lines.append(f"Outcome: {outcome}")
    return "\n".join(lines)


class EpisodeTransition(StrEnum):
    """What a lifecycle mutation does to an Episode in its current state."""

    APPLY = "apply"
    NO_OP = "no_op"
    CONFLICT = "conflict"


_INVALIDATE_TRANSITIONS: dict[EpisodeLifecycle, EpisodeTransition] = {
    EpisodeLifecycle.ACTIVE: EpisodeTransition.APPLY,
    EpisodeLifecycle.INVALIDATED: EpisodeTransition.NO_OP,
    EpisodeLifecycle.FORGOTTEN: EpisodeTransition.CONFLICT,
}

_FORGET_TRANSITIONS: dict[EpisodeLifecycle, EpisodeTransition] = {
    EpisodeLifecycle.ACTIVE: EpisodeTransition.APPLY,
    EpisodeLifecycle.INVALIDATED: EpisodeTransition.APPLY,
    EpisodeLifecycle.FORGOTTEN: EpisodeTransition.NO_OP,
}


def episode_invalidate_transition(lifecycle: EpisodeLifecycle) -> EpisodeTransition:
    """Contract v1 SS 20.1: ACTIVE -> INVALIDATED, INVALIDATED -> 200 no-op,
    FORGOTTEN -> conflict."""

    return _INVALIDATE_TRANSITIONS[lifecycle]


def episode_forget_transition(lifecycle: EpisodeLifecycle) -> EpisodeTransition:
    """Contract v1 SS 21: ACTIVE|INVALIDATED -> FORGOTTEN, FORGOTTEN -> 200 no-op."""

    return _FORGET_TRANSITIONS[lifecycle]


# --- ContextCheckpoint primitives (SM-CTX-002) ----------------------------------


class InvalidContextStatementTextError(ValueError):
    """A caller-supplied ContextStatement/checkpoint text field is blank."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid context statement text: {reason}")


def resolve_context_key(value: str | None) -> str:
    """Omitted (``None``) -> ``main``; otherwise validate-only (exact)."""

    if value is None:
        return DEFAULT_CONTEXT_KEY
    return validate_context_key(value)


def normalize_context_statement_text(value: str, *, field: str) -> str:
    """CRLF/CR -> LF, trim edges, require non-blank. No length limit and no
    silent truncation (handoff 10 SS 23)."""

    normalized = normalize_newlines(value).strip()
    if not normalized:
        raise InvalidContextStatementTextError(f"{field} must not be blank")
    return normalized


def next_checkpoint_revision(max_existing_revision: int | None) -> int:
    """Revision belongs to the Context Stream and is never reused
    (Contract v1 SS 66): a brand-new stream gets 1, a historical stream
    without ACTIVE gets ``MAX(existing revision) + 1`` -- tombstones included."""

    return 1 if max_existing_revision is None else max_existing_revision + 1


def context_stream_lock_key(scope: str, context_key: str) -> int:
    """Stable signed 64-bit key for ``pg_advisory_xact_lock(bigint)``.

    Derived from SHA-256 over a versioned domain separator plus the already
    canonical ``(scope, context_key)`` -- deterministic across processes,
    restarts and PostgreSQL versions (unlike ``hashtext``). A 64-bit collision
    between two different streams would only serialize their Creates
    needlessly; correctness never depends on the absence of collisions
    because the single-ACTIVE index and the revision unique constraint stay
    the structural safety nets. The NUL separator cannot appear in either
    grammar, so distinct pairs always produce distinct hash input."""

    material = "\x00".join((CONTEXT_STREAM_LOCK_DOMAIN, scope, context_key)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)


class CheckpointTransition(StrEnum):
    """What a lifecycle mutation does to a ContextCheckpoint in its current state."""

    APPLY = "apply"
    NO_OP = "no_op"
    CONFLICT = "conflict"


_CHECKPOINT_INVALIDATE_TRANSITIONS: dict[ContextCheckpointLifecycle, CheckpointTransition] = {
    ContextCheckpointLifecycle.ACTIVE: CheckpointTransition.APPLY,
    ContextCheckpointLifecycle.INVALIDATED: CheckpointTransition.NO_OP,
    ContextCheckpointLifecycle.FORGOTTEN: CheckpointTransition.CONFLICT,
    # Contract SS 33 / handoff 11 SS 24: historical lineage
    # (superseded_at/superseded_by) is preserved on the INVALIDATED row.
    ContextCheckpointLifecycle.SUPERSEDED: CheckpointTransition.APPLY,
}

_CHECKPOINT_FORGET_TRANSITIONS: dict[ContextCheckpointLifecycle, CheckpointTransition] = {
    ContextCheckpointLifecycle.ACTIVE: CheckpointTransition.APPLY,
    ContextCheckpointLifecycle.INVALIDATED: CheckpointTransition.APPLY,
    ContextCheckpointLifecycle.FORGOTTEN: CheckpointTransition.NO_OP,
    # Contract SS 34 / handoff 11 SS 26: structural lineage survives Forget.
    ContextCheckpointLifecycle.SUPERSEDED: CheckpointTransition.APPLY,
}


def checkpoint_invalidate_transition(lifecycle: ContextCheckpointLifecycle) -> CheckpointTransition:
    """Contract v1 SS 33: ACTIVE|SUPERSEDED -> INVALIDATED, INVALIDATED -> 200
    no-op, FORGOTTEN -> CHECKPOINT_STATE_CONFLICT."""

    return _CHECKPOINT_INVALIDATE_TRANSITIONS[lifecycle]


def checkpoint_forget_transition(lifecycle: ContextCheckpointLifecycle) -> CheckpointTransition:
    """Contract v1 SS 34: ACTIVE|SUPERSEDED|INVALIDATED -> FORGOTTEN, FORGOTTEN ->
    200 no-op."""

    return _CHECKPOINT_FORGET_TRANSITIONS[lifecycle]
