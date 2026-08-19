"""Real PostgreSQL + Neo4j regression for the graph_reconciliation stage order.

Reproduces the GATE-B4 scenario N finding: reconciliation must observe the
Neo4j projection before graph maintenance (hygiene + importance) can enqueue
and drain its own entity/relation upserts, since an entity_upsert command
uses Cypher MERGE and would otherwise silently recreate a missing Entity node
before reconciliation ever reads "actual", masking genuine drift. No LLM or
embedding provider calls are made; only the graph_reconciliation stage is
exercised, which never touches the embedding client.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.neo4j import (
    Neo4jProjection,
    create_neo4j_resource_from_settings,
)
from sofias_memory.infrastructure.postgres import (
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.schemas.improve import ImproveRequest
from sofias_memory.services.graph_maintenance_service import GraphMaintenanceService
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.graph_rebuild_service import GraphRebuildService
from sofias_memory.services.graph_reconciliation_service import GraphReconciliationService
from sofias_memory.services.improve import ImproveService
from tests.integration.test_graph_maintenance_postgres_integration import (
    GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_NAME,
    graph_maintenance_test_database_url,
    vector_literal,
)

NEO4J_TESTS_ENV = "SOFIAS_MEMORY_RUN_GRAPH_RECONCILIATION_ORDER_NEO4J_TESTS"


class NoOpEmbeddingClient:
    """graph_reconciliation never calls embed_texts; this exists only to
    satisfy ImproveService's required constructor parameter."""

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("embed_texts must not be called for graph_reconciliation")


@pytest_asyncio.fixture()
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    database_url = graph_maintenance_test_database_url(os.environ)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            current_database = await connection.scalar(text("SELECT current_database()"))
        if current_database != GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_NAME:
            pytest.skip(
                "connected PostgreSQL database is not the dedicated graph maintenance test database"
            )
        yield engine
    finally:
        await dispose_async_engine(engine)


def require_real_neo4j() -> None:
    if os.environ.get(NEO4J_TESTS_ENV) != "1":
        pytest.skip(f"set {NEO4J_TESTS_ENV}=1 to run real Neo4j graph_reconciliation order tests")


class OrderIds:
    def __init__(self) -> None:
        self.dataset_id = uuid4()
        self.e1_id = uuid4()
        self.e2_id = uuid4()


async def seed_two_active_entities(engine: AsyncEngine, ids: OrderIds) -> None:
    vector = vector_literal(3072)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO datasets (id, name, slug, description, status, active_generation) "
                "VALUES (:dataset_id, :name, :slug, NULL, 'active', 0)"
            ),
            {
                "dataset_id": ids.dataset_id,
                "name": f"Reconciliation order {ids.dataset_id}",
                "slug": f"reconciliation-order-{ids.dataset_id}",
            },
        )
        await connection.execute(
            text(
                "INSERT INTO entities (id, dataset_id, generation, canonical_key, name, "
                "entity_type, description, aliases, properties, confidence, importance_weight, "
                "embedding, is_active) VALUES "
                "(:e1_id, :dataset_id, 0, :e1_key, 'Entity One', 'concept', 'e1', "
                "ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5, CAST(:vector AS vector), TRUE), "
                "(:e2_id, :dataset_id, 0, :e2_key, 'Entity Two', 'concept', 'e2', "
                "ARRAY[]::text[], '{}'::jsonb, 0.9, 0.5, CAST(:vector AS vector), TRUE)"
            ),
            {
                "e1_id": ids.e1_id,
                "e2_id": ids.e2_id,
                "dataset_id": ids.dataset_id,
                "e1_key": f"e1-{ids.e1_id}",
                "e2_key": f"e2-{ids.e2_id}",
                "vector": vector,
            },
        )


async def cleanup_order_fixture(engine: AsyncEngine, ids: OrderIds) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM graph_outbox WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text("DELETE FROM entities WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text("DELETE FROM relations WHERE dataset_id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )
        await connection.execute(
            text("DELETE FROM datasets WHERE id = :dataset_id"),
            {"dataset_id": ids.dataset_id},
        )


