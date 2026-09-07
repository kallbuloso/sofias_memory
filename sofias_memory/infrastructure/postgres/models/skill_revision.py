"""SkillRevision ORM model (ADR-0013, Feature Contract v0.4.0 Skills SS 4.2)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import (
    COMPATIBILITY_MAX_LENGTH,
    CONTENT_SHA256_PATTERN,
    DESCRIPTION_MAX_LENGTH,
    PROCEDURE_MAX_LENGTH,
    SOFIAS_MEMORY_TAGS_METADATA_KEY,
)
from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.chunk import EMBEDDING_DIMENSIONS


class SkillRevision(Base):
    """Immutable, append-only procedural content for one Skill (ADR-0013).

    ``id`` is an internal UUID -- it is never exposed in a public URL; a
    revision is always addressed as ``{skill_uuid}/revisions/{revision}``,
    the per-Skill monotonic integer ordinal. ``UNIQUE(skill_id, id)`` is the
    candidate key ``Skill.current_revision_id``'s composite FK targets, so
    PostgreSQL itself enforces that a Skill's current revision always
    belongs to that exact Skill.

    ``metadata`` can never contain :data:`SOFIAS_MEMORY_TAGS_METADATA_KEY`
    (``sofias-memory.tags``) -- that key exists only as a SKILL.md transport
    representation of ``tags``, never as semantic persisted metadata
    (Feature Contract SS 12.6). Enforced twice: at the domain layer
    (:func:`~sofias_memory.domain.validate_metadata`) and, authoritatively,
    by the ``metadata_excludes_reserved_tags_key`` CHECK below.
    """

    __tablename__ = "skill_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            f"char_length(description) BETWEEN 1 AND {DESCRIPTION_MAX_LENGTH}",
            name="description_length",
        ),
        CheckConstraint(
            f"char_length(procedure) <= {PROCEDURE_MAX_LENGTH}",
            name="procedure_max_length",
        ),
        CheckConstraint(r"procedure ~ '\S'", name="procedure_not_blank"),
        CheckConstraint(
            f"compatibility IS NULL OR char_length(compatibility) BETWEEN 1 AND "
            f"{COMPATIBILITY_MAX_LENGTH}",
            name="compatibility_length",
        ),
        CheckConstraint(
            f"content_sha256 ~ '{CONTENT_SHA256_PATTERN}'",
            name="content_sha256_hex",
        ),
        CheckConstraint(
            f"NOT (metadata ? '{SOFIAS_MEMORY_TAGS_METADATA_KEY}')",
            name="metadata_excludes_reserved_tags_key",
        ),
        UniqueConstraint(
            "skill_id",
            "revision",
            name="uq_skill_revisions_skill_id_revision",
        ),
        UniqueConstraint(
            "skill_id",
            "id",
            name="uq_skill_revisions_skill_id_id",
        ),
        UniqueConstraint(
            "skill_id",
            "content_sha256",
            name="uq_skill_revisions_skill_id_content_sha256",
        ),
        Index(
            "ix_skill_revisions_resolution_embedding_halfvec_hnsw",
            sql_text(f"(resolution_embedding::halfvec({EMBEDDING_DIMENSIONS})) halfvec_cosine_ops"),
            postgresql_using="hnsw",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    skill_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text(), nullable=False)
    procedure: Mapped[str] = mapped_column(Text(), nullable=False)
    license: Mapped[str | None] = mapped_column(Text(), nullable=True)
    compatibility: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metadata_: Mapped[dict[str, str]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sql_text("'{}'::jsonb"),
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text()),
        nullable=False,
        server_default=sql_text("'{}'"),
    )
    declared_tools: Mapped[list[str]] = mapped_column(
        ARRAY(Text()),
        nullable=False,
        server_default=sql_text("'{}'"),
    )
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    resolution_embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
