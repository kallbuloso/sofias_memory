"""Agent ORM model (ADR-0014, v0.5.0 Agent Management Feature Contract SS 3/4/10)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import (
    AGENT_DESCRIPTION_MAX_LENGTH,
    AGENT_DISPLAY_NAME_MAX_LENGTH,
    AGENT_INSTRUCTIONS_MAX_LENGTH,
    AGENT_NAME_MAX_LENGTH,
    AgentStatus,
)
from sofias_memory.infrastructure.postgres.base import Base


def _agent_status_values(enum_type: type[AgentStatus]) -> list[str]:
    return [status.value for status in enum_type]


AGENT_STATUS_ENUM = ENUM(
    AgentStatus,
    name="agent_status",
    values_callable=_agent_status_values,
    validate_strings=True,
    create_type=False,
)


class Agent(Base):
    """PostgreSQL source-of-truth row for a first-class durable Agent
    Profile (ADR-0014).

    ``id`` is the structural ``agent_uuid`` -- there is no separate
    ``agent_uuid`` column duplicating it (mirrors ``Session.id``/
    ``session_uuid`` and ``Skill.id``/``skill_uuid``). ``name`` is the
    portable, immutable, globally unique logical identity. Agent Profile
    fields are mutable in place -- there is no ``AgentRevision``. Agents
    never carry a Dataset/Session/Skill/runtime FK or column and are never
    projected to Neo4j.
    """

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            f"char_length(name) <= {AGENT_NAME_MAX_LENGTH}",
            name="name_max_length",
        ),
        CheckConstraint(
            "name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="name_portable_charset",
        ),
        CheckConstraint(
            f"display_name IS NULL OR char_length(display_name) <= {AGENT_DISPLAY_NAME_MAX_LENGTH}",
            name="display_name_max_length",
        ),
        CheckConstraint(
            f"description IS NULL OR char_length(description) <= {AGENT_DESCRIPTION_MAX_LENGTH}",
            name="description_max_length",
        ),
        CheckConstraint(
            f"instructions IS NULL OR char_length(instructions) <= {AGENT_INSTRUCTIONS_MAX_LENGTH}",
            name="instructions_max_length",
        ),
        CheckConstraint(
            r"instructions IS NULL OR instructions ~ '\S'",
            name="instructions_not_blank",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sql_text("'{}'::jsonb"),
    )
    status: Mapped[AgentStatus] = mapped_column(
        AGENT_STATUS_ENUM,
        nullable=False,
        server_default=AgentStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
