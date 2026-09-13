"""Native Cognitive Memory Create/Get service (SM-1002, ADR-0016, Feature
Contract v0.7.0 Native Cognitive Memory SS 12/13/17).

Owns the embedding boundary exactly like ``services.skills.SkillService``:
the external embedding call always happens before any PostgreSQL
transaction is opened, and Create's authoritative
``Idempotency-Key``/``UNIQUE`` claim happens inside one short transaction
together with the ``MemoryItem``/``MemoryProvenance`` insert -- never a
separate pipeline stage, never a ``PipelineRun``.

Race handling mirrors ``services.pipeline_submission``/``services.skills``:
a losing insert is caught as an ``IntegrityError`` on the specific
``UNIQUE(idempotency_key)`` constraint (never string-matched, never a
generic ``except IntegrityError`` treated as a race), then resolved by
re-reading the already-committed winner inside the same
``uow.savepoint()`` recovery -- the whole attempt (``MemoryItem`` +
``MemoryProvenance`` + ledger row) lives in one ``SAVEPOINT``, so a losing
race leaves zero orphan rows of any kind (ADR-0016 SS 14).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    CognitiveMemoryIdempotencyRequest,
    CognitiveMemoryOperation,
    build_canonical_request,
    compute_request_digest,
    is_reserved_idempotency_key,
)
from sofias_memory.infrastructure.postgres.models import (
    CognitiveMemoryIdempotency,
    MemoryItem,
    MemoryProvenance,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.memories import (
    MemoryCreateRequest,
    MemoryItemResult,
    MemoryProvenanceResult,
)
from sofias_memory.services.pipeline_submission import (
    idempotency_conflict_error,
    reserved_idempotency_key_namespace_error,
)

IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT = "uq_cognitive_memory_idempotency_idempotency_key"
"""The one constraint whose violation may ever be reinterpreted as a
Cognitive Memory idempotency race (mirrors
``pipeline_submission.IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT``) -- an
``IntegrityError`` from any OTHER constraint always propagates unchanged."""


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


type UnitOfWorkFactory = Callable[[], PostgresUnitOfWork]


def memory_not_found_error(memory_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.MEMORY_NOT_FOUND,
        status_code=HTTPStatus.NOT_FOUND,
        message="MemoryItem does not exist.",
        details={"memory_id": str(memory_id)},
    )


def _is_cognitive_idempotency_key_unique_violation(error: IntegrityError) -> bool:
    """Reads ``constraint_name`` safely off the underlying DBAPI exception,
    never guessed from the error message (same pattern as
    ``pipeline_submission._is_idempotency_key_unique_violation`` /
    ``services.skills._is_skill_name_unique_violation``)."""

    orig = error.orig
    constraint_name = getattr(orig, "constraint_name", None) or getattr(
        getattr(orig, "__cause__", None), "constraint_name", None
    )
    return constraint_name == IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT


def _timestamp_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _uuid_or_none(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _canonical_create_payload(request: MemoryCreateRequest) -> dict[str, object]:
    """The exact semantic fields that define Create's work identity (Feature
    Contract SS 12.1) -- never the embedding, generated ``memory_id``,
    ``created_at``, request id, transport headers, or any secret."""

    return {
        "memory_type": request.memory_type.value,
        "scope": request.scope,
        "content": request.content,
        "confidence": request.confidence,
        "valid_from": _timestamp_or_none(request.valid_from),
        "valid_until": _timestamp_or_none(request.valid_until),
        "provenance": {
            "origin_kind": request.provenance.origin_kind.value,
            "source_system": request.provenance.source_system,
            "conversation_uuid": _uuid_or_none(request.provenance.conversation_uuid),
            "turn_uuid": _uuid_or_none(request.provenance.turn_uuid),
            "task_uuid": _uuid_or_none(request.provenance.task_uuid),
            "confirmation_ref": request.provenance.confirmation_ref,
            "source_ref": request.provenance.source_ref,
            "observed_at": _timestamp_or_none(request.provenance.observed_at),
        },
    }


def memory_item_result(item: MemoryItem, provenance: MemoryProvenance) -> MemoryItemResult:
    """Public, not module-private: the one MemoryItem+provenance -> public
    shape conversion, reused by every Cognitive Memory entry point
    (Create/Get here, Recall in ``services.cognitive_memory_recall``) so
    the hydration shape never diverges between them."""

    return MemoryItemResult(
        memory_id=item.id,
        memory_type=item.memory_type,
        scope=item.scope,
        content=item.content,
        lifecycle=item.lifecycle,
        confidence=item.confidence,
        valid_from=item.valid_from,
        valid_until=item.valid_until,
        created_at=item.created_at,
        superseded_at=item.superseded_at,
        superseded_by=item.superseded_by,
        forgotten_at=item.forgotten_at,
        provenance=MemoryProvenanceResult(
            origin_kind=provenance.origin_kind,
            source_system=provenance.source_system,
            conversation_uuid=provenance.conversation_uuid,
            turn_uuid=provenance.turn_uuid,
            task_uuid=provenance.task_uuid,
            confirmation_ref=provenance.confirmation_ref,
            source_ref=provenance.source_ref,
            observed_at=provenance.observed_at,
        ),
    )


@dataclass(frozen=True, slots=True)
class _LedgerSnapshot:
    operation: CognitiveMemoryOperation
    request_digest: str
    result_memory_id: UUID | None


def _snapshot(entry: CognitiveMemoryIdempotency) -> _LedgerSnapshot:
    return _LedgerSnapshot(
        operation=entry.operation,
        request_digest=entry.request_digest,
        result_memory_id=entry.result_memory_id,
    )


class CognitiveMemoryService:
    """Public Create/Get service backing ``api/routes/memories.py``."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def create(
        self,
        request: MemoryCreateRequest,
        *,
        idempotency_key: str | None,
    ) -> MemoryItemResult:
        if idempotency_key is not None and is_reserved_idempotency_key(idempotency_key):
            raise reserved_idempotency_key_namespace_error()

        digest: str | None = None
        if idempotency_key is not None:
            digest = self._create_digest(request)
            pre_check_result = await self._pre_check(idempotency_key, digest)
            if pre_check_result is not None:
                return pre_check_result

        embedding = await self._embed_content(request.content)

        async with self._unit_of_work_factory() as uow:
            memory_item = MemoryItem(
                id=uuid4(),
                memory_type=request.memory_type,
                scope=request.scope,
                content=request.content,
                embedding=embedding,
                confidence=request.confidence,
                valid_from=request.valid_from,
                valid_until=request.valid_until,
            )
            provenance = MemoryProvenance(
                memory_id=memory_item.id,
                origin_kind=request.provenance.origin_kind,
                source_system=request.provenance.source_system,
                conversation_uuid=request.provenance.conversation_uuid,
                turn_uuid=request.provenance.turn_uuid,
                task_uuid=request.provenance.task_uuid,
                confirmation_ref=request.provenance.confirmation_ref,
                source_ref=request.provenance.source_ref,
                observed_at=request.provenance.observed_at,
            )

            if idempotency_key is None:
                await uow.memory_items.add(memory_item)
                await uow.memory_provenance.add(provenance)
                await uow.commit()
                return memory_item_result(memory_item, provenance)

            assert digest is not None  # noqa: S101 - set above whenever a key is present
            ledger_entry = CognitiveMemoryIdempotency(
                id=uuid4(),
                idempotency_key=idempotency_key,
                operation=CognitiveMemoryOperation.CREATE,
                request_digest=digest,
                target_memory_id=None,
                result_memory_id=memory_item.id,
            )
            try:
                async with uow.savepoint():
                    await uow.memory_items.add(memory_item)
                    await uow.memory_provenance.add(provenance)
                    await uow.cognitive_memory_idempotency.add(ledger_entry)
            except IntegrityError as error:
                if not _is_cognitive_idempotency_key_unique_violation(error):
                    raise
                result = await self._resolve_existing_winner(uow, idempotency_key, digest)
                await uow.commit()
                return result

            await uow.commit()
            return memory_item_result(memory_item, provenance)

    async def get(self, memory_id: UUID) -> MemoryItemResult:
        async with self._unit_of_work_factory() as uow:
            item = await uow.memory_items.get_by_id(memory_id)
            if item is None:
                raise memory_not_found_error(memory_id)
            provenance = await uow.memory_provenance.get_by_memory_id(memory_id)
            assert provenance is not None  # noqa: S101 - 1:1 invariant, ADR-0016 SS 7
            return memory_item_result(item, provenance)

    def _create_digest(self, request: MemoryCreateRequest) -> str:
        idempotency_request = CognitiveMemoryIdempotencyRequest(
            operation=CognitiveMemoryOperation.CREATE,
            target_memory_id=None,
            payload=_canonical_create_payload(request),
        )
        canonical_request = build_canonical_request(idempotency_request)
        return compute_request_digest(
            secret=self._settings.cognitive_idempotency_hmac_key.get_secret_value().encode("utf-8"),
            canonical_request=canonical_request,
        )

    async def _pre_check(self, idempotency_key: str, digest: str) -> MemoryItemResult | None:
        """Optimization only (Feature Contract SS 12.1/17.2) -- never the
        race authority. A miss here always falls through to embedding + the
        authoritative transactional claim; a hit avoids an unnecessary
        embedding call."""

        async with self._unit_of_work_factory() as uow:
            existing = await uow.cognitive_memory_idempotency.get_by_key(idempotency_key)
            if existing is None:
                return None
            snapshot = _snapshot(existing)
            if (
                snapshot.operation is not CognitiveMemoryOperation.CREATE
                or snapshot.request_digest != digest
            ):
                raise idempotency_conflict_error()
            return await self._hydrate(uow, snapshot.result_memory_id)

    async def _resolve_existing_winner(
        self,
        uow: PostgresUnitOfWork,
        idempotency_key: str,
        digest: str,
    ) -> MemoryItemResult:
        """Runs after the savepoint absorbing a losing ``INSERT`` has rolled
        back -- a fresh read on this still-open outer transaction observes
        the winner's already-committed row (Feature Contract SS 17.2:
        correctness never depends on a pre-check, only on this claim)."""

        winner = await uow.cognitive_memory_idempotency.get_by_key(idempotency_key)
        if winner is None:
            raise SofiasMemoryError(
                code=ErrorCode.INTERNAL_ERROR,
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                message="Idempotency-Key race lost but no winner row was found.",
            )
        snapshot = _snapshot(winner)
        if (
            snapshot.operation is not CognitiveMemoryOperation.CREATE
            or snapshot.request_digest != digest
        ):
            raise idempotency_conflict_error()
        return await self._hydrate(uow, snapshot.result_memory_id)

    async def _hydrate(self, uow: PostgresUnitOfWork, memory_id: UUID | None) -> MemoryItemResult:
        if memory_id is None:
            raise SofiasMemoryError(
                code=ErrorCode.INTERNAL_ERROR,
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                message="Idempotency ledger entry has no result_memory_id.",
            )
        item = await uow.memory_items.get_by_id(memory_id)
        provenance = await uow.memory_provenance.get_by_memory_id(memory_id) if item else None
        if item is None or provenance is None:
            raise SofiasMemoryError(
                code=ErrorCode.INTERNAL_ERROR,
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                message="Idempotency ledger references a missing MemoryItem.",
                details={"memory_id": str(memory_id)},
            )
        return memory_item_result(item, provenance)

    async def _embed_content(self, content: str) -> list[float]:
        try:
            embeddings = await self._embedding_client.embed_texts([content])
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Embedding provider is unavailable.") from exc
        if len(embeddings) != 1 or len(embeddings[0]) != self._settings.embedding_dimensions:
            raise DependencyUnavailableError(
                "Embedding provider returned an unexpected vector dimension."
            )
        return embeddings[0]


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(session_factory)

    return create_unit_of_work