async def entity_properties(engine: AsyncEngine, entity_id: UUID) -> dict[str, object]:
    async with engine.connect() as connection:
        row = await connection.execute(
            text("SELECT properties, importance_weight, is_active FROM entities WHERE id = :id"),
            {"id": entity_id},
        )
        record = row.mappings().one()
        return dict(record)


def build_improve_service(
    session_factory,
    neo4j_resource,
) -> tuple[ImproveService, Neo4jProjection]:
    projection = Neo4jProjection(neo4j_resource)
    outbox_processor = GraphOutboxProcessor(session_factory=session_factory, projection=projection)
    rebuild_service = GraphRebuildService(
        session_factory=session_factory,
        neo4j_resource=neo4j_resource,
        projection=projection,
    )
    return ImproveService(
        load_settings(),
        session_factory=session_factory,
        embedding_client=NoOpEmbeddingClient(),
        graph_projection_drain=GraphOutboxBatchProcessor(
            session_factory=session_factory,
            processor=outbox_processor,
        ),
        graph_reconciliation=GraphReconciliationService(
            session_factory=session_factory,
            neo4j_resource=neo4j_resource,
            rebuild_service=rebuild_service,
        ),
        graph_maintenance=GraphMaintenanceService(session_factory=session_factory),
    ), projection


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciliation_detects_missing_entity_despite_fresh_importance_recalculation(
    postgres_engine: AsyncEngine,
) -> None:
    require_real_neo4j()
    ids = OrderIds()
    session_factory = create_session_factory(postgres_engine)
    neo4j_resource = create_neo4j_resource_from_settings(load_settings())
    try:
        await seed_two_active_entities(postgres_engine, ids)
        service, projection = build_improve_service(session_factory, neo4j_resource)

        rebuild_service = GraphRebuildService(
            session_factory=session_factory,
            neo4j_resource=neo4j_resource,
            projection=projection,
        )
        await rebuild_service.rebuild_dataset(ids.dataset_id)

        # Sanity: both entities projected, neither has an importance marker yet
        # (maintenance will therefore enqueue entity_upsert for both on the
        # first graph_reconciliation call -- the exact precondition that
        # masked the original bug).
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            result = await session.run(
                "MATCH (n:Entity {dataset_id: $ds}) RETURN n.id AS id",
                ds=str(ids.dataset_id),
            )
            projected_ids = {record["id"] async for record in result}
        assert projected_ids == {str(ids.e1_id), str(ids.e2_id)}

        # Remove only E1 from Neo4j, by identity.
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(ids.e1_id))

        e1_before = await entity_properties(postgres_engine, ids.e1_id)
        assert e1_before["is_active"] is True

        result_1 = await service.improve(
            ImproveRequest(
                dataset="reconciliation-order-" + str(ids.dataset_id),
                stages=["graph_reconciliation"],
            )
        )

        assert result_1.graph_entities_missing == 1
        assert result_1.graph_rebuilt is True

        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            restored = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN count(n) AS c", id=str(ids.e1_id)
            )
            record = await restored.single()
            assert record is not None and record["c"] == 1

        e1_after = await entity_properties(postgres_engine, ids.e1_id)
        assert "_sofias_memory_importance" in e1_after["properties"]

        result_2 = await service.improve(
            ImproveRequest(
                dataset="reconciliation-order-" + str(ids.dataset_id),
                stages=["graph_reconciliation"],
            )
        )
        assert result_2.graph_entities_missing == 0
        assert result_2.graph_entities_extra == 0
        assert result_2.graph_rebuilt is False
    finally:
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(ids.e1_id))
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(ids.e2_id))
        await neo4j_resource.driver.close()
        await cleanup_order_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciliation_order_preserves_relation_hygiene(
    postgres_engine: AsyncEngine,
) -> None:
    """SM-420 regression: reconciliation-first must not break relation hygiene."""
    require_real_neo4j()
    ids = OrderIds()
    relation_id = uuid4()
    session_factory = create_session_factory(postgres_engine)
    neo4j_resource = create_neo4j_resource_from_settings(load_settings())
    try:
        await seed_two_active_entities(postgres_engine, ids)
        vector = vector_literal(3072)
        async with postgres_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO relations (id, dataset_id, generation, source_entity_id, "
                    "target_entity_id, predicate, description, properties, confidence, "
                    "importance_weight, embedding, is_active) VALUES "
                    "(:relation_id, :dataset_id, 0, :e1_id, :e2_id, 'no_evidence', "
                    "'no evidence relation', '{}'::jsonb, 0.8, 0.5, CAST(:vector AS vector), TRUE)"
                ),
                {
                    "relation_id": relation_id,
                    "dataset_id": ids.dataset_id,
                    "e1_id": ids.e1_id,
                    "e2_id": ids.e2_id,
                    "vector": vector,
                },
            )

        service, projection = build_improve_service(session_factory, neo4j_resource)
        rebuild_service = GraphRebuildService(
            session_factory=session_factory,
            neo4j_resource=neo4j_resource,
            projection=projection,
        )
        await rebuild_service.rebuild_dataset(ids.dataset_id)

        result = await service.improve(
            ImproveRequest(
                dataset="reconciliation-order-" + str(ids.dataset_id),
                stages=["graph_reconciliation"],
            )
        )
        assert result.graph_relations_deactivated == 1

        async with postgres_engine.connect() as connection:
            row = await connection.execute(
                text("SELECT is_active FROM relations WHERE id = :id"), {"id": relation_id}
            )
            record = row.mappings().one()
        assert record["is_active"] is False

        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            projected = await session.run(
                "MATCH ()-[r:RELATES_TO {relation_id: $id}]->() RETURN count(r) AS c",
                id=str(relation_id),
            )
            record2 = await projected.single()
            assert record2 is not None and record2["c"] == 0

        result_2 = await service.improve(
            ImproveRequest(
                dataset="reconciliation-order-" + str(ids.dataset_id),
                stages=["graph_reconciliation"],
            )
        )
        assert result_2.graph_relations_deactivated == 0
        assert result_2.graph_entities_missing == 0
        assert result_2.graph_entities_extra == 0
    finally:
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(ids.e1_id))
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(ids.e2_id))
        await neo4j_resource.driver.close()
        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM relations WHERE id = :id"), {"id": relation_id}
            )
        await cleanup_order_fixture(postgres_engine, ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciliation_order_still_computes_importance_without_drift(
    postgres_engine: AsyncEngine,
) -> None:
    """No Neo4j drift + no importance marker: reconciliation-first must not
    invent divergence, and maintenance must still compute+persist importance."""
    require_real_neo4j()
    ids = OrderIds()
    session_factory = create_session_factory(postgres_engine)
    neo4j_resource = create_neo4j_resource_from_settings(load_settings())
    try:
        await seed_two_active_entities(postgres_engine, ids)
        service, projection = build_improve_service(session_factory, neo4j_resource)
        rebuild_service = GraphRebuildService(
            session_factory=session_factory,
            neo4j_resource=neo4j_resource,
            projection=projection,
        )
        await rebuild_service.rebuild_dataset(ids.dataset_id)

        result_1 = await service.improve(
            ImproveRequest(
                dataset="reconciliation-order-" + str(ids.dataset_id),
                stages=["graph_reconciliation"],
            )
        )
        assert result_1.graph_entities_missing == 0
        assert result_1.graph_entities_extra == 0
        assert result_1.graph_rebuilt is False
        assert result_1.graph_entities_importance_updated == 2

        e1_after = await entity_properties(postgres_engine, ids.e1_id)
        assert "_sofias_memory_importance" in e1_after["properties"]

        result_2 = await service.improve(
            ImproveRequest(
                dataset="reconciliation-order-" + str(ids.dataset_id),
                stages=["graph_reconciliation"],
            )
        )
        assert result_2.graph_entities_importance_updated == 0
    finally:
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(ids.e1_id))
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(ids.e2_id))
        await neo4j_resource.driver.close()
        await cleanup_order_fixture(postgres_engine, ids)
