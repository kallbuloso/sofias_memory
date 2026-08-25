from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, Index
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.sql.schema import Column

from sofias_memory.infrastructure.postgres import GraphOutbox, PipelineRun, PipelineStep


def column_names(model: type[object]) -> list[str]:
    return [column.name for column in model.__table__.columns]  # type: ignore[attr-defined]


def foreign_keys(model: type[object]) -> dict[str, ForeignKeyConstraint]:
    return {
        constraint.name: constraint
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, ForeignKeyConstraint)
    }


def check_sql(model: type[object]) -> dict[str | None, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, CheckConstraint)
    }


def indexes(model: type[object]) -> dict[str, Index]:
    return {index.name: index for index in model.__table__.indexes}  # type: ignore[attr-defined]


def index_columns(index: Index) -> list[str]:
    return [column.name for column in index.columns if isinstance(column, Column)]


def test_pipeline_runs_columns_types_nullability_and_fks_are_exact() -> None:
    assert PipelineRun.__tablename__ == "pipeline_runs"
    assert column_names(PipelineRun) == [
        "id",
        "pipeline_type",
        "dataset_id",
        "source_id",
        "status",
        "idempotency_key",
        "payload_hash",
        "input",
        "progress",
        "current_step",
        "attempt",
        "worker_id",
        "heartbeat_at",
        "config_fingerprint",
        "error_code",
        "error_message",
        "metrics",
        "created_at",
        "started_at",
        "finished_at",
        "next_attempt_at",
        "retry_of_run_id",
    ]

    columns = PipelineRun.__table__.c
    fks = foreign_keys(PipelineRun)

    assert isinstance(columns.id.type, PostgreSQLUUID)
    assert columns.id.primary_key is True
    assert isinstance(columns.pipeline_type.type, ENUM)
    assert columns.pipeline_type.type.name == "pipeline_type"
    assert columns.pipeline_type.type.enums == ["remember", "cognify", "improve", "forget"]
    assert isinstance(columns.status.type, ENUM)
    assert columns.status.type.name == "pipeline_run_status"
    assert "stale" not in columns.status.type.enums
    assert columns.dataset_id.nullable is True
    assert columns.source_id.nullable is True
    assert fks["fk_pipeline_runs_dataset_id_datasets"].ondelete == "SET NULL"
    assert fks["fk_pipeline_runs_source_id_sources"].ondelete == "SET NULL"
    assert fks["fk_pipeline_runs_retry_of_run_id_pipeline_runs"].ondelete == "SET NULL"
    assert columns.idempotency_key.nullable is True
    assert columns.payload_hash.type.length == 64
    assert isinstance(columns.input.type, JSONB)
    assert columns.progress.type.python_type is float
    assert columns.current_step.nullable is True
    assert columns.worker_id.nullable is True
    assert columns.heartbeat_at.nullable is True
    assert columns.heartbeat_at.type.timezone is True
    assert columns.config_fingerprint.type.length == 64
    assert columns.error_code.nullable is True
    assert columns.error_message.nullable is True
    assert isinstance(columns.metrics.type, JSONB)
    assert columns.created_at.type.timezone is True
    assert columns.started_at.nullable is True
    assert columns.finished_at.nullable is True
    assert columns.next_attempt_at.nullable is True
    assert columns.next_attempt_at.type.timezone is True
    assert columns.retry_of_run_id.nullable is True
    assert isinstance(columns.retry_of_run_id.type, PostgreSQLUUID)
    assert "updated_at" not in columns


def test_pipeline_runs_checks_and_indexes_are_exact() -> None:
    checks = check_sql(PipelineRun)
    model_indexes = indexes(PipelineRun)
    idempotency_index = model_indexes["uq_pipeline_runs_idempotency_key"]

    assert checks["ck_pipeline_runs_payload_hash_hex"] == "payload_hash ~ '^[0-9a-fA-F]{64}$'"
    assert checks["ck_pipeline_runs_config_fingerprint_hex"] == (
        "config_fingerprint ~ '^[0-9a-fA-F]{64}$'"
    )
    assert checks["ck_pipeline_runs_attempt_non_negative"] == "attempt >= 0"
    assert "progress" not in " ".join(checks.values())
    assert set(model_indexes) == {
        "ix_pipeline_runs_created_at",
        "ix_pipeline_runs_dataset_id_status",
        "ix_pipeline_runs_heartbeat_at",
        "ix_pipeline_runs_status",
        "uq_pipeline_runs_idempotency_key",
        "ix_pipeline_runs_status_next_attempt_at",
        "ix_pipeline_runs_retry_of_run_id",
        "uq_pipeline_runs_dataset_id_operational",
    }
    assert index_columns(model_indexes["ix_pipeline_runs_status"]) == ["status"]
    assert index_columns(model_indexes["ix_pipeline_runs_dataset_id_status"]) == [
        "dataset_id",
        "status",
    ]
    assert index_columns(model_indexes["ix_pipeline_runs_heartbeat_at"]) == ["heartbeat_at"]
    assert index_columns(model_indexes["ix_pipeline_runs_created_at"]) == ["created_at"]
    assert idempotency_index.unique is True
    assert index_columns(idempotency_index) == ["idempotency_key"]
    assert str(idempotency_index.dialect_options["postgresql"]["where"]) == (
        "idempotency_key IS NOT NULL"
    )
    assert index_columns(model_indexes["ix_pipeline_runs_status_next_attempt_at"]) == [
        "status",
        "next_attempt_at",
    ]
    assert index_columns(model_indexes["ix_pipeline_runs_retry_of_run_id"]) == ["retry_of_run_id"]
    # ADR-0009 SS D's UNIQUE(dataset_id) WHERE ... IN ('running', 'cancelling')
    # operational backstop, activated by SM-513 now that Remember (the last
    # direct-RUNNING B4 writer) has moved to the B5 runtime.
    operational_index = model_indexes["uq_pipeline_runs_dataset_id_operational"]
    assert operational_index.unique is True
    assert index_columns(operational_index) == ["dataset_id"]
    assert str(operational_index.dialect_options["postgresql"]["where"]) == (
        "dataset_id IS NOT NULL AND status IN ('running', 'cancelling')"
    )


