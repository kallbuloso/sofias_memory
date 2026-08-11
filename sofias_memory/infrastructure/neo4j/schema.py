"""Neo4j schema bootstrap contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sofias_memory.infrastructure.neo4j.driver import Neo4jResource


@dataclass(frozen=True)
class Neo4jSchemaStatement:
    """One canonical Neo4j schema bootstrap statement."""

    name: str
    kind: Literal["constraint", "index"]
    label: str
    property_name: str
    cypher: str


NEO4J_SCHEMA_STATEMENTS: tuple[Neo4jSchemaStatement, ...] = (
    Neo4jSchemaStatement(
        name="entity_id_unique",
        kind="constraint",
        label="Entity",
        property_name="id",
        cypher=" ".join(
            (
                "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS",
                "FOR (n:Entity)",
                "REQUIRE n.id IS UNIQUE",
            )
        ),
    ),
    Neo4jSchemaStatement(
        name="chunk_id_unique",
        kind="constraint",
        label="Chunk",
        property_name="id",
        cypher=" ".join(
            (
                "CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS",
                "FOR (n:Chunk)",
                "REQUIRE n.id IS UNIQUE",
            )
        ),
    ),
    Neo4jSchemaStatement(
        name="entity_dataset_id_index",
        kind="index",
        label="Entity",
        property_name="dataset_id",
        cypher=" ".join(
            (
                "CREATE INDEX entity_dataset_id_index IF NOT EXISTS",
                "FOR (n:Entity)",
                "ON (n.dataset_id)",
            )
        ),
    ),
    Neo4jSchemaStatement(
        name="chunk_dataset_id_index",
        kind="index",
        label="Chunk",
        property_name="dataset_id",
        cypher=" ".join(
            (
                "CREATE INDEX chunk_dataset_id_index IF NOT EXISTS",
                "FOR (n:Chunk)",
                "ON (n.dataset_id)",
            )
        ),
    ),
    Neo4jSchemaStatement(
        name="entity_name_index",
        kind="index",
        label="Entity",
        property_name="name",
        cypher=" ".join(
            (
                "CREATE INDEX entity_name_index IF NOT EXISTS",
                "FOR (n:Entity)",
                "ON (n.name)",
            )
        ),
    ),
)

NEO4J_SCHEMA_NAMES = frozenset(statement.name for statement in NEO4J_SCHEMA_STATEMENTS)


async def ensure_neo4j_schema(resource: Neo4jResource) -> None:
    """Install the minimal Neo4j schema explicitly and idempotently."""

    for statement in NEO4J_SCHEMA_STATEMENTS:
        await resource.driver.execute_query(statement.cypher, database_=resource.database)
