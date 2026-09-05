from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0013 = MIGRATIONS_VERSIONS / "0013_session_entry_external_id_trim_invariant.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_check_constraints: dict[str, tuple[str, str]] = {}
        self.dropped_constraints: list[tuple[str, str, str | None]] = []

    def f(self, name: str) -> str:
        return name

    def create_check_constraint(
        self,
        constraint_name: str,
        table_name: str,
        condition: str,
        **kwargs: object,
    ) -> None:
        del kwargs
        self.created_check_constraints[constraint_name] = (table_name, condition)

    def drop_constraint(
        self,
        constraint_name: str,
        table_name: str,
        type_: str | None = None,
    ) -> None:
        self.dropped_constraints.append((constraint_name, table_name, type_))


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    CONSTRAINT_NAME: str
    CONSTRAINT_CONDITION: str
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_sm603_migration_0013", MIGRATION_0013)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load SM-603 migration 0013")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0013.read_text(encoding="utf-8")


def test_sm603_revision_is_current_head() -> None:
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
    ]


def test_sm603_revision_metadata_is_exact() -> None:
    module = load_migration_module()

    assert module.revision == "0013"
    assert module.down_revision == "0012"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert module.CONSTRAINT_NAME == "ck_session_entries_external_id_trimmed"
    assert module.CONSTRAINT_CONDITION == "external_id IS NULL OR external_id = btrim(external_id)"


def test_upgrade_creates_exactly_one_check_constraint() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    module.op = operation_spy

    module.upgrade()

    assert operation_spy.created_check_constraints == {
        "ck_session_entries_external_id_trimmed": (
            "session_entries",
            "external_id IS NULL OR external_id = btrim(external_id)",
        )
    }


def test_downgrade_drops_only_that_check_constraint() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_constraints == [
        ("ck_session_entries_external_id_trimmed", "session_entries", "check")
    ]


def test_migration_does_not_mutate_data_or_touch_forbidden_schema() -> None:
    text = migration_text().upper()

    assert "UPDATE " not in text
    assert "INSERT INTO" not in text
    assert "DELETE FROM" not in text
    assert "CREATE TABLE" not in text
    assert "DROP TABLE" not in text
    assert "CREATE INDEX" not in text
    assert "DROP INDEX" not in text
    assert "CREATE TYPE" not in text
    assert "NEO4J" not in text
    assert "GRAPH_OUTBOX" not in text
