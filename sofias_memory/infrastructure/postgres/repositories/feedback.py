"""Feedback persistence repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Feedback, Query


@dataclass(frozen=True)
class UnappliedFeedback:
    """Detached feedback snapshot for one improve pass."""

    id: UUID
    query_id: UUID
    target_type: str
    target_id: UUID | None
    score: int
    references: dict[str, object]


class FeedbackRepository:
    """Minimal persistence operations for feedback records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, feedback: Feedback) -> Feedback:
        self._session.add(feedback)
        await self._session.flush()
        return feedback

    async def get_by_id(self, feedback_id: UUID) -> Feedback | None:
        result = await self._session.scalar(select(Feedback).where(Feedback.id == feedback_id))
        return cast(Feedback | None, result)

    async def list_unapplied_for_dataset(self, dataset_id: UUID) -> list[UnappliedFeedback]:
        statement = (
            select(
                Feedback.id,
                Feedback.query_id,
                Feedback.target_type,
                Feedback.target_id,
                Feedback.score,
                Query.references,
            )
            .join(Query, Feedback.query_id == Query.id)
            .where(
                Feedback.applied_at.is_(None),
                Query.dataset_ids.contains([dataset_id]),
            )
            .order_by(Feedback.created_at, Feedback.id)
        )
        result = await self._session.execute(statement)
        return [
            UnappliedFeedback(
                id=row.id,
                query_id=row.query_id,
                target_type=row.target_type,
                target_id=row.target_id,
                score=row.score,
                references=dict(row.references or {}),
            )
            for row in result
        ]

    async def mark_applied(
        self,
        feedback_id: UUID,
        *,
        applied_at: datetime,
    ) -> Feedback | None:
        feedback = await self.get_by_id(feedback_id)
        if feedback is None:
            return None
        feedback.applied_at = applied_at
        await self._session.flush()
        return feedback
