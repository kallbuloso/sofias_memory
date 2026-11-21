"""ContextCheckpointStatement ORM model (SM-CTX-002, handoff 10 SS 17-20/27/40).

One semantic unit (``ContextStatement``) of a ContextCheckpoint -- never an
opaque JSON blob. ``id`` is the server-generated public ``statement_id``
(scalars included), which is what later field-level Evidence (SM-CTX-004)
will attach to; no support/Evidence column exists yet.

Collection order is the persisted ``ordinal`` per
``(checkpoint_id, statement_type)``. ``UNIQUE(checkpoint_id, statement_type,
ordinal)`` plus ``scalar_ordinal_zero`` structurally bound
``objective``/``current_state``/``next_action`` to at most one per checkpoint.
Rows are hard-deleted by direct Forget (they hold semantic content only).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import ContextStatementType, SupportMode
from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.contextual_evidence import SUPPORT_MODE_ENUM


def _statement_type_values(enum_type: type[ContextStatementType]) -> list[str]:
    return [member.value for member in enum_type]


CONTEXT_STATEMENT_TYPE_ENUM = ENUM(
    ContextStatementType,
    name="context_statement_type",
    values_callable=_statement_type_values,
    validate_strings=True,
    create_type=False,
)


class ContextCheckpointStatement(Base):
    """PostgreSQL source-of-truth row for one ContextStatement."""

    __tablename__ = "context_checkpoint_statements"
    __table_args__ = (
        CheckConstraint(r"content ~ '\S'", name="content_not_blank"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint(
            "statement_type NOT IN ('objective', 'current_state', 'next_action') OR ordinal = 0",
            name="scalar_ordinal_zero",
        ),
        UniqueConstraint(
            "checkpoint_id",
            "statement_type",
            "ordinal",
            name="uq_context_checkpoint_statements_checkpoint_id_type_ordinal",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    checkpoint_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "context_checkpoints.id",
            ondelete="CASCADE",
            name="fk_context_checkpoint_statements_checkpoint_id",
        ),
        nullable=False,
    )
    statement_type: Mapped[ContextStatementType] = mapped_column(
        CONTEXT_STATEMENT_TYPE_ENUM, nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer(), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    support_mode: Mapped[SupportMode | None] = mapped_column(SUPPORT_MODE_ENUM, nullable=True)
    """SM-CTX-004: statement-level support; NULL = no declared support."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
