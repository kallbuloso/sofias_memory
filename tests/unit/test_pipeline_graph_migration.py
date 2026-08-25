from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint
from sqlalchemy.dialects.postgresql import ENUM, JSONB

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0007 = MIGRATIONS_VERSIONS / "0007_create_pipeline_and_graph_outbox.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: dict[str, tuple[str, tuple[str, ...], dict[str, object]]] = {}
        self.dropped_indexes: list[tuple[str, str | None]] = []
        self.dropped_tables: list[str] = []

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> str:
        return "bind"

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        **kwargs: object,
    ) -> None:
        self.created_indexes[name] = (table_name, tuple(columns), kwargs)

    def drop_index(self, name: str, table_name: str | None = None) -> None:
        self.dropped_indexes.append((name, table_name))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    pipeline_type: ENUM
    pipeline_run_status: ENUM
    pipeline_step_status: ENUM
    graph_outbox_operation: ENUM
    graph_outbox_status: ENUM
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_sm211_migration", MIGRATION_0007)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load SM-211 migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0007.read_text(encoding="utf-8")


def upgrade_result() -> tuple[list[str], OperationSpy]:
    module = load_migration_module()
    operation_spy = OperationSpy()
    created_enums: list[str] = []

    def track_create(enum_name: str):
        def create_enum(bind: object) -> None:
            if bind == "bind":
                created_enums.append(enum_name)

        return create_enum

    module.pipeline_type.create = track_create("pipeline_type")
    module.pipeline_run_status.create = track_create("pipeline_run_status")
    module.pipeline_step_status.create = track_create("pipeline_step_status")
    module.graph_outbox_operation.create = track_create("graph_outbox_operation")
    module.graph_outbox_status.create = track_create("graph_outbox_status")
    module.op = operation_spy

    module.upgrade()

    return created_enums, operation_spy


