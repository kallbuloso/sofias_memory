"""Migration/model tests for 0020 (SM-CTX-002, handoff 10 SS 95). Structural
only -- the real-PostgreSQL behaviour (single ACTIVE, revision uniqueness,
grammar CHECKs, downgrade) is proven by the opt-in integration suite and the
migration gate, never by ``Base.metadata.create_all()``."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, Index, UniqueConstraint

from sofias_memory.infrastructure.postgres.models import (
    ContextCheckpoint,
    ContextCheckpointProvenance,
    ContextCheckpointStatement,
)

MIGRATION_0020 = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0020_create_context_checkpoints.py"
)
TABLES = ("context_checkpoints", "context_checkpoint_statements", "context_checkpoint_provenance")


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: dict[str, tuple[str, list[object], dict[str, Any]]] = {}
        self.dropped_tables: list[str] = []
        self.dropped_indexes: list[str] = []

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> str:
        return "bind"

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def create_index(self, name: str, table_name: str, columns: list[object], **kw: Any) -> None:
        self.created_indexes[name] = (table_name, columns, kw)

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)

    def drop_index(self, name: str, **_: Any) -> None:
        self.dropped_indexes.append(name)


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("test_sm_ctx_002_migration", MIGRATION_0020)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(direction: str) -> tuple[Any, OperationSpy, list[str], list[str]]:
    module = load_module()
    spy = OperationSpy()
    created: list[str] = []
    dropped: list[str] = []
    for name in (
        "context_checkpoint_lifecycle",
        "context_statement_type",
        "cognitive_memory_origin_kind",
    ):
        enum = getattr(module, name)
        enum.create = lambda bind, _n=name: created.append(_n)
        enum.drop = lambda bind, _n=name: dropped.append(_n)
    module.op = spy
    getattr(module, direction)()
    return module, spy, created, dropped


def columns(objects: tuple[object, ...]) -> dict[str, Column[Any]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def checks(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {
        str(item.name): str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)
    }


def model_checks(model: Any) -> dict[str, str]:
    table = model.__table__
    return {
        str(item.name): str(item.sqltext)
        for item in table.constraints
        if isinstance(item, CheckConstraint)
    }


def test_revision_metadata_chains_from_sm_ctx_001_head() -> None:
    module = load_module()
    assert module.revision == "0020"
    assert module.down_revision == "0019"


def test_upgrade_creates_exactly_three_tables_and_two_new_enums() -> None:
    _, spy, created, _ = run("upgrade")
    assert tuple(spy.created_tables) == TABLES
    assert created == ["context_checkpoint_lifecycle", "context_statement_type"]
    assert "context_streams" not in spy.created_tables and "projects" not in spy.created_tables


def test_enum_values_are_the_approved_sets() -> None:
    module = load_module()
    assert list(module.context_checkpoint_lifecycle.enums) == [
        "active",
        "superseded",
        "invalidated",
        "forgotten",
    ]
    assert list(module.context_statement_type.enums) == [
        "objective",
        "current_state",
        "approved_decision",
        "pending_work",
        "open_item",
        "blocker",
        "next_action",
    ]


def test_checkpoint_stream_identity_columns_are_not_null_and_no_embedding() -> None:
    _, spy, _, _ = run("upgrade")
    cols = columns(spy.created_tables["context_checkpoints"])
    for name in ("scope", "context_key", "revision", "lifecycle", "created_at"):
        assert cols[name].nullable is False, name
    for name in ("superseded_at", "superseded_by", "invalidated_at", "forgotten_at"):
        assert cols[name].nullable is True, name
    assert "embedding" not in cols and "session_id" not in cols and "confidence" not in cols
    statement_cols = columns(spy.created_tables["context_checkpoint_statements"])
    assert set(statement_cols) == {
        "id",
        "checkpoint_id",
        "statement_type",
        "ordinal",
        "content",
        "created_at",
    }


def test_single_active_partial_unique_index_and_revision_uniqueness() -> None:
    _, spy, _, _ = run("upgrade")
    table, cols, kw = spy.created_indexes["uq_context_checkpoints_active_stream"]
    assert table == "context_checkpoints" and cols == ["scope", "context_key"]
    assert kw["unique"] is True
    assert str(kw["postgresql_where"]) == "lifecycle = 'active'"
    uniques = {
        item.name: [c.name if hasattr(c, "name") else c for c in item._pending_colargs]  # type: ignore[attr-defined]
        for item in spy.created_tables["context_checkpoints"]
        if isinstance(item, UniqueConstraint)
    }
    assert uniques == {
        "uq_context_checkpoints_scope_context_key_revision": ["scope", "context_key", "revision"]
    }


def test_grammar_and_lifecycle_checks() -> None:
    _, spy, _, _ = run("upgrade")
    found = checks(spy.created_tables["context_checkpoints"])
    assert found["ck_context_checkpoints_scope_grammar"] == (
        "scope ~ '^project:[a-z0-9][a-z0-9._-]{0,127}$'"
    )
    assert found["ck_context_checkpoints_context_key_grammar"] == (
        "context_key ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'"
    )
    assert "revision >= 1" in found["ck_context_checkpoints_revision_positive"]
    forgotten = found["ck_context_checkpoints_forgotten_requires_tombstone_shape"]
    assert "invalidation_reason IS NULL" in forgotten
    assert "scope" not in forgotten and "context_key" not in forgotten


def test_statement_order_and_scalar_constraints() -> None:
    _, spy, _, _ = run("upgrade")
    objects = spy.created_tables["context_checkpoint_statements"]
    found = checks(objects)
    assert found["ck_context_checkpoint_statements_ordinal_non_negative"] == "ordinal >= 0"
    assert found["ck_context_checkpoint_statements_scalar_ordinal_zero"] == (
        "statement_type NOT IN ('objective', 'current_state', 'next_action') OR ordinal = 0"
    )
    assert "content" in found["ck_context_checkpoint_statements_content_not_blank"]
    uniques = [item.name for item in objects if isinstance(item, UniqueConstraint)]
    assert uniques == ["uq_context_checkpoint_statements_checkpoint_id_type_ordinal"]


def test_foreign_keys_restrict_lineage_and_cascade_children() -> None:
    _, spy, _, _ = run("upgrade")
    policies = {
        item.name: item.ondelete
        for table in TABLES
        for item in spy.created_tables[table]
        if isinstance(item, ForeignKeyConstraint)
    }
    assert policies == {
        "fk_context_checkpoints_superseded_by_context_checkpoints": "RESTRICT",
        "fk_context_checkpoint_statements_checkpoint_id": "CASCADE",
        "fk_context_checkpoint_provenance_checkpoint_id": "CASCADE",
    }


def test_downgrade_drops_exactly_what_upgrade_created() -> None:
    _, up, created, _ = run("upgrade")
    _, down, _, dropped = run("downgrade")
    assert sorted(down.dropped_tables) == sorted(up.created_tables)
    assert sorted(down.dropped_indexes) == sorted(up.created_indexes)
    assert sorted(dropped) == sorted(created)
    assert "cognitive_memory_origin_kind" not in dropped


def test_orm_models_mirror_migration_checks_and_indexes() -> None:
    _, spy, _, _ = run("upgrade")
    for model, table in (
        (ContextCheckpoint, "context_checkpoints"),
        (ContextCheckpointStatement, "context_checkpoint_statements"),
        (ContextCheckpointProvenance, "context_checkpoint_provenance"),
    ):
        assert model_checks(model) == checks(spy.created_tables[table]), table
    model_indexes = {
        index.name for index in ContextCheckpoint.__table__.indexes if isinstance(index, Index)
    }
    # 0021 (SM-CTX-003) adds uq_context_checkpoints_superseded_by on top of 0020.
    assert model_indexes == set(spy.created_indexes) | {"uq_context_checkpoints_superseded_by"}


def test_every_declared_identifier_fits_postgresql_63_byte_limit() -> None:
    """PostgreSQL silently truncates longer identifiers (SQLAlchemy appends a
    hash), which would make the declared names differ from the real catalog."""

    _, spy, _, _ = run("upgrade")
    names = [
        str(item.name)
        for table in TABLES
        for item in spy.created_tables[table]
        if getattr(item, "name", None) is not None and not isinstance(item, Column)
    ]
    names.extend(spy.created_indexes)
    assert names
    assert [name for name in names if len(name.encode()) > 63] == []
    model_names = [
        str(constraint.name)
        for model in (ContextCheckpoint, ContextCheckpointStatement, ContextCheckpointProvenance)
        for constraint in model.__table__.constraints
    ]
    assert [name for name in model_names if len(name.encode()) > 63] == []
    assert "fk_context_checkpoint_statements_checkpoint_id" in model_names
    assert "fk_context_checkpoint_provenance_checkpoint_id" in model_names
    assert "ck_context_checkpoints_reason_requires_invalidated_at" in model_names
