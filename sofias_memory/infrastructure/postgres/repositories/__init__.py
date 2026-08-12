"""Concrete PostgreSQL repositories used by the Unit of Work."""

from sofias_memory.infrastructure.postgres.repositories.chunks import ChunkRepository
from sofias_memory.infrastructure.postgres.repositories.datasets import DatasetRepository
from sofias_memory.infrastructure.postgres.repositories.documents import DocumentRepository
from sofias_memory.infrastructure.postgres.repositories.entities import EntityRepository
from sofias_memory.infrastructure.postgres.repositories.entity_mentions import (
    EntityMentionRepository,
)
from sofias_memory.infrastructure.postgres.repositories.graph_outbox import GraphOutboxRepository
from sofias_memory.infrastructure.postgres.repositories.graph_rebuild import GraphRebuildRepository
from sofias_memory.infrastructure.postgres.repositories.pipeline_runs import PipelineRunRepository
from sofias_memory.infrastructure.postgres.repositories.pipeline_steps import PipelineStepRepository
from sofias_memory.infrastructure.postgres.repositories.relation_evidence import (
    RelationEvidenceRepository,
)
from sofias_memory.infrastructure.postgres.repositories.relations import RelationRepository
from sofias_memory.infrastructure.postgres.repositories.sources import SourceRepository

__all__ = [
    "ChunkRepository",
    "DatasetRepository",
    "DocumentRepository",
    "EntityMentionRepository",
    "EntityRepository",
    "GraphOutboxRepository",
    "GraphRebuildRepository",
    "PipelineRunRepository",
    "PipelineStepRepository",
    "RelationEvidenceRepository",
    "RelationRepository",
    "SourceRepository",
]
