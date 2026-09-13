"""MemoryItem-specific PostgreSQL repository (ADR-0016, SM-1001/SM-1003)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import CognitiveMemoryLifecycle, CognitiveMemoryType
from sofias_memory.infrastructure.postgres.models import MemoryItem, MemoryProvenance


@dataclass(frozen=True, slots=True)
class TypedRecallHit:
    """One row of a typed recall query, already hydrated with everything
    ``services.cognitive_memory_recall`` needs to build a public result --
    ``relevance``/``is_current_truth`` are computed by PostgreSQL itself
    (Feature Contract SS 14.3), never recomputed in Python from a second,
    potentially divergent predicate."""

    item: MemoryItem
    provenance: MemoryProvenance
    relevance: float
    is_current_truth: bool


def _temporal_validity_predicate(as_of: datetime) -> ColumnElement[bool]:
    """``valid_from``/``valid_until`` semantic validity at ``as_of``
    (``valid_from`` inclusive, ``valid_until`` exclusive; Feature Contract
    SS 9). Required for EVERY eligible row regardless of
    ``include_superseded`` -- a historical superseded item is only
    eligible when it still falls inside its own semantic validity window
    at ``as_of``, never merely because it existed and was not yet
    superseded (Feature Contract SS 14.1)."""

    return and_(
        or_(MemoryItem.valid_from.is_(None), MemoryItem.valid_from <= as_of),
        or_(MemoryItem.valid_until.is_(None), MemoryItem.valid_until > as_of),
    )


def _supersession_currentness_predicate(as_of: datetime) -> ColumnElement[bool]:
    """Whether the item had not yet been superseded at ``as_of``. Applied
    to ``WHERE`` only when ``include_superseded=false`` (in addition to
    :func:`_temporal_validity_predicate`, which always applies) -- and
    combined with it below to define ``is_current_truth`` for every
    returned row."""

    return or_(MemoryItem.superseded_at.is_(None), MemoryItem.superseded_at > as_of)


def _current_truth_predicate(as_of: datetime) -> ColumnElement[bool]:
    """Current truth at ``as_of``, beyond existence (``created_at <=
    as_of``, non-FORGOTTEN, content/embedding present -- those are
    structural preconditions shared by every eligible row, see
    :meth:`MemoryItemRepository.typed_recall`): temporal validity AND not
    yet superseded. Selected unchanged as the ``is_current_truth`` column
    for every returned row (ADR-0016 SS 9) -- composed from the same two
    predicates ``typed_recall`` uses for ``WHERE``, so the two can never
    diverge."""

    return and_(
        _temporal_validity_predicate(as_of),
        _supersession_currentness_predicate(as_of),
    )


class MemoryItemRepository:
    """Persistence operations for Native Cognitive Memory items."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, memory_item: MemoryItem) -> MemoryItem:
        self._session.add(memory_item)
        await self._session.flush()
        return memory_item

    async def get_by_id(self, memory_id: UUID) -> MemoryItem | None:
        statement = select(MemoryItem).where(MemoryItem.id == memory_id)
        result = await self._session.scalar(statement)
        return cast(MemoryItem | None, result)

    async def get_by_id_for_update(self, memory_id: UUID) -> MemoryItem | None:
        """Row-locked read serializing every lifecycle-mutating operation
        (supersede, forget) for this MemoryItem (ADR-0016 SS 12/13)."""

        statement = select(MemoryItem).where(MemoryItem.id == memory_id).with_for_update()
        result = await self._session.scalar(statement)
        return cast(MemoryItem | None, result)

    async def typed_recall(
        self,
        *,
        memory_types: Sequence[CognitiveMemoryType],
        scopes: Sequence[str],
        query_embedding: Sequence[float],
        as_of: datetime,
        include_superseded: bool,
        min_relevance: float | None,
        limit: int,
    ) -> list[TypedRecallHit]:
        """Exact cosine similarity over the authoritative full-precision
        ``VECTOR(3072)`` (ADR-0006), never ANN/HNSW/IVFFlat, never Neo4j,
        never a lexical fallback (Feature Contract SS 14.2/14.3).

        Structural eligibility (always required, regardless of
        ``include_superseded``): non-FORGOTTEN, content/embedding present,
        ``created_at <= as_of`` (an item created after ``as_of`` did not
        cognitively exist yet), exact ``memory_type``/``scope`` match, and
        :func:`_temporal_validity_predicate` -- a historical superseded
        item is eligible for ``include_superseded=true`` only when it
        still falls inside its own semantic validity window at ``as_of``,
        never merely because it existed and was not yet superseded
        (Feature Contract SS 14.1). ``include_superseded=false``
        additionally requires :func:`_supersession_currentness_predicate`
        -- never a naive ``lifecycle = 'active'`` shortcut, so an item
        that is presently SUPERSEDED but was current truth at ``as_of`` is
        still eligible (ADR-0016 SS 9).
        """

        relevance = (1 - MemoryItem.embedding.cosine_distance(list(query_embedding))).label(
            "relevance"
        )
        is_current_truth = _current_truth_predicate(as_of).label("is_current_truth")

        statement = (
            select(MemoryItem, MemoryProvenance, relevance, is_current_truth)
            .join(MemoryProvenance, MemoryProvenance.memory_id == MemoryItem.id)
            .where(
                MemoryItem.lifecycle != CognitiveMemoryLifecycle.FORGOTTEN,
                MemoryItem.content.is_not(None),
                MemoryItem.embedding.is_not(None),
                MemoryItem.created_at <= as_of,
                MemoryItem.memory_type.in_(memory_types),
                MemoryItem.scope.in_(scopes),
                _temporal_validity_predicate(as_of),
            )
        )
        if not include_superseded:
            statement = statement.where(_supersession_currentness_predicate(as_of))
        if min_relevance is not None:
            statement = statement.where(relevance >= min_relevance)
        statement = statement.order_by(
            relevance.desc(), MemoryItem.created_at.desc(), MemoryItem.id.asc()
        ).limit(limit)

        result = await self._session.execute(statement)
        return [
            TypedRecallHit(
                item=item,
                provenance=provenance,
                relevance=float(relevance_value),
                is_current_truth=bool(is_current_truth_value),
            )
            for item, provenance, relevance_value, is_current_truth_value in result.all()
        ]
