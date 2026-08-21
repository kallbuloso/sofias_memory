"""Pipeline step ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CHAR, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import PipelineStepStatus
from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.source import SHA256_HEX_PATTERN


def _pipeline_step_status_values(enum_type: type[PipelineStepStatus]) -> list[str]:
    return [status.value for status in enum_type]


PIPELINE_STEP_STATUS_ENUM = ENUM(
    PipelineStepStatus,
    name="pipeline_step_status",
    values_callable=_pipeline_step_status_values,
    validate_strings=True,
    create_type=False,
)


class PipelineStep(Base):
    """Persisted state for one step inside a pipeline run."""

    __tablename__ = "pipeline_steps"
    __table_args__ = (
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint(
            f"input_hash IS NULL OR input_hash ~ {SHA256_HEX_PATTERN}",
            name="input_hash_hex",
        ),
        Index("ix_pipeline_steps_run_id", "run_id"),
        Index("uq_pipeline_steps_run_id_ordinal", "run_id", "ordinal", unique=True),
        Index("ix_pipeline_steps_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PipelineStepStatus] = mapped_column(PIPELINE_STEP_STATUS_ENUM, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    input_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    output: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    error: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
