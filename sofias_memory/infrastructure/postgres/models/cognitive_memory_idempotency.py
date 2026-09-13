"""CognitiveMemoryIdempotency ORM model (ADR-0016 SS 14, Feature Contract
v0.7.0 SS 17).

Dedicated ledger, independent of ``PipelineRun``/``PipelineStep`` --
Cognitive Memory mutations are synchronous and never routed through the
durable pipeline subsystem. The ledger never stores the request body,
cognitive content, or embedding; it stores only operation identity, a
keyed/non-reversible request digest
(:func:`sofias_memory.domain.compute_request_digest`), and result
identities sufficient for replay.

``UNIQUE(idempotency_key)`` is the authoritative race boundary -- correctness
never depends on a ``SELECT`` before ``INSERT``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CHAR, CheckConstraint, DateTime, ForeignKey, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import CognitiveMemoryOperation
from sofias_memory.infrastructure.postgres.base import Base

REQUEST_DIGEST_HEX_PATTERN = "'^[0-9a-f]{64}$'"


def _cognitive_memory_operation_values(enum_type: type[CognitiveMemoryOperation]) -> list[str]:
    return [member.value for member in enum_type]


COGNITIVE_MEMORY_OPERATION_ENUM = ENUM(
    CognitiveMemoryOperation,
    name="cognitive_memory_operation",
    values_callable=_cognitive_memory_operation_values,
    validate_strings=True,
    create_type=False,
)


class CognitiveMemoryIdempotency(Base):
    """PostgreSQL source-of-truth row for one Cognitive Memory idempotency claim."""

    __tablename__ = "cognitive_memory_idempotency"
    __table_args__ = (
        CheckConstraint(
            f"request_digest ~ {REQUEST_DIGEST_HEX_PATTERN}",
            name="request_digest_hex",
        ),
        CheckConstraint(
            "(operation = 'create' AND target_memory_id IS NULL) "
            "OR (operation IN ('supersede', 'forget') AND target_memory_id IS NOT NULL)",
            # Kept short deliberately: "..._target_memory_id_matches_operation"
            # exceeds PostgreSQL's 63-byte identifier limit on this
            # already-long table name and gets silently truncated with a
            # hash suffix, which no longer matches the name this model (or
            # a migration) asks PostgreSQL to drop by name.
            name="operation_target_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    idempotency_key: Mapped[str] = mapped_column(Text(), nullable=False, unique=True)
    operation: Mapped[CognitiveMemoryOperation] = mapped_column(
        COGNITIVE_MEMORY_OPERATION_ENUM,
        nullable=False,
    )
    request_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    target_memory_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("memory_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    result_memory_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("memory_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
