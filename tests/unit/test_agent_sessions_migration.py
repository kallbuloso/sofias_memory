from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from sqlalchemy import Column, ForeignKeyConstraint, PrimaryKeyConstraint

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0017 = MIGRATIONS_VERSIONS / "0017_create_agent_sessions.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.dropped_tables: list[str] = []

    def f(self, name: str) -> str:
        return name

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_sm804_migration", MIGRATION_0017)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load SM-804 migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0017.read_text(encoding="utf-8")


def upgrade_result() -> OperationSpy:
    module = load_migration_module()
    operation_spy = OperationSpy()
    module.op = operation_spy
    module.upgrade()
    return operation_spy


def table_columns(objects: tuple[object, ...]) -> dict[str, Column[object]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def foreign_keys(objects: tuple[object, ...]) -> dict[str | None, ForeignKeyConstraint]:
    return {item.name: item for item in objects if isinstance(item, ForeignKeyConstraint)}


def primary_key_columns(objects: tuple[object, ...]) -> list[str]:
    constraints = [item for item in objects if isinstance(item, PrimaryKeyConstraint)]
    assert len(constraints) == 1
    # Not yet bound to a real Table (the spy never constructs one), so the
    # column names live in the constraint's own pending-args, not `.columns`.
    return list(constraints[0]._pending_colargs)  # type: ignore[attr-defined]


def test_sm804_revision_is_current_head() -> None:
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


def test_sm804_revision_metadata_is_exact() -> None:
    module = load_migration_module()

    assert module.revision == "0017"
    assert module.down_revision == "0016"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_exactly_agent_sessions_table() -> None:
    operation_spy = upgrade_result()

    assert set(operation_spy.created_tables) == {"agent_sessions"}


def test_agent_sessions_table_contract() -> None:
    operation_spy = upgrade_result()
    objects = operation_spy.created_tables["agent_sessions"]
    columns = table_columns(objects)
    fks = foreign_keys(objects)

    assert list(columns) == ["agent_id", "session_id", "created_at"]
    assert columns["agent_id"].nullable is False
    assert columns["session_id"].nullable is False
    assert columns["created_at"].nullable is False
    assert columns["created_at"].type.timezone is True

    # No redundant/speculative columns -- never a surrogate id, never
    # anything shaped like provenance, participation, or a role/status.
    for forbidden in (
        "id",
        "updated_at",
        "status",
        "metadata",
        "role",
        "agent_revision_id",
        "first_seen_at",
        "last_seen_at",
        "participated_at",
        "query_id",
        "pipeline_run_id",
        "turn_id",
        "conversation_id",
    ):
        assert forbidden not in columns

    # Composite PK is exactly (agent_id, session_id) -- no surrogate PK.
    assert primary_key_columns(objects) == ["agent_id", "session_id"]

    # Exactly two logical FK constraints, both CASCADE -- no reference to
    # queries/pipeline_runs/session_entries/datasets/skills/skill_revisions.
    assert set(fks) == {
        "fk_agent_sessions_agent_id_agents",
        "fk_agent_sessions_session_id_sessions",
    }

    agent_fk = fks["fk_agent_sessions_agent_id_agents"]
    assert list(agent_fk._pending_colargs) == ["agent_id"]  # type: ignore[attr-defined]
    assert agent_fk.elements[0].target_fullname == "agents.id"
    assert agent_fk.ondelete == "CASCADE"

    session_fk = fks["fk_agent_sessions_session_id_sessions"]
    assert list(session_fk._pending_colargs) == ["session_id"]  # type: ignore[attr-defined]
    assert session_fk.elements[0].target_fullname == "sessions.id"
    assert session_fk.ondelete == "CASCADE"


def test_downgrade_removes_only_agent_sessions() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_tables == ["agent_sessions"]


def test_migration_does_not_touch_forbidden_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "DROP EXTENSION" not in text
    assert "UPDATE " not in text
    assert "INSERT INTO" not in text
    assert "OWNER_ID" not in text
    assert "TENANT_ID" not in text
    assert "USER_ID" not in text
    # The exact created-table set is proven to be exactly {"agent_sessions"}
    # in test_upgrade_creates_exactly_agent_sessions_table -- nothing else
    # (queries/pipeline_runs/session_entries/agent_skills) is ever touched.
