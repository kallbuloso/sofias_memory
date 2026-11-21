"""Unit tests for the shared contextual idempotency primitives (SM-CTX-001,
Contract v1 SS 36, Architecture SS 28)."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from sofias_memory.domain import (
    CANONICAL_REQUEST_DOMAIN,
    CONTEXTUAL_CANONICAL_REQUEST_DOMAIN,
    CONTEXTUAL_CREATE_OPERATIONS,
    CONTEXTUAL_IDEMPOTENCY_KEY_DOMAIN,
    RESERVED_IDEMPOTENCY_KEY_PREFIX,
    CognitiveMemoryIdempotencyRequest,
    CognitiveMemoryOperation,
    ContextualIdempotencyRequest,
    ContextualMemoryOperation,
    InvalidContextualIdempotencyRequestError,
    build_canonical_request,
    build_contextual_canonical_request,
    compute_contextual_request_digest,
    compute_request_digest,
    is_reserved_idempotency_key,
    protect_contextual_idempotency_key,
)
from sofias_memory.services.pipeline_submission import (
    RESERVED_IDEMPOTENCY_KEY_PREFIX as PIPELINE_RESERVED_PREFIX,
)

SECRET = b"test-cognitive-idempotency-hmac-key-0123456789abcdef"
EPISODE_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_ID = UUID("00000000-0000-4000-8000-000000000002")


def digest(request: ContextualIdempotencyRequest, secret: bytes = SECRET) -> str:
    return compute_contextual_request_digest(
        secret=secret, canonical_request=build_contextual_canonical_request(request)
    )


def create_request(**payload: object) -> ContextualIdempotencyRequest:
    base: dict[str, object] = {"scope": "project:x", "summary": "S", "context_key": None}
    base.update(payload)
    return ContextualIdempotencyRequest(
        operation=ContextualMemoryOperation.EPISODE_CREATE, target_resource_id=None, payload=base
    )


def test_operation_enum_has_episode_and_reserved_checkpoint_values() -> None:
    assert [op.value for op in ContextualMemoryOperation] == [
        "episode_create",
        "episode_invalidate",
        "episode_forget",
        "checkpoint_create",
        "checkpoint_supersede",
        "checkpoint_invalidate",
        "checkpoint_forget",
    ]
    assert {
        ContextualMemoryOperation.EPISODE_CREATE,
        ContextualMemoryOperation.CHECKPOINT_CREATE,
    } == CONTEXTUAL_CREATE_OPERATIONS


def test_canonical_request_is_stable_regardless_of_payload_key_order() -> None:
    first = ContextualIdempotencyRequest(
        operation=ContextualMemoryOperation.EPISODE_CREATE,
        target_resource_id=None,
        payload={"a": 1, "b": {"y": 2, "x": 1}},
    )
    second = ContextualIdempotencyRequest(
        operation=ContextualMemoryOperation.EPISODE_CREATE,
        target_resource_id=None,
        payload={"b": {"x": 1, "y": 2}, "a": 1},
    )
    assert build_contextual_canonical_request(first) == build_contextual_canonical_request(second)
    assert digest(first) == digest(second)


def test_contextual_domain_is_separate_from_cognitive_domain() -> None:
    assert CONTEXTUAL_CANONICAL_REQUEST_DOMAIN != CANONICAL_REQUEST_DOMAIN
    assert CONTEXTUAL_IDEMPOTENCY_KEY_DOMAIN not in (
        CANONICAL_REQUEST_DOMAIN,
        CONTEXTUAL_CANONICAL_REQUEST_DOMAIN,
    )
    canonical = json.loads(build_contextual_canonical_request(create_request()))
    assert canonical["domain"] == "sofias-memory.contextual-memory.idempotency"

    payload: dict[str, object] = {"x": 1}
    contextual = digest(
        ContextualIdempotencyRequest(
            operation=ContextualMemoryOperation.EPISODE_FORGET,
            target_resource_id=EPISODE_ID,
            payload=payload,
        )
    )
    cognitive = compute_request_digest(
        secret=SECRET,
        canonical_request=build_canonical_request(
            CognitiveMemoryIdempotencyRequest(
                operation=CognitiveMemoryOperation.FORGET,
                target_memory_id=EPISODE_ID,
                payload=payload,
            )
        ),
    )
    assert contextual != cognitive


def test_same_semantic_request_same_digest_changed_request_changed_digest() -> None:
    assert digest(create_request()) == digest(create_request())
    assert digest(create_request()) != digest(create_request(summary="S2"))
    assert digest(create_request()) != digest(create_request(context_key="main"))


def test_digest_is_keyed_hmac_sha256() -> None:
    value = digest(create_request())
    assert len(value) == 64
    assert all(c in "0123456789abcdef" for c in value)
    assert digest(create_request(), secret=b"another-secret") != value


def test_target_resource_participates_in_targeted_operations() -> None:
    def forget(target: UUID) -> ContextualIdempotencyRequest:
        return ContextualIdempotencyRequest(
            operation=ContextualMemoryOperation.EPISODE_FORGET,
            target_resource_id=target,
            payload={},
        )

    assert digest(forget(EPISODE_ID)) != digest(forget(OTHER_ID))


def test_resource_uuid_alone_does_not_identify_the_operation() -> None:
    by_operation = {
        operation: digest(
            ContextualIdempotencyRequest(
                operation=operation, target_resource_id=EPISODE_ID, payload={}
            )
        )
        for operation in (
            ContextualMemoryOperation.EPISODE_INVALIDATE,
            ContextualMemoryOperation.EPISODE_FORGET,
            ContextualMemoryOperation.CHECKPOINT_FORGET,
        )
    }
    assert len(set(by_operation.values())) == 3


def test_operation_target_shape_is_enforced() -> None:
    with pytest.raises(InvalidContextualIdempotencyRequestError):
        build_contextual_canonical_request(
            ContextualIdempotencyRequest(
                operation=ContextualMemoryOperation.EPISODE_CREATE,
                target_resource_id=EPISODE_ID,
                payload={},
            )
        )
    for operation in (
        ContextualMemoryOperation.EPISODE_INVALIDATE,
        ContextualMemoryOperation.EPISODE_FORGET,
    ):
        with pytest.raises(InvalidContextualIdempotencyRequestError):
            build_contextual_canonical_request(
                ContextualIdempotencyRequest(
                    operation=operation, target_resource_id=None, payload={}
                )
            )


def test_protected_key_is_stable_non_reversible_and_domain_separated() -> None:
    raw = "sm-ctx-001-create-001"
    protected = protect_contextual_idempotency_key(secret=SECRET, idempotency_key=raw)
    assert protected == protect_contextual_idempotency_key(secret=SECRET, idempotency_key=raw)
    assert len(protected) == 64
    assert raw not in protected
    assert protected != protect_contextual_idempotency_key(secret=SECRET, idempotency_key=raw + "x")
    assert protected != protect_contextual_idempotency_key(
        secret=b"another-secret", idempotency_key=raw
    )
    # A key can never collide with a request digest computed from the same text.
    assert protected != compute_contextual_request_digest(secret=SECRET, canonical_request=raw)


def test_reserved_sys_namespace_is_the_single_project_wide_rule() -> None:
    assert RESERVED_IDEMPOTENCY_KEY_PREFIX == PIPELINE_RESERVED_PREFIX == "sys:"
    assert is_reserved_idempotency_key("sys:anything")
    assert not is_reserved_idempotency_key("user:sys:x")
