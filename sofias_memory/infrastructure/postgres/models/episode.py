"""Episode ORM model (SM-CTX-001, ADR-0017, Contract v1 Contextual Project
Memory SS 14-22/67).

``Episode`` is a first-class historical resource -- *what happened* --
never a ``MemoryItem``, ``SessionEntry``, ``Document``, ``Source``, or Dataset
metadata. ``id`` is the structural ``episode_id``. PostgreSQL is
authoritative; there is no Neo4j projection and no graph_outbox event.

``context_key IS NULL`` means a project-wide Episode -- it is never mapped to
``main`` (that default belongs to Checkpoints, not Episodes).

Lifecycle invariants are PostgreSQL CHECK constraints so they are
structurally testable:

- non-FORGOTTEN requires ``scope``/``kind``/``summary``/``occurred_at``/
  ``embedding``;
- ACTIVE forbids every invalidation/forget marker;
- INVALIDATED requires ``invalidated_at``/``invalidation_reason`` and forbids
  ``forgotten_at``;
- FORGOTTEN requires ``forgotten_at`` and forbids every semantic payload
  column, including ``scope``/``context_key``/``kind`` and
  ``replacement_episode_id`` (kept, it would re-associate the tombstone with
  the replacement's project). Only identity, lifecycle and timestamps
  (``created_at``, ``invalidated_at`` when historically applicable,
  ``forgotten_at``) survive.

No ANN index: SM-CTX-001 retrieval is exact pgvector cosine.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import REAL, CheckConstraint, DateTime, ForeignKey, Index, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import EpisodeKind, EpisodeLifecycle, SupportMode
from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.chunk import EMBEDDING_DIMENSIONS
from sofias_memory.infrastructure.postgres.models.contextual_evidence import SUPPORT_MODE_ENUM


def _episode_kind_values(enum_type: type[EpisodeKind]) -> list[str]:
    return [member.value for member in enum_type]


def _episode_lifecycle_values(enum_type: type[EpisodeLifecycle]) -> list[str]:
    return [member.value for member in enum_type]


EPISODE_KIND_ENUM = ENUM(
    EpisodeKind,
    name="episode_kind",
    values_callable=_episode_kind_values,
    validate_strings=True,
    create_type=False,
)
EPISODE_LIFECYCLE_ENUM = ENUM(
    EpisodeLifecycle,
    name="episode_lifecycle",
    values_callable=_episode_lifecycle_values,
    validate_strings=True,
    create_type=False,
)


class Episode(Base):
    """PostgreSQL source-of-truth row for one Contextual Project Memory Episode."""

    __tablename__ = "episodes"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="confidence_bounds",
        ),
        CheckConstraint(
            "scope IS NULL OR scope ~ '^project:[a-z0-9][a-z0-9._-]{0,127}$'",
            name="scope_grammar",
        ),
        CheckConstraint(
            "context_key IS NULL OR context_key ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'",
            name="context_key_grammar",
        ),
        CheckConstraint(r"summary IS NULL OR summary ~ '\S'", name="summary_not_blank"),
        CheckConstraint(r"situation IS NULL OR situation ~ '\S'", name="situation_not_blank"),
        CheckConstraint(r"reason IS NULL OR reason ~ '\S'", name="reason_not_blank"),
        CheckConstraint(r"outcome IS NULL OR outcome ~ '\S'", name="outcome_not_blank"),
        CheckConstraint(
            r"invalidation_reason IS NULL OR invalidation_reason ~ '\S'",
            name="invalidation_reason_not_blank",
        ),
        CheckConstraint(
            "replacement_episode_id IS NULL OR replacement_episode_id <> id",
            name="no_self_replacement",
        ),
        CheckConstraint(
            "lifecycle = 'forgotten' "
            "OR (scope IS NOT NULL AND kind IS NOT NULL AND summary IS NOT NULL "
            "AND occurred_at IS NOT NULL AND embedding IS NOT NULL)",
            name="non_forgotten_requires_payload",
        ),
        CheckConstraint(
            "lifecycle <> 'active' "
            "OR (invalidated_at IS NULL AND invalidation_reason IS NULL "
            "AND replacement_episode_id IS NULL AND forgotten_at IS NULL)",
            name="active_requires_clean_markers",
        ),
        CheckConstraint(
            "lifecycle <> 'invalidated' "
            "OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL "
            "AND forgotten_at IS NULL)",
            name="invalidated_requires_markers",
        ),
        CheckConstraint(
            "lifecycle <> 'forgotten' "
            "OR (forgotten_at IS NOT NULL AND scope IS NULL AND context_key IS NULL "
            "AND kind IS NULL AND summary IS NULL AND situation IS NULL AND reason IS NULL "
            "AND outcome IS NULL AND occurred_at IS NULL AND embedding IS NULL "
            "AND confidence IS NULL AND invalidation_reason IS NULL "
            "AND replacement_episode_id IS NULL)",
            name="forgotten_requires_tombstone_shape",
        ),
        CheckConstraint(
            "lifecycle <> 'forgotten' OR support_mode IS NULL",
            name="forgotten_has_no_support",
        ),
        Index(
            "ix_episodes_scope_lifecycle_occurred_at",
            "scope",
            "lifecycle",
            sql_text("occurred_at DESC"),
            "id",
        ),
        Index(
            "ix_episodes_scope_context_key_lifecycle_occurred_at",
            "scope",
            "context_key",
            "lifecycle",
            sql_text("occurred_at DESC"),
            "id",
        ),
        Index("ix_episodes_kind", "kind"),
        Index("ix_episodes_occurred_at", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    scope: Mapped[str | None] = mapped_column(Text(), nullable=True)
    context_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    kind: Mapped[EpisodeKind | None] = mapped_column(EPISODE_KIND_ENUM, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    situation: Mapped[str | None] = mapped_column(Text(), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text(), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True
    )
    lifecycle: Mapped[EpisodeLifecycle] = mapped_column(
        EPISODE_LIFECYCLE_ENUM,
        nullable=False,
        server_default=EpisodeLifecycle.ACTIVE.value,
    )
    confidence: Mapped[float | None] = mapped_column(REAL(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    replacement_episode_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("episodes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    forgotten_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    support_mode: Mapped[SupportMode | None] = mapped_column(SUPPORT_MODE_ENUM, nullable=True)
    """SM-CTX-004: NULL = no declared support (independent assertion)."""
