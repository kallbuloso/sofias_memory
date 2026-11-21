"""Migration/model tests for 0019 (SM-CTX-001, handoff 09 SS 55)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import CHAR, CheckConstraint, Column, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM

from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models import (
    ContextualMemoryIdempotency,
    Episode,
    EpisodeProvenance,
)

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0019 = MIGRATIONS_VERSIONS / "0019_create_contextual_episodes.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: dict[str, tuple[str, list[object]]] = {}
        self.dropped_tables: list[str] = []
        self.dropped_indexes: list[str] = []

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> str:
        return "bind"

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def create_index(self, name: str, table_name: str, columns: list[object], **_: Any) -> None:
        self.created_indexes[name] = (table_name, columns)

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)

    def drop_index(self, name: str, **_: Any) -> None:
        self.dropped_indexes.append(name)


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("test_sm_ctx_001_migration", MIGRATION_0019)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(direction: str) -> tuple[Any, OperationSpy, list[str], list[str]]:
    module = load_module()
    spy = OperationSpy()
    created: list[str] = []
    dropped: list[str] = []
    for name in ("episode_kind", "episode_lifecycle", "contextual_memory_operation"):
        enum = getattr(module, name)
        enum.create = lambda bind, _n=name: created.append(_n)
        enum.drop = lambda bind, _n=name: dropped.append(_n)
    origin = module.cognitive_memory_origin_kind
    origin.create = lambda bind: created.append("cognitive_memory_origin_kind")
    origin.drop = lambda bind: dropped.append("cognitive_memory_origin_kind")
    module.op = spy
    getattr(module, direction)()
    return module, spy, created, dropped


def columns(objects: tuple[object, ...]) -> dict[str, Column[Any]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def checks(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {item.name: str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)}


def fks(objects: tuple[object, ...]) -> list[ForeignKeyConstraint]:
    return [item for item in objects if isinstance(item, ForeignKeyConstraint)]


def test_revision_metadata_follows_observed_head_0018() -> None:
    module = load_module()
    assert module.revision == "0019"
    assert module.down_revision == "0018"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_exactly_three_tables_and_three_new_enums() -> None:
    _, spy, created, _ = run("upgrade")
    assert set(spy.created_tables) == {
        "episodes",
        "episode_provenance",
        "contextual_memory_idempotency",
    }
    assert created == ["episode_kind", "episode_lifecycle", "contextual_memory_operation"]
    assert "projects" not in spy.created_tables
    assert "context_streams" not in spy.created_tables


def test_enum_values_are_exact() -> None:
    module = load_module()
    assert list(module.episode_kind.enums) == [
        "decision",
        "investigation",
        "experiment",
        "incident",
        "discovery",
        "change",
        "outcome",
        "learning",
        "milestone",
    ]
    assert list(module.episode_lifecycle.enums) == ["active", "invalidated", "forgotten"]
    assert list(module.contextual_memory_operation.enums) == [
        "episode_create",
        "episode_invalidate",
        "episode_forget",
        "checkpoint_create",
        "checkpoint_supersede",
        "checkpoint_invalidate",
        "checkpoint_forget",
    ]


def test_episodes_table_contract() -> None:
    _, spy, _, _ = run("upgrade")
    objects = spy.created_tables["episodes"]
    cols = columns(objects)
    assert list(cols) == [
        "id",
        "scope",
        "context_key",
        "kind",
        "summary",
        "situation",
        "reason",
        "outcome",
        "occurred_at",
        "embedding",
        "lifecycle",
        "confidence",
        "created_at",
        "invalidated_at",
        "invalidation_reason",
        "replacement_episode_id",
        "forgotten_at",
    ]
    assert isinstance(cols["embedding"].type, Vector)
    assert cols["embedding"].type.dim == 3072
    assert cols["lifecycle"].nullable is False
    assert cols["created_at"].nullable is False
    for nullable in ("scope", "context_key", "kind", "summary", "occurred_at", "embedding"):
        assert cols[nullable].nullable is True
    check_sql = checks(objects)
    assert set(check_sql) == {
        "ck_episodes_confidence_bounds",
        "ck_episodes_scope_grammar",
        "ck_episodes_context_key_grammar",
        "ck_episodes_summary_not_blank",
        "ck_episodes_situation_not_blank",
        "ck_episodes_reason_not_blank",
        "ck_episodes_outcome_not_blank",
        "ck_episodes_invalidation_reason_not_blank",
        "ck_episodes_no_self_replacement",
        "ck_episodes_non_forgotten_requires_payload",
        "ck_episodes_active_requires_clean_markers",
        "ck_episodes_invalidated_requires_markers",
        "ck_episodes_forgotten_requires_tombstone_shape",
    }
    assert "'global'" not in check_sql["ck_episodes_scope_grammar"]
    tombstone = check_sql["ck_episodes_forgotten_requires_tombstone_shape"]
    for scrubbed in (
        "scope",
        "context_key",
        "kind",
        "summary",
        "situation",
        "reason",
        "outcome",
        "occurred_at",
        "embedding",
        "confidence",
        "invalidation_reason",
        "replacement_episode_id",
    ):
        assert f"{scrubbed} IS NULL" in tombstone
    (fk,) = fks(objects)
    assert [element.target_fullname for element in fk.elements] == ["episodes.id"]
    assert fk.column_keys == ["replacement_episode_id"]
    assert fk.ondelete == "RESTRICT"


def test_episode_indexes_and_no_ann_index() -> None:
    _, spy, _, _ = run("upgrade")
    assert set(spy.created_indexes) == {
        "ix_episodes_scope_lifecycle_occurred_at",
        "ix_episodes_scope_context_key_lifecycle_occurred_at",
        "ix_episodes_kind",
        "ix_episodes_occurred_at",
    }
    source = MIGRATION_0019.read_text(encoding="utf-8").lower()
    assert "hnsw" not in source.replace("no ann index", "")
    assert "ivfflat" not in source


def test_episode_provenance_contract_reuses_origin_kind_enum() -> None:
    module, spy, created, _ = run("upgrade")
    objects = spy.created_tables["episode_provenance"]
    cols = columns(objects)
    assert list(cols) == [
        "episode_id",
        "origin_kind",
        "source_system",
        "conversation_ref",
        "turn_ref",
    ]
    assert isinstance(cols["origin_kind"].type, ENUM)
    assert cols["origin_kind"].type.name == "cognitive_memory_origin_kind"
    assert "cognitive_memory_origin_kind" not in created
    (fk,) = fks(objects)
    assert fk.ondelete == "CASCADE"


def test_contextual_ledger_is_resource_generic_without_episode_fk() -> None:
    _, spy, _, _ = run("upgrade")
    objects = spy.created_tables["contextual_memory_idempotency"]
    cols = columns(objects)
    assert list(cols) == [
        "id",
        "idempotency_key",
        "operation",
        "request_digest",
        "target_resource_id",
        "result_resource_id",
        "created_at",
    ]
    assert "target_episode_id" not in cols and "result_episode_id" not in cols
    assert isinstance(cols["idempotency_key"].type, CHAR)
    assert cols["idempotency_key"].type.length == 64
    assert fks(objects) == []
    uniques = {item.name for item in objects if isinstance(item, UniqueConstraint)}
    assert uniques == {"uq_contextual_memory_idempotency_idempotency_key"}
    assert set(checks(objects)) == {
        "ck_contextual_memory_idempotency_idempotency_key_hex",
        "ck_contextual_memory_idempotency_request_digest_hex",
        "ck_contextual_memory_idempotency_operation_target_shape",
    }


def test_migration_creates_no_graph_or_pipeline_schema() -> None:
    source = MIGRATION_0019.read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert "graph_outbox" not in code
    assert "pipeline_runs" not in code
    assert "memory_items" not in code


def test_downgrade_reverses_exactly_this_revision() -> None:
    _, spy, _, dropped = run("downgrade")
    assert spy.dropped_tables == ["contextual_memory_idempotency", "episode_provenance", "episodes"]
    assert set(spy.dropped_indexes) == {
        "ix_episodes_scope_lifecycle_occurred_at",
        "ix_episodes_scope_context_key_lifecycle_occurred_at",
        "ix_episodes_kind",
        "ix_episodes_occurred_at",
    }
    assert dropped == ["contextual_memory_operation", "episode_lifecycle", "episode_kind"]
    assert "cognitive_memory_origin_kind" not in dropped


def test_orm_models_mirror_migration_constraint_names() -> None:
    _, spy, _, _ = run("upgrade")
    for model in (Episode, EpisodeProvenance, ContextualMemoryIdempotency):
        table = Base.metadata.tables[model.__tablename__]
        orm_checks = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
        # 0022 (SM-CTX-004) adds ck_episodes_forgotten_has_no_support on top of 0019.
        orm_checks.discard("ck_episodes_forgotten_has_no_support")
        assert orm_checks == set(checks(spy.created_tables[model.__tablename__]))
    ledger = Base.metadata.tables["contextual_memory_idempotency"]
    assert not ledger.foreign_keys
