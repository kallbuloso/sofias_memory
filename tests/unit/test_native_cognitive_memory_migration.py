"""Migration/model tests for 0018 (ADR-0016, Feature Contract v0.7.0, SM-1001)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from pgvector.sqlalchemy import Vector
from sqlalchemy import CHAR, REAL, CheckConstraint, Column, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0018 = MIGRATIONS_VERSIONS / "0018_create_native_cognitive_memory.py"


class CreatedIndex:
    def __init__(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        *,
        unique: bool,
        postgresql_where: object | None,
    ) -> None:
        self.name = name
        self.table_name = table_name
        self.columns = columns
        self.unique = unique
        self.postgresql_where = postgresql_where


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: dict[str, CreatedIndex] = {}
        self.executed_statements: list[str] = []
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
        *,
        unique: bool = False,
        postgresql_where: object | None = None,
    ) -> None:
        self.created_indexes[name] = CreatedIndex(
            name, table_name, columns, unique=unique, postgresql_where=postgresql_where
        )

    def execute(self, statement: str) -> None:
        self.executed_statements.append(statement)

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    cognitive_memory_type: ENUM
    cognitive_memory_lifecycle: ENUM
    cognitive_memory_origin_kind: ENUM
    cognitive_memory_operation: ENUM
    EMBEDDING_DIMENSIONS: int
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_sm1001_migration", MIGRATION_0018)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load SM-1001 migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0018.read_text(encoding="utf-8")


def upgrade_result() -> tuple[list[str], OperationSpy]:
    module = load_migration_module()
    operation_spy = OperationSpy()
    created_enums: list[str] = []

    def make_creator(enum_name: str):
        def _create(bind: object) -> None:
            if bind == "bind":
                created_enums.append(enum_name)

        return _create

    module.cognitive_memory_type.create = make_creator("cognitive_memory_type")
    module.cognitive_memory_lifecycle.create = make_creator("cognitive_memory_lifecycle")
    module.cognitive_memory_origin_kind.create = make_creator("cognitive_memory_origin_kind")
    module.cognitive_memory_operation.create = make_creator("cognitive_memory_operation")
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


def test_sm1001_revision_is_current_head() -> None:
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
        "0018_create_native_cognitive_memory.py",
    ]


def test_sm1001_revision_metadata_is_exact() -> None:
    module = load_migration_module()

    assert module.revision == "0018"
    assert module.down_revision == "0017"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_exactly_three_tables() -> None:
    created_enums, operation_spy = upgrade_result()

    assert set(operation_spy.created_tables) == {
        "memory_items",
        "memory_provenance",
        "cognitive_memory_idempotency",
    }
    assert created_enums == [
        "cognitive_memory_type",
        "cognitive_memory_lifecycle",
        "cognitive_memory_origin_kind",
        "cognitive_memory_operation",
    ]


def test_memory_items_table_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["memory_items"]
    columns = table_columns(objects)
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert list(columns) == [
        "id",
        "memory_type",
        "scope",
        "content",
        "embedding",
        "lifecycle",
        "confidence",
        "valid_from",
        "valid_until",
        "created_at",
        "superseded_at",
        "superseded_by",
        "forgotten_at",
    ]
    assert columns["id"].nullable is False
    assert columns["memory_type"].nullable is False
    assert columns["scope"].nullable is True
    assert columns["content"].nullable is True
    assert isinstance(columns["embedding"].type, Vector)
    assert columns["embedding"].type.dim == 3072
    assert columns["embedding"].nullable is True
    assert columns["lifecycle"].nullable is False
    assert isinstance(columns["confidence"].type, REAL)
    assert columns["confidence"].nullable is True
    assert columns["superseded_by"].nullable is True
    assert columns["created_at"].nullable is False
    assert columns["created_at"].type.timezone is True

    # Forbidden columns: no separate public identity, no importance, no
    # PATCH-shaped audit columns, no Dataset/Session/Skill/Agent coupling.
    for forbidden in (
        "memory_id",
        "importance",
        "updated_at",
        "dataset_id",
        "session_id",
        "skill_id",
        "agent_id",
        "owner_id",
        "tenant_id",
    ):
        assert forbidden not in columns

    assert set(fks) == {"fk_memory_items_superseded_by_memory_items"}
    self_fk = fks["fk_memory_items_superseded_by_memory_items"]
    assert list(self_fk._pending_colargs) == ["superseded_by"]  # type: ignore[attr-defined]
    assert self_fk.elements[0].target_fullname == "memory_items.id"
    assert self_fk.ondelete == "RESTRICT"

    assert checks["ck_memory_items_confidence_bounds"] == (
        "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)"
    )
    assert checks["ck_memory_items_validity_window_ordered"] == (
        "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from"
    )
    assert checks["ck_memory_items_no_self_supersession"] == (
        "superseded_by IS NULL OR superseded_by <> id"
    )
    assert checks["ck_memory_items_scope_grammar"] == (
        "scope IS NULL OR scope = 'global' OR scope ~ '^project:[a-z0-9][a-z0-9._-]{0,127}$'"
    )
    non_forgotten_check = checks["ck_memory_items_non_forgotten_requires_cognitive_payload"]
    superseded_check = checks["ck_memory_items_superseded_requires_lineage_markers"]
    forgotten_check = checks["ck_memory_items_forgotten_requires_tombstone_shape"]
    active_check = checks["ck_memory_items_active_requires_clean_lineage"]
    assert "lifecycle = 'forgotten'" in non_forgotten_check
    assert "lifecycle <> 'active'" in active_check
    assert "lifecycle <> 'superseded'" in superseded_check
    assert "lifecycle <> 'forgotten'" in forgotten_check


def test_memory_items_superseded_by_partial_unique_index() -> None:
    _, operation_spy = upgrade_result()

    index = operation_spy.created_indexes["uq_memory_items_superseded_by"]
    assert index.table_name == "memory_items"
    assert index.columns == ["superseded_by"]
    assert index.unique is True
    assert str(index.postgresql_where) == "superseded_by IS NOT NULL"


def test_memory_provenance_table_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["memory_provenance"]
    columns = table_columns(objects)
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert list(columns) == [
        "memory_id",
        "origin_kind",
        "source_system",
        "conversation_uuid",
        "turn_uuid",
        "task_uuid",
        "confirmation_ref",
        "source_ref",
        "observed_at",
    ]
    assert columns["memory_id"].nullable is False
    assert columns["origin_kind"].nullable is False
    assert columns["source_system"].nullable is False
    for optional in (
        "conversation_uuid",
        "turn_uuid",
        "task_uuid",
        "confirmation_ref",
        "source_ref",
        "observed_at",
    ):
        assert columns[optional].nullable is True

    assert set(fks) == {"fk_memory_provenance_memory_id_memory_items"}
    memory_fk = fks["fk_memory_provenance_memory_id_memory_items"]
    assert list(memory_fk._pending_colargs) == ["memory_id"]  # type: ignore[attr-defined]
    assert memory_fk.elements[0].target_fullname == "memory_items.id"
    assert memory_fk.ondelete == "CASCADE"

    assert checks["ck_memory_provenance_source_system_slug"] == (
        "source_system ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(source_system) <= 64"
    )
    assert checks["ck_memory_provenance_confirmation_ref_length"] == (
        "confirmation_ref IS NULL OR char_length(confirmation_ref) BETWEEN 1 AND 255"
    )
    assert checks["ck_memory_provenance_source_ref_length"] == (
        "source_ref IS NULL OR char_length(source_ref) BETWEEN 1 AND 255"
    )

    # No CHECK enforcing "origin X requires ref Y": that would make the
    # Forget-approved scrub structurally impossible (see module docstring).
    assert "user_asserted" not in " ".join(checks.values())
    assert "tool_observed" not in " ".join(checks.values())


def test_memory_provenance_primary_key_is_memory_id_only() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["memory_provenance"]
    columns = table_columns(objects)

    # 1:1 with memory_items: the FK column IS the primary key, no surrogate id.
    assert "id" not in columns


def test_cognitive_memory_idempotency_table_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["cognitive_memory_idempotency"]
    columns = table_columns(objects)
    checks = check_sql(objects)
    fks = foreign_keys(objects)
    uniques = unique_names(objects)

    assert list(columns) == [
        "id",
        "idempotency_key",
        "operation",
        "request_digest",
        "target_memory_id",
        "result_memory_id",
        "created_at",
    ]
    assert columns["idempotency_key"].nullable is False
    assert columns["operation"].nullable is False
    assert isinstance(columns["request_digest"].type, CHAR)
    assert columns["request_digest"].type.length == 64
    assert columns["target_memory_id"].nullable is True
    assert columns["result_memory_id"].nullable is True

    assert "uq_cognitive_memory_idempotency_idempotency_key" in uniques

    assert set(fks) == {
        "fk_cognitive_memory_idempotency_target_memory_id_memory_items",
        "fk_cognitive_memory_idempotency_result_memory_id_memory_items",
    }
    for fk_name in fks:
        assert fks[fk_name].ondelete == "RESTRICT"
        assert fks[fk_name].elements[0].target_fullname == "memory_items.id"

    assert checks["ck_cognitive_memory_idempotency_request_digest_hex"] == (
        "request_digest ~ '^[0-9a-f]{64}$'"
    )
    assert checks["ck_cognitive_memory_idempotency_operation_target_shape"] == (
        "(operation = 'create' AND target_memory_id IS NULL) "
        "OR (operation IN ('supersede', 'forget') AND target_memory_id IS NOT NULL)"
    )

    # Never a second copy of request/content/embedding.
    for forbidden in ("content", "embedding", "payload", "request_body"):
        assert forbidden not in columns


def test_downgrade_removes_exactly_the_three_tables_and_enums() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    dropped_enums: list[str] = []

    def make_dropper(enum_name: str):
        def _drop(bind: object) -> None:
            if bind == "bind":
                dropped_enums.append(enum_name)

        return _drop

    module.cognitive_memory_type.drop = make_dropper("cognitive_memory_type")
    module.cognitive_memory_lifecycle.drop = make_dropper("cognitive_memory_lifecycle")
    module.cognitive_memory_origin_kind.drop = make_dropper("cognitive_memory_origin_kind")
    module.cognitive_memory_operation.drop = make_dropper("cognitive_memory_operation")
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_tables == [
        "cognitive_memory_idempotency",
        "memory_provenance",
        "memory_items",
    ]
    assert any(
        "DROP INDEX uq_memory_items_superseded_by" in stmt
        for stmt in operation_spy.executed_statements
    )
    assert dropped_enums == [
        "cognitive_memory_operation",
        "cognitive_memory_origin_kind",
        "cognitive_memory_lifecycle",
        "cognitive_memory_type",
    ]


def test_migration_does_not_touch_forbidden_schema() -> None:
    text = migration_text()
    # Drop the module docstring first: it legitimately *discusses* tables
    # this migration deliberately does not touch (graph_outbox,
    # pipeline_runs, ...) as rationale -- the code body is what must never
    # reference them.
    _, _, code_body = text.partition('"""\n')
    _, _, code_body = code_body.partition('"""')
    upper = code_body.upper()

    assert "CREATE EXTENSION" not in upper
    assert "DROP EXTENSION" not in upper
    assert "UPDATE " not in upper
    assert "INSERT INTO" not in upper
    assert "OWNER_ID" not in upper
    assert "TENANT_ID" not in upper
    assert "USER_ID" not in upper

    assert "graph_outbox" not in code_body
    assert "pipeline_runs" not in code_body
    assert "pipeline_steps" not in code_body
    assert "datasets" not in code_body
    assert "sessions" not in code_body
    assert "session_entries" not in code_body
    assert "skills" not in code_body
    assert "agents" not in code_body
    assert "episodic" not in code_body.lower()
    assert "procedural" not in code_body.lower()
    assert "importance" not in code_body.lower()
    assert "hnsw" not in code_body.lower()
    assert "halfvec" not in code_body.lower()
