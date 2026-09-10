from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from sqlalchemy import CheckConstraint, Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM, JSONB

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0015 = MIGRATIONS_VERSIONS / "0015_create_agents_foundation.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.dropped_tables: list[str] = []

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> str:
        return "bind"

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    agent_status: ENUM
    AGENT_NAME_MAX_LENGTH: int
    AGENT_DISPLAY_NAME_MAX_LENGTH: int
    AGENT_DESCRIPTION_MAX_LENGTH: int
    AGENT_INSTRUCTIONS_MAX_LENGTH: int
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_sm801_migration", MIGRATION_0015)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load SM-801 migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0015.read_text(encoding="utf-8")


def upgrade_result() -> tuple[list[str], OperationSpy]:
    module = load_migration_module()
    operation_spy = OperationSpy()
    created_enums: list[str] = []

    def create_agent_status(bind: object) -> None:
        if bind == "bind":
            created_enums.append("agent_status")

    module.agent_status.create = create_agent_status
    module.op = operation_spy

    module.upgrade()

    return created_enums, operation_spy


def table_columns(objects: tuple[object, ...]) -> dict[str, Column[object]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def check_sql(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {item.name: str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)}


def unique_names(objects: tuple[object, ...]) -> set[str | None]:
    return {item.name for item in objects if isinstance(item, UniqueConstraint)}


def test_sm801_revision_is_current_head() -> None:
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
        "0011_add_dataset_delete_pipeline_type.py",
        "0012_create_sessions_foundation.py",
        "0013_session_entry_external_id_trim_invariant.py",
        "0014_create_skills_foundation.py",
        "0015_create_agents_foundation.py",
        "0016_create_agent_skills.py",
        "0017_create_agent_sessions.py",
    ]


def test_sm801_revision_metadata_and_enum_definition_are_exact() -> None:
    module = load_migration_module()

    assert module.revision == "0015"
    assert module.down_revision == "0014"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert module.agent_status.name == "agent_status"
    assert module.agent_status.enums == ["active", "archived"]
    assert module.AGENT_NAME_MAX_LENGTH == 64
    assert module.AGENT_DISPLAY_NAME_MAX_LENGTH == 120
    assert module.AGENT_DESCRIPTION_MAX_LENGTH == 1024
    assert module.AGENT_INSTRUCTIONS_MAX_LENGTH == 65_536


def test_upgrade_creates_enum_and_exact_sm801_table() -> None:
    created_enums, operation_spy = upgrade_result()

    assert created_enums == ["agent_status"]
    assert set(operation_spy.created_tables) == {"agents"}


def test_agents_table_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["agents"]
    columns = table_columns(objects)
    checks = check_sql(objects)

    assert list(columns) == [
        "id",
        "name",
        "display_name",
        "description",
        "instructions",
        "metadata",
        "status",
        "created_at",
        "updated_at",
        "archived_at",
    ]
    assert columns["name"].nullable is False
    assert columns["display_name"].nullable is True
    assert columns["description"].nullable is True
    assert columns["instructions"].nullable is True
    assert isinstance(columns["metadata"].type, JSONB)
    assert columns["metadata"].nullable is False
    assert isinstance(columns["status"].type, ENUM)
    assert columns["status"].server_default is not None
    assert columns["archived_at"].nullable is True
    assert columns["created_at"].type.timezone is True
    assert columns["updated_at"].type.timezone is True

    assert checks["ck_agents_name_max_length"] == "char_length(name) <= 64"
    assert checks["ck_agents_name_portable_charset"] == "name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'"
    assert checks["ck_agents_display_name_max_length"] == (
        "display_name IS NULL OR char_length(display_name) <= 120"
    )
    assert checks["ck_agents_description_max_length"] == (
        "description IS NULL OR char_length(description) <= 1024"
    )
    assert checks["ck_agents_instructions_max_length"] == (
        "instructions IS NULL OR char_length(instructions) <= 65536"
    )
    assert checks["ck_agents_instructions_not_blank"] == (
        r"instructions IS NULL OR instructions ~ '\S'"
    )

    assert "uq_agents_name" in unique_names(objects)
    # No agent_uuid column: id IS the public agent_uuid, no duplicate identity column.
    assert "agent_uuid" not in columns
    # No runtime/ownership columns of any kind.
    for forbidden in (
        "dataset_id",
        "session_id",
        "tenant_id",
        "user_id",
        "owner_id",
        "model",
        "provider",
        "temperature",
        "max_tokens",
        "provider_session_id",
        "conversation_id",
        "api_key",
        "tool_credentials",
        "tool_permissions",
    ):
        assert forbidden not in columns


def test_downgrade_removes_only_sm801_objects() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    dropped_enums: list[str] = []

    def drop_agent_status(bind: object) -> None:
        if bind == "bind":
            dropped_enums.append("agent_status")

    module.agent_status.drop = drop_agent_status
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_tables == ["agents"]
    assert dropped_enums == ["agent_status"]


def test_migration_does_not_touch_forbidden_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "DROP EXTENSION" not in text
    assert "UPDATE " not in text
    assert "INSERT INTO" not in text
    assert "DATASET_ID" not in text
    assert "SESSION_ID" not in text
    assert "AGENT_UUID" not in text
    assert "OWNER_ID" not in text
    assert "TENANT_ID" not in text
    assert "USER_ID" not in text
    assert "SKILL_ID" not in text
    # Neo4j/graph_outbox non-involvement is proven structurally instead of by
    # forbidden-substring matching: test_upgrade_creates_enum_and_exact_sm801_table
    # already proves the exact created-table set is {"agents"} -- nothing
    # else is ever touched.
