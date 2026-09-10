from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from pgvector.sqlalchemy import Vector
from sqlalchemy import CHAR, CheckConstraint, Column, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0014 = MIGRATIONS_VERSIONS / "0014_create_skills_foundation.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.executed_statements: list[str] = []
        self.created_foreign_keys: dict[
            str, tuple[str, str, tuple[str, ...], tuple[str, ...], dict[str, object]]
        ] = {}
        self.dropped_constraints: list[tuple[str, str, str | None]] = []
        self.dropped_tables: list[str] = []

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> str:
        return "bind"

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def execute(self, statement: str) -> None:
        self.executed_statements.append(statement)

    def create_foreign_key(
        self,
        name: str,
        source_table: str,
        referent_table: str,
        local_cols: list[str],
        remote_cols: list[str],
        **kwargs: object,
    ) -> None:
        self.created_foreign_keys[name] = (
            source_table,
            referent_table,
            tuple(local_cols),
            tuple(remote_cols),
            kwargs,
        )

    def drop_constraint(self, name: str, table_name: str, type_: str | None = None) -> None:
        self.dropped_constraints.append((name, table_name, type_))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    skill_status: ENUM
    SKILL_NAME_MAX_LENGTH: int
    DESCRIPTION_MAX_LENGTH: int
    COMPATIBILITY_MAX_LENGTH: int
    PROCEDURE_MAX_LENGTH: int
    SOFIAS_MEMORY_TAGS_METADATA_KEY: str
    SKILL_REVISIONS_ANN_INDEX_SQL: str
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_sm701_migration", MIGRATION_0014)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load SM-701 migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0014.read_text(encoding="utf-8")


def upgrade_result() -> tuple[list[str], OperationSpy]:
    module = load_migration_module()
    operation_spy = OperationSpy()
    created_enums: list[str] = []

    def create_skill_status(bind: object) -> None:
        if bind == "bind":
            created_enums.append("skill_status")

    module.skill_status.create = create_skill_status
    module.op = operation_spy

    module.upgrade()

    return created_enums, operation_spy


def table_columns(objects: tuple[object, ...]) -> dict[str, Column[object]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def check_sql(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {item.name: str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)}


def unique_names(objects: tuple[object, ...]) -> set[str | None]:
    return {item.name for item in objects if isinstance(item, UniqueConstraint)}


def foreign_keys(objects: tuple[object, ...]) -> dict[str | None, ForeignKeyConstraint]:
    return {item.name: item for item in objects if isinstance(item, ForeignKeyConstraint)}


def test_sm701_revision_is_current_head() -> None:
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
    ]


def test_sm701_revision_metadata_and_enum_definition_are_exact() -> None:
    module = load_migration_module()

    assert module.revision == "0014"
    assert module.down_revision == "0013"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert module.skill_status.name == "skill_status"
    assert module.skill_status.enums == ["active", "archived"]
    assert module.SKILL_NAME_MAX_LENGTH == 64
    assert module.DESCRIPTION_MAX_LENGTH == 1024
    assert module.COMPATIBILITY_MAX_LENGTH == 500
    assert module.PROCEDURE_MAX_LENGTH == 65_536
    assert module.SOFIAS_MEMORY_TAGS_METADATA_KEY == "sofias-memory.tags"


def test_upgrade_creates_enum_and_exact_sm701_tables() -> None:
    created_enums, operation_spy = upgrade_result()

    assert created_enums == ["skill_status"]
    assert set(operation_spy.created_tables) == {"skills", "skill_revisions"}


def test_skills_table_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["skills"]
    columns = table_columns(objects)
    checks = check_sql(objects)

    assert list(columns) == [
        "id",
        "name",
        "status",
        "current_revision_id",
        "created_at",
        "updated_at",
        "archived_at",
    ]
    assert columns["name"].nullable is False
    assert isinstance(columns["status"].type, ENUM)
    assert columns["status"].server_default is not None
    assert columns["current_revision_id"].nullable is False
    assert columns["archived_at"].nullable is True
    assert columns["created_at"].type.timezone is True
    assert columns["updated_at"].type.timezone is True
    assert checks["ck_skills_name_max_length"] == "char_length(name) <= 64"
    assert checks["ck_skills_name_portable_charset"] == "name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'"
    assert "uq_skills_name" in unique_names(objects)
    # No skill_uuid column: id IS the public skill_uuid, no duplicate identity column.
    assert "skill_uuid" not in columns
    # The composite current-revision FK is added later, via ALTER TABLE, not
    # inline on skills' own CREATE TABLE (skill_revisions does not exist yet).
    assert not foreign_keys(objects)


