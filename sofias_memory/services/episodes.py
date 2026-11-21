"""Contextual Episode service (SM-CTX-001, ADR-0017, Contract v1 Contextual
Project Memory SS 14-22/36/64/67).

Owns the embedding boundary exactly like ``CognitiveMemoryService``: the
external embedding call always happens before any PostgreSQL session is
opened, and the authoritative ``Idempotency-Key`` claim happens inside one
short transaction together with the Episode write -- never a PipelineRun,
never a graph_outbox event, never Neo4j.

Idempotency uses the shared, resource-generic ``contextual_memory_idempotency``
ledger. A losing race is recognised only by the
``uq_contextual_memory_idempotency_idempotency_key`` constraint name (never by
string matching) and resolved inside the same transaction after its
``SAVEPOINT`` rolled back, so a lost race leaves no orphan rows. Replay is
always resolved before any lifecycle rejection (Contract SS 36.3).

Episode mutations are explicit and supervised: nothing here creates a
``MemoryItem``, ``Session``/``SessionEntry``, Evidence, or Checkpoint, and
Forget is direct only (no propagation -- SM-CTX-005).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError, IntegrityError

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import (
    ContextualIdempotencyRequest,
    ContextualMemoryOperation,
    EpisodeKind,
    EpisodeLifecycle,
    EpisodeTransition,
    build_contextual_canonical_request,
    build_episode_embedding_text,
    canonical_support_payload,
    compute_contextual_request_digest,
    episode_forget_transition,
    episode_invalidate_transition,
    is_reserved_idempotency_key,
    normalize_contextual_scope,
    normalize_episode_text,
    protect_contextual_idempotency_key,
    validate_context_key,
)
from sofias_memory.infrastructure.postgres.models import (
    ContextualMemoryIdempotency,
    Episode,
    EpisodeProvenance,
)
from sofias_memory.infrastructure.postgres.repositories.episodes import EpisodePosition
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.observability.logging import get_logger
from sofias_memory.schemas.common import ErrorCode, utc_now
from sofias_memory.schemas.contextual_evidence import SupportResult
from sofias_memory.schemas.episodes import (
    EpisodeCreateRequest,
    EpisodeInvalidateRequest,
    EpisodeListResult,
    EpisodeProvenanceResult,
    EpisodeResult,
)
from sofias_memory.services.cognitive_memory import EmbeddingClient
from sofias_memory.services.contextual_evidence import (
    canonical_keys_or_empty,
    hydrate_episode_support,
    lock_and_resolve,
    persist_episode_support,
    raise_if_transient_conflict,
    validate_new_support,
)
from sofias_memory.services.pipeline_submission import (
    idempotency_conflict_error,
    reserved_idempotency_key_namespace_error,
)

CONTEXTUAL_IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT = "uq_contextual_memory_idempotency_idempotency_key"
"""The one constraint whose violation may be reinterpreted as a contextual
idempotency race -- any other ``IntegrityError`` propagates unchanged."""

EPISODE_SEARCH_DEFAULT_LIMIT = 10
EPISODE_SEARCH_MAX_LIMIT = 50
_CURSOR_VERSION = 1

type UnitOfWorkFactory = Callable[[], PostgresUnitOfWork]

_logger = get_logger(__name__)


def episode_not_found_error(episode_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.EPISODE_NOT_FOUND,
        status_code=HTTPStatus.NOT_FOUND,
        message="Episode does not exist.",
        details={"episode_id": str(episode_id)},
    )


def episode_state_conflict_error(episode_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.EPISODE_STATE_CONFLICT,
        status_code=HTTPStatus.CONFLICT,
        message="Episode is not in a state that allows this operation.",
        details={"episode_id": str(episode_id)},
    )


def _invalid_replacement_error(reason: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        message="replacement_episode_id is not a valid replacement for this Episode.",
        details={"field": "replacement_episode_id", "reason": reason},
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


def _is_contextual_idempotency_key_unique_violation(error: IntegrityError) -> bool:
    orig = error.orig
    constraint_name = getattr(orig, "constraint_name", None) or getattr(
        getattr(orig, "__cause__", None), "constraint_name", None
    )
    return constraint_name == CONTEXTUAL_IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT


def encode_episode_cursor(position: EpisodePosition) -> str:
    """Opaque keyset cursor for the public order (occurred_at DESC, id ASC)."""

    raw = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "o": position.occurred_at.isoformat(),
            "i": str(position.episode_id),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_episode_cursor(cursor: str) -> EpisodePosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise ValueError("unsupported cursor")
        occurred_at = datetime.fromisoformat(str(payload["o"]))
        if occurred_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return EpisodePosition(occurred_at=occurred_at, episode_id=UUID(str(payload["i"])))
    except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error) as error:
        raise _invalid_cursor_error() from error


def episode_result(
    episode: Episode,
    provenance: EpisodeProvenance,
    support: SupportResult | None = None,
) -> EpisodeResult:
    """The one Episode+provenance -> public shape conversion (Create/Get/List/
    lifecycle mutations/internal retrieval). Never exposes the embedding, the
    invalidation reason, or any ledger value. ``support`` carries current
    Evidence availability (SM-CTX-004) or ``None`` when none is declared."""

    return EpisodeResult(
        episode_id=episode.id,
        scope=episode.scope,
        context_key=episode.context_key,
        kind=episode.kind,
        summary=episode.summary,
        situation=episode.situation,
        reason=episode.reason,
        outcome=episode.outcome,
        occurred_at=episode.occurred_at,
        lifecycle=episode.lifecycle,
        confidence=episode.confidence,
        provenance=EpisodeProvenanceResult(
            origin_kind=provenance.origin_kind,
            source_system=provenance.source_system,
            conversation_ref=provenance.conversation_ref,
            turn_ref=provenance.turn_ref,
        ),
        support=support,
        created_at=episode.created_at,
        invalidated_at=episode.invalidated_at,
        forgotten_at=episode.forgotten_at,
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _canonical_create_payload(request: EpisodeCreateRequest) -> dict[str, object]:
    """Every semantic Create field (handoff 09 SS 32) -- never the embedding,
    a generated id, created_at, transport metadata, or a secret.

    SM-CTX-004 (handoff 12 SS 71/72): ``support`` is added *only when declared*,
    as mode + canonical Evidence set, so a request without support keeps the
    exact pre-SM-CTX-004 canonical form (and digest) and Evidence order never
    changes it."""

    payload: dict[str, object] = {
        "scope": request.scope,
        "context_key": request.context_key,
        "kind": request.kind.value,
        "summary": request.summary,
        "situation": request.situation,
        "reason": request.reason,
        "outcome": request.outcome,
        "occurred_at": _timestamp(request.occurred_at),
        "confidence": request.confidence,
        "provenance": {
            "origin_kind": request.provenance.origin_kind.value,
            "source_system": request.provenance.source_system,
            "conversation_ref": request.provenance.conversation_ref,
            "turn_ref": request.provenance.turn_ref,
        },
    }
    if request.support is not None:
        payload["support"] = canonical_support_payload(request.support.mode, request.support.keys())
    return payload


def _canonical_invalidate_payload(request: EpisodeInvalidateRequest) -> dict[str, object]:
    return {
        "reason": request.reason,
        "replacement_episode_id": (
            str(request.replacement_episode_id)
            if request.replacement_episode_id is not None
            else None
        ),
    }


@dataclass(frozen=True, slots=True)
class _LedgerSnapshot:
    operation: ContextualMemoryOperation
    request_digest: str
    result_resource_id: UUID | None


@dataclass(frozen=True, slots=True)
class EpisodeSearchHit:
    """One internal semantic-retrieval hit (for future Contextual Recall).
    Relevance is semantic similarity only -- never Evidence, never causality."""

    episode: EpisodeResult
    relevance: float


class EpisodeService:
    """Create/Get/List/internal semantic retrieval/Invalidate/Forget for Episodes."""

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

    # --- Create -------------------------------------------------------------

    async def create(
        self,
        request: EpisodeCreateRequest,
        *,
        idempotency_key: str,
        episode_id: UUID | None = None,
    ) -> EpisodeResult:
        """``episode_id`` is an internal/test hook only (never exposed over HTTP):
        it lets the Evidence cycle invariant be exercised (handoff 12 SS 57/104)."""

        protected_key = self._protected_key(idempotency_key)
        operation = ContextualMemoryOperation.EPISODE_CREATE
        digest = self._digest(operation, None, _canonical_create_payload(request))

        replay = await self._pre_check(protected_key, operation, digest)
        if replay is not None:
            return replay

        new_id = episode_id or uuid4()
        evidence_keys = canonical_keys_or_empty(request.support)
        if evidence_keys:
            # Fail fast before the paid embedding call; the authoritative,
            # locked validation is repeated inside the write transaction.
            async with self._unit_of_work_factory() as uow:
                resolutions = await lock_and_resolve(uow, evidence_keys, lock=False)
                await validate_new_support(
                    uow,
                    owner_scope=request.scope,
                    supports=[request.support],
                    resolutions=resolutions,
                    owner_episode_id=new_id,
                )

        embedding = await self._embed(
            build_episode_embedding_text(
                summary=request.summary,
                situation=request.situation,
                reason=request.reason,
                outcome=request.outcome,
            )
        )

        try:
            async with self._unit_of_work_factory() as uow:
                # 1. Lock Evidence rows (FOR SHARE) before the ledger.
                resolutions = await lock_and_resolve(uow, evidence_keys)
                # 2. A committed same-key winner replays before any revalidation.
                winner = await self._find_snapshot(uow, protected_key)
                if winner is not None:
                    result = await self._replay_from_snapshot(uow, winner, operation, digest)
                    await uow.commit()
                    return result
                # 3. Authoritative Evidence validation under the share locks.
                if request.support is not None:
                    await validate_new_support(
                        uow,
                        owner_scope=request.scope,
                        supports=[request.support],
                        resolutions=resolutions,
                        owner_episode_id=new_id,
                    )

                episode = Episode(
                    id=new_id,
                    scope=request.scope,
                    context_key=request.context_key,
                    kind=request.kind,
                    summary=request.summary,
                    situation=request.situation,
                    reason=request.reason,
                    outcome=request.outcome,
                    occurred_at=request.occurred_at,
                    embedding=embedding,
                    lifecycle=EpisodeLifecycle.ACTIVE,
                    confidence=request.confidence,
                    support_mode=request.support.mode if request.support else None,
                )
                provenance = EpisodeProvenance(
                    episode_id=episode.id,
                    origin_kind=request.provenance.origin_kind,
                    source_system=request.provenance.source_system,
                    conversation_ref=request.provenance.conversation_ref,
                    turn_ref=request.provenance.turn_ref,
                )
                ledger_entry = ContextualMemoryIdempotency(
                    id=uuid4(),
                    idempotency_key=protected_key,
                    operation=operation,
                    request_digest=digest,
                    target_resource_id=None,
                    result_resource_id=episode.id,
                )
                try:
                    async with uow.savepoint():
                        await uow.episodes.add(episode)
                        await uow.episode_provenance.add(provenance)
                        if request.support is not None:
                            await persist_episode_support(uow, episode.id, request.support)
                        await uow.contextual_memory_idempotency.add(ledger_entry)
                except IntegrityError as error:
                    if not _is_contextual_idempotency_key_unique_violation(error):
                        raise
                    result = await self._resolve_winner(uow, protected_key, operation, digest)
                    await uow.commit()
                    return result

                result = await self._hydrate(uow, episode.id, missing_is_internal=True)
                await uow.commit()
        except DBAPIError as error:
            raise_if_transient_conflict(error)
            raise

        _logger.info(
            "episode_created",
            operation=operation.value,
            episode_id=str(result.episode_id),
            lifecycle=result.lifecycle.value,
            support_mode=result.support.mode.value if result.support else None,
        )
        return result

    # --- Reads --------------------------------------------------------------

    async def get(self, episode_id: UUID) -> EpisodeResult:
        async with self._unit_of_work_factory() as uow:
            return await self._hydrate(uow, episode_id, missing_is_internal=False)

    async def list_episodes(
        self,
        *,
        scope: str,
        context_key: str | None,
        kind: EpisodeKind | None,
        lifecycle: EpisodeLifecycle | None,
        limit: int,
        cursor: str | None,
    ) -> EpisodeListResult:
        """``lifecycle`` omitted means ACTIVE only (Contract v1 SS 19)."""

        after = decode_episode_cursor(cursor) if cursor is not None else None
        async with self._unit_of_work_factory() as uow:
            rows = await uow.episodes.list_page(
                scope=scope,
                context_key=context_key,
                kind=kind,
                lifecycle=lifecycle or EpisodeLifecycle.ACTIVE,
                after=after,
                limit=limit + 1,
            )
            page = rows[:limit]
            supports = await hydrate_episode_support(
                uow, {row.episode.id: row.episode.support_mode for row in page}
            )
            items = [
                episode_result(row.episode, row.provenance, supports[row.episode.id])
                for row in page
            ]

        next_cursor: str | None = None
        if len(rows) > limit and items:
            last = items[-1]
            assert last.occurred_at is not None  # noqa: S101 - scoped rows are never tombstones
            next_cursor = encode_episode_cursor(
                EpisodePosition(occurred_at=last.occurred_at, episode_id=last.episode_id)
            )
        return EpisodeListResult(items=items, limit=limit, next_cursor=next_cursor)

    async def semantic_search(
        self,
        *,
        scope: str,
        query: str,
        context_key: str | None = None,
        limit: int = EPISODE_SEARCH_DEFAULT_LIMIT,
    ) -> list[EpisodeSearchHit]:
        """Internal exact-cosine retrieval over ACTIVE Episodes of one Project
        Scope (no public endpoint in SM-CTX-001). The query embedding call
        happens before the read-only session opens."""

        normalized_scope = normalize_contextual_scope(scope)
        normalized_context_key = (
            validate_context_key(context_key) if context_key is not None else None
        )
        normalized_query = normalize_episode_text(query, field="query")
        if not (1 <= limit <= EPISODE_SEARCH_MAX_LIMIT):
            raise ValueError(f"limit must be between 1 and {EPISODE_SEARCH_MAX_LIMIT}")

        query_embedding = await self._embed(normalized_query)
        async with self._unit_of_work_factory() as uow:
            hits = await uow.episodes.semantic_search(
                scope=normalized_scope,
                context_key=normalized_context_key,
                query_embedding=query_embedding,
                limit=limit,
            )
            supports = await hydrate_episode_support(
                uow, {hit.episode.id: hit.episode.support_mode for hit in hits}
            )
            return [
                EpisodeSearchHit(
                    episode=episode_result(hit.episode, hit.provenance, supports[hit.episode.id]),
                    relevance=hit.relevance,
                )
                for hit in hits
            ]

    # --- Invalidate ---------------------------------------------------------

    async def invalidate(
        self,
        episode_id: UUID,
        request: EpisodeInvalidateRequest,
        *,
        idempotency_key: str,
    ) -> EpisodeResult:
        protected_key = self._protected_key(idempotency_key)
        operation = ContextualMemoryOperation.EPISODE_INVALIDATE
        digest = self._digest(operation, episode_id, _canonical_invalidate_payload(request))

        replay = await self._pre_check(protected_key, operation, digest)
        if replay is not None:
            return replay

        replacement_id = request.replacement_episode_id
        async with self._unit_of_work_factory() as uow:
            lock_ids = [episode_id]
            if replacement_id is not None and replacement_id != episode_id:
                lock_ids.append(replacement_id)
            locked = await uow.episodes.get_by_ids_for_update(lock_ids)
            episode = locked.get(episode_id)
            if episode is None:
                raise episode_not_found_error(episode_id)

            replayed = await self._claim(uow, protected_key, operation, digest, episode_id)
            if replayed is not None:
                await uow.commit()
                return replayed

            transition = episode_invalidate_transition(episode.lifecycle)
            if transition is EpisodeTransition.CONFLICT:
                raise episode_state_conflict_error(episode_id)
            if transition is EpisodeTransition.APPLY:
                if replacement_id is not None:
                    self._validate_replacement(episode, replacement_id, locked.get(replacement_id))
                episode.lifecycle = EpisodeLifecycle.INVALIDATED
                episode.invalidated_at = utc_now()
                episode.invalidation_reason = request.reason
                episode.replacement_episode_id = replacement_id
            # NO_OP: already INVALIDATED -- 200 state no-op, original reason kept.

            result = await self._hydrate(uow, episode_id, missing_is_internal=True)
            await uow.commit()

        _logger.info(
            "episode_invalidated",
            operation=operation.value,
            episode_id=str(episode_id),
            lifecycle=result.lifecycle.value,
            no_op=transition is EpisodeTransition.NO_OP,
        )
        return result

    @staticmethod
    def _validate_replacement(
        episode: Episode, replacement_id: UUID, replacement: Episode | None
    ) -> None:
        if replacement_id == episode.id:
            raise _invalid_replacement_error("an Episode cannot replace itself")
        if replacement is None:
            raise _invalid_replacement_error("replacement Episode does not exist")
        if replacement.lifecycle is EpisodeLifecycle.FORGOTTEN:
            raise _invalid_replacement_error("replacement Episode is forgotten")
        if replacement.scope != episode.scope:
            raise _invalid_replacement_error("replacement Episode belongs to another Project Scope")

    # --- Forget -------------------------------------------------------------

    async def forget(self, episode_id: UUID, *, idempotency_key: str) -> EpisodeResult:
        """Direct, destructive Forget (Contract v1 SS 21/67): scrubs every
        semantic field, the embedding, and the external provenance refs;
        keeps only identity, lifecycle, timestamps and minimal provenance
        (``origin_kind``/``source_system``). No provider call, no
        propagation (SM-CTX-005), no restore."""

        protected_key = self._protected_key(idempotency_key)
        operation = ContextualMemoryOperation.EPISODE_FORGET
        digest = self._digest(operation, episode_id, {})

        replay = await self._pre_check(protected_key, operation, digest)
        if replay is not None:
            return replay

        async with self._unit_of_work_factory() as uow:
            episode = (await uow.episodes.get_by_ids_for_update([episode_id])).get(episode_id)
            if episode is None:
                raise episode_not_found_error(episode_id)
            provenance = await uow.episode_provenance.get_by_episode_id_for_update(episode_id)
            if provenance is None:
                raise _internal_error("Episode provenance is missing.")

            replayed = await self._claim(uow, protected_key, operation, digest, episode_id)
            if replayed is not None:
                await uow.commit()
                return replayed

            transition = episode_forget_transition(episode.lifecycle)
            if transition is EpisodeTransition.APPLY:
                episode.scope = None
                episode.context_key = None
                episode.kind = None
                episode.summary = None
                episode.situation = None
                episode.reason = None
                episode.outcome = None
                episode.occurred_at = None
                episode.embedding = None
                episode.confidence = None
                episode.invalidation_reason = None
                episode.replacement_episode_id = None
                # SM-CTX-004: the Episode's own outgoing support is scrubbed;
                # inbound refs from other owners stay resolvable (FORGOTTEN).
                await uow.contextual_evidence.delete_episode_links(episode_id)
                episode.support_mode = None
                episode.lifecycle = EpisodeLifecycle.FORGOTTEN
                episode.forgotten_at = utc_now()
                provenance.conversation_ref = None
                provenance.turn_ref = None
            # NO_OP: already FORGOTTEN -- terminal, 200 state no-op.

            result = episode_result(episode, provenance)
            await uow.commit()

        _logger.info(
            "episode_forgotten",
            operation=operation.value,
            episode_id=str(episode_id),
            lifecycle=result.lifecycle.value,
            no_op=transition is EpisodeTransition.NO_OP,
        )
        return result

    # --- Idempotency helpers -------------------------------------------------

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
    ) -> EpisodeResult:
        """Same key bound to a different operation or a different semantic
        request (including another target) -> 409 IDEMPOTENCY_CONFLICT,
        always before any lifecycle concern. Otherwise the result resource is
        interpreted through ``operation`` (an ``episode_*`` operation always
        yields an Episode id) and re-hydrated in its current state."""

        if snapshot.operation is not operation or snapshot.request_digest != digest:
            raise idempotency_conflict_error()
        if snapshot.result_resource_id is None:
            raise _internal_error("Idempotency ledger entry has no result_resource_id.")
        return await self._hydrate(uow, snapshot.result_resource_id, missing_is_internal=True)

    async def _pre_check(
        self, protected_key: str, operation: ContextualMemoryOperation, digest: str
    ) -> EpisodeResult | None:
        """Optimization only -- never the race authority. A hit skips the
        embedding call; a miss always falls through to the transactional claim."""

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
    ) -> EpisodeResult:
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
        episode_id: UUID,
    ) -> EpisodeResult | None:
        """Authoritative claim for a targeted mutation, inside its own
        SAVEPOINT. Returns the replayed winner when the key was already
        claimed, else ``None`` (the caller proceeds and commits)."""

        ledger_entry = ContextualMemoryIdempotency(
            id=uuid4(),
            idempotency_key=protected_key,
            operation=operation,
            request_digest=digest,
            target_resource_id=episode_id,
            result_resource_id=episode_id,
        )
        try:
            async with uow.savepoint():
                await uow.contextual_memory_idempotency.add(ledger_entry)
        except IntegrityError as error:
            if not _is_contextual_idempotency_key_unique_violation(error):
                raise
            return await self._resolve_winner(uow, protected_key, operation, digest)
        return None

    async def _hydrate(
        self, uow: PostgresUnitOfWork, episode_id: UUID, *, missing_is_internal: bool
    ) -> EpisodeResult:
        episode = await uow.episodes.get_by_id(episode_id)
        provenance = await uow.episode_provenance.get_by_episode_id(episode_id) if episode else None
        if episode is None or provenance is None:
            if missing_is_internal:
                raise _internal_error("Contextual idempotency ledger references a missing Episode.")
            raise episode_not_found_error(episode_id)
        supports = await hydrate_episode_support(uow, {episode.id: episode.support_mode})
        return episode_result(episode, provenance, supports[episode.id])

    async def _embed(self, text: str) -> list[float]:
        try:
            embeddings = await self._embedding_client.embed_texts([text])
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


__all__ = [
    "EPISODE_SEARCH_DEFAULT_LIMIT",
    "EPISODE_SEARCH_MAX_LIMIT",
    "EpisodeSearchHit",
    "EpisodeService",
    "decode_episode_cursor",
    "encode_episode_cursor",
    "episode_not_found_error",
    "episode_result",
    "episode_state_conflict_error",
]
