"""PostgreSQL snapshot loader for Neo4j projection rebuilds."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Entity,
    EntityMention,
    Relation,
)
from sofias_memory.ports import GRAPH_PROJECTION_SCHEMA_VERSION, ProjectionCommand


@dataclass(frozen=True)
class DatasetRebuildRow:
    id: str
    status: DatasetStatus
    active_generation: int


@dataclass(frozen=True)
class EntityRebuildRow:
    id: str
    dataset_id: str
    generation: int
    name: str
    entity_type: str
    description: str
    importance_weight: float
    is_active: bool


@dataclass(frozen=True)
class ChunkRebuildRow:
    id: str
    dataset_id: str
    source_id: str
    document_id: str
    generation: int
    ordinal: int
    is_active: bool


@dataclass(frozen=True)
class EntityMentionRebuildRow:
    id: str
    entity_id: str
    chunk_id: str
    confidence: float


@dataclass(frozen=True)
class RelationRebuildRow:
    id: str
    dataset_id: str
    generation: int
    source_entity_id: str
    target_entity_id: str
    predicate: str
    description: str
    confidence: float
    importance_weight: float
    is_active: bool


@dataclass(frozen=True)
class GraphRebuildSnapshot:
    """Detached PostgreSQL state converted to ordered projection commands."""

    dataset_ids: tuple[str, ...]
    entity_commands: tuple[ProjectionCommand, ...]
    chunk_commands: tuple[ProjectionCommand, ...]
    entity_mention_commands: tuple[ProjectionCommand, ...]
    relation_commands: tuple[ProjectionCommand, ...]
    next_commands: tuple[ProjectionCommand, ...]

    def commands_in_projection_order(self) -> tuple[ProjectionCommand, ...]:
        """Return commands in ADR-0008 rebuild order."""

        return (
            *self.entity_commands,
            *self.chunk_commands,
            *self.entity_mention_commands,
            *self.relation_commands,
            *self.next_commands,
        )


class GraphRebuildRepository:
    """Load rebuildable graph state from PostgreSQL without reading graph_outbox."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_dataset(self, dataset_id: UUID) -> GraphRebuildSnapshot:
        dataset = await self._session.scalar(select(Dataset).where(Dataset.id == dataset_id))
        if dataset is None:
            return empty_graph_rebuild_snapshot()
        return await self._load_for_datasets((_dataset_row(dataset),))

    async def load_all(self) -> GraphRebuildSnapshot:
        datasets = await self._session.scalars(select(Dataset).order_by(Dataset.id))
        return await self._load_for_datasets(tuple(_dataset_row(dataset) for dataset in datasets))

    async def _load_for_datasets(
        self,
        datasets: tuple[DatasetRebuildRow, ...],
    ) -> GraphRebuildSnapshot:
        active_dataset_ids = tuple(
            UUID(dataset.id) for dataset in datasets if dataset.status == DatasetStatus.ACTIVE
        )
        if not active_dataset_ids:
            return build_graph_rebuild_snapshot(
                datasets=datasets,
                entities=(),
                chunks=(),
                mentions=(),
                relations=(),
            )

        entities = await self._session.scalars(
            select(Entity).where(Entity.dataset_id.in_(active_dataset_ids)).order_by(Entity.id)
        )
        chunks = await self._session.scalars(
            select(Chunk)
            .where(Chunk.dataset_id.in_(active_dataset_ids))
            .order_by(
                Chunk.dataset_id,
                Chunk.document_id,
                Chunk.generation,
                Chunk.ordinal,
                Chunk.id,
            )
        )
        relations = await self._session.scalars(
            select(Relation)
            .where(Relation.dataset_id.in_(active_dataset_ids))
            .order_by(Relation.id)
        )

        entity_rows = tuple(_entity_row(entity) for entity in entities)
        chunk_rows = tuple(_chunk_row(chunk) for chunk in chunks)
        entity_ids = [UUID(entity.id) for entity in entity_rows]
        if entity_ids:
            mentions = await self._session.scalars(
                select(EntityMention)
                .where(EntityMention.entity_id.in_(entity_ids))
                .order_by(EntityMention.id)
            )
            mention_rows = tuple(_entity_mention_row(mention) for mention in mentions)
        else:
            mention_rows = ()

        return build_graph_rebuild_snapshot(
            datasets=datasets,
            entities=entity_rows,
            chunks=chunk_rows,
            mentions=mention_rows,
            relations=tuple(_relation_row(relation) for relation in relations),
        )


