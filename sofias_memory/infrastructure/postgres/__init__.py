"""PostgreSQL async infrastructure primitives."""

from sofias_memory.infrastructure.postgres.base import NAMING_CONVENTION, Base
from sofias_memory.infrastructure.postgres.engine import (
    create_async_engine_from_settings,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    EntityMention,
    Feedback,
    GraphOutbox,
    MemoryEntry,
    PipelineRun,
    PipelineStep,
    Query,
    Relation,
    RelationEvidence,
    Source,
    Summary,
)
from sofias_memory.infrastructure.postgres.session import (
    create_session_factory,
    session_scope,
    transaction_scope,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork

__all__ = [
    "AsyncSessionFactory",
    "Base",
    "Chunk",
    "Dataset",
    "Document",
    "Entity",
    "EntityMention",
    "Feedback",
    "GraphOutbox",
    "MemoryEntry",
    "NAMING_CONVENTION",
    "PipelineRun",
    "PipelineStep",
    "PostgresUnitOfWork",
    "Query",
    "Relation",
    "RelationEvidence",
    "Source",
    "Summary",
    "create_async_engine_from_settings",
    "create_session_factory",
    "dispose_async_engine",
    "session_scope",
    "transaction_scope",
]
