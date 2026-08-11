from __future__ import annotations

import os
from collections.abc import Mapping

import pytest

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.neo4j import (
    Neo4jResource,
    create_neo4j_resource_from_settings,
    ensure_neo4j_schema,
)

NEO4J_SCHEMA_TESTS_ENV = "SOFIAS_MEMORY_RUN_NEO4J_SCHEMA_TESTS"

EXPECTED_CONSTRAINTS = {
    "entity_id_unique": ("Entity", "id"),
    "chunk_id_unique": ("Chunk", "id"),
}
EXPECTED_INDEXES = {
    "entity_dataset_id_index": ("Entity", "dataset_id"),
    "chunk_dataset_id_index": ("Chunk", "dataset_id"),
    "entity_name_index": ("Entity", "name"),
}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_neo4j_schema_bootstrap_against_configured_database() -> None:
    if os.environ.get(NEO4J_SCHEMA_TESTS_ENV) != "1":
        pytest.skip(f"set {NEO4J_SCHEMA_TESTS_ENV}=1 to run the Neo4j schema bootstrap test")

    resource = create_neo4j_resource_from_settings(load_settings())
    try:
        await resource.verify_connectivity()
        await ensure_neo4j_schema(resource)
        await ensure_neo4j_schema(resource)

        constraints = await show_constraints(resource)
        indexes = await show_indexes(resource)

        assert_required_constraints(constraints)
        assert_required_indexes(indexes)
    finally:
        await resource.close()


async def show_constraints(resource: Neo4jResource) -> list[Mapping[str, object]]:
    result = await resource.driver.execute_query("SHOW CONSTRAINTS", database_=resource.database)
    return result_records(result)


async def show_indexes(resource: Neo4jResource) -> list[Mapping[str, object]]:
    result = await resource.driver.execute_query("SHOW INDEXES", database_=resource.database)
    return result_records(result)


def result_records(result: object) -> list[Mapping[str, object]]:
    records = getattr(result, "records", ())
    return [record.data() for record in records]


def assert_required_constraints(records: list[Mapping[str, object]]) -> None:
    records_by_name = {str(record.get("name")): record for record in records}
    missing = sorted(set(EXPECTED_CONSTRAINTS) - set(records_by_name))
    assert missing == [], f"Missing Neo4j constraints: {', '.join(missing)}"

    for name, (label, property_name) in EXPECTED_CONSTRAINTS.items():
        record = records_by_name[name]
        assert record_targets_label_and_property(record, label, property_name), (
            f"Neo4j constraint {name} does not target :{label}({property_name})"
        )
        constraint_type = str(record.get("type", "")).upper()
        assert "UNIQU" in constraint_type, f"Neo4j constraint {name} is not unique"


def assert_required_indexes(records: list[Mapping[str, object]]) -> None:
    records_by_name = {str(record.get("name")): record for record in records}
    missing = sorted(set(EXPECTED_INDEXES) - set(records_by_name))
    assert missing == [], f"Missing Neo4j indexes: {', '.join(missing)}"

    for name, (label, property_name) in EXPECTED_INDEXES.items():
        record = records_by_name[name]
        assert record_targets_label_and_property(record, label, property_name), (
            f"Neo4j index {name} does not target :{label}({property_name})"
        )


def record_targets_label_and_property(
    record: Mapping[str, object],
    label: str,
    property_name: str,
) -> bool:
    labels_or_types = set(str(value) for value in object_list(record.get("labelsOrTypes")))
    properties = set(str(value) for value in object_list(record.get("properties")))
    return label in labels_or_types and property_name in properties


def object_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []
