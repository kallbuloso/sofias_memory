"""ContextCheckpoint service (SM-CTX-002/SM-CTX-003, ADR-0017/ADR-0018,
Contract v1 Contextual Project Memory SS 9/23-36/64/66-67, handoffs 10/11).

Every mutation is synchronous and PostgreSQL-authoritative: one short
transaction holds the resource change *and* its idempotency outcome. There is
no provider call, no embedding, no PipelineRun, no graph_outbox, no Neo4j.

Create current (``create_current``)
    Allowed only when the Context Stream ``(scope, context_key)`` has no ACTIVE
    checkpoint. Creates for one stream are serialized by a PostgreSQL
    transaction-scoped advisory lock (:func:``sofias_memory.domain.context_stream_lock_key``)
    taken *before* anything is read, so both the brand-new-stream race and the
    recovery-create race (history without ACTIVE) have exactly one winner:

    1. lock stream;
    2. idempotency ledger lookup -- a same-key replay wins over every state
       concern (Contract SS 36, handoff 10 SS 51);
    3. ACTIVE exists -> 409 ``CONTEXT_STATE_CONFLICT`` (no revision computed);
    4. revision = ``MAX(revision) + 1`` over every lifecycle (tombstones too);
    5. insert checkpoint + provenance + statements + ledger in one SAVEPOINT.

    The loser of a race re-reads *after* the winner committed (READ COMMITTED,
    the lock is acquired before the first snapshot-taking statement), so it
    observes the winner's ACTIVE (or ledger row) and never allocates another
    revision. The single-ACTIVE partial unique index and the revision unique
    constraint stay structural safety nets: their violation is reported as
    ``CONTEXT_STATE_CONFLICT``, never as a second winner. Recovery Create never
    touches historical rows -- no reactivation, no supersession lineage.

Supersede (``supersede``, SM-CTX-003)
    The path id is the caller's ``expected_current_checkpoint_id``. Order inside
    one transaction (handoff 11 SS 33/44):

    1. claim the idempotency key in a SAVEPOINT *before* touching the target --
       a same-key replay (or IDEMPOTENCY_CONFLICT) always wins over any
       lifecycle/staleness rejection, even after the target became SUPERSEDED;
    2. row-lock the target (``SELECT ... FOR UPDATE``): concurrent supersedes,
       invalidates and forgets of the same checkpoint are serialized, and a
       loser re-reads the committed lifecycle after the winner's COMMIT;
    3. target missing -> 404; not ACTIVE or another stream than the payload's
       -> 409 ``CONTEXT_STATE_CONFLICT`` (expected/current ids in details); the
       whole transaction rolls back, including the claim -- a stale writer
       leaves no checkpoint, statement, provenance or ledger row;
    4. defer the lineage FK for this transaction, mark the target SUPERSEDED
       (``superseded_at``, ``superseded_by`` = pre-generated replacement id) and
       flush, then insert the replacement ACTIVE (``revision = old + 1``), its
       new statements and new provenance; COMMIT checks the FK.

    Old transition and replacement creation are therefore atomic. No retry or
    merge against the new current is ever attempted.

Invalidate/Forget
    Claim the ledger in a SAVEPOINT (replay before any lifecycle rejection),
    then row-lock the target, then apply the transition -- the same
    key-then-row acquisition order as Supersede (handoff 10 SS 73-74,
    handoff 11 SS 33), so mutations sharing a key cannot deadlock. ACTIVE and SUPERSEDED can
    be invalidated or forgotten; ``superseded_at``/``superseded_by`` lineage is
    kept on both. Forget hard-deletes the semantic statements, scrubs external
    provenance refs and the invalidation reason, and keeps
    ``scope``/``context_key``/``revision``/timestamps/lineage (Contract SS 67).
    Neither follows lineage: descendants are never invalidated or forgotten.

A checkpoint is not an Evidence source type: its own Invalidate/Forget start no
inbound propagation (handoff 13 SS 39). Its statements are converged by
``ContextualEvidenceInvalidator`` when Evidence they declare is forgotten
(SM-CTX-005). Lineage is not Evidence.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError, IntegrityError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    CheckpointTransition,
    ContextCheckpointLifecycle,
    ContextStatementType,
    ContextualIdempotencyRequest,
    ContextualMemoryOperation,
    SupportMode,
    build_contextual_canonical_request,
    canonical_support_payload,
    checkpoint_forget_transition,
    checkpoint_invalidate_transition,
    compute_contextual_request_digest,
    context_stream_lock_key,
    is_reserved_idempotency_key,
    next_checkpoint_revision,
    normalize_contextual_scope,
    protect_contextual_idempotency_key,
    resolve_context_key,
)
from sofias_memory.infrastructure.postgres.models import (
    ContextCheckpoint,
    ContextCheckpointProvenance,
    ContextCheckpointStatement,
    ContextualMemoryIdempotency,
)
from sofias_memory.infrastructure.postgres.models.context_checkpoint import (
    ACTIVE_STREAM_UNIQUE_INDEX,
    STREAM_REVISION_UNIQUE_CONSTRAINT,
    SUPERSEDED_BY_UNIQUE_INDEX,
)
from sofias_memory.infrastructure.postgres.repositories.context_checkpoints import (
    ContextCheckpointPosition,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.observability.logging import get_logger
from sofias_memory.schemas.common import ErrorCode, utc_now
from sofias_memory.schemas.context_checkpoints import (
    ContextCheckpointCreateRequest,
    ContextCheckpointInvalidateRequest,
    ContextCheckpointListResult,
    ContextCheckpointProvenanceResult,
    ContextCheckpointResult,
    ContextCheckpointSnapshotFields,
    ContextCheckpointSupersedeRequest,
    ContextStatementCreateRequest,
    ContextStatementResult,
)
from sofias_memory.schemas.contextual_evidence import SupportRequest, SupportResult
from sofias_memory.services.contextual_evidence import (
    all_support_keys,
    hydrate_statement_support,
    lock_and_resolve,
    persist_statement_support,
    raise_if_transient_conflict,
    validate_new_support,
)
from sofias_memory.services.pipeline_submission import (
    idempotency_conflict_error,
    reserved_idempotency_key_namespace_error,
)

CONTEXTUAL_IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT = "uq_contextual_memory_idempotency_idempotency_key"
"""The one constraint whose violation is reinterpreted as an idempotency race."""

_STREAM_SAFETY_NET_CONSTRAINTS = frozenset(
    {ACTIVE_STREAM_UNIQUE_INDEX, STREAM_REVISION_UNIQUE_CONSTRAINT, SUPERSEDED_BY_UNIQUE_INDEX}
)
"""Structural single-ACTIVE/revision/linear-lineage guarantees; reported as
CONTEXT_STATE_CONFLICT, never as a second winner."""

_CURSOR_VERSION = 1

_SCALAR_FIELDS: tuple[tuple[str, ContextStatementType], ...] = (
    ("objective", ContextStatementType.OBJECTIVE),
    ("current_state", ContextStatementType.CURRENT_STATE),
    ("next_action", ContextStatementType.NEXT_ACTION),
)
_COLLECTION_FIELDS: tuple[tuple[str, ContextStatementType], ...] = (
    ("approved_decisions", ContextStatementType.APPROVED_DECISION),
    ("pending_work", ContextStatementType.PENDING_WORK),
    ("open_items", ContextStatementType.OPEN_ITEM),
    ("blockers", ContextStatementType.BLOCKER),
)

type UnitOfWorkFactory = Callable[[], PostgresUnitOfWork]

_logger = get_logger(__name__)


# --- errors ---------------------------------------------------------------------


def checkpoint_not_found_error(checkpoint_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.CHECKPOINT_NOT_FOUND,
        status_code=HTTPStatus.NOT_FOUND,
        message="Context checkpoint does not exist.",
        details={"checkpoint_id": str(checkpoint_id)},
    )


def current_checkpoint_not_found_error(scope: str, context_key: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.CURRENT_CHECKPOINT_NOT_FOUND,
        status_code=HTTPStatus.NOT_FOUND,
        message="The Context Stream has no current checkpoint.",
        details={"scope": scope, "context_key": context_key},
    )


def context_state_conflict_error(
    current_checkpoint_id: UUID | None,
    *,
    expected_checkpoint_id: UUID | None = None,
) -> SofiasMemoryError:
    """Create: the stream already has a current checkpoint. Supersede (stale
    writer): ``expected_checkpoint_id`` is not the stream's current checkpoint;
    ``current_checkpoint_id`` is ``null`` when the stream has none. Only ids --
    never semantic content."""

    details: dict[str, str | None] = {
        "current_checkpoint_id": (
            str(current_checkpoint_id) if current_checkpoint_id is not None else None
        )
    }
    message = "The Context Stream already has a current checkpoint."
    if expected_checkpoint_id is not None:
        details["expected_checkpoint_id"] = str(expected_checkpoint_id)
        message = "The expected checkpoint is not the current checkpoint of its Context Stream."
    return SofiasMemoryError(
        code=ErrorCode.CONTEXT_STATE_CONFLICT,
        status_code=HTTPStatus.CONFLICT,
        message=message,
        details=details,
    )


def checkpoint_state_conflict_error(checkpoint_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.CHECKPOINT_STATE_CONFLICT,
        status_code=HTTPStatus.CONFLICT,
        message="Context checkpoint is not in a state that allows this operation.",
        details={"checkpoint_id": str(checkpoint_id)},
    )


def _invalid_cursor_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        message="cursor is invalid.",
        details={"field": "cursor"},
    )


def _internal_error(message: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message=message,
    )


def _constraint_name(error: IntegrityError) -> str | None:
    orig = error.orig
    name = getattr(orig, "constraint_name", None) or getattr(
        getattr(orig, "__cause__", None), "constraint_name", None
    )
    return cast(str | None, name)


# --- cursor ---------------------------------------------------------------------


def encode_checkpoint_cursor(position: ContextCheckpointPosition) -> str:
    """Opaque keyset cursor for (context_key ASC, revision DESC, id ASC)."""

    raw = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "k": position.context_key,
            "r": position.revision,
            "i": str(position.checkpoint_id),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_checkpoint_cursor(cursor: str) -> ContextCheckpointPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise ValueError("unsupported cursor")
        context_key = payload["k"]
        revision = payload["r"]
        if not isinstance(context_key, str) or type(revision) is not int:
            raise ValueError("malformed cursor")
        return ContextCheckpointPosition(
            context_key=context_key, revision=revision, checkpoint_id=UUID(str(payload["i"]))
        )
    except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error) as error:
        raise _invalid_cursor_error() from error


# --- result conversion ------------------------------------------------------------


def checkpoint_result(
    checkpoint: ContextCheckpoint,
    provenance: ContextCheckpointProvenance,
    statements: Sequence[ContextCheckpointStatement],
    supports: Mapping[UUID, SupportResult | None] | None = None,
) -> ContextCheckpointResult:
    """The one row -> public shape conversion. Statements are grouped by type
    and ordered by ``ordinal``; never exposes ``invalidation_reason`` or any
    ledger value."""

    by_type: dict[ContextStatementType, list[ContextCheckpointStatement]] = {}
    for statement in sorted(statements, key=lambda item: item.ordinal):
        by_type.setdefault(statement.statement_type, []).append(statement)

    def collection(statement_type: ContextStatementType) -> list[ContextStatementResult]:
        return [
            ContextStatementResult(
                statement_id=item.id,
                text=item.content,
                support=(supports or {}).get(item.id),
            )
            for item in by_type.get(statement_type, [])
        ]

    def scalar(statement_type: ContextStatementType) -> ContextStatementResult | None:
        items = collection(statement_type)
        if len(items) > 1:
            raise _internal_error("Context checkpoint has a duplicated scalar statement.")
        return items[0] if items else None

    return ContextCheckpointResult(
        checkpoint_id=checkpoint.id,
        scope=checkpoint.scope,
        context_key=checkpoint.context_key,
        revision=checkpoint.revision,
        lifecycle=checkpoint.lifecycle,
        objective=scalar(ContextStatementType.OBJECTIVE),
        current_state=scalar(ContextStatementType.CURRENT_STATE),
        approved_decisions=collection(ContextStatementType.APPROVED_DECISION),
        pending_work=collection(ContextStatementType.PENDING_WORK),
        open_items=collection(ContextStatementType.OPEN_ITEM),
        blockers=collection(ContextStatementType.BLOCKER),
        next_action=scalar(ContextStatementType.NEXT_ACTION),
        provenance=ContextCheckpointProvenanceResult(
            origin_kind=provenance.origin_kind,
            source_system=provenance.source_system,
            conversation_ref=provenance.conversation_ref,
            turn_ref=provenance.turn_ref,
        ),
        created_at=checkpoint.created_at,
        superseded_at=checkpoint.superseded_at,
        superseded_by=checkpoint.superseded_by,
        invalidated_at=checkpoint.invalidated_at,
        forgotten_at=checkpoint.forgotten_at,
    )


def statement_canonical(statement: ContextStatementCreateRequest) -> object:
    """A statement without support keeps its exact pre-SM-CTX-004 canonical form
    (the bare text), so historical digests are unchanged; a supported statement
    becomes ``{"text", "support": mode + canonical Evidence set}`` (handoff 12 SS 72/73)."""

    if statement.support is None:
        return statement.text
    return {
        "text": statement.text,
        "support": canonical_support_payload(statement.support.mode, statement.support.keys()),
    }


def snapshot_supports(request: ContextCheckpointSnapshotFields) -> list[SupportRequest | None]:
    """Every statement-level support declared by a snapshot (for validation)."""

    items: list[ContextStatementCreateRequest | None] = [
        request.objective,
        request.current_state,
        request.next_action,
        *request.approved_decisions,
        *request.pending_work,
        *request.open_items,
        *request.blockers,
    ]
    return [item.support for item in items if item is not None]


def canonical_create_payload(request: ContextCheckpointSnapshotFields) -> dict[str, object]:
    """Every semantic Create field (handoff 10 SS 66) with the *resolved*
    ``context_key`` and arrays in caller order (order is semantic, SS 67).
    Never a generated id, ``created_at``, the server-allocated revision,
    transport metadata, or a secret."""

    def text(statement: ContextStatementCreateRequest | None) -> object:
        return None if statement is None else statement_canonical(statement)

    return {
        "scope": request.scope,
        "context_key": request.context_key,
        "objective": text(request.objective),
        "current_state": statement_canonical(request.current_state),
        "approved_decisions": [statement_canonical(i) for i in request.approved_decisions],
        "pending_work": [statement_canonical(i) for i in request.pending_work],
        "open_items": [statement_canonical(i) for i in request.open_items],
        "blockers": [statement_canonical(i) for i in request.blockers],
        "next_action": text(request.next_action),
        "provenance": {
            "origin_kind": request.provenance.origin_kind.value,
            "source_system": request.provenance.source_system,
            "conversation_ref": request.provenance.conversation_ref,
            "turn_ref": request.provenance.turn_ref,
        },
    }


def build_checkpoint_statements(
    checkpoint_id: UUID, request: ContextCheckpointSnapshotFields
) -> list[ContextCheckpointStatement]:
    """Scalars at ordinal 0; collections keep the caller order as 0..n-1."""

    return build_checkpoint_statements_with_support(checkpoint_id, request)[0]


def build_checkpoint_statements_with_support(
    checkpoint_id: UUID, request: ContextCheckpointSnapshotFields
) -> tuple[list[ContextCheckpointStatement], dict[UUID, SupportRequest]]:
    """Rows (with ``support_mode``) plus the statement-level support to link."""

    supports: dict[UUID, SupportRequest] = {}
    rows: list[ContextCheckpointStatement] = []
    for field, statement_type in _SCALAR_FIELDS:
        value = cast(ContextStatementCreateRequest | None, getattr(request, field))
        if value is not None:
            rows.append(
                ContextCheckpointStatement(
                    id=uuid4(),
                    checkpoint_id=checkpoint_id,
                    statement_type=statement_type,
                    ordinal=0,
                    content=value.text,
                )
            )
    for field, statement_type in _COLLECTION_FIELDS:
        items = cast(list[ContextStatementCreateRequest], getattr(request, field))
        for ordinal, item in enumerate(items):
            rows.append(
                ContextCheckpointStatement(
                    id=uuid4(),
                    checkpoint_id=checkpoint_id,
                    statement_type=statement_type,
                    ordinal=ordinal,
                    content=item.text,
                )
            )
    for row, item in zip(rows, _ordered_inputs(request), strict=True):
        if item.support is not None:
            row.support_mode = item.support.mode
            supports[row.id] = item.support
    return rows, supports


def _ordered_inputs(
    request: ContextCheckpointSnapshotFields,
) -> list[ContextStatementCreateRequest]:
    """Inputs in exactly the order build_checkpoint_statements_with_support emits rows."""

    out: list[ContextStatementCreateRequest] = []
    for field, _ in _SCALAR_FIELDS:
        value = cast(ContextStatementCreateRequest | None, getattr(request, field))
        if value is not None:
            out.append(value)
    for field, _ in _COLLECTION_FIELDS:
        out.extend(cast(list[ContextStatementCreateRequest], getattr(request, field)))
    return out


@dataclass(frozen=True, slots=True)
class _LedgerSnapshot:
    operation: ContextualMemoryOperation
    request_digest: str
    result_resource_id: UUID | None


class ContextCheckpointService:
    """create_current/get/get_current/list/invalidate/forget for ContextCheckpoints."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    # --- Create current -------------------------------------------------------

    async def create_current(
        self, request: ContextCheckpointCreateRequest, *, idempotency_key: str
    ) -> ContextCheckpointResult:
        protected_key = self._protected_key(idempotency_key)
        operation = ContextualMemoryOperation.CHECKPOINT_CREATE
        digest = self._digest(operation, None, canonical_create_payload(request))

        replay = await self._pre_check(protected_key, operation, digest)
        if replay is not None:
            return replay

        supports = snapshot_supports(request)
        evidence_keys = all_support_keys(supports)
        try:
            return await self._create_current_locked(
                request, protected_key, operation, digest, supports, evidence_keys
            )
        except DBAPIError as error:
            raise_if_transient_conflict(error)
            raise

    async def _create_current_locked(
        self,
        request: ContextCheckpointCreateRequest,
        protected_key: str,
        operation: ContextualMemoryOperation,
        digest: str,
        supports: list[SupportRequest | None],
        evidence_keys: list[Any],
    ) -> ContextCheckpointResult:
        scope = request.scope
        context_key = request.context_key
        async with self._unit_of_work_factory() as uow:
            # Global order: Evidence rows (FOR SHARE) -> stream advisory -> ledger.
            resolutions = await lock_and_resolve(uow, evidence_keys)
            await uow.context_checkpoints.lock_stream(context_stream_lock_key(scope, context_key))

            snapshot = await self._find_snapshot(uow, protected_key)
            if snapshot is not None:
                result = await self._replay_from_snapshot(uow, snapshot, operation, digest)
                await uow.commit()
                return result

            active = await uow.context_checkpoints.get_active(scope=scope, context_key=context_key)
            if active is not None:
                _logger.info(
                    "checkpoint_conflict",
                    operation=operation.value,
                    current_checkpoint_id=str(active.id),
                )
                raise context_state_conflict_error(active.id)

            if evidence_keys:
                await validate_new_support(
                    uow, owner_scope=scope, supports=supports, resolutions=resolutions
                )

            revision = next_checkpoint_revision(
                await uow.context_checkpoints.max_revision(scope=scope, context_key=context_key)
            )
            checkpoint = ContextCheckpoint(
                id=uuid4(),
                scope=scope,
                context_key=context_key,
                revision=revision,
                lifecycle=ContextCheckpointLifecycle.ACTIVE,
            )
            provenance = ContextCheckpointProvenance(
                checkpoint_id=checkpoint.id,
                origin_kind=request.provenance.origin_kind,
                source_system=request.provenance.source_system,
                conversation_ref=request.provenance.conversation_ref,
                turn_ref=request.provenance.turn_ref,
            )
            statements, statement_supports = build_checkpoint_statements_with_support(
                checkpoint.id, request
            )
            ledger_entry = ContextualMemoryIdempotency(
                id=uuid4(),
                idempotency_key=protected_key,
                operation=operation,
                request_digest=digest,
                target_resource_id=None,
                result_resource_id=checkpoint.id,
            )
            try:
                async with uow.savepoint():
                    await uow.context_checkpoints.add(checkpoint)
                    await uow.context_checkpoint_provenance.add(provenance)
                    await uow.context_checkpoint_statements.add_all(statements)
                    if statement_supports:
                        await persist_statement_support(uow, statement_supports)
                    await uow.contextual_memory_idempotency.add(ledger_entry)
            except IntegrityError as error:
                name = _constraint_name(error)
                if name == CONTEXTUAL_IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT:
                    result = await self._resolve_winner(uow, protected_key, operation, digest)
                    await uow.commit()
                    return result
                if name in _STREAM_SAFETY_NET_CONSTRAINTS:
                    _logger.info("checkpoint_conflict", operation=operation.value)
                    raise context_state_conflict_error(None) from error
                raise

            result = await self._hydrate(uow, checkpoint.id, missing_is_internal=True)
            await uow.commit()

        _logger.info(
            "checkpoint_created",
            operation=operation.value,
            checkpoint_id=str(result.checkpoint_id),
            revision=result.revision,
            lifecycle=result.lifecycle.value,
        )
        return result

    # --- Reads ----------------------------------------------------------------

    async def get(self, checkpoint_id: UUID) -> ContextCheckpointResult:
        async with self._unit_of_work_factory() as uow:
            return await self._hydrate(uow, checkpoint_id, missing_is_internal=False)

    async def get_current(self, *, scope: str, context_key: str | None) -> ContextCheckpointResult:
        """Only ACTIVE -- never the latest INVALIDATED/FORGOTTEN/SUPERSEDED."""

        normalized_scope = normalize_contextual_scope(scope)
        resolved_key = resolve_context_key(context_key)
        async with self._unit_of_work_factory() as uow:
            active = await uow.context_checkpoints.get_active(
                scope=normalized_scope, context_key=resolved_key
            )
            if active is None:
                raise current_checkpoint_not_found_error(normalized_scope, resolved_key)
            return (await self._hydrate_many(uow, [active]))[0]

    async def list_checkpoints(
        self,
        *,
        scope: str,
        context_key: str | None,
        lifecycle: ContextCheckpointLifecycle | None,
        limit: int,
        cursor: str | None,
    ) -> ContextCheckpointListResult:
        """``lifecycle`` omitted means ACTIVE only (handoff 10 SS 55)."""

        after = decode_checkpoint_cursor(cursor) if cursor is not None else None
        async with self._unit_of_work_factory() as uow:
            rows = await uow.context_checkpoints.list_page(
                scope=scope,
                context_key=context_key,
                lifecycle=lifecycle or ContextCheckpointLifecycle.ACTIVE,
                after=after,
                limit=limit + 1,
            )
            page = rows[:limit]
            items = await self._hydrate_many(uow, page)

        next_cursor: str | None = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = encode_checkpoint_cursor(
                ContextCheckpointPosition(
                    context_key=last.context_key,
                    revision=last.revision,
                    checkpoint_id=last.checkpoint_id,
                )
            )
        return ContextCheckpointListResult(items=items, limit=limit, next_cursor=next_cursor)

    # --- Supersede (SM-CTX-003) -----------------------------------------------

    async def supersede(
        self,
        checkpoint_id: UUID,
        request: ContextCheckpointSupersedeRequest,
        *,
        idempotency_key: str,
    ) -> ContextCheckpointResult:
        """Atomically replace the ACTIVE ``checkpoint_id`` (the caller's expected
        current) by a complete new snapshot with ``revision = old + 1``."""

        protected_key = self._protected_key(idempotency_key)
        operation = ContextualMemoryOperation.CHECKPOINT_SUPERSEDE
        digest = self._digest(operation, checkpoint_id, canonical_create_payload(request))

        replay = await self._pre_check(protected_key, operation, digest)
        if replay is not None:
            return replay

        try:
            return await self._supersede_locked(
                checkpoint_id, request, protected_key, operation, digest
            )
        except DBAPIError as error:
            raise_if_transient_conflict(error)
            raise

    async def _supersede_locked(
        self,
        checkpoint_id: UUID,
        request: ContextCheckpointSupersedeRequest,
        protected_key: str,
        operation: ContextualMemoryOperation,
        digest: str,
    ) -> ContextCheckpointResult:
        supports = snapshot_supports(request)
        evidence_keys = all_support_keys(supports)
        replacement_id = uuid4()
        async with self._unit_of_work_factory() as uow:
            # 0. Evidence rows (FOR SHARE) precede the ledger key and the row lock.
            resolutions = await lock_and_resolve(uow, evidence_keys)
            # 1. Idempotency first: replay/IDEMPOTENCY_CONFLICT beat lifecycle.
            replayed = await self._claim(
                uow,
                protected_key,
                operation,
                digest,
                checkpoint_id,
                result_resource_id=replacement_id,
            )
            if replayed is not None:
                await uow.commit()
                return replayed

            # 2. Serialize every lifecycle mutation of the expected current.
            target = await uow.context_checkpoints.get_by_id_for_update(checkpoint_id)
            if target is None:
                raise checkpoint_not_found_error(checkpoint_id)

            # 3. Expected-current: must be ACTIVE and in the payload's stream.
            if (
                target.lifecycle is not ContextCheckpointLifecycle.ACTIVE
                or target.scope != request.scope
                or target.context_key != request.context_key
            ):
                current = await uow.context_checkpoints.get_active(
                    scope=target.scope, context_key=target.context_key
                )
                current_id = current.id if current is not None else None
                _logger.info(
                    "checkpoint_conflict",
                    operation=operation.value,
                    expected_checkpoint_id=str(checkpoint_id),
                    current_checkpoint_id=str(current_id) if current_id else None,
                )
                raise context_state_conflict_error(current_id, expected_checkpoint_id=checkpoint_id)

            # 3b. New support is validated against current authoritative state.
            if evidence_keys:
                await validate_new_support(
                    uow, owner_scope=target.scope, supports=supports, resolutions=resolutions
                )

            # 4. Atomic transition + replacement (FK checked at COMMIT).
            old_revision = target.revision
            replacement = ContextCheckpoint(
                id=replacement_id,
                scope=target.scope,
                context_key=target.context_key,
                revision=old_revision + 1,
                lifecycle=ContextCheckpointLifecycle.ACTIVE,
            )
            provenance = ContextCheckpointProvenance(
                checkpoint_id=replacement_id,
                origin_kind=request.provenance.origin_kind,
                source_system=request.provenance.source_system,
                conversation_ref=request.provenance.conversation_ref,
                turn_ref=request.provenance.turn_ref,
            )
            statements, statement_supports = build_checkpoint_statements_with_support(
                replacement_id, request
            )
            try:
                async with uow.savepoint():
                    await uow.context_checkpoints.defer_lineage_foreign_key()
                    target.lifecycle = ContextCheckpointLifecycle.SUPERSEDED
                    target.superseded_at = utc_now()
                    target.superseded_by = replacement_id
                    await uow.flush()
                    await uow.context_checkpoints.add(replacement)
                    await uow.context_checkpoint_provenance.add(provenance)
                    await uow.context_checkpoint_statements.add_all(statements)
                    if statement_supports:
                        await persist_statement_support(uow, statement_supports)
            except IntegrityError as error:
                if _constraint_name(error) in _STREAM_SAFETY_NET_CONSTRAINTS:
                    _logger.info("checkpoint_conflict", operation=operation.value)
                    raise context_state_conflict_error(
                        None, expected_checkpoint_id=checkpoint_id
                    ) from error
                raise

            result = await self._hydrate(uow, replacement_id, missing_is_internal=True)
            await uow.commit()

        _logger.info(
            "checkpoint_superseded",
            operation=operation.value,
            target_checkpoint_id=str(checkpoint_id),
            replacement_checkpoint_id=str(result.checkpoint_id),
            old_revision=old_revision,
            new_revision=result.revision,
        )
        return result

    # --- Invalidate -----------------------------------------------------------

    async def invalidate(
        self,
        checkpoint_id: UUID,
        request: ContextCheckpointInvalidateRequest,
        *,
        idempotency_key: str,
    ) -> ContextCheckpointResult:
        protected_key = self._protected_key(idempotency_key)
        operation = ContextualMemoryOperation.CHECKPOINT_INVALIDATE
        digest = self._digest(operation, checkpoint_id, {"reason": request.reason})

        replay = await self._pre_check(protected_key, operation, digest)
        if replay is not None:
            return replay

        async with self._unit_of_work_factory() as uow:
            # Global acquisition order (see _claim): ledger key, then row lock.
            replayed = await self._claim(uow, protected_key, operation, digest, checkpoint_id)
            if replayed is not None:
                await uow.commit()
                return replayed

            checkpoint = await uow.context_checkpoints.get_by_id_for_update(checkpoint_id)
            if checkpoint is None:
                raise checkpoint_not_found_error(checkpoint_id)

            transition = checkpoint_invalidate_transition(checkpoint.lifecycle)
            if transition is CheckpointTransition.CONFLICT:
                raise checkpoint_state_conflict_error(checkpoint_id)
            if transition is CheckpointTransition.APPLY:
                checkpoint.lifecycle = ContextCheckpointLifecycle.INVALIDATED
                checkpoint.invalidated_at = utc_now()
                checkpoint.invalidation_reason = request.reason
            # NO_OP: already INVALIDATED -- 200 state no-op, original reason kept.

            await uow.flush()
            result = await self._hydrate(uow, checkpoint_id, missing_is_internal=True)
            await uow.commit()

        _logger.info(
            "checkpoint_invalidated",
            operation=operation.value,
            checkpoint_id=str(checkpoint_id),
            revision=result.revision,
            lifecycle=result.lifecycle.value,
            no_op=transition is CheckpointTransition.NO_OP,
        )
        return result

    # --- Forget ---------------------------------------------------------------

    async def forget(self, checkpoint_id: UUID, *, idempotency_key: str) -> ContextCheckpointResult:
        """Direct, destructive Forget (Contract v1 SS 34/67): deletes every
        semantic statement, scrubs external provenance refs and the
        invalidation reason; keeps checkpoint id, ``scope``, ``context_key``,
        ``revision``, lifecycle, timestamps and minimal provenance. No
        inbound propagation (a checkpoint is not Evidence, handoff 13 SS 39), no
        restore, no background job."""

        protected_key = self._protected_key(idempotency_key)
        operation = ContextualMemoryOperation.CHECKPOINT_FORGET
        digest = self._digest(operation, checkpoint_id, {})

        replay = await self._pre_check(protected_key, operation, digest)
        if replay is not None:
            return replay

        async with self._unit_of_work_factory() as uow:
            # Global acquisition order (see _claim): ledger key, then row locks.
            replayed = await self._claim(uow, protected_key, operation, digest, checkpoint_id)
            if replayed is not None:
                await uow.commit()
                return replayed

            checkpoint = await uow.context_checkpoints.get_by_id_for_update(checkpoint_id)
            if checkpoint is None:
                raise checkpoint_not_found_error(checkpoint_id)
            provenance = await uow.context_checkpoint_provenance.get_by_checkpoint_id_for_update(
                checkpoint_id
            )
            if provenance is None:
                raise _internal_error("Context checkpoint provenance is missing.")

            transition = checkpoint_forget_transition(checkpoint.lifecycle)
            if transition is CheckpointTransition.CONFLICT:
                raise checkpoint_state_conflict_error(checkpoint_id)
            if transition is CheckpointTransition.APPLY:
                await uow.context_checkpoint_statements.delete_by_checkpoint_id(checkpoint_id)
                checkpoint.invalidation_reason = None
                checkpoint.lifecycle = ContextCheckpointLifecycle.FORGOTTEN
                checkpoint.forgotten_at = utc_now()
                provenance.conversation_ref = None
                provenance.turn_ref = None
            # NO_OP: already FORGOTTEN -- terminal, 200 state no-op.

            await uow.flush()
            result = await self._hydrate(uow, checkpoint_id, missing_is_internal=True)
            await uow.commit()

        _logger.info(
            "checkpoint_forgotten",
            operation=operation.value,
            checkpoint_id=str(checkpoint_id),
            revision=result.revision,
            lifecycle=result.lifecycle.value,
            no_op=transition is CheckpointTransition.NO_OP,
        )
        return result

    # --- Idempotency helpers ----------------------------------------------------

    def _secret(self) -> bytes:
        return self._settings.cognitive_idempotency_hmac_key.get_secret_value().encode("utf-8")

    def _protected_key(self, idempotency_key: str) -> str:
        if is_reserved_idempotency_key(idempotency_key):
            raise reserved_idempotency_key_namespace_error()
        return protect_contextual_idempotency_key(
            secret=self._secret(), idempotency_key=idempotency_key
        )

    def _digest(
        self,
        operation: ContextualMemoryOperation,
        target_resource_id: UUID | None,
        payload: dict[str, object],
    ) -> str:
        canonical_request = build_contextual_canonical_request(
            ContextualIdempotencyRequest(
                operation=operation, target_resource_id=target_resource_id, payload=payload
            )
        )
        return compute_contextual_request_digest(
            secret=self._secret(), canonical_request=canonical_request
        )

    async def _find_snapshot(
        self, uow: PostgresUnitOfWork, protected_key: str
    ) -> _LedgerSnapshot | None:
        entry = await uow.contextual_memory_idempotency.get_by_protected_key(protected_key)
        if entry is None:
            return None
        return _LedgerSnapshot(
            operation=entry.operation,
            request_digest=entry.request_digest,
            result_resource_id=entry.result_resource_id,
        )

    async def _replay_from_snapshot(
        self,
        uow: PostgresUnitOfWork,
        snapshot: _LedgerSnapshot,
        operation: ContextualMemoryOperation,
        digest: str,
    ) -> ContextCheckpointResult:
        """Same key bound to another operation (an Episode operation included)
        or another semantic request -> 409 IDEMPOTENCY_CONFLICT, always before
        any lifecycle/state concern. Otherwise the result checkpoint is
        re-hydrated in its current state."""

        if snapshot.operation is not operation or snapshot.request_digest != digest:
            raise idempotency_conflict_error()
        if snapshot.result_resource_id is None:
            raise _internal_error("Idempotency ledger entry has no result_resource_id.")
        return await self._hydrate(uow, snapshot.result_resource_id, missing_is_internal=True)

    async def _pre_check(
        self, protected_key: str, operation: ContextualMemoryOperation, digest: str
    ) -> ContextCheckpointResult | None:
        """Optimization only -- never the race authority."""

        async with self._unit_of_work_factory() as uow:
            snapshot = await self._find_snapshot(uow, protected_key)
            if snapshot is None:
                return None
            return await self._replay_from_snapshot(uow, snapshot, operation, digest)

    async def _resolve_winner(
        self,
        uow: PostgresUnitOfWork,
        protected_key: str,
        operation: ContextualMemoryOperation,
        digest: str,
    ) -> ContextCheckpointResult:
        snapshot = await self._find_snapshot(uow, protected_key)
        if snapshot is None:
            raise _internal_error("Idempotency-Key race lost but no winner row was found.")
        return await self._replay_from_snapshot(uow, snapshot, operation, digest)

    async def _claim(
        self,
        uow: PostgresUnitOfWork,
        protected_key: str,
        operation: ContextualMemoryOperation,
        digest: str,
        checkpoint_id: UUID,
        *,
        result_resource_id: UUID | None = None,
    ) -> ContextCheckpointResult | None:
        """Authoritative claim for a targeted mutation, in its own SAVEPOINT.
        Returns the replayed winner when the key was already claimed. The claim
        is only durable if the surrounding transaction commits: any later
        rejection rolls it back with everything else.

        Lock-order policy: every checkpoint mutation (supersede, invalidate,
        forget) calls this *before* taking any checkpoint row lock. No
        transaction therefore ever holds a checkpoint row lock while waiting
        for a ledger key held by another transaction, which removes the
        same-key cross-operation deadlock cycle. Create holds only its stream
        advisory lock (never a checkpoint row lock) while it writes its key."""

        ledger_entry = ContextualMemoryIdempotency(
            id=uuid4(),
            idempotency_key=protected_key,
            operation=operation,
            request_digest=digest,
            target_resource_id=checkpoint_id,
            result_resource_id=result_resource_id or checkpoint_id,
        )
        try:
            async with uow.savepoint():
                await uow.contextual_memory_idempotency.add(ledger_entry)
        except IntegrityError as error:
            if _constraint_name(error) != CONTEXTUAL_IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT:
                raise
            return await self._resolve_winner(uow, protected_key, operation, digest)
        return None

    async def _hydrate(
        self, uow: PostgresUnitOfWork, checkpoint_id: UUID, *, missing_is_internal: bool
    ) -> ContextCheckpointResult:
        checkpoint = await uow.context_checkpoints.get_by_id(checkpoint_id)
        if checkpoint is None:
            if missing_is_internal:
                raise _internal_error(
                    "Contextual idempotency ledger references a missing checkpoint."
                )
            raise checkpoint_not_found_error(checkpoint_id)
        return (await self._hydrate_many(uow, [checkpoint]))[0]

    async def _hydrate_many(
        self, uow: PostgresUnitOfWork, checkpoints: Sequence[ContextCheckpoint]
    ) -> list[ContextCheckpointResult]:
        return await hydrate_checkpoints(uow, checkpoints)


async def hydrate_checkpoints(
    uow: PostgresUnitOfWork, checkpoints: Sequence[ContextCheckpoint]
) -> list[ContextCheckpointResult]:
    """Rows -> public shapes with current statement Evidence availability
    (read-only; shared by the checkpoint reads and Contextual Recall)."""

    results, _ = await hydrate_checkpoints_with_support_modes(uow, checkpoints)
    return results


async def hydrate_checkpoints_with_support_modes(
    uow: PostgresUnitOfWork, checkpoints: Sequence[ContextCheckpoint]
) -> tuple[list[ContextCheckpointResult], dict[UUID, SupportMode | None]]:
    """``hydrate_checkpoints`` plus the persisted ``support_mode`` of every
    hydrated statement (keyed by statement id), read from the same rows. Lets
    Contextual Recall tell a declared support whose links are missing from an
    independent assertion (``support_mode=None``) -- the public shape alone
    cannot."""

    ids = [checkpoint.id for checkpoint in checkpoints]
    provenance = await uow.context_checkpoint_provenance.get_by_checkpoint_ids(ids)
    statements: dict[UUID, list[ContextCheckpointStatement]] = {}
    all_rows = await uow.context_checkpoint_statements.list_by_checkpoint_ids(ids)
    for row in all_rows:
        statements.setdefault(row.checkpoint_id, []).append(row)
    supports = await hydrate_statement_support(uow, {row.id: row.support_mode for row in all_rows})
    results: list[ContextCheckpointResult] = []
    for checkpoint in checkpoints:
        prov = provenance.get(checkpoint.id)
        if prov is None:
            raise _internal_error("Context checkpoint provenance is missing.")
        results.append(
            checkpoint_result(checkpoint, prov, statements.get(checkpoint.id, []), supports)
        )
    return results, {row.id: row.support_mode for row in all_rows}


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(session_factory)

    return create_unit_of_work


__all__ = [
    "ContextCheckpointService",
    "build_checkpoint_statements",
    "canonical_create_payload",
    "checkpoint_not_found_error",
    "checkpoint_result",
    "checkpoint_state_conflict_error",
    "context_state_conflict_error",
    "current_checkpoint_not_found_error",
    "decode_checkpoint_cursor",
    "encode_checkpoint_cursor",
    "hydrate_checkpoints",
    "hydrate_checkpoints_with_support_modes",
]
