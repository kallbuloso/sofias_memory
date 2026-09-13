"""MemoryItem ORM model (ADR-0016, Feature Contract v0.7.0 Native Cognitive Memory).

``MemoryItem`` is a native aggregate -- never represented as Dataset,
Source, Document, Chunk, SessionEntry, Skill, Agent, Query, or PipelineRun.
``id`` is the structural ``memory_id`` -- there is no separate ``memory_id``
column duplicating it (mirrors ``Skill.id`` / ``Session.id``). Content hash
is never identity and never triggers dedupe/upsert.

Lifecycle invariants are enforced at the PostgreSQL level so they are
structurally testable, not merely conventions (ADR-0016 SS 6):

- non-FORGOTTEN requires ``scope``/``content``/``embedding``;
- ACTIVE forbids any lineage/forget marker;
- SUPERSEDED requires ``superseded_at``/``superseded_by`` and forbids
  ``forgotten_at``;
- FORGOTTEN requires ``forgotten_at`` and forbids every other cognitive
  payload column (``scope``/``content``/``embedding``/``confidence``/
  ``valid_from``/``valid_until``), while still permitting a preserved
  ``superseded_at``/``superseded_by`` when the item was already SUPERSEDED
  before Forget.

``superseded_by`` is a self-reference. ``ck_memory_items_no_self_supersession``
forbids ``superseded_by == id``. The partial ``UNIQUE`` index on
``superseded_by`` guarantees a replacement has at most one direct
predecessor, keeping lineage strictly linear -- two different old items can
never both point at the same replacement.
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

from sofias_memory.domain import (
    COGNITIVE_MEMORY_CONTENT_MAX_LENGTH,
    CognitiveMemoryLifecycle,
    CognitiveMemoryType,
)
from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.chunk import EMBEDDING_DIMENSIONS


def _cognitive_memory_type_values(enum_type: type[CognitiveMemoryType]) -> list[str]:
    return [member.value for member in enum_type]


def _cognitive_memory_lifecycle_values(enum_type: type[CognitiveMemoryLifecycle]) -> list[str]:
    return [member.value for member in enum_type]


COGNITIVE_MEMORY_TYPE_ENUM = ENUM(
    CognitiveMemoryType,
    name="cognitive_memory_type",
    values_callable=_cognitive_memory_type_values,
    validate_strings=True,
    create_type=False,
)
COGNITIVE_MEMORY_LIFECYCLE_ENUM = ENUM(
    CognitiveMemoryLifecycle,
    name="cognitive_memory_lifecycle",
    values_callable=_cognitive_memory_lifecycle_values,
    validate_strings=True,
    create_type=False,
)


class MemoryItem(Base):
    """PostgreSQL source-of-truth row for one Native Cognitive Memory item."""

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="confidence_bounds",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name="validity_window_ordered",
        ),
        CheckConstraint(
            "superseded_by IS NULL OR superseded_by <> id",
            name="no_self_supersession",
        ),
        CheckConstraint(r"content IS NULL OR content ~ '\S'", name="content_not_blank"),
        CheckConstraint(
            f"content IS NULL OR char_length(content) <= {COGNITIVE_MEMORY_CONTENT_MAX_LENGTH}",
            name="content_max_length",
        ),
        CheckConstraint(
            "scope IS NULL OR scope = 'global' OR scope ~ '^project:[a-z0-9][a-z0-9._-]{0,127}$'",
            name="scope_grammar",
        ),
        CheckConstraint(
            "lifecycle = 'forgotten' "
            "OR (scope IS NOT NULL AND content IS NOT NULL AND embedding IS NOT NULL)",
            name="non_forgotten_requires_cognitive_payload",
        ),
        CheckConstraint(
            "lifecycle <> 'active' "
            "OR (superseded_at IS NULL AND superseded_by IS NULL AND forgotten_at IS NULL)",
            name="active_requires_clean_lineage",
        ),
        CheckConstraint(
            "lifecycle <> 'superseded' "
            "OR (superseded_at IS NOT NULL AND superseded_by IS NOT NULL "
            "AND forgotten_at IS NULL)",
            name="superseded_requires_lineage_markers",
        ),
        CheckConstraint(
            "lifecycle <> 'forgotten' "
            "OR (forgotten_at IS NOT NULL AND scope IS NULL AND content IS NULL "
            "AND embedding IS NULL AND confidence IS NULL AND valid_from IS NULL "
            "AND valid_until IS NULL)",
            name="forgotten_requires_tombstone_shape",
        ),
        Index(
            "uq_memory_items_superseded_by",
            "superseded_by",
            unique=True,
            postgresql_where=sql_text("superseded_by IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    memory_type: Mapped[CognitiveMemoryType] = mapped_column(
        COGNITIVE_MEMORY_TYPE_ENUM,
        nullable=False,
    )
    scope: Mapped[str | None] = mapped_column(Text(), nullable=True)
    content: Mapped[str | None] = mapped_column(Text(), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS),
        nullable=True,
    )
    lifecycle: Mapped[CognitiveMemoryLifecycle] = mapped_column(
        COGNITIVE_MEMORY_LIFECYCLE_ENUM,
        nullable=False,
        server_default=CognitiveMemoryLifecycle.ACTIVE.value,
    )
    confidence: Mapped[float | None] = mapped_column(REAL(), nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("memory_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    forgotten_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