def test_pipeline_steps_columns_fks_checks_and_indexes_are_exact() -> None:
    assert PipelineStep.__tablename__ == "pipeline_steps"
    assert column_names(PipelineStep) == [
        "id",
        "run_id",
        "name",
        "ordinal",
        "status",
        "attempt",
        "input_hash",
        "output",
        "metrics",
        "error",
        "started_at",
        "finished_at",
    ]

    columns = PipelineStep.__table__.c
    fks = foreign_keys(PipelineStep)
    checks = check_sql(PipelineStep)
    model_indexes = indexes(PipelineStep)

    assert isinstance(columns.id.type, PostgreSQLUUID)
    assert columns.id.primary_key is True
    assert fks["fk_pipeline_steps_run_id_pipeline_runs"].ondelete == "CASCADE"
    assert isinstance(columns.status.type, ENUM)
    assert columns.status.type.name == "pipeline_step_status"
    assert "stale" not in columns.status.type.enums
    assert columns.input_hash.type.length == 64
    assert columns.input_hash.nullable is True
    assert isinstance(columns.output.type, JSONB)
    assert isinstance(columns.metrics.type, JSONB)
    assert isinstance(columns.error.type, JSONB)
    assert columns.error.nullable is True
    assert columns.started_at.nullable is True
    assert columns.finished_at.nullable is True
    assert checks["ck_pipeline_steps_attempt_non_negative"] == "attempt >= 0"
    assert checks["ck_pipeline_steps_input_hash_hex"] == (
        "input_hash IS NULL OR input_hash ~ '^[0-9a-fA-F]{64}$'"
    )
    assert set(model_indexes) == {
        "ix_pipeline_steps_run_id",
        "uq_pipeline_steps_run_id_ordinal",
        "ix_pipeline_steps_status",
    }
    assert index_columns(model_indexes["ix_pipeline_steps_run_id"]) == ["run_id"]
    assert index_columns(model_indexes["uq_pipeline_steps_run_id_ordinal"]) == [
        "run_id",
        "ordinal",
    ]
    assert model_indexes["uq_pipeline_steps_run_id_ordinal"].unique is True
    assert index_columns(model_indexes["ix_pipeline_steps_status"]) == ["status"]


def test_graph_outbox_columns_types_no_fks_and_indexes_are_exact() -> None:
    assert GraphOutbox.__tablename__ == "graph_outbox"
    assert column_names(GraphOutbox) == [
        "id",
        "dataset_id",
        "aggregate_type",
        "aggregate_id",
        "operation",
        "payload",
        "status",
        "attempt",
        "created_at",
        "processed_at",
        "processing_started_at",
        "worker_id",
    ]

    columns = GraphOutbox.__table__.c
    checks = check_sql(GraphOutbox)
    model_indexes = indexes(GraphOutbox)

    assert isinstance(columns.id.type, BigInteger)
    assert columns.id.primary_key is True
    assert columns.id.autoincrement is True
    assert isinstance(columns.dataset_id.type, PostgreSQLUUID)
    assert len(columns.dataset_id.foreign_keys) == 0
    assert len(columns.aggregate_id.foreign_keys) == 0
    assert isinstance(columns.operation.type, ENUM)
    assert columns.operation.type.name == "graph_outbox_operation"
    assert columns.operation.type.enums == ["upsert", "delete"]
    assert isinstance(columns.payload.type, JSONB)
    assert isinstance(columns.status.type, ENUM)
    assert columns.status.type.name == "graph_outbox_status"
    assert columns.status.type.enums == ["pending", "processing", "done", "failed"]
    assert checks["ck_graph_outbox_attempt_non_negative"] == "attempt >= 0"
    assert columns.created_at.type.timezone is True
    assert columns.processed_at.nullable is True
    assert columns.processing_started_at.nullable is True
    assert columns.worker_id.nullable is True
    assert set(model_indexes) == {
        "ix_graph_outbox_aggregate",
        "ix_graph_outbox_dataset_id",
        "ix_graph_outbox_status",
        "ix_graph_outbox_status_created_at",
        "ix_graph_outbox_status_processing_started_at",
    }
    assert index_columns(model_indexes["ix_graph_outbox_status"]) == ["status"]
    assert index_columns(model_indexes["ix_graph_outbox_status_created_at"]) == [
        "status",
        "created_at",
    ]
    assert index_columns(model_indexes["ix_graph_outbox_dataset_id"]) == ["dataset_id"]
    assert index_columns(model_indexes["ix_graph_outbox_aggregate"]) == [
        "aggregate_type",
        "aggregate_id",
    ]
    assert index_columns(model_indexes["ix_graph_outbox_status_processing_started_at"]) == [
        "status",
        "processing_started_at",
    ]


def test_no_forbidden_or_runtime_columns_are_present() -> None:
    forbidden = {
        "deleted_at",
        "lease_id",
        "locked_at",
        "owner_id",
        "priority",
        "queue_name",
        "stale_at",
        "tenant_id",
        "updated_at",
        "user_id",
        "worker_runtime",
    }

    for model in (PipelineRun, PipelineStep, GraphOutbox):
        assert forbidden.isdisjoint(column_names(model))