def empty_graph_rebuild_snapshot() -> GraphRebuildSnapshot:
    return GraphRebuildSnapshot(
        dataset_ids=(),
        entity_commands=(),
        chunk_commands=(),
        entity_mention_commands=(),
        relation_commands=(),
        next_commands=(),
    )


def build_graph_rebuild_snapshot(
    *,
    datasets: tuple[DatasetRebuildRow, ...],
    entities: tuple[EntityRebuildRow, ...],
    chunks: tuple[ChunkRebuildRow, ...],
    mentions: tuple[EntityMentionRebuildRow, ...],
    relations: tuple[RelationRebuildRow, ...],
) -> GraphRebuildSnapshot:
    """Filter authoritative rows and create projection commands."""

    active_generations = {
        dataset.id: dataset.active_generation
        for dataset in datasets
        if dataset.status == DatasetStatus.ACTIVE
    }
    projectable_entities = tuple(
        entity
        for entity in entities
        if entity.dataset_id in active_generations
        and entity.generation == active_generations[entity.dataset_id]
        and entity.is_active
    )
    projectable_chunks = tuple(
        chunk
        for chunk in chunks
        if chunk.dataset_id in active_generations
        and chunk.generation == active_generations[chunk.dataset_id]
        and chunk.is_active
    )

    entity_dataset_by_id = {entity.id: entity.dataset_id for entity in projectable_entities}
    chunk_dataset_by_id = {chunk.id: chunk.dataset_id for chunk in projectable_chunks}

    projectable_mentions = tuple(
        mention
        for mention in mentions
        if mention.entity_id in entity_dataset_by_id
        and mention.chunk_id in chunk_dataset_by_id
        and entity_dataset_by_id[mention.entity_id] == chunk_dataset_by_id[mention.chunk_id]
    )
    projectable_relations = tuple(
        relation
        for relation in relations
        if relation.dataset_id in active_generations
        and relation.generation == active_generations[relation.dataset_id]
        and relation.is_active
        and entity_dataset_by_id.get(relation.source_entity_id) == relation.dataset_id
        and entity_dataset_by_id.get(relation.target_entity_id) == relation.dataset_id
    )

    return GraphRebuildSnapshot(
        dataset_ids=tuple(sorted(active_generations)),
        entity_commands=tuple(_entity_command(entity) for entity in projectable_entities),
        chunk_commands=tuple(_chunk_command(chunk) for chunk in projectable_chunks),
        entity_mention_commands=tuple(
            _entity_mention_command(mention, entity_dataset_by_id[mention.entity_id])
            for mention in projectable_mentions
        ),
        relation_commands=tuple(_relation_command(relation) for relation in projectable_relations),
        next_commands=_next_commands(projectable_chunks),
    )


def _next_commands(chunks: tuple[ChunkRebuildRow, ...]) -> tuple[ProjectionCommand, ...]:
    groups: dict[tuple[str, str, int], list[ChunkRebuildRow]] = defaultdict(list)
    for chunk in chunks:
        groups[(chunk.dataset_id, chunk.document_id, chunk.generation)].append(chunk)

    commands: list[ProjectionCommand] = []
    for (_dataset_id, _document_id, _generation), group_chunks in sorted(groups.items()):
        ordered_chunks = sorted(group_chunks, key=lambda chunk: (chunk.ordinal, chunk.id))
        for from_chunk, to_chunk in zip(ordered_chunks, ordered_chunks[1:], strict=False):
            if to_chunk.ordinal != from_chunk.ordinal + 1:
                continue
            commands.append(_chunk_next_command(from_chunk, to_chunk))
    return tuple(commands)


def _entity_command(entity: EntityRebuildRow) -> ProjectionCommand:
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="entity",
        operation="upsert",
        dataset_id=entity.dataset_id,
        aggregate_id=entity.id,
        identity={"id": entity.id},
        endpoints={},
        properties={
            "id": entity.id,
            "dataset_id": entity.dataset_id,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "description": entity.description,
            "importance_weight": entity.importance_weight,
            "generation": entity.generation,
        },
    )