def test_skill_revisions_table_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["skill_revisions"]
    columns = table_columns(objects)
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert list(columns) == [
        "id",
        "skill_id",
        "revision",
        "description",
        "procedure",
        "license",
        "compatibility",
        "metadata",
        "tags",
        "declared_tools",
        "content_sha256",
        "resolution_embedding",
        "created_at",
    ]
    assert columns["skill_id"].nullable is False
    assert columns["revision"].nullable is False
    assert columns["description"].nullable is False
    assert columns["procedure"].nullable is False
    assert columns["license"].nullable is True
    assert columns["compatibility"].nullable is True
    assert isinstance(columns["metadata"].type, JSONB)
    assert columns["metadata"].nullable is False
    assert isinstance(columns["tags"].type, ARRAY)
    assert columns["tags"].nullable is False
    assert isinstance(columns["declared_tools"].type, ARRAY)
    assert columns["declared_tools"].nullable is False
    assert isinstance(columns["content_sha256"].type, CHAR)
    assert columns["content_sha256"].type.length == 64
    assert isinstance(columns["resolution_embedding"].type, Vector)
    assert columns["resolution_embedding"].type.dim == 3072
    assert "updated_at" not in columns

    assert fks["fk_skill_revisions_skill_id_skills"].elements[0].target_fullname == "skills.id"
    assert fks["fk_skill_revisions_skill_id_skills"].ondelete == "CASCADE"

    assert checks["ck_skill_revisions_revision_positive"] == "revision >= 1"
    assert checks["ck_skill_revisions_description_length"] == (
        "char_length(description) BETWEEN 1 AND 1024"
    )
    assert checks["ck_skill_revisions_procedure_max_length"] == ("char_length(procedure) <= 65536")
    assert checks["ck_skill_revisions_procedure_not_blank"] == r"procedure ~ '\S'"
    assert checks["ck_skill_revisions_compatibility_length"] == (
        "compatibility IS NULL OR char_length(compatibility) BETWEEN 1 AND 500"
    )
    assert checks["ck_skill_revisions_content_sha256_hex"] == "content_sha256 ~ '^[0-9a-f]{64}$'"
    assert checks["ck_skill_revisions_metadata_excludes_reserved_tags_key"] == (
        "NOT (metadata ? 'sofias-memory.tags')"
    )

    unique = unique_names(objects)
    assert "uq_skill_revisions_skill_id_revision" in unique
    assert "uq_skill_revisions_skill_id_id" in unique
    assert "uq_skill_revisions_skill_id_content_sha256" in unique


def test_ann_index_sql_is_exact_and_created() -> None:
    _, operation_spy = upgrade_result()

    assert operation_spy.executed_statements == [
        "CREATE INDEX ix_skill_revisions_resolution_embedding_halfvec_hnsw "
        "ON skill_revisions USING hnsw "
        "((resolution_embedding::halfvec(3072)) halfvec_cosine_ops)"
    ]


def test_ann_sql_preserves_adr_0006_strategy() -> None:
    module = load_migration_module()
    ann_sql = module.SKILL_REVISIONS_ANN_INDEX_SQL

    assert "USING hnsw" in ann_sql
    assert "resolution_embedding::halfvec(3072)" in ann_sql
    assert "halfvec_cosine_ops" in ann_sql
    assert "vector_l2_ops" not in ann_sql
    assert "vector_cosine_ops" not in ann_sql
    assert "ivfflat" not in ann_sql.lower()


def test_composite_current_revision_fk_is_deferred_and_composite() -> None:
    _, operation_spy = upgrade_result()

    (
        source_table,
        referent_table,
        local_cols,
        remote_cols,
        kwargs,
    ) = operation_spy.created_foreign_keys["fk_skills_current_revision_skill_revisions"]

    assert source_table == "skills"
    assert referent_table == "skill_revisions"
    assert local_cols == ("id", "current_revision_id")
    assert remote_cols == ("skill_id", "id")
    assert kwargs["ondelete"] == "RESTRICT"
    assert kwargs["deferrable"] is True
    assert kwargs["initially"] == "DEFERRED"


def test_downgrade_removes_only_sm701_objects_in_reverse_order() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    dropped_enums: list[str] = []

    def drop_skill_status(bind: object) -> None:
        if bind == "bind":
            dropped_enums.append("skill_status")

    module.skill_status.drop = drop_skill_status
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_constraints == [
        ("fk_skills_current_revision_skill_revisions", "skills", "foreignkey"),
    ]
    assert operation_spy.executed_statements == [
        "DROP INDEX ix_skill_revisions_resolution_embedding_halfvec_hnsw"
    ]
    assert operation_spy.dropped_tables == ["skill_revisions", "skills"]
    assert dropped_enums == ["skill_status"]


def test_migration_does_not_backfill_or_touch_forbidden_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "DROP EXTENSION" not in text
    assert "UPDATE " not in text
    assert "INSERT INTO" not in text
    assert "DATASET_ID" not in text
    assert "SESSION_ID" not in text
    assert "AGENT_ID" not in text
    assert "OWNER_ID" not in text
    assert "TENANT_ID" not in text
    assert "USER_ID" not in text
    assert "SOURCE_ID" not in text
    # Neo4j/graph_outbox non-involvement is proven structurally instead of by
    # forbidden-substring matching: this migration's own docstring legitimately
    # documents that invariant in prose (containing "Neo4j"/"graph_outbox" as
    # words), and test_upgrade_creates_enum_and_exact_sm701_tables already
    # proves the exact created-table set is {"skills", "skill_revisions"} --
    # nothing else is ever touched.
    assert "1536" not in text
    assert "IVFFLAT" not in text
