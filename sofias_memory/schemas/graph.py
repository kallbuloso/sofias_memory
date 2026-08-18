"""Public request and response schemas for the read-only graph API."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

GRAPH_NODE_LABELS: tuple[str, ...] = ("Entity", "Chunk")
GRAPH_RELATIONSHIP_TYPES: tuple[str, ...] = ("RELATES_TO", "MENTIONED_IN", "NEXT")


class GraphSchemaResult(BaseModel):
    """Authoritative entity types/predicates for one dataset, plus the fixed model."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    dataset: str
    entity_types: list[str]
    relation_predicates: list[str]
    node_labels: list[str] = Field(default_factory=lambda: list(GRAPH_NODE_LABELS))
    relationship_types: list[str] = Field(default_factory=lambda: list(GRAPH_RELATIONSHIP_TYPES))


class GraphEntityNode(BaseModel):
    """Authoritative entity returned by graph traversal endpoints."""

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    name: str
    entity_type: str
    description: str
    importance_weight: float


class GraphRelationEdge(BaseModel):
    """Authoritative relation returned by graph traversal endpoints."""

    model_config = ConfigDict(extra="forbid")

    relation_id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    predicate: str
    description: str
    confidence: float
    importance_weight: float


class GraphSubgraphResult(BaseModel):
    """Bounded, PostgreSQL-hydrated neighborhood around one root entity."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    root_entity_id: UUID
    depth: int
    entities: list[GraphEntityNode]
    relations: list[GraphRelationEdge]
    truncated: bool


class GraphPathResult(BaseModel):
    """Bounded, PostgreSQL-hydrated path between two entities, if one exists."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    from_entity_id: UUID
    to_entity_id: UUID
    max_depth: int
    found: bool
    entities: list[GraphEntityNode]
    relations: list[GraphRelationEdge]