def _chunk_command(chunk: ChunkRebuildRow) -> ProjectionCommand:
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="chunk",
        operation="upsert",
        dataset_id=chunk.dataset_id,
        aggregate_id=chunk.id,
        identity={"id": chunk.id},
        endpoints={},
        properties={
            "id": chunk.id,
            "dataset_id": chunk.dataset_id,
            "source_id": chunk.source_id,
            "document_id": chunk.document_id,
            "ordinal": chunk.ordinal,
            "generation": chunk.generation,
        },
    )


def _entity_mention_command(
    mention: EntityMentionRebuildRow,
    dataset_id: str,
) -> ProjectionCommand:
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="entity_mention",
        operation="upsert",
        dataset_id=dataset_id,
        aggregate_id=mention.id,
        identity={"mention_id": mention.id},
        endpoints={"entity_id": mention.entity_id, "chunk_id": mention.chunk_id},
        properties={"mention_id": mention.id, "confidence": mention.confidence},
    )


def _relation_command(relation: RelationRebuildRow) -> ProjectionCommand:
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="relation",
        operation="upsert",
        dataset_id=relation.dataset_id,
        aggregate_id=relation.id,
        identity={"relation_id": relation.id},
        endpoints={
            "source_entity_id": relation.source_entity_id,
            "target_entity_id": relation.target_entity_id,
        },
        properties={
            "relation_id": relation.id,
            "predicate": relation.predicate,
            "description": relation.description,
            "confidence": relation.confidence,
            "importance_weight": relation.importance_weight,
            "generation": relation.generation,
        },
    )


def _chunk_next_command(
    from_chunk: ChunkRebuildRow,
    to_chunk: ChunkRebuildRow,
) -> ProjectionCommand:
    return ProjectionCommand(
        schema_version=GRAPH_PROJECTION_SCHEMA_VERSION,
        aggregate_type="chunk_next",
        operation="upsert",
        dataset_id=from_chunk.dataset_id,
        aggregate_id=from_chunk.id,
        identity={"from_chunk_id": from_chunk.id, "to_chunk_id": to_chunk.id},
        endpoints={"from_chunk_id": from_chunk.id, "to_chunk_id": to_chunk.id},
        properties={},
    )


def _dataset_row(dataset: Dataset) -> DatasetRebuildRow:
    return DatasetRebuildRow(
        id=str(dataset.id),
        status=dataset.status,
        active_generation=dataset.active_generation,
    )


def _entity_row(entity: Entity) -> EntityRebuildRow:
    return EntityRebuildRow(
        id=str(entity.id),
        dataset_id=str(entity.dataset_id),
        generation=entity.generation,
        name=entity.name,
        entity_type=entity.entity_type,
        description=entity.description,
        importance_weight=float(entity.importance_weight),
        is_active=entity.is_active,
    )


def _chunk_row(chunk: Chunk) -> ChunkRebuildRow:
    return ChunkRebuildRow(
        id=str(chunk.id),
        dataset_id=str(chunk.dataset_id),
        source_id=str(chunk.source_id),
        document_id=str(chunk.document_id),
        generation=chunk.generation,
        ordinal=chunk.ordinal,
        is_active=chunk.is_active,
    )


def _entity_mention_row(mention: EntityMention) -> EntityMentionRebuildRow:
    return EntityMentionRebuildRow(
        id=str(mention.id),
        entity_id=str(mention.entity_id),
        chunk_id=str(mention.chunk_id),
        confidence=float(mention.confidence),
    )


def _relation_row(relation: Relation) -> RelationRebuildRow:
    return RelationRebuildRow(
        id=str(relation.id),
        dataset_id=str(relation.dataset_id),
        generation=relation.generation,
        source_entity_id=str(relation.source_entity_id),
        target_entity_id=str(relation.target_entity_id),
        predicate=relation.predicate,
        description=relation.description,
        confidence=float(relation.confidence),
        importance_weight=float(relation.importance_weight),
        is_active=relation.is_active,
    )
