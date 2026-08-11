"""Rebuild the Neo4j projection from authoritative PostgreSQL state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.infrastructure.neo4j import (
    Neo4jResource,
    delete_all_projection,
    delete_dataset_projection,
    ensure_neo4j_schema,
)
from sofias_memory.infrastructure.postgres.repositories.graph_rebuild import (
    GraphRebuildRepository,
    GraphRebuildSnapshot,
)
from sofias_memory.infrastructure.postgres.session import session_scope
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.ports import GraphProjectionPort

type GraphRebuildScope = Literal["dataset", "global"]
type GraphRebuildRepositoryFactory = Callable[[AsyncSession], GraphRebuildRepository]


@dataclass(frozen=True)
class GraphRebuildResult:
    """Operational summary of one graph rebuild."""

    scope: GraphRebuildScope
    dataset_id: str | None
    datasets: int
    entities: int
    chunks: int
    entity_mentions: int
    relations: int
    next_relationships: int


class GraphRebuildService:
    """Administrative rebuild of the Neo4j projection from PostgreSQL snapshots."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        neo4j_resource: Neo4jResource,
        projection: GraphProjectionPort,
        repository_factory: GraphRebuildRepositoryFactory = GraphRebuildRepository,
    ) -> None:
        self._session_factory = session_factory
        self._neo4j_resource = neo4j_resource
        self._projection = projection
        self._repository_factory = repository_factory

    async def rebuild_dataset(self, dataset_id: UUID | str) -> GraphRebuildResult:
        dataset_uuid = _coerce_uuid(dataset_id)
        snapshot = await self._load_dataset_snapshot(dataset_uuid)
        await ensure_neo4j_schema(self._neo4j_resource)
        await delete_dataset_projection(self._neo4j_resource, str(dataset_uuid))
        await self._apply(snapshot)
        return _result("dataset", str(dataset_uuid), snapshot)

    async def rebuild_all(self) -> GraphRebuildResult:
        snapshot = await self._load_all_snapshot()
        await ensure_neo4j_schema(self._neo4j_resource)
        await delete_all_projection(self._neo4j_resource)
        await self._apply(snapshot)
        return _result("global", None, snapshot)

    async def _load_dataset_snapshot(self, dataset_id: UUID) -> GraphRebuildSnapshot:
        async with session_scope(self._session_factory) as session:
            repository = self._repository_factory(session)
            return await repository.load_dataset(dataset_id)

    async def _load_all_snapshot(self) -> GraphRebuildSnapshot:
        async with session_scope(self._session_factory) as session:
            repository = self._repository_factory(session)
            return await repository.load_all()

    async def _apply(self, snapshot: GraphRebuildSnapshot) -> None:
        for command in snapshot.commands_in_projection_order():
            await self._projection.apply(command)


def _result(
    scope: GraphRebuildScope,
    dataset_id: str | None,
    snapshot: GraphRebuildSnapshot,
) -> GraphRebuildResult:
    return GraphRebuildResult(
        scope=scope,
        dataset_id=dataset_id,
        datasets=len(snapshot.dataset_ids),
        entities=len(snapshot.entity_commands),
        chunks=len(snapshot.chunk_commands),
        entity_mentions=len(snapshot.entity_mention_commands),
        relations=len(snapshot.relation_commands),
        next_relationships=len(snapshot.next_commands),
    )


def _coerce_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(value)
