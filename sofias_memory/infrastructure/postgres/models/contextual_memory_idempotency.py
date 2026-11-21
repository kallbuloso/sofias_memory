"""ContextualMemoryIdempotency ORM model (SM-CTX-001, Contract v1 SS 36,
Architecture SS 28-29, handoff 09 SS 23.3-23.5).

The ledger belongs to Contextual Project Memory as a whole -- Episodes are
only its first consumer; Checkpoints reuse it in later Slices. It is
resource-generic on purpose:

- ``target_resource_id``/``result_resource_id`` are structural UUIDs whose
  resource type is determined by ``operation``; they deliberately carry no
  foreign key (PostgreSQL has no polymorphic FK, and an ``episodes`` FK would
  force remodelling as soon as Checkpoints arrive). Resource validity is
  enforced by the service of each operation.
- ``idempotency_key`` is the *protected* lookup identity
  (:func:`sofias_memory.domain.protect_contextual_idempotency_key`, a
  64-hex HMAC) -- never the raw ``Idempotency-Key`` header.
- ``request_digest`` is a keyed HMAC of the canonical request; no raw
  request or semantic content is ever stored.

``UNIQUE(idempotency_key)`` is the authoritative race boundary.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CHAR, CheckConstraint, DateTime
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import ContextualMemoryOperation
from sofias_memory.infrastructure.postgres.base import Base

HEX_DIGEST_PATTERN = "'^[0-9a-f]{64}$'"


def _contextual_memory_operation_values(enum_type: type[ContextualMemoryOperation]) -> list[str]:
    return [member.value for member in enum_type]


CONTEXTUAL_MEMORY_OPERATION_ENUM = ENUM(
    ContextualMemoryOperation,
    name="contextual_memory_operation",
    values_callable=_contextual_memory_operation_values,
    validate_strings=True,
    create_type=False,
)


class ContextualMemoryIdempotency(Base):
    """PostgreSQL source-of-truth row for one contextual idempotency claim."""

    __tablename__ = "contextual_memory_idempotency"
    __table_args__ = (
        CheckConstraint(f"idempotency_key ~ {HEX_DIGEST_PATTERN}", name="idempotency_key_hex"),
        CheckConstraint(f"request_digest ~ {HEX_DIGEST_PATTERN}", name="request_digest_hex"),
        CheckConstraint(
            "(operation IN ('episode_create', 'checkpoint_create') "
            "AND target_resource_id IS NULL) "
            "OR (operation NOT IN ('episode_create', 'checkpoint_create') "
            "AND target_resource_id IS NOT NULL)",
            name="operation_target_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    operation: Mapped[ContextualMemoryOperation] = mapped_column(
        CONTEXTUAL_MEMORY_OPERATION_ENUM, nullable=False
    )
    request_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    target_resource_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=True
    )
    result_resource_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
