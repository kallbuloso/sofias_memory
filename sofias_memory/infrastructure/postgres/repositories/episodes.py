"""Episode-specific PostgreSQL repository (SM-CTX-001, ADR-0017).

Persistence only -- no product rules. Lifecycle decisions live in
``services.episodes``; lifecycle shape is additionally enforced by the
``episodes`` CHECK constraints.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import EpisodeKind, EpisodeLifecycle
from sofias_memory.infrastructure.postgres.models import Episode, EpisodeProvenance


@dataclass(frozen=True, slots=True)
class EpisodePosition:
    """Keyset position in the public listing order (occurred_at DESC, id ASC)."""

    occurred_at: datetime
    episode_id: UUID


@dataclass(frozen=True, slots=True)
class EpisodeRow:
    episode: Episode
    provenance: EpisodeProvenance


@dataclass(frozen=True, slots=True)
class EpisodeSemanticHit:
    """One exact-cosine retrieval row; ``relevance`` is computed by PostgreSQL."""

    episode: Episode
    provenance: EpisodeProvenance
    relevance: float


class EpisodeRepository:
    """Persistence operations for contextual Episodes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, episode: Episode) -> Episode:
        self._session.add(episode)
        await self._session.flush()
        return episode

    async def get_by_id(self, episode_id: UUID) -> Episode | None:
        result = await self._session.scalar(select(Episode).where(Episode.id == episode_id))
        return cast(Episode | None, result)

    async def get_by_ids_for_update(self, episode_ids: Sequence[UUID]) -> dict[UUID, Episode]:
        """Row-locks every existing Episode in ``episode_ids`` in ascending id
        order -- a consistent global lock order, so two lifecycle mutations
        touching the same pair of Episodes (e.g. invalidate-with-replacement
        in both directions) can never deadlock each other."""

        statement = (
            select(Episode)
            .where(Episode.id.in_(list(episode_ids)))
            .order_by(Episode.id.asc())
            .with_for_update()
        )
        result = await self._session.scalars(statement)
        return {episode.id: episode for episode in result.all()}

    async def list_page(
        self,
        *,
        scope: str,
        context_key: str | None,
        kind: EpisodeKind | None,
        lifecycle: EpisodeLifecycle,
        after: EpisodePosition | None,
        limit: int,
    ) -> list[EpisodeRow]:
        """Exact ``scope`` match, optional exact ``context_key``/``kind``
        filters, ordered ``occurred_at DESC, id ASC`` with keyset pagination.

        A FORGOTTEN tombstone has a scrubbed (NULL) scope, so it never
        matches any project scope -- the listing never reconstructs the
        association Forget erased (handoff 09 SS 29)."""

        statement = (
            select(Episode, EpisodeProvenance)
            .join(EpisodeProvenance, EpisodeProvenance.episode_id == Episode.id)
            .where(Episode.scope == scope, Episode.lifecycle == lifecycle)
        )
        if context_key is not None:
            statement = statement.where(Episode.context_key == context_key)
        if kind is not None:
            statement = statement.where(Episode.kind == kind)
        if after is not None:
            statement = statement.where(
                or_(
                    Episode.occurred_at < after.occurred_at,
                    and_(Episode.occurred_at == after.occurred_at, Episode.id > after.episode_id),
                )
            )
        statement = statement.order_by(Episode.occurred_at.desc(), Episode.id.asc()).limit(limit)
        result = await self._session.execute(statement)
        return [EpisodeRow(episode=episode, provenance=prov) for episode, prov in result.all()]

    async def semantic_search(
        self,
        *,
        scope: str,
        context_key: str | None,
        query_embedding: Sequence[float],
        limit: int,
    ) -> list[EpisodeSemanticHit]:
        """Exact pgvector cosine over the authoritative full-precision
        ``VECTOR(3072)`` -- never ANN, never Neo4j, never a lexical fallback.

        Eligible: ACTIVE only, exact scope, embedding present, optional exact
        ``context_key``. Order: relevance DESC, occurred_at DESC, id ASC."""

        relevance = (1 - Episode.embedding.cosine_distance(list(query_embedding))).label(
            "relevance"
        )
        statement = (
            select(Episode, EpisodeProvenance, relevance)
            .join(EpisodeProvenance, EpisodeProvenance.episode_id == Episode.id)
            .where(
                Episode.lifecycle == EpisodeLifecycle.ACTIVE,
                Episode.scope == scope,
                Episode.embedding.is_not(None),
            )
        )
        if context_key is not None:
            statement = statement.where(Episode.context_key == context_key)
        statement = statement.order_by(
            relevance.desc(), Episode.occurred_at.desc(), Episode.id.asc()
        ).limit(limit)
        result = await self._session.execute(statement)
        return [
            EpisodeSemanticHit(episode=episode, provenance=prov, relevance=float(value))
            for episode, prov, value in result.all()
        ]
