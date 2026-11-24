"""ContextCheckpoint-specific PostgreSQL repository (SM-CTX-002, ADR-0018).

Persistence only -- no product rules. Lifecycle and revision decisions live in
``services.context_checkpoints``; lifecycle shape is additionally enforced by
the ``context_checkpoints`` CHECK constraints, single-ACTIVE by the partial
unique index and revision uniqueness by the unique constraint.

``lock_stream`` is the stream-level Create serialization primitive: a
PostgreSQL *transaction-scoped* advisory lock (released automatically at
COMMIT/ROLLBACK, never held across requests, never process-local).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import ContextCheckpointLifecycle
from sofias_memory.infrastructure.postgres.models import ContextCheckpoint
from sofias_memory.infrastructure.postgres.models.context_checkpoint import LINEAGE_FOREIGN_KEY


@dataclass(frozen=True, slots=True)
class ContextCheckpointPosition:
    """Keyset position in the public listing order
    (context_key ASC, revision DESC, id ASC)."""

    context_key: str
    revision: int
    checkpoint_id: UUID


class ContextCheckpointRepository:
    """Persistence operations for ContextCheckpoints."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_stream(self, lock_key: int) -> None:
        """Blocks until this transaction holds the stream's advisory lock
        (``pg_advisory_xact_lock(bigint)``). See
        :func:``sofias_memory.domain.context_stream_lock_key``."""

        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
        )

    async def defer_lineage_foreign_key(self) -> None:
        """Defers the ``superseded_by`` FK until COMMIT for the *current
        transaction only* (it is ``DEFERRABLE INITIALLY IMMEDIATE``). Lets the
        supersede transaction mark the old ACTIVE SUPERSEDED (CHECK requires a
        non-null ``superseded_by``) before inserting the replacement ACTIVE (the
        single-ACTIVE index is not deferrable). The FK is still enforced at
        COMMIT."""

        await self._session.execute(text(f"SET CONSTRAINTS {LINEAGE_FOREIGN_KEY} DEFERRED"))

    async def add(self, checkpoint: ContextCheckpoint) -> ContextCheckpoint:
        self._session.add(checkpoint)
        await self._session.flush()
        return checkpoint

    async def get_by_id(self, checkpoint_id: UUID) -> ContextCheckpoint | None:
        statement = select(ContextCheckpoint).where(ContextCheckpoint.id == checkpoint_id)
        return cast(ContextCheckpoint | None, await self._session.scalar(statement))

    async def get_by_id_for_update(self, checkpoint_id: UUID) -> ContextCheckpoint | None:
        statement = (
            select(ContextCheckpoint).where(ContextCheckpoint.id == checkpoint_id).with_for_update()
        )
        return cast(ContextCheckpoint | None, await self._session.scalar(statement))

    async def get_active(self, *, scope: str, context_key: str) -> ContextCheckpoint | None:
        statement = select(ContextCheckpoint).where(
            ContextCheckpoint.scope == scope,
            ContextCheckpoint.context_key == context_key,
            ContextCheckpoint.lifecycle == ContextCheckpointLifecycle.ACTIVE,
        )
        return cast(ContextCheckpoint | None, await self._session.scalar(statement))

    async def list_active(
        self, *, scope: str, context_key: str, limit: int = 2
    ) -> list[ContextCheckpoint]:
        """Every ACTIVE checkpoint of the stream, at most ``limit``, ordered by id.

        Contextual Recall (SM-CTX-006) reads two so a violated single-ACTIVE
        invariant is detected and fails closed instead of silently picking one
        (handoff 14 SS 104); the partial unique index normally guarantees <= 1."""

        statement = (
            select(ContextCheckpoint)
            .where(
                ContextCheckpoint.scope == scope,
                ContextCheckpoint.context_key == context_key,
                ContextCheckpoint.lifecycle == ContextCheckpointLifecycle.ACTIVE,
            )
            .order_by(ContextCheckpoint.id.asc())
            .limit(limit)
        )
        result = await self._session.scalars(statement)
        return list(result.all())

    async def list_history(
        self, *, scope: str, context_key: str, limit: int
    ) -> list[ContextCheckpoint]:
        """Historical revisions of exactly one Context Stream for Contextual Recall
        ``history`` (SM-CTX-007, handoff 15 SS 26-31/89): SUPERSEDED and
        INVALIDATED only -- never ACTIVE (that is ``current_checkpoint``) and never
        a FORGOTTEN tombstone. Ordered ``revision DESC, id ASC`` (revision is the
        authority, never created_at), at most ``limit``. Served by
        ``ix_context_checkpoints_scope_lifecycle_listing``."""

        statement = (
            select(ContextCheckpoint)
            .where(
                ContextCheckpoint.scope == scope,
                ContextCheckpoint.context_key == context_key,
                ContextCheckpoint.lifecycle.in_(
                    (ContextCheckpointLifecycle.SUPERSEDED, ContextCheckpointLifecycle.INVALIDATED)
                ),
            )
            .order_by(ContextCheckpoint.revision.desc(), ContextCheckpoint.id.asc())
            .limit(limit)
        )
        result = await self._session.scalars(statement)
        return list(result.all())

    async def max_revision(self, *, scope: str, context_key: str) -> int | None:
        """Highest revision of the stream over *every* lifecycle, FORGOTTEN
        tombstones included -- revisions are never reused."""

        statement = select(func.max(ContextCheckpoint.revision)).where(
            ContextCheckpoint.scope == scope,
            ContextCheckpoint.context_key == context_key,
        )
        value = await self._session.scalar(statement)
        return None if value is None else int(value)

    async def list_page(
        self,
        *,
        scope: str,
        context_key: str | None,
        lifecycle: ContextCheckpointLifecycle,
        after: ContextCheckpointPosition | None,
        limit: int,
    ) -> list[ContextCheckpoint]:
        """Exact ``scope``/``lifecycle``, optional exact ``context_key``, ordered
        ``context_key ASC, revision DESC, id ASC`` (within one stream this is the
        Contract's ``revision DESC, checkpoint_id ASC``; across streams revisions
        are never compared as one global sequence)."""

        statement = select(ContextCheckpoint).where(
            ContextCheckpoint.scope == scope, ContextCheckpoint.lifecycle == lifecycle
        )
        if context_key is not None:
            statement = statement.where(ContextCheckpoint.context_key == context_key)
        if after is not None:
            statement = statement.where(
                or_(
                    ContextCheckpoint.context_key > after.context_key,
                    and_(
                        ContextCheckpoint.context_key == after.context_key,
                        ContextCheckpoint.revision < after.revision,
                    ),
                    and_(
                        ContextCheckpoint.context_key == after.context_key,
                        ContextCheckpoint.revision == after.revision,
                        ContextCheckpoint.id > after.checkpoint_id,
                    ),
                )
            )
        statement = statement.order_by(
            ContextCheckpoint.context_key.asc(),
            ContextCheckpoint.revision.desc(),
            ContextCheckpoint.id.asc(),
        ).limit(limit)
        result = await self._session.scalars(statement)
        return list(result.all())
