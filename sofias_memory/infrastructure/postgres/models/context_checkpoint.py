"""ContextCheckpoint ORM model (SM-CTX-002, ADR-0017/ADR-0018, Contract v1
Contextual Project Memory SS 9/24-35/66-67).

A ContextCheckpoint is a durable semantic snapshot of *where this work
currently stands* for one logical Context Stream ``(scope, context_key)``.
There is no ``context_streams``/``projects`` table: the stream exists
logically. PostgreSQL is authoritative; no Neo4j, no embedding.

``scope``/``context_key``/``revision`` are NOT NULL for every lifecycle --
including FORGOTTEN: they are structural stream identity (Contract SS 67), so
tombstones keep participating in ``UNIQUE(scope, context_key, revision)`` and in
``MAX(revision) + 1`` allocation. Semantic content lives only in
``context_checkpoint_statements`` (deleted on Forget) and
``invalidation_reason`` (scrubbed on Forget).

The partial unique index ``uq_context_checkpoints_active_stream`` is the
structural single-ACTIVE guarantee. Supersession columns are schema
readiness for SM-CTX-003 only; SM-CTX-002 never writes them.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import ContextCheckpointLifecycle
from sofias_memory.infrastructure.postgres.base import Base

ACTIVE_STREAM_UNIQUE_INDEX = "uq_context_checkpoints_active_stream"
STREAM_REVISION_UNIQUE_CONSTRAINT = "uq_context_checkpoints_scope_context_key_revision"


def _lifecycle_values(enum_type: type[ContextCheckpointLifecycle]) -> list[str]:
    return [member.value for member in enum_type]


CONTEXT_CHECKPOINT_LIFECYCLE_ENUM = ENUM(
    ContextCheckpointLifecycle,
    name="context_checkpoint_lifecycle",
    values_callable=_lifecycle_values,
    validate_strings=True,
    create_type=False,
)


class ContextCheckpoint(Base):
    """PostgreSQL source-of-truth row for one ContextCheckpoint revision."""

    __tablename__ = "context_checkpoints"
    __table_args__ = (
        CheckConstraint(
            "scope ~ '^project:[a-z0-9][a-z0-9._-]{0,127}$'",
            name="scope_grammar",
        ),
        CheckConstraint(
            "context_key ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'",
            name="context_key_grammar",
        ),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            r"invalidation_reason IS NULL OR invalidation_reason ~ '\S'",
            name="invalidation_reason_not_blank",
        ),
        CheckConstraint(
            "invalidation_reason IS NULL OR invalidated_at IS NOT NULL",
            name="reason_requires_invalidated_at",
        ),
        CheckConstraint(
            "(superseded_at IS NULL) = (superseded_by IS NULL)",
            name="supersession_markers_paired",
        ),
        CheckConstraint(
            "superseded_by IS NULL OR superseded_by <> id",
            name="no_self_supersession",
        ),
        CheckConstraint(
            "lifecycle <> 'active' "
            "OR (superseded_at IS NULL AND superseded_by IS NULL AND invalidated_at IS NULL "
            "AND invalidation_reason IS NULL AND forgotten_at IS NULL)",
            name="active_requires_clean_markers",
        ),
        CheckConstraint(
            "lifecycle <> 'superseded' "
            "OR (superseded_at IS NOT NULL AND superseded_by IS NOT NULL "
            "AND invalidated_at IS NULL AND forgotten_at IS NULL)",
            name="superseded_requires_markers",
        ),
        CheckConstraint(
            "lifecycle <> 'invalidated' "
            "OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL "
            "AND forgotten_at IS NULL)",
            name="invalidated_requires_markers",
        ),
        CheckConstraint(
            "lifecycle <> 'forgotten' "
            "OR (forgotten_at IS NOT NULL AND invalidation_reason IS NULL)",
            name="forgotten_requires_tombstone_shape",
        ),
        UniqueConstraint(
            "scope", "context_key", "revision", name=STREAM_REVISION_UNIQUE_CONSTRAINT
        ),
        Index(
            ACTIVE_STREAM_UNIQUE_INDEX,
            "scope",
            "context_key",
            unique=True,
            postgresql_where=sql_text("lifecycle = 'active'"),
        ),
        Index(
            "ix_context_checkpoints_scope_context_key_lifecycle",
            "scope",
            "context_key",
            "lifecycle",
        ),
        Index(
            "ix_context_checkpoints_scope_lifecycle_listing",
            "scope",
            "lifecycle",
            "context_key",
            sql_text("revision DESC"),
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    scope: Mapped[str] = mapped_column(Text(), nullable=False)
    context_key: Mapped[str] = mapped_column(Text(), nullable=False)
    revision: Mapped[int] = mapped_column(Integer(), nullable=False)
    lifecycle: Mapped[ContextCheckpointLifecycle] = mapped_column(
        CONTEXT_CHECKPOINT_LIFECYCLE_ENUM,
        nullable=False,
        server_default=ContextCheckpointLifecycle.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("context_checkpoints.id", ondelete="RESTRICT"),
        nullable=True,
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    forgotten_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
