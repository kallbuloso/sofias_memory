"""Contextual Evidence ORM models (SM-CTX-004, ADR-0019, handoff 12 SS 13/29/32).

- ``ContextualEvidenceRef``: canonical Evidence identity ``(source_type,
  source_id)``, shared by every owner. ``source_id`` has no FK (polymorphic);
  no availability column -- availability is computed from the authoritative
  source on every operation that needs it.
- ``EpisodeEvidenceRef`` / ``ContextStatementEvidenceRef``: owner links.

Evidence never copies source content (Contract SS 11.3).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import EvidenceSourceType, SupportMode
from sofias_memory.infrastructure.postgres.base import Base

EVIDENCE_SOURCE_UNIQUE_CONSTRAINT = "uq_contextual_evidence_refs_source"


def _values(enum_type: type[EvidenceSourceType] | type[SupportMode]) -> list[str]:
    return [member.value for member in enum_type]


EVIDENCE_SOURCE_TYPE_ENUM = ENUM(
    EvidenceSourceType,
    name="evidence_source_type",
    values_callable=_values,
    validate_strings=True,
    create_type=False,
)
SUPPORT_MODE_ENUM = ENUM(
    SupportMode,
    name="support_mode",
    values_callable=_values,
    validate_strings=True,
    create_type=False,
)


class ContextualEvidenceRef(Base):
    """Canonical Evidence identity row (one per source)."""

    __tablename__ = "contextual_evidence_refs"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name=EVIDENCE_SOURCE_UNIQUE_CONSTRAINT),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_type: Mapped[EvidenceSourceType] = mapped_column(
        EVIDENCE_SOURCE_TYPE_ENUM, nullable=False
    )
    source_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class EpisodeEvidenceRef(Base):
    """Episode -> canonical Evidence link (outgoing support of the Episode)."""

    __tablename__ = "episode_evidence_refs"
    __table_args__ = (Index("ix_episode_evidence_refs_evidence_ref_id", "evidence_ref_id"),)

    episode_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("episodes.id", ondelete="CASCADE", name="fk_episode_evidence_refs_episode_id"),
        primary_key=True,
    )
    evidence_ref_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "contextual_evidence_refs.id",
            ondelete="RESTRICT",
            name="fk_episode_evidence_refs_evidence_ref_id",
        ),
        primary_key=True,
    )


class ContextStatementEvidenceRef(Base):
    """ContextStatement -> canonical Evidence link (statement-level support)."""

    __tablename__ = "context_statement_evidence_refs"
    __table_args__ = (
        Index("ix_context_statement_evidence_refs_evidence_ref_id", "evidence_ref_id"),
    )

    statement_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "context_checkpoint_statements.id",
            ondelete="CASCADE",
            name="fk_context_statement_evidence_refs_statement_id",
        ),
        primary_key=True,
    )
    evidence_ref_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "contextual_evidence_refs.id",
            ondelete="RESTRICT",
            name="fk_context_statement_evidence_refs_evidence_ref_id",
        ),
        primary_key=True,
    )
