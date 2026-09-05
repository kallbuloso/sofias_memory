"""Concrete PostgreSQL repositories used by the Unit of Work."""

from sofias_memory.infrastructure.postgres.repositories.chunks import ChunkRepository
from sofias_memory.infrastructure.postgres.repositories.datasets import DatasetRepository
from sofias_memory.infrastructure.postgres.repositories.documents import DocumentRepository
from sofias_memory.infrastructure.postgres.repositories.entities import EntityRepository
from sofias_memory.infrastructure.postgres.repositories.entity_mentions import (
    EntityMentionRepository,
)
from sofias_memory.infrastructure.postgres.repositories.feedback import FeedbackRepository
from sofias_memory.infrastructure.postgres.repositories.graph_outbox import GraphOutboxRepository
from sofias_memory.infrastructure.postgres.repositories.graph_rebuild import GraphRebuildRepository
from sofias_memory.infrastructure.postgres.repositories.pipeline_runs import PipelineRunRepository
from sofias_memory.infrastructure.postgres.repositories.pipeline_steps import PipelineStepRepository
from sofias_memory.infrastructure.postgres.repositories.queries import QueryRepository
from sofias_memory.infrastructure.postgres.repositories.relation_evidence import (
    RelationEvidenceRepository,
)
from sofias_memory.infrastructure.postgres.repositories.relations import RelationRepository
from sofias_memory.infrastructure.postgres.repositories.session_entries import (
    SessionEntryRepository,
)
from sofias_memory.infrastructure.postgres.repositories.sessions import SessionRepository
from sofias_memory.infrastructure.postgres.repositories.sources import SourceRepository
from sofias_memory.infrastructure.postgres.repositories.summaries import SummaryRepository

__all__ = [
    "ChunkRepository",
    "DatasetRepository",
    "DocumentRepository",
    "EntityMentionRepository",
    "EntityRepository",
    "FeedbackRepository",
    "GraphOutboxRepository",
    "GraphRebuildRepository",
    "PipelineRunRepository",
    "PipelineStepRepository",
    "QueryRepository",
    "RelationEvidenceRepository",
    "RelationRepository",
    "SessionEntryRepository",
    "SessionRepository",
    "SourceRepository",
    "SummaryRepository",
]
