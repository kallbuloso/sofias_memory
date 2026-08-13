"""Feedback persistence repository."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.postgres.models import Feedback


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
