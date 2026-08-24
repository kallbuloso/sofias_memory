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
from dataclasses import dataclass
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
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.pipelines.context import PipelineContext
from sofias_memory.pipelines.steps.improve import (
    GRAPH_RECONCILIATION_STAGE,
    IMPROVE_RESOURCES_RESOURCE,
    GraphDrainStep,
    GraphMaintainStep,
    GraphReconcileStep,
    ImprovePipelineResources,
)
from sofias_memory.services.graph_maintenance_service import GraphMaintenanceService
from sofias_memory.services.graph_outbox_batch_processor import GraphOutboxBatchProcessor
from sofias_memory.services.graph_outbox_processor import GraphOutboxProcessor
from sofias_memory.services.graph_rebuild_service import GraphRebuildService
from sofias_memory.services.graph_reconciliation_service import GraphReconciliationService
from tests.integration.test_graph_maintenance_postgres_integration import (
    GRAPH_MAINTENANCE_POSTGRES_TEST_DATABASE_NAME,
    graph_maintenance_test_database_url,
    vector_literal,
)

NEO4J_TESTS_ENV = "SOFIAS_MEMORY_RUN_GRAPH_RECONCILIATION_ORDER_NEO4J_TESTS"


class NoOpEmbeddingClient:
    """graph_reconciliation never calls embed_texts; this exists only to
    satisfy :class:`ImprovePipelineResources`'s required field."""

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("embed_texts must not be called for graph_reconciliation")


@dataclass(frozen=True)
class GraphReconciliationStageResult:
    """Just the fields these regression tests assert on, aggregated from the
    three B5 operational steps (SM-511) the same way
    ``pipelines.steps.improve.FinalizeResultStep`` does in production."""

    graph_entities_missing: int
    graph_entities_extra: int
    graph_rebuilt: bool
    graph_relations_deactivated: int
    graph_entities_importance_updated: int


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


def build_improve_resources(
    session_factory,
    neo4j_resource,
) -> tuple[ImprovePipelineResources, Neo4jProjection]:
    projection = Neo4jProjection(neo4j_resource)
    outbox_processor = GraphOutboxProcessor(session_factory=session_factory, projection=projection)
    rebuild_service = GraphRebuildService(
        session_factory=session_factory,
        neo4j_resource=neo4j_resource,
        projection=projection,
    )
    resources = ImprovePipelineResources(
        settings=load_settings(),
        embedding_client=NoOpEmbeddingClient(),
        graph_maintenance=GraphMaintenanceService(session_factory=session_factory),
        summary_rebuild=None,  # type: ignore[arg-type] - unused by graph_reconciliation
        graph_reconciliation=GraphReconciliationService(
            session_factory=session_factory,
            neo4j_resource=neo4j_resource,
            rebuild_service=rebuild_service,
        ),
        graph_outbox_drain=GraphOutboxBatchProcessor(
            session_factory=session_factory,
            processor=outbox_processor,
        ),
    )
    return resources, projection


async def run_graph_reconciliation_stage(
    *,
    session_factory,
    resources: ImprovePipelineResources,
    dataset_id: UUID,
) -> GraphReconciliationStageResult:
    """Drives the three real B5 operational steps (SM-511:
    ``graph_reconcile`` -> ``graph_maintain`` -> ``graph_drain``) exactly the
    way the pipeline engine would, against real PostgreSQL + Neo4j -- the
    frozen GATE-B4 order invariant this file regression-tests."""

    context = PipelineContext(
        run_id=uuid4(),
        pipeline_type=None,  # type: ignore[arg-type] - unused by these steps
        dataset_id=dataset_id,
        source_id=None,
        run_input={"dataset": str(dataset_id), "stages": [GRAPH_RECONCILIATION_STAGE]},
        step_outputs={},
        session_factory=session_factory,
        resources={IMPROVE_RESOURCES_RESOURCE: resources},
    )

    reconcile_result = await GraphReconcileStep().execute(context)

    maintain_result = await GraphMaintainStep().execute(context)
    async with PostgresUnitOfWork(session_factory) as uow:
        await GraphMaintainStep().persist(context, maintain_result, uow)
        await uow.commit()

    await GraphDrainStep().execute(context)

    reconcile_output = reconcile_result.output
    maintain_output = maintain_result.output
    return GraphReconciliationStageResult(
        graph_entities_missing=int(reconcile_output.get("entities_missing", 0)),
        graph_entities_extra=int(reconcile_output.get("entities_extra", 0)),
        graph_rebuilt=bool(reconcile_output.get("rebuilt", False)),
        graph_relations_deactivated=int(maintain_output.get("relations_deactivated", 0)),
        graph_entities_importance_updated=int(
            maintain_output.get("entities_importance_updated", 0)
        ),
    )


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
        resources, projection = build_improve_resources(session_factory, neo4j_resource)

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

        result_1 = await run_graph_reconciliation_stage(
            session_factory=session_factory,
            resources=resources,
            dataset_id=ids.dataset_id,
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

        result_2 = await run_graph_reconciliation_stage(
            session_factory=session_factory,
            resources=resources,
            dataset_id=ids.dataset_id,
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

        resources, projection = build_improve_resources(session_factory, neo4j_resource)
        rebuild_service = GraphRebuildService(
            session_factory=session_factory,
            neo4j_resource=neo4j_resource,
            projection=projection,
        )
        await rebuild_service.rebuild_dataset(ids.dataset_id)

        result = await run_graph_reconciliation_stage(
            session_factory=session_factory,
            resources=resources,
            dataset_id=ids.dataset_id,
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

        result_2 = await run_graph_reconciliation_stage(
            session_factory=session_factory,
            resources=resources,
            dataset_id=ids.dataset_id,
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
        resources, projection = build_improve_resources(session_factory, neo4j_resource)
        rebuild_service = GraphRebuildService(
            session_factory=session_factory,
            neo4j_resource=neo4j_resource,
            projection=projection,
        )
        await rebuild_service.rebuild_dataset(ids.dataset_id)

        result_1 = await run_graph_reconciliation_stage(
            session_factory=session_factory,
            resources=resources,
            dataset_id=ids.dataset_id,
        )
        assert result_1.graph_entities_missing == 0
        assert result_1.graph_entities_extra == 0
        assert result_1.graph_rebuilt is False
        assert result_1.graph_entities_importance_updated == 2

        e1_after = await entity_properties(postgres_engine, ids.e1_id)
        assert "_sofias_memory_importance" in e1_after["properties"]

        result_2 = await run_graph_reconciliation_stage(
            session_factory=session_factory,
            resources=resources,
            dataset_id=ids.dataset_id,
        )
        assert result_2.graph_entities_importance_updated == 0
    finally:
        async with neo4j_resource.driver.session(database=neo4j_resource.database) as session:
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(ids.e1_id))
            await session.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=str(ids.e2_id))
        await neo4j_resource.driver.close()
        await cleanup_order_fixture(postgres_engine, ids)
