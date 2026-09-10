"""AgentSession ORM model (ADR-0014, v0.5.0 Agent Management Feature Contract SS 27)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.infrastructure.postgres.base import Base


class AgentSession(Base):
    """PostgreSQL source-of-truth row for the explicit Agent <-> Session
    association (ADR-0014).

    Identity is exactly ``(agent_id, session_id)`` -- no surrogate UUID PK,
    no ``updated_at``, no ``role``/``metadata``. This is a **current
    explicit management association only** -- never historical
    participation, Session ownership, or operation attribution. ``created_at``
    records exclusively when the current row was created; it is never a
    "first-ever participation" or "originated" timestamp. ``DELETE`` is
    permitted and idempotent precisely because this table carries no audit
    obligation: removing a row destroys the current association fact, and a
    later re-association creates a new row with a new ``created_at``.

    Cardinality is M:N: the same Agent may be associated with many
    Sessions, and the same Session may be associated with many Agents
    simultaneously -- when it is, no data here (or anywhere else) can
    determine which specific Agent originated any given Query, PipelineRun,
    or SessionEntry linked to that Session (Feature Contract SS 35, 40-42).
    Neither ``Query`` nor ``PipelineRun`` nor ``SessionEntry`` gains an
    ``agent_id`` column because of this table.
    """

    __tablename__ = "agent_sessions"

    agent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