def table_columns(objects: tuple[object, ...]) -> dict[str, Column[object]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def check_sql(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {item.name: str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)}


def foreign_keys(objects: tuple[object, ...]) -> dict[str | None, ForeignKeyConstraint]:
    return {item.name: item for item in objects if isinstance(item, ForeignKeyConstraint)}


def test_sm211_revision_is_current_head() -> None:
    revision_files = sorted(path.name for path in MIGRATIONS_VERSIONS.glob("*.py"))

    assert revision_files == [
        "0001_enable_required_extensions.py",
        "0002_create_datasets.py",
        "0003_create_sources_and_documents.py",
        "0004_create_chunks.py",
        "0005_create_entities_relations.py",
        "0006_create_summaries_memory_queries_feedback.py",
        "0007_create_pipeline_and_graph_outbox.py",
        "0008_pipeline_run_retry_and_operational_constraints.py",
        "0009_graph_outbox_processing_lease.py",
        "0010_pipeline_runs_operational_unique_constraint.py",
    ]


def test_sm211_revision_metadata_and_enum_definitions_are_exact() -> None:
    module = load_migration_module()

    assert module.revision == "0007"
    assert module.down_revision == "0006"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert module.pipeline_type.enums == ["remember", "cognify", "improve", "forget"]
    assert module.pipeline_run_status.enums == [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelling",
        "cancelled",
    ]
    assert module.pipeline_step_status.enums == module.pipeline_run_status.enums
    assert "stale" not in module.pipeline_run_status.enums
    assert module.graph_outbox_operation.enums == ["upsert", "delete"]
    assert module.graph_outbox_status.enums == ["pending", "processing", "done", "failed"]


def test_upgrade_creates_enums_and_exact_sm211_tables() -> None:
    created_enums, operation_spy = upgrade_result()

    assert created_enums == [
        "pipeline_type",
        "pipeline_run_status",
        "pipeline_step_status",
        "graph_outbox_operation",
        "graph_outbox_status",
    ]
    assert set(operation_spy.created_tables) == {
        "graph_outbox",
        "pipeline_runs",
        "pipeline_steps",
    }


def test_pipeline_runs_migration_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["pipeline_runs"]
    columns = table_columns(objects)
    fks = foreign_keys(objects)
    checks = check_sql(objects)

    assert list(columns) == [
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
    ]
    assert columns["dataset_id"].nullable is True
    assert columns["source_id"].nullable is True
    assert columns["idempotency_key"].nullable is True
    assert columns["payload_hash"].type.length == 64
    assert isinstance(columns["input"].type, JSONB)
    assert columns["config_fingerprint"].type.length == 64
    assert isinstance(columns["metrics"].type, JSONB)
    assert columns["heartbeat_at"].nullable is True
    assert columns["started_at"].nullable is True
    assert columns["finished_at"].nullable is True
    assert fks["fk_pipeline_runs_dataset_id_datasets"].ondelete == "SET NULL"
    assert fks["fk_pipeline_runs_source_id_sources"].ondelete == "SET NULL"
    assert checks["ck_pipeline_runs_payload_hash_hex"] == "payload_hash ~ '^[0-9a-fA-F]{64}$'"
    assert checks["ck_pipeline_runs_config_fingerprint_hex"] == (
        "config_fingerprint ~ '^[0-9a-fA-F]{64}$'"
    )
    assert checks["ck_pipeline_runs_attempt_non_negative"] == "attempt >= 0"


def test_pipeline_runs_indexes_and_idempotency_unique_are_exact() -> None:
    _, operation_spy = upgrade_result()

    assert operation_spy.created_indexes["ix_pipeline_runs_status"] == (
        "pipeline_runs",
        ("status",),
        {},
    )
    assert operation_spy.created_indexes["ix_pipeline_runs_dataset_id_status"] == (
        "pipeline_runs",
        ("dataset_id", "status"),
        {},
    )
    assert operation_spy.created_indexes["ix_pipeline_runs_heartbeat_at"] == (
        "pipeline_runs",
        ("heartbeat_at",),
        {},
    )
    table_name, columns, kwargs = operation_spy.created_indexes["uq_pipeline_runs_idempotency_key"]
    assert table_name == "pipeline_runs"
    assert columns == ("idempotency_key",)
    assert kwargs["unique"] is True
    assert str(kwargs["postgresql_where"]) == "idempotency_key IS NOT NULL"
    assert "payload_hash" not in columns
    assert "ix_pipeline_runs_idempotency_key" not in operation_spy.created_indexes
    assert operation_spy.created_indexes["ix_pipeline_runs_created_at"] == (
        "pipeline_runs",
        ("created_at",),
        {},
    )


def test_pipeline_steps_migration_contract_and_indexes() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["pipeline_steps"]
    columns = table_columns(objects)
    fks = foreign_keys(objects)
    checks = check_sql(objects)

    assert list(columns) == [
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
    assert fks["fk_pipeline_steps_run_id_pipeline_runs"].ondelete == "CASCADE"
    assert columns["input_hash"].nullable is True
    assert columns["input_hash"].type.length == 64
    assert isinstance(columns["output"].type, JSONB)
    assert isinstance(columns["metrics"].type, JSONB)
    assert isinstance(columns["error"].type, JSONB)
    assert columns["error"].nullable is True
    assert checks["ck_pipeline_steps_attempt_non_negative"] == "attempt >= 0"
    assert checks["ck_pipeline_steps_input_hash_hex"] == (
        "input_hash IS NULL OR input_hash ~ '^[0-9a-fA-F]{64}$'"
    )
    assert operation_spy.created_indexes["ix_pipeline_steps_run_id"] == (
        "pipeline_steps",
        ("run_id",),
        {},
    )
    assert operation_spy.created_indexes["ix_pipeline_steps_run_id_ordinal"] == (
        "pipeline_steps",
        ("run_id", "ordinal"),
        {},
    )
    assert operation_spy.created_indexes["ix_pipeline_steps_status"] == (
        "pipeline_steps",
        ("status",),
        {},
    )


def test_graph_outbox_migration_contract_and_indexes() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["graph_outbox"]
    columns = table_columns(objects)
    fks = foreign_keys(objects)
    checks = check_sql(objects)

    assert list(columns) == [
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
    ]
    assert columns["id"].autoincrement is True
    assert not fks
    assert len(columns["dataset_id"].foreign_keys) == 0
    assert len(columns["aggregate_id"].foreign_keys) == 0
    assert isinstance(columns["payload"].type, JSONB)
    assert checks["ck_graph_outbox_attempt_non_negative"] == "attempt >= 0"
    assert operation_spy.created_indexes["ix_graph_outbox_status"] == (
        "graph_outbox",
        ("status",),
        {},
    )
    assert operation_spy.created_indexes["ix_graph_outbox_status_created_at"] == (
        "graph_outbox",
        ("status", "created_at"),
        {},
    )
    assert operation_spy.created_indexes["ix_graph_outbox_dataset_id"] == (
        "graph_outbox",
        ("dataset_id",),
        {},
    )
    assert operation_spy.created_indexes["ix_graph_outbox_aggregate"] == (
        "graph_outbox",
        ("aggregate_type", "aggregate_id"),
        {},
    )


def test_downgrade_removes_only_sm211_objects_and_enums() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    dropped_enums: list[str] = []

    def track_drop(enum_name: str):
        def drop_enum(bind: object) -> None:
            if bind == "bind":
                dropped_enums.append(enum_name)

        return drop_enum

    module.graph_outbox_status.drop = track_drop("graph_outbox_status")
    module.graph_outbox_operation.drop = track_drop("graph_outbox_operation")
    module.pipeline_step_status.drop = track_drop("pipeline_step_status")
    module.pipeline_run_status.drop = track_drop("pipeline_run_status")
    module.pipeline_type.drop = track_drop("pipeline_type")
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_tables == ["graph_outbox", "pipeline_steps", "pipeline_runs"]
    assert dropped_enums == [
        "graph_outbox_status",
        "graph_outbox_operation",
        "pipeline_step_status",
        "pipeline_run_status",
        "pipeline_type",
    ]


def test_migration_does_not_create_runtime_or_future_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "DROP EXTENSION" not in text
    assert ".DROP(OP.GET_BIND())" in text
    assert "DROP TABLE" not in text
    assert "DROP INDEX" not in text
    assert "DROP CASCADE" not in text
    assert text.count('ONDELETE="CASCADE"') == 1
    assert "STALE" not in text
    assert "REDIS" not in text
    assert "CELERY" not in text
    assert "NEO4J" not in text
    assert "FOR UPDATE" not in text
    assert "SKIP LOCKED" not in text
    assert "REPOSITORY" not in text
    assert "UNITOFWORK" not in text
    assert "OWNER_ID" not in text
    assert "TENANT_ID" not in text
    assert "USER_ID" not in text
    assert "DELETED_AT" not in text
