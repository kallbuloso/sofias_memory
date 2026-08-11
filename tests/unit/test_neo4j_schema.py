from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping

import pytest

from sofias_memory.infrastructure.neo4j import Neo4jResource
from sofias_memory.infrastructure.neo4j.schema import (
    NEO4J_SCHEMA_NAMES,
    NEO4J_SCHEMA_STATEMENTS,
    ensure_neo4j_schema,
)

CONFIGURED_DATABASE = "sofias-memory-schema-test"

EXPECTED_SCHEMA = (
    ("entity_id_unique", "constraint", "Entity", "id"),
    ("chunk_id_unique", "constraint", "Chunk", "id"),
    ("entity_dataset_id_index", "index", "Entity", "dataset_id"),
    ("chunk_dataset_id_index", "index", "Chunk", "dataset_id"),
    ("entity_name_index", "index", "Entity", "name"),
)


class RecordingAsyncNeo4jDriver:
    def __init__(self) -> None:
        self.verify_connectivity_calls: list[dict[str, object]] = []
        self.execute_query_calls: list[dict[str, object]] = []
        self.close_calls = 0

    async def verify_connectivity(self, **config: object) -> None:
        if "database_" in config:
            raise AssertionError("verify_connectivity must use database= session config")
        self.verify_connectivity_calls.append(config)

    async def execute_query(
        self,
        query_: str,
        parameters_: Mapping[str, object] | None = None,
        *,
        database_: str | None = None,
    ) -> object:
        if parameters_ is not None:
            raise AssertionError("schema bootstrap does not use query parameters")
        self.execute_query_calls.append({"query": query_, "database_": database_})
        return object()

    async def close(self) -> None:
        self.close_calls += 1


def test_neo4j_schema_statement_contract_is_exact() -> None:
    actual = tuple(
        (statement.name, statement.kind, statement.label, statement.property_name)
        for statement in NEO4J_SCHEMA_STATEMENTS
    )

    assert actual == EXPECTED_SCHEMA
    assert frozenset(name for name, *_ in EXPECTED_SCHEMA) == NEO4J_SCHEMA_NAMES


def test_neo4j_schema_statements_use_canonical_cypher() -> None:
    statements_by_name = {statement.name: statement.cypher for statement in NEO4J_SCHEMA_STATEMENTS}

    assert statements_by_name == {
        "entity_id_unique": " ".join(
            (
                "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS",
                "FOR (n:Entity)",
                "REQUIRE n.id IS UNIQUE",
            )
        ),
        "chunk_id_unique": " ".join(
            (
                "CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS",
                "FOR (n:Chunk)",
                "REQUIRE n.id IS UNIQUE",
            )
        ),
        "entity_dataset_id_index": " ".join(
            (
                "CREATE INDEX entity_dataset_id_index IF NOT EXISTS",
                "FOR (n:Entity)",
                "ON (n.dataset_id)",
            )
        ),
        "chunk_dataset_id_index": " ".join(
            (
                "CREATE INDEX chunk_dataset_id_index IF NOT EXISTS",
                "FOR (n:Chunk)",
                "ON (n.dataset_id)",
            )
        ),
        "entity_name_index": " ".join(
            (
                "CREATE INDEX entity_name_index IF NOT EXISTS",
                "FOR (n:Entity)",
                "ON (n.name)",
            )
        ),
    }


def test_neo4j_schema_has_only_authorized_constraints_and_indexes() -> None:
    constraints = {
        statement.name for statement in NEO4J_SCHEMA_STATEMENTS if statement.kind == "constraint"
    }
    indexes = {statement.name for statement in NEO4J_SCHEMA_STATEMENTS if statement.kind == "index"}

    assert constraints == {"entity_id_unique", "chunk_id_unique"}
    assert indexes == {
        "entity_dataset_id_index",
        "chunk_dataset_id_index",
        "entity_name_index",
    }


def test_neo4j_schema_statements_are_safe_bootstrap_only() -> None:
    for statement in NEO4J_SCHEMA_STATEMENTS:
        normalized = " ".join(statement.cypher.lower().split())
        assert "if not exists" in normalized
        assert "drop " not in normalized
        assert "apoc" not in normalized
        assert "gds" not in normalized
        assert "merge " not in normalized
        assert "create (" not in normalized
        assert "create relationship" not in normalized


def test_neo4j_schema_constraints_are_unique_and_indexes_are_simple() -> None:
    for statement in NEO4J_SCHEMA_STATEMENTS:
        normalized = " ".join(statement.cypher.split())
        if statement.kind == "constraint":
            assert normalized.startswith(f"CREATE CONSTRAINT {statement.name} IF NOT EXISTS")
            assert normalized.endswith(f"REQUIRE n.{statement.property_name} IS UNIQUE")
        else:
            assert normalized.startswith(f"CREATE INDEX {statement.name} IF NOT EXISTS")
            assert normalized.endswith(f"ON (n.{statement.property_name})")
            assert "," not in normalized.split("ON", maxsplit=1)[1]
        assert f"FOR (n:{statement.label})" in normalized


@pytest.mark.asyncio
async def test_ensure_neo4j_schema_executes_statements_in_order_with_database() -> None:
    driver = RecordingAsyncNeo4jDriver()
    resource = Neo4jResource(driver, database=CONFIGURED_DATABASE)

    await ensure_neo4j_schema(resource)

    assert [call["query"] for call in driver.execute_query_calls] == [
        statement.cypher for statement in NEO4J_SCHEMA_STATEMENTS
    ]
    assert {call["database_"] for call in driver.execute_query_calls} == {CONFIGURED_DATABASE}
    assert driver.verify_connectivity_calls == []


@pytest.mark.asyncio
async def test_ensure_neo4j_schema_can_be_called_twice() -> None:
    driver = RecordingAsyncNeo4jDriver()
    resource = Neo4jResource(driver, database=CONFIGURED_DATABASE)

    await ensure_neo4j_schema(resource)
    await ensure_neo4j_schema(resource)

    assert len(driver.execute_query_calls) == 2 * len(NEO4J_SCHEMA_STATEMENTS)


def test_neo4j_schema_import_does_not_execute_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    import sofias_memory.infrastructure.neo4j.driver as neo4j_driver_module

    def fail_if_resource_is_created(*args: object, **kwargs: object) -> Neo4jResource:
        raise AssertionError("schema import must not create a Neo4j resource")

    monkeypatch.setattr(neo4j_driver_module, "Neo4jResource", fail_if_resource_is_created)
    sys.modules.pop("sofias_memory.infrastructure.neo4j.schema", None)

    importlib.import_module("sofias_memory.infrastructure.neo4j.schema")


def test_resource_construction_does_not_bootstrap_schema() -> None:
    driver = RecordingAsyncNeo4jDriver()

    Neo4jResource(driver, database=CONFIGURED_DATABASE)

    assert driver.execute_query_calls == []


@pytest.mark.asyncio
async def test_verify_connectivity_still_uses_database_session_config() -> None:
    driver = RecordingAsyncNeo4jDriver()
    resource = Neo4jResource(driver, database=CONFIGURED_DATABASE)

    await resource.verify_connectivity()

    assert driver.verify_connectivity_calls == [{"database": CONFIGURED_DATABASE}]
    assert driver.execute_query_calls == []
