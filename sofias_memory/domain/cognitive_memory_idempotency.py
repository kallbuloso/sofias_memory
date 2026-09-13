"""Native Cognitive Memory idempotency digest primitives (ADR-0016 SS 14,
Feature Contract v0.7.0 Native Cognitive Memory SS 17).

The ``cognitive_memory_idempotency`` ledger never stores raw request
content -- only a keyed, non-reversible digest computed by this module.
HMAC-SHA-256 with a server-side secret (``COGNITIVE_IDEMPOTENCY_HMAC_KEY``,
never the HTTP ``API_KEY``) satisfies the frozen requirements: keyed,
non-reversible, deterministic for the same semantic request, and resistant
to offline dictionary guessing of low-entropy memory content. Raw/unkeyed
SHA-256 or any other unkeyed request/content hash is explicitly forbidden.

This module intentionally defines no HTTP/Pydantic request shape (SM-1001
scope; the public schema belongs to SM-1002+) -- the canonical request is a
generic, already-normalized payload supplied by the caller.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from uuid import UUID

from sofias_memory.domain.enums import CognitiveMemoryOperation

RESERVED_IDEMPOTENCY_KEY_PREFIX = "sys:"
"""Mirrors ``sofias_memory.services.pipeline_submission.
RESERVED_IDEMPOTENCY_KEY_PREFIX`` (ADR-0009): one project-wide reserved
``Idempotency-Key`` namespace for internal mechanisms, frozen so a
caller-supplied key can never collide with or forge an internal one.
Duplicated here rather than imported because domain modules never depend on
the services layer (AGENTS.md SS 7); the two values must stay equal by
contract -- proven by
``tests/unit/test_cognitive_memory_idempotency.py``."""

CANONICAL_REQUEST_DOMAIN = "sofias-memory.cognitive-memory.idempotency"
CANONICAL_REQUEST_VERSION = 1


class InvalidCognitiveMemoryIdempotencyRequestError(ValueError):
    """A :class:`CognitiveMemoryIdempotencyRequest` violates operation/target
    consistency (Feature Contract SS 12.1/15.1/16)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid Cognitive Memory idempotency request: {reason}")


def is_reserved_idempotency_key(idempotency_key: str) -> bool:
    """``True`` when ``idempotency_key`` falls in the reserved ``sys:`` namespace."""

    return idempotency_key.startswith(RESERVED_IDEMPOTENCY_KEY_PREFIX)


@dataclass(frozen=True, slots=True)
class CognitiveMemoryIdempotencyRequest:
    """Already-normalized semantic request identity for one Cognitive Memory
    mutation.

    ``target_memory_id`` is ``None`` for ``create`` and required for
    ``supersede``/``forget`` (validated by :func:`build_canonical_request`).
    ``payload`` is the already-canonicalized semantic fields the caller
    normalized -- this module never re-normalizes or re-validates it
    (Feature Contract SS 17.3: the canonical request body is never
    duplicated beyond what the digest needs).
    """

    operation: CognitiveMemoryOperation
    target_memory_id: UUID | None
    payload: dict[str, object]


def build_canonical_request(request: CognitiveMemoryIdempotencyRequest) -> str:
    """Stable, deterministic, domain-separated JSON representation used only
    as HMAC input -- never persisted or returned (Feature Contract SS 17.3).
    """

    if request.operation is CognitiveMemoryOperation.CREATE:
        if request.target_memory_id is not None:
            raise InvalidCognitiveMemoryIdempotencyRequestError(
                "create requests must not carry a target_memory_id"
            )
    elif request.target_memory_id is None:
        raise InvalidCognitiveMemoryIdempotencyRequestError(
            f"{request.operation.value} requests require a target_memory_id"
        )

    canonical_object: dict[str, object] = {
        "domain": CANONICAL_REQUEST_DOMAIN,
        "version": CANONICAL_REQUEST_VERSION,
        "operation": request.operation.value,
        "target_memory_id": (
            str(request.target_memory_id) if request.target_memory_id is not None else None
        ),
        "payload": request.payload,
    }
    return json.dumps(
        canonical_object,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compute_request_digest(*, secret: bytes, canonical_request: str) -> str:
    """HMAC-SHA-256 hex digest of ``canonical_request``, keyed by ``secret``
    (Feature Contract SS 17.3). Never raw/unkeyed SHA-256 -- the key is
    mandatory and changes the digest for the same input (ADR-0016 SS 14).
    """

    return hmac.new(secret, canonical_request.encode("utf-8"), hashlib.sha256).hexdigest()
