"""Graph outbox ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import GraphOutboxOperation, GraphOutboxStatus
from sofias_memory.infrastructure.postgres.base import Base


def _graph_outbox_operation_values(enum_type: type[GraphOutboxOperation]) -> list[str]:
    return [operation.value for operation in enum_type]


def _graph_outbox_status_values(enum_type: type[GraphOutboxStatus]) -> list[str]:
    return [status.value for status in enum_type]


GRAPH_OUTBOX_OPERATION_ENUM = ENUM(
    GraphOutboxOperation,
    name="graph_outbox_operation",
    values_callable=_graph_outbox_operation_values,
    validate_strings=True,
    create_type=False,
)
GRAPH_OUTBOX_STATUS_ENUM = ENUM(
    GraphOutboxStatus,
    name="graph_outbox_status",
    values_callable=_graph_outbox_status_values,
    validate_strings=True,
    create_type=False,
)


class GraphOutbox(Base):
    """Durable PostgreSQL-to-Neo4j projection instruction."""

    __tablename__ = "graph_outbox"
    __table_args__ = (
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        Index("ix_graph_outbox_status", "status"),
        Index("ix_graph_outbox_status_created_at", "status", "created_at"),
        Index("ix_graph_outbox_dataset_id", "dataset_id"),
        Index("ix_graph_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text(), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    operation: Mapped[GraphOutboxOperation] = mapped_column(
        GRAPH_OUTBOX_OPERATION_ENUM,
        nullable=False,
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[GraphOutboxStatus] = mapped_column(GRAPH_OUTBOX_STATUS_ENUM, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
