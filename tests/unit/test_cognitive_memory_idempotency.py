from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from sofias_memory.domain import CognitiveMemoryOperation
from sofias_memory.domain.cognitive_memory_idempotency import (
    RESERVED_IDEMPOTENCY_KEY_PREFIX,
    CognitiveMemoryIdempotencyRequest,
    InvalidCognitiveMemoryIdempotencyRequestError,
    build_canonical_request,
    compute_request_digest,
    is_reserved_idempotency_key,
)
from sofias_memory.services.pipeline_submission import (
    RESERVED_IDEMPOTENCY_KEY_PREFIX as PIPELINE_RESERVED_IDEMPOTENCY_KEY_PREFIX,
)


def test_reserved_prefix_matches_pipeline_submission_contract() -> None:
    """ADR-0009's ``sys:`` reserved namespace is one project-wide contract;
    the Cognitive Memory domain module duplicates the literal (domain code
    never imports services) but the value must never drift."""

    assert RESERVED_IDEMPOTENCY_KEY_PREFIX == PIPELINE_RESERVED_IDEMPOTENCY_KEY_PREFIX
    assert RESERVED_IDEMPOTENCY_KEY_PREFIX == "sys:"


@pytest.mark.parametrize(
    "key,expected",
    [
        ("sys:retry:abc", True),
        ("sys:", True),
        ("regular-key", False),
        ("", False),
        ("SYS:not-lowercase", False),
    ],
)
def test_is_reserved_idempotency_key(key: str, expected: bool) -> None:
    assert is_reserved_idempotency_key(key) is expected


def make_request(
    *, operation: CognitiveMemoryOperation, target_memory_id: object = None, **payload: object
) -> CognitiveMemoryIdempotencyRequest:
    return CognitiveMemoryIdempotencyRequest(
        operation=operation,
        target_memory_id=target_memory_id,  # type: ignore[arg-type]
        payload=dict(payload),
    )


def test_create_request_forbids_target_memory_id() -> None:
    request = make_request(operation=CognitiveMemoryOperation.CREATE, target_memory_id=uuid4())
    with pytest.raises(InvalidCognitiveMemoryIdempotencyRequestError):
        build_canonical_request(request)


def test_supersede_request_requires_target_memory_id() -> None:
    request = make_request(operation=CognitiveMemoryOperation.SUPERSEDE, target_memory_id=None)
    with pytest.raises(InvalidCognitiveMemoryIdempotencyRequestError):
        build_canonical_request(request)


def test_forget_request_requires_target_memory_id() -> None:
    request = make_request(operation=CognitiveMemoryOperation.FORGET, target_memory_id=None)
    with pytest.raises(InvalidCognitiveMemoryIdempotencyRequestError):
        build_canonical_request(request)


def test_canonical_request_distinguishes_operation_identity() -> None:
    target = uuid4()
    create_request = make_request(operation=CognitiveMemoryOperation.CREATE, content="hello")
    supersede_request = make_request(
        operation=CognitiveMemoryOperation.SUPERSEDE, target_memory_id=target, content="hello"
    )

    assert build_canonical_request(create_request) != build_canonical_request(supersede_request)


def test_canonical_request_distinguishes_target_when_applicable() -> None:
    request_a = make_request(operation=CognitiveMemoryOperation.FORGET, target_memory_id=uuid4())
    request_b = make_request(operation=CognitiveMemoryOperation.FORGET, target_memory_id=uuid4())

    assert build_canonical_request(request_a) != build_canonical_request(request_b)


def test_canonical_request_is_deterministic_for_same_semantic_request() -> None:
    target = uuid4()
    request_1 = make_request(
        operation=CognitiveMemoryOperation.SUPERSEDE,
        target_memory_id=target,
        content="x",
        confidence=0.5,
    )
    request_2 = make_request(
        operation=CognitiveMemoryOperation.SUPERSEDE,
        target_memory_id=target,
        confidence=0.5,
        content="x",
    )

    assert build_canonical_request(request_1) == build_canonical_request(request_2)


def test_digest_is_deterministic_for_same_request_and_same_key() -> None:
    request = make_request(operation=CognitiveMemoryOperation.CREATE, content="hello world")
    canonical = build_canonical_request(request)

    digest_1 = compute_request_digest(secret=b"secret-key-material", canonical_request=canonical)
    digest_2 = compute_request_digest(secret=b"secret-key-material", canonical_request=canonical)

    assert digest_1 == digest_2


def test_digest_changes_with_different_key_same_request() -> None:
    request = make_request(operation=CognitiveMemoryOperation.CREATE, content="hello world")
    canonical = build_canonical_request(request)

    digest_a = compute_request_digest(secret=b"key-a", canonical_request=canonical)
    digest_b = compute_request_digest(secret=b"key-b", canonical_request=canonical)

    assert digest_a != digest_b


def test_digest_is_not_raw_unkeyed_sha256_of_canonical_body() -> None:
    request = make_request(operation=CognitiveMemoryOperation.CREATE, content="hello world")
    canonical = build_canonical_request(request)

    keyed_digest = compute_request_digest(secret=b"some-secret", canonical_request=canonical)
    raw_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert keyed_digest != raw_sha256


def test_digest_is_64_char_lowercase_hex() -> None:
    request = make_request(operation=CognitiveMemoryOperation.CREATE, content="hello world")
    canonical = build_canonical_request(request)

    digest = compute_request_digest(secret=b"secret", canonical_request=canonical)

    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)  # raises ValueError if not valid hex
