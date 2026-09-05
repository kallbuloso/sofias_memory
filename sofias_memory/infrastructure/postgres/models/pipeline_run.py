"""Pipeline run ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CHAR, REAL, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import PipelineRunStatus, PipelineType
from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.source import SHA256_HEX_PATTERN


def _pipeline_type_values(enum_type: type[PipelineType]) -> list[str]:
    return [pipeline_type.value for pipeline_type in enum_type]


def _pipeline_run_status_values(enum_type: type[PipelineRunStatus]) -> list[str]:
    return [status.value for status in enum_type]


PIPELINE_TYPE_ENUM = ENUM(
    PipelineType,
    name="pipeline_type",
    values_callable=_pipeline_type_values,
    validate_strings=True,
    create_type=False,
)
PIPELINE_RUN_STATUS_ENUM = ENUM(
    PipelineRunStatus,
    name="pipeline_run_status",
    values_callable=_pipeline_run_status_values,
    validate_strings=True,
    create_type=False,
)


class PipelineRun(Base):
    """Persisted write-pipeline run state."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        CheckConstraint(f"payload_hash ~ {SHA256_HEX_PATTERN}", name="payload_hash_hex"),
        CheckConstraint(
            f"config_fingerprint ~ {SHA256_HEX_PATTERN}",
            name="config_fingerprint_hex",
        ),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        Index("ix_pipeline_runs_status", "status"),
        Index("ix_pipeline_runs_dataset_id_status", "dataset_id", "status"),
        Index("ix_pipeline_runs_heartbeat_at", "heartbeat_at"),
        Index(
            "uq_pipeline_runs_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=sql_text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_pipeline_runs_created_at", "created_at"),
        Index("ix_pipeline_runs_status_next_attempt_at", "status", "next_attempt_at"),
        Index("ix_pipeline_runs_retry_of_run_id", "retry_of_run_id"),
        Index("ix_pipeline_runs_session_id", "session_id"),
        # ADR-0009 SS D's partial unique operational-run backstop, activated
        # by SM-513 (migration 0010) now that Remember -- the last
        # direct-RUNNING B4 writer -- has moved to the B5 runtime: every
        # public write pipeline creates PipelineRun rows exclusively through
        # the B5 claimer, which already serializes per-dataset execution, so
        # this index is pure defense-in-depth, never the primary arbiter.
        # dataset_id IS NULL (a true global run) stays outside the predicate.
        Index(
            "uq_pipeline_runs_dataset_id_operational",
            "dataset_id",
            unique=True,
            postgresql_where=sql_text(
                "dataset_id IS NOT NULL AND status IN ('running', 'cancelling')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    pipeline_type: Mapped[PipelineType] = mapped_column(PIPELINE_TYPE_ENUM, nullable=False)
    dataset_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[PipelineRunStatus] = mapped_column(PIPELINE_RUN_STATUS_ENUM, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    payload_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    input: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    progress: Mapped[float] = mapped_column(REAL(), nullable=False)
    current_step: Mapped[str | None] = mapped_column(Text(), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    config_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metrics: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_of_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    session_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
