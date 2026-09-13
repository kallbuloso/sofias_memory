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
    CognitiveMemoryLifecycle,
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
from sofias_memory.schemas.common import ErrorCode, utc_now
from sofias_memory.schemas.memories import (
    MemoryCreateRequest,
    MemoryItemResult,
    MemoryProvenanceCreateRequest,
    MemoryProvenanceResult,
    MemorySupersedeRequest,
    MemorySupersedeResult,
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


def memory_state_conflict_error(memory_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.MEMORY_STATE_CONFLICT,
        status_code=HTTPStatus.CONFLICT,
        message="MemoryItem is not in a state that allows this operation.",
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


def _canonical_provenance_payload(provenance: MemoryProvenanceCreateRequest) -> dict[str, object]:
    return {
        "origin_kind": provenance.origin_kind.value,
        "source_system": provenance.source_system,
        "conversation_uuid": _uuid_or_none(provenance.conversation_uuid),
        "turn_uuid": _uuid_or_none(provenance.turn_uuid),
        "task_uuid": _uuid_or_none(provenance.task_uuid),
        "confirmation_ref": provenance.confirmation_ref,
        "source_ref": provenance.source_ref,
        "observed_at": _timestamp_or_none(provenance.observed_at),
    }


def _canonical_content_payload(
    request: MemoryCreateRequest | MemorySupersedeRequest,
) -> dict[str, object]:
    """The semantic fields shared by Create's and Supersede's replacement
    payload -- content/confidence/validity/provenance -- never the
    embedding, generated ids, timestamps, request id, transport headers, or
    any secret (Feature Contract SS 12.1/SS 15)."""

    return {
        "content": request.content,
        "confidence": request.confidence,
        "valid_from": _timestamp_or_none(request.valid_from),
        "valid_until": _timestamp_or_none(request.valid_until),
        "provenance": _canonical_provenance_payload(request.provenance),
    }


def _canonical_create_payload(request: MemoryCreateRequest) -> dict[str, object]:
    """Create's full work identity: the shared content payload plus the
    identity-of-meaning fields Supersede's replacement never carries
    (``memory_type``/``scope`` are always inherited from the target, never
    part of a Supersede request)."""

    return {
        "memory_type": request.memory_type.value,
        "scope": request.scope,
        **_canonical_content_payload(request),
    }


def _canonical_supersede_payload(request: MemorySupersedeRequest) -> dict[str, object]:
    """Supersede's work identity: the replacement payload only --
    ``memory_type``/``scope`` are never caller-supplied here, and the
    target ``memory_id`` is carried separately as the idempotency request's
    ``target_memory_id`` (Feature Contract SS 15)."""

    return _canonical_content_payload(request)


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


def _require_same_operation_and_digest(
    snapshot: _LedgerSnapshot, *, operation: CognitiveMemoryOperation, digest: str
) -> None:
    """The one same-key conflict check shared by Create/Supersede/Forget
    (Feature Contract SS 17.1/17.2): a key already bound to a different
    operation or a different semantic request digest -- including a key
    reused across operations, or reused for a different Supersede/Forget
    target (``target_memory_id`` is part of the canonical request the
    digest covers) -- is always ``409 IDEMPOTENCY_CONFLICT``, checked
    before any resource-state/lifecycle concern is ever consulted."""

    if snapshot.operation is not operation or snapshot.request_digest != digest:
        raise idempotency_conflict_error()


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

    async def supersede(
        self,
        memory_id: UUID,
        request: MemorySupersedeRequest,
        *,
        idempotency_key: str | None,
    ) -> MemorySupersedeResult:
        """Atomically replace an ``ACTIVE`` old item with exactly one new
        ``ACTIVE`` replacement (Feature Contract SS 15).

        The authoritative ``Idempotency-Key`` claim/check happens BEFORE
        the old item's lifecycle is ever evaluated -- a same-key/
        same-request retry replays the original winner even though the
        winner's own commit already moved ``old`` to ``SUPERSEDED``; only a
        genuinely distinct operation (different digest, or no key at all)
        ever reaches the ``lifecycle == ACTIVE`` check (Feature Contract
        SS 15.2/SS 17.2). Existence is confirmed (and the row locked)
        before that claim, never after: ``cognitive_memory_idempotency.
        target_memory_id`` has a ``RESTRICT`` FK to ``memory_items.id``, so
        a claim naming a target that does not exist cannot be inserted at
        all -- this is a distinct, prior gate from the ACTIVE/SUPERSEDED
        lifecycle question the replay-precedence invariant is about."""

        if idempotency_key is not None and is_reserved_idempotency_key(idempotency_key):
            raise reserved_idempotency_key_namespace_error()

        digest: str | None = None
        if idempotency_key is not None:
            digest = self._supersede_digest(memory_id, request)
            pre_check_result = await self._pre_check_supersede(memory_id, idempotency_key, digest)
            if pre_check_result is not None:
                return pre_check_result

        embedding = await self._embed_content(request.content)

        async with self._unit_of_work_factory() as uow:
            old = await uow.memory_items.get_by_id_for_update(memory_id)
            if old is None:
                raise memory_not_found_error(memory_id)

            ledger_entry: CognitiveMemoryIdempotency | None = None
            if idempotency_key is not None:
                assert digest is not None  # noqa: S101 - set above whenever a key is present
                ledger_entry = CognitiveMemoryIdempotency(
                    id=uuid4(),
                    idempotency_key=idempotency_key,
                    operation=CognitiveMemoryOperation.SUPERSEDE,
                    request_digest=digest,
                    target_memory_id=memory_id,
                    result_memory_id=None,
                )
                try:
                    async with uow.savepoint():
                        await uow.cognitive_memory_idempotency.add(ledger_entry)
                except IntegrityError as error:
                    if not _is_cognitive_idempotency_key_unique_violation(error):
                        raise
                    result = await self._resolve_existing_supersede_winner(
                        uow, memory_id, idempotency_key, digest
                    )
                    await uow.commit()
                    return result

            if old.lifecycle is not CognitiveMemoryLifecycle.ACTIVE:
                raise memory_state_conflict_error(memory_id)
            old_provenance = await uow.memory_provenance.get_by_memory_id(memory_id)
            assert old_provenance is not None  # noqa: S101 - 1:1 invariant, ADR-0016 SS 7

            replacement = MemoryItem(
                id=uuid4(),
                memory_type=old.memory_type,
                scope=old.scope,
                content=request.content,
                embedding=embedding,
                confidence=request.confidence,
                valid_from=request.valid_from,
                valid_until=request.valid_until,
            )
            replacement_provenance = MemoryProvenance(
                memory_id=replacement.id,
                origin_kind=request.provenance.origin_kind,
                source_system=request.provenance.source_system,
                conversation_uuid=request.provenance.conversation_uuid,
                turn_uuid=request.provenance.turn_uuid,
                task_uuid=request.provenance.task_uuid,
                confirmation_ref=request.provenance.confirmation_ref,
                source_ref=request.provenance.source_ref,
                observed_at=request.provenance.observed_at,
            )
            await uow.memory_items.add(replacement)
            await uow.memory_provenance.add(replacement_provenance)

            old.lifecycle = CognitiveMemoryLifecycle.SUPERSEDED
            old.superseded_at = utc_now()
            old.superseded_by = replacement.id

            if ledger_entry is not None:
                ledger_entry.result_memory_id = replacement.id

            await uow.commit()
            return MemorySupersedeResult(
                old=memory_item_result(old, old_provenance),
                replacement=memory_item_result(replacement, replacement_provenance),
            )

    async def forget(self, memory_id: UUID, *, idempotency_key: str | None) -> MemoryItemResult:
        """Precise, destructive Forget by exact ``memory_id`` (Feature
        Contract SS 16). ``ACTIVE``/``SUPERSEDED`` -> ``FORGOTTEN``
        destroys cognitive content/embedding/scope/confidence/validity and
        scrubs every external provenance reference in the same
        transaction; ``FORGOTTEN`` -> ``FORGOTTEN`` is a resource-state
        idempotent no-op, including with a brand-new key -- but same-key
        conflict semantics are checked first, so a reused key with a
        different request never silently becomes a no-op ``200``.
        Existence is confirmed (and the row locked) before the ledger
        claim: ``cognitive_memory_idempotency.target_memory_id`` has a
        ``RESTRICT`` FK to ``memory_items.id``, so a claim naming a
        missing target cannot be inserted at all."""

        if idempotency_key is not None and is_reserved_idempotency_key(idempotency_key):
            raise reserved_idempotency_key_namespace_error()

        digest: str | None = None
        if idempotency_key is not None:
            digest = self._forget_digest(memory_id)
            pre_check_result = await self._pre_check_forget(memory_id, idempotency_key, digest)
            if pre_check_result is not None:
                return pre_check_result

        async with self._unit_of_work_factory() as uow:
            item = await uow.memory_items.get_by_id_for_update(memory_id)
            if item is None:
                raise memory_not_found_error(memory_id)
            provenance = await uow.memory_provenance.get_by_memory_id_for_update(memory_id)
            assert provenance is not None  # noqa: S101 - 1:1 invariant, ADR-0016 SS 7

            ledger_entry: CognitiveMemoryIdempotency | None = None
            if idempotency_key is not None:
                assert digest is not None  # noqa: S101 - set above whenever a key is present
                ledger_entry = CognitiveMemoryIdempotency(
                    id=uuid4(),
                    idempotency_key=idempotency_key,
                    operation=CognitiveMemoryOperation.FORGET,
                    request_digest=digest,
                    target_memory_id=memory_id,
                    result_memory_id=None,
                )
                try:
                    async with uow.savepoint():
                        await uow.cognitive_memory_idempotency.add(ledger_entry)
                except IntegrityError as error:
                    if not _is_cognitive_idempotency_key_unique_violation(error):
                        raise
                    result = await self._resolve_existing_forget_winner(
                        uow, memory_id, idempotency_key, digest
                    )
                    await uow.commit()
                    return result

            if item.lifecycle is not CognitiveMemoryLifecycle.FORGOTTEN:
                item.content = None
                item.embedding = None
                item.scope = None
                item.confidence = None
                item.valid_from = None
                item.valid_until = None
                item.lifecycle = CognitiveMemoryLifecycle.FORGOTTEN
                item.forgotten_at = utc_now()
                # superseded_at/superseded_by are never touched: preserved
                # unchanged if the item was already SUPERSEDED, and already
                # NULL if it was ACTIVE (ADR-0016 SS 10/13).
                provenance.conversation_uuid = None
                provenance.turn_uuid = None
                provenance.task_uuid = None
                provenance.confirmation_ref = None
                provenance.source_ref = None
                provenance.observed_at = None
            # else: already FORGOTTEN -- resource-state idempotent no-op,
            # even under a brand-new key (Feature Contract SS 16); no
            # further destructive mutation.

            if ledger_entry is not None:
                ledger_entry.result_memory_id = memory_id

            await uow.commit()
            return memory_item_result(item, provenance)

    def _digest_for(
        self,
        *,
        operation: CognitiveMemoryOperation,
        target_memory_id: UUID | None,
        payload: dict[str, object],
    ) -> str:
        idempotency_request = CognitiveMemoryIdempotencyRequest(
            operation=operation,
            target_memory_id=target_memory_id,
            payload=payload,
        )
        canonical_request = build_canonical_request(idempotency_request)
        return compute_request_digest(
            secret=self._settings.cognitive_idempotency_hmac_key.get_secret_value().encode("utf-8"),
            canonical_request=canonical_request,
        )

    def _create_digest(self, request: MemoryCreateRequest) -> str:
        return self._digest_for(
            operation=CognitiveMemoryOperation.CREATE,
            target_memory_id=None,
            payload=_canonical_create_payload(request),
        )

    def _supersede_digest(self, memory_id: UUID, request: MemorySupersedeRequest) -> str:
        return self._digest_for(
            operation=CognitiveMemoryOperation.SUPERSEDE,
            target_memory_id=memory_id,
            payload=_canonical_supersede_payload(request),
        )

    def _forget_digest(self, memory_id: UUID) -> str:
        """Forget carries no caller body -- the semantic identity is
        entirely ``operation`` + ``target_memory_id`` (Feature Contract
        SS 17.3)."""

        return self._digest_for(
            operation=CognitiveMemoryOperation.FORGET,
            target_memory_id=memory_id,
            payload={},
        )

    async def _pre_check_supersede(
        self, memory_id: UUID, idempotency_key: str, digest: str
    ) -> MemorySupersedeResult | None:
        """Optimization only (Feature Contract SS 15.1/17.2) -- never the
        race authority. A miss here always falls through to embedding + the
        authoritative transactional claim."""

        async with self._unit_of_work_factory() as uow:
            snapshot = await self._find_ledger_snapshot(uow, idempotency_key)
            if snapshot is None:
                return None
            _require_same_operation_and_digest(
                snapshot, operation=CognitiveMemoryOperation.SUPERSEDE, digest=digest
            )
            return await self._hydrate_supersede(uow, memory_id, snapshot.result_memory_id)

    async def _resolve_existing_supersede_winner(
        self,
        uow: PostgresUnitOfWork,
        memory_id: UUID,
        idempotency_key: str,
        digest: str,
    ) -> MemorySupersedeResult:
        """Runs after the savepoint absorbing a losing ledger ``INSERT`` has
        rolled back, BEFORE any lifecycle check -- so a same-operation retry
        of the very request that already superseded ``memory_id`` replays
        that outcome instead of ever observing (and misreading) the
        now-SUPERSEDED old row (Feature Contract SS 15.2)."""

        snapshot = await self._find_ledger_snapshot(uow, idempotency_key)
        if snapshot is None:
            raise SofiasMemoryError(
                code=ErrorCode.INTERNAL_ERROR,
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                message="Idempotency-Key race lost but no winner row was found.",
            )
        _require_same_operation_and_digest(
            snapshot, operation=CognitiveMemoryOperation.SUPERSEDE, digest=digest
        )
        return await self._hydrate_supersede(uow, memory_id, snapshot.result_memory_id)

    async def _hydrate_supersede(
        self, uow: PostgresUnitOfWork, old_id: UUID, replacement_id: UUID | None
    ) -> MemorySupersedeResult:
        if replacement_id is None:
            raise SofiasMemoryError(
                code=ErrorCode.INTERNAL_ERROR,
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                message="Idempotency ledger entry has no result_memory_id.",
            )
        old = await uow.memory_items.get_by_id(old_id)
        old_provenance = await uow.memory_provenance.get_by_memory_id(old_id) if old else None
        replacement = await uow.memory_items.get_by_id(replacement_id)
        replacement_provenance = (
            await uow.memory_provenance.get_by_memory_id(replacement_id) if replacement else None
        )
        if (
            old is None
            or old_provenance is None
            or replacement is None
            or replacement_provenance is None
        ):
            raise SofiasMemoryError(
                code=ErrorCode.INTERNAL_ERROR,
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                message="Idempotency ledger references a missing MemoryItem.",
                details={"memory_id": str(old_id)},
            )
        return MemorySupersedeResult(
            old=memory_item_result(old, old_provenance),
            replacement=memory_item_result(replacement, replacement_provenance),
        )

    async def _pre_check_forget(
        self, memory_id: UUID, idempotency_key: str, digest: str
    ) -> MemoryItemResult | None:
        """Optimization only (Feature Contract SS 16/17.2). Same-key
        conflict semantics are checked here BEFORE any resource-state
        no-op concern -- a reused key with a different request is always
        ``409``, never silently a Forget no-op replay."""

        async with self._unit_of_work_factory() as uow:
            snapshot = await self._find_ledger_snapshot(uow, idempotency_key)
            if snapshot is None:
                return None
            _require_same_operation_and_digest(
                snapshot, operation=CognitiveMemoryOperation.FORGET, digest=digest
            )
            return await self._hydrate(uow, memory_id)

    async def _resolve_existing_forget_winner(
        self,
        uow: PostgresUnitOfWork,
        memory_id: UUID,
        idempotency_key: str,
        digest: str,
    ) -> MemoryItemResult:
        snapshot = await self._find_ledger_snapshot(uow, idempotency_key)
        if snapshot is None:
            raise SofiasMemoryError(
                code=ErrorCode.INTERNAL_ERROR,
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                message="Idempotency-Key race lost but no winner row was found.",
            )
        _require_same_operation_and_digest(
            snapshot, operation=CognitiveMemoryOperation.FORGET, digest=digest
        )
        return await self._hydrate(uow, memory_id)

    async def _find_ledger_snapshot(
        self, uow: PostgresUnitOfWork, idempotency_key: str
    ) -> _LedgerSnapshot | None:
        existing = await uow.cognitive_memory_idempotency.get_by_key(idempotency_key)
        return _snapshot(existing) if existing is not None else None

    async def _pre_check(self, idempotency_key: str, digest: str) -> MemoryItemResult | None:
        """Optimization only (Feature Contract SS 12.1/17.2) -- never the
        race authority. A miss here always falls through to embedding + the
        authoritative transactional claim; a hit avoids an unnecessary
        embedding call."""

        async with self._unit_of_work_factory() as uow:
            snapshot = await self._find_ledger_snapshot(uow, idempotency_key)
            if snapshot is None:
                return None
            _require_same_operation_and_digest(
                snapshot, operation=CognitiveMemoryOperation.CREATE, digest=digest
            )
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

        snapshot = await self._find_ledger_snapshot(uow, idempotency_key)
        if snapshot is None:
            raise SofiasMemoryError(
                code=ErrorCode.INTERNAL_ERROR,
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                message="Idempotency-Key race lost but no winner row was found.",
            )
        _require_same_operation_and_digest(
            snapshot, operation=CognitiveMemoryOperation.CREATE, digest=digest
        )
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
