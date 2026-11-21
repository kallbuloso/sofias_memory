"""Domain/idempotency unit tests for SM-CTX-002 ContextCheckpoints (handoff 10 SS 94)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from sofias_memory.domain import (
    CONTEXT_STREAM_LOCK_DOMAIN,
    DEFAULT_CONTEXT_KEY,
    SCALAR_CONTEXT_STATEMENT_TYPES,
    CheckpointTransition,
    ContextCheckpointLifecycle,
    ContextStatementType,
    ContextualIdempotencyRequest,
    ContextualMemoryOperation,
    InvalidContextKeyError,
    InvalidContextStatementTextError,
    build_contextual_canonical_request,
    checkpoint_forget_transition,
    checkpoint_invalidate_transition,
    compute_contextual_request_digest,
    context_stream_lock_key,
    next_checkpoint_revision,
    normalize_context_statement_text,
    resolve_context_key,
)
from sofias_memory.infrastructure.postgres.models import (
    ContextCheckpoint,
    ContextCheckpointProvenance,
)
from sofias_memory.schemas.context_checkpoints import ContextCheckpointCreateRequest
from sofias_memory.services.context_checkpoints import (
    build_checkpoint_statements,
    canonical_create_payload,
    checkpoint_result,
)

SECRET = b"test-cognitive-idempotency-hmac-key-0123456789abcdef"


def body(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "scope": "project:sofias-memory",
        "context_key": "feature:contextual-memory",
        "objective": {"text": "Implementar memória contextual persistente."},
        "current_state": {"text": "PRD, Spec e Contract estão aprovados."},
        "approved_decisions": [{"text": "Episode será first-class."}],
        "pending_work": [{"text": "A"}, {"text": "B"}],
        "open_items": [],
        "blockers": [],
        "next_action": {"text": "Implementar SM-CTX-002."},
        "provenance": {"origin_kind": "assistant_generated", "source_system": "assistant"},
    }
    value.update(overrides)
    return value


def digest(**overrides: Any) -> str:
    request = ContextCheckpointCreateRequest.model_validate(body(**overrides))
    canonical = build_contextual_canonical_request(
        ContextualIdempotencyRequest(
            operation=ContextualMemoryOperation.CHECKPOINT_CREATE,
            target_resource_id=None,
            payload=canonical_create_payload(request),
        )
    )
    return compute_contextual_request_digest(secret=SECRET, canonical_request=canonical)


# --- context_key ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["main", "feature:contextual-memory", "release:0.8.0", "incident:retrieval-001", "a" * 128],
)
def test_valid_context_keys_are_kept_exactly(value: str) -> None:
    assert resolve_context_key(value) == value


@pytest.mark.parametrize(
    "value", ["Main", "feature x", " main", "main ", "", "a" * 129, "-main", "feat/x", "ção"]
)
def test_invalid_context_keys_are_rejected(value: str) -> None:
    with pytest.raises(InvalidContextKeyError):
        resolve_context_key(value)


def test_omitted_context_key_resolves_to_main() -> None:
    assert DEFAULT_CONTEXT_KEY == "main"
    assert resolve_context_key(None) == "main"


# --- statements ----------------------------------------------------------------


def test_statement_text_is_normalized_and_blank_rejected() -> None:
    assert normalize_context_statement_text("  a\r\nb  ", field="text") == "a\nb"
    long_text = "x" * 100_000
    assert normalize_context_statement_text(long_text, field="text") == long_text
    for blank in ("", "   ", "\r\n\t"):
        with pytest.raises(InvalidContextStatementTextError):
            normalize_context_statement_text(blank, field="text")


def test_scalar_types_are_exactly_objective_current_state_next_action() -> None:
    assert {
        ContextStatementType.OBJECTIVE,
        ContextStatementType.CURRENT_STATE,
        ContextStatementType.NEXT_ACTION,
    } == SCALAR_CONTEXT_STATEMENT_TYPES


def test_build_statements_scalars_at_zero_and_collections_keep_caller_order() -> None:
    request = ContextCheckpointCreateRequest.model_validate(
        body(pending_work=[{"text": "C"}, {"text": "A"}, {"text": "B"}])
    )
    checkpoint_id = uuid4()
    rows = build_checkpoint_statements(checkpoint_id, request)
    assert {row.checkpoint_id for row in rows} == {checkpoint_id}
    assert len({row.id for row in rows}) == len(rows)
    pending = [row for row in rows if row.statement_type is ContextStatementType.PENDING_WORK]
    assert [(row.ordinal, row.content) for row in pending] == [(0, "C"), (1, "A"), (2, "B")]
    for statement_type in SCALAR_CONTEXT_STATEMENT_TYPES:
        scalars = [row for row in rows if row.statement_type is statement_type]
        assert len(scalars) == 1 and scalars[0].ordinal == 0


def test_build_statements_omits_absent_optionals() -> None:
    request = ContextCheckpointCreateRequest.model_validate(
        {
            "scope": "project:p",
            "current_state": {"text": "only"},
            "provenance": {"origin_kind": "user_asserted", "source_system": "s"},
        }
    )
    rows = build_checkpoint_statements(uuid4(), request)
    assert [(row.statement_type, row.content) for row in rows] == [
        (ContextStatementType.CURRENT_STATE, "only")
    ]


def test_checkpoint_result_groups_and_orders_statements_and_represents_tombstone() -> None:
    request = ContextCheckpointCreateRequest.model_validate(body())
    checkpoint = ContextCheckpoint(
        id=uuid4(),
        scope="project:sofias-memory",
        context_key="feature:contextual-memory",
        revision=3,
        lifecycle=ContextCheckpointLifecycle.ACTIVE,
        created_at=datetime.now(UTC),
    )
    provenance = ContextCheckpointProvenance(
        checkpoint_id=checkpoint.id, origin_kind="assistant_generated", source_system="assistant"
    )
    rows = build_checkpoint_statements(checkpoint.id, request)
    result = checkpoint_result(checkpoint, provenance, list(reversed(rows)))
    assert result.current_state is not None
    assert result.current_state.text == "PRD, Spec e Contract estão aprovados."
    assert [item.text for item in result.pending_work] == ["A", "B"]
    assert result.revision == 3

    checkpoint.lifecycle = ContextCheckpointLifecycle.FORGOTTEN
    tombstone = checkpoint_result(checkpoint, provenance, [])
    assert tombstone.current_state is None and tombstone.objective is None
    assert tombstone.next_action is None and tombstone.pending_work == []
    assert (tombstone.scope, tombstone.context_key, tombstone.revision) == (
        "project:sofias-memory",
        "feature:contextual-memory",
        3,
    )


# --- lifecycle -----------------------------------------------------------------


def test_invalidate_transitions() -> None:
    assert checkpoint_invalidate_transition(ContextCheckpointLifecycle.ACTIVE) is (
        CheckpointTransition.APPLY
    )
    assert checkpoint_invalidate_transition(ContextCheckpointLifecycle.INVALIDATED) is (
        CheckpointTransition.NO_OP
    )
    assert checkpoint_invalidate_transition(ContextCheckpointLifecycle.FORGOTTEN) is (
        CheckpointTransition.CONFLICT
    )


def test_forget_transitions() -> None:
    assert checkpoint_forget_transition(ContextCheckpointLifecycle.ACTIVE) is (
        CheckpointTransition.APPLY
    )
    assert checkpoint_forget_transition(ContextCheckpointLifecycle.INVALIDATED) is (
        CheckpointTransition.APPLY
    )
    assert checkpoint_forget_transition(ContextCheckpointLifecycle.FORGOTTEN) is (
        CheckpointTransition.NO_OP
    )


def test_lifecycle_enum_is_the_full_approved_set() -> None:
    assert [member.value for member in ContextCheckpointLifecycle] == [
        "active",
        "superseded",
        "invalidated",
        "forgotten",
    ]


# --- revision / lock key ---------------------------------------------------------


def test_revision_allocation_new_stream_and_history() -> None:
    assert next_checkpoint_revision(None) == 1
    assert next_checkpoint_revision(1) == 2
    assert next_checkpoint_revision(41) == 42


def test_stream_lock_key_is_stable_signed_64_bit_and_stream_specific() -> None:
    key = context_stream_lock_key("project:a", "main")
    assert key == context_stream_lock_key("project:a", "main")
    assert -(2**63) <= key < 2**63
    assert key != context_stream_lock_key("project:b", "main")
    assert key != context_stream_lock_key("project:a", "feature:x")
    # Pinned value: the derivation must never change silently (it would let an
    # old and a new process serialize on different keys during a rollout).
    assert CONTEXT_STREAM_LOCK_DOMAIN == "sofias-memory.context-stream.create-lock.v1"
    assert key == -4050048694212474701


# --- idempotency digest ----------------------------------------------------------


def test_same_request_same_digest() -> None:
    assert digest() == digest()


def test_omitted_context_key_participates_as_resolved_main() -> None:
    omitted = body()
    del omitted["context_key"]
    request = ContextCheckpointCreateRequest.model_validate(omitted)
    assert canonical_create_payload(request)["context_key"] == "main"
    assert digest(context_key="main") == digest_of(omitted)


def digest_of(value: dict[str, Any]) -> str:
    request = ContextCheckpointCreateRequest.model_validate(value)
    canonical = build_contextual_canonical_request(
        ContextualIdempotencyRequest(
            operation=ContextualMemoryOperation.CHECKPOINT_CREATE,
            target_resource_id=None,
            payload=canonical_create_payload(request),
        )
    )
    return compute_contextual_request_digest(secret=SECRET, canonical_request=canonical)


def test_array_order_affects_digest() -> None:
    assert digest(pending_work=[{"text": "A"}, {"text": "B"}]) != digest(
        pending_work=[{"text": "B"}, {"text": "A"}]
    )


def test_changed_statement_changes_digest() -> None:
    assert digest() != digest(current_state={"text": "Outro estado."})
    assert digest() != digest(next_action=None)
    assert digest() != digest(scope="project:other")


def test_normalized_whitespace_does_not_change_digest() -> None:
    assert digest() == digest(current_state={"text": "  PRD, Spec e Contract estão aprovados.  "})


def test_canonical_payload_never_contains_server_allocated_values() -> None:
    request = ContextCheckpointCreateRequest.model_validate(body())
    payload = canonical_create_payload(request)
    assert set(payload) == {
        "scope",
        "context_key",
        "objective",
        "current_state",
        "approved_decisions",
        "pending_work",
        "open_items",
        "blockers",
        "next_action",
        "provenance",
    }
    rendered = repr(payload)
    for forbidden in ("revision", "checkpoint_id", "statement_id", "created_at"):
        assert forbidden not in rendered
