"""Typed Cognitive Recall service (SM-1003, ADR-0016, Feature Contract
v0.7.0 Native Cognitive Memory SS 14).

A read-only, single-purpose service, separate from
``services.recall.RecallService`` (legacy knowledge recall) and never
mutating anything Cognitive Memory owns. Reuses the exact same embedding
boundary discipline as ``services.cognitive_memory.CognitiveMemoryService``:
the external query embedding call always happens before any PostgreSQL
session is opened, and the transaction (a single read) is never held open
across it. Never commits -- there is nothing to commit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.memories import MemoryRecallItem, MemoryRecallRequest, MemoryRecallResult
from sofias_memory.services.cognitive_memory import EmbeddingClient, memory_item_result

type UnitOfWorkFactory = Callable[[], PostgresUnitOfWork]


class CognitiveMemoryRecallService:
    """Typed Cognitive Recall: query embedding + exact pgvector cosine +
    temporal/current-truth filtering + deterministic ranking. No Neo4j, no
    ANN, no lexical fallback, no PipelineRun (Feature Contract SS 14)."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def recall(self, request: MemoryRecallRequest) -> MemoryRecallResult:
        query_embedding = await self._embed_query(request.query)

        async with self._unit_of_work_factory() as uow:
            hits = await uow.memory_items.typed_recall(
                memory_types=request.memory_types,
                scopes=request.scopes,
                query_embedding=query_embedding,
                as_of=request.as_of,
                include_superseded=request.include_superseded,
                min_relevance=request.min_relevance,
                limit=request.top_k,
            )
            # Built while the UoW's session is still open: closing it below
            # rolls back (recall never commits) and PostgresUnitOfWork's
            # session expires every loaded ORM instance on rollback, so a
            # MemoryItem/MemoryProvenance attribute touched after this
            # block exits would raise DetachedInstanceError.
            items = [
                MemoryRecallItem(
                    memory=memory_item_result(hit.item, hit.provenance),
                    relevance=hit.relevance,
                    is_current_truth=hit.is_current_truth,
                )
                for hit in hits
            ]

        return MemoryRecallResult(items=items)

    async def _embed_query(self, query: str) -> list[float]:
        try:
            embeddings = await self._embedding_client.embed_texts([query])
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Embedding provider is unavailable.") from exc
        if len(embeddings) != 1 or len(embeddings[0]) != self._settings.embedding_dimensions:
            raise DependencyUnavailableError(
                "Embedding provider returned an unexpected vector dimension."
            )
        return embeddings[0]


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(session_factory)

    return create_unit_of_work
