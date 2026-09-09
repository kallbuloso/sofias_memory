"""Agent-specific PostgreSQL repository (ADR-0014, SM-801/SM-802)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import AgentStatus
from sofias_memory.infrastructure.postgres.models import Agent


class AgentRepository:
    """Persistence operations for first-class durable Agent Profiles."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, agent: Agent) -> Agent:
        self._session.add(agent)
        await self._session.flush()
        return agent

    async def get_by_id(self, agent_id: UUID) -> Agent | None:
        statement = select(Agent).where(Agent.id == agent_id)
        result = await self._session.scalar(statement)
        return cast(Agent | None, result)

    async def get_by_name(self, name: str) -> Agent | None:
        statement = select(Agent).where(Agent.name == name)
        result = await self._session.scalar(statement)
        return cast(Agent | None, result)

    async def get_by_id_for_update(self, agent_id: UUID) -> Agent | None:
        """Row-locked read serializing every profile-mutating operation for
        this Agent (``PATCH``, archive, restore -- v0.5.0 Feature Contract
        SS 19.2). Not yet exercised by any public route in SM-801; the
        primitive is part of the persistence foundation this task builds,
        and SM-802 acquires it before deciding a ``PATCH``/archive/restore
        effect."""

        statement = select(Agent).where(Agent.id == agent_id).with_for_update()
        result = await self._session.scalar(statement)
        return cast(Agent | None, result)

    async def list_paginated(
        self,
        *,
        limit: int,
        offset: int,
        status: AgentStatus,
    ) -> tuple[list[Agent], int]:
        """Deterministic order ``(created_at, id)`` -- the same tiebreak
        convention Session/Skill listing already use. ``total`` reflects
        ``status`` (SM-802, Feature Contract SS 9: ``GET /agents`` defaults
        to ``active`` only, unlike Session/Skill's all-status default)."""

        statement = (
            select(Agent)
            .where(Agent.status == status)
            .order_by(Agent.created_at, Agent.id)
            .limit(limit)
            .offset(offset)
        )
        total_statement = select(func.count()).select_from(Agent).where(Agent.status == status)
        result = await self._session.scalars(statement)
        total = await self._session.scalar(total_statement)
        return list(result), int(total or 0)
