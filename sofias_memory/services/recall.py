"""Chunk and RAG recall backed by PostgreSQL retrieval and query auditing."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import Dataset, Query
from sofias_memory.infrastructure.postgres.repositories.chunks import RetrievedChunk
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.recall import (
    RecallContextItem,
    RecallReference,
    RecallRequest,
    RecallResult,
)

NO_EVIDENCE_ANSWER = "No sufficient evidence was found in memory to answer this query."
REFERENCE_QUOTE_MAX_CHARS = 500


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


class RagAnswerClient(Protocol):
    async def answer(self, query: str, context: str) -> str: ...


class DatasetRepositoryForRecall(Protocol):
    async def get_by_slug(self, slug: str) -> Dataset | None: ...


class ChunkRepositoryForRecall(Protocol):
    async def vector_search(
        self,
        *,
        dataset_ids: list[UUID],
        query_embedding: list[float],
        limit: int,
        filters: object,
    ) -> list[RetrievedChunk]: ...

    async def lexical_search(
        self,
        *,
        dataset_ids: list[UUID],
        query_text: str,
        limit: int,
        filters: object,
    ) -> list[RetrievedChunk]: ...


class QueryRepositoryForRecall(Protocol):
    async def add(self, query: Query) -> Query: ...


class RecallUnitOfWork(Protocol):
    datasets: DatasetRepositoryForRecall
    chunks: ChunkRepositoryForRecall
    queries: QueryRepositoryForRecall

    async def __aenter__(self) -> RecallUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], RecallUnitOfWork]


@dataclass(frozen=True)
class RecallDatasetSnapshot:
    id: UUID
    slug: str


@dataclass(frozen=True)
class RecallChunkHit:
    chunk_id: UUID
    dataset_id: UUID
    source_id: UUID
    source_name: str
    source_url: str | None
    document_id: UUID
    ordinal: int
    text: str
    start_char: int
    end_char: int
    vector_rank: int | None
    lexical_rank: int | None
    score: float


class RecallService:
    """Run bounded PostgreSQL recall without holding sessions over API calls."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        rag_answer_client: RagAnswerClient,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._rag_answer_client = rag_answer_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def recall(self, request: RecallRequest) -> RecallResult:
        self._validate_supported_mode(request)
        started_at = time.perf_counter()
        datasets = await self._resolve_datasets(request.datasets)

        embedding_started_at = time.perf_counter()
        query_embedding = await self._embed_query(request.query)
        embedding_ms = elapsed_ms(embedding_started_at)

        retrieval_started_at = time.perf_counter()
        vector_results, lexical_results = await asyncio.gather(
            self._vector_search(datasets, query_embedding, request),
            self._lexical_search(datasets, request),
        )
        hits = reciprocal_rank_fusion(
            vector_results,
            lexical_results,
            rrf_k=self._settings.recall_rrf_k,
        )[: self._effective_top_k(request)]
        retrieval_ms = elapsed_ms(retrieval_started_at)

        context = [context_item_from_hit(hit) for hit in hits]
        references = [reference_from_hit(hit) for hit in hits] if request.include_references else []
        generation_started_at = time.perf_counter()
        answer = await self._answer(request, hits)
        generation_ms = elapsed_ms(generation_started_at)
        timings_ms = {
            "embedding": embedding_ms,
            "retrieval": retrieval_ms,
            "graph": 0,
            "generation": generation_ms,
            "total": elapsed_ms(started_at),
        }
        query_id = await self._persist_query_audit(
            request,
            datasets=datasets,
            answer=answer,
            hits=hits,
            timings_ms=timings_ms,
        )
        return RecallResult(
            query_id=query_id,
            mode=request.mode,
            answer=answer,
            context=context,
            references=references,
            timings_ms=timings_ms,
        )

    def _validate_supported_mode(self, request: RecallRequest) -> None:
        if request.mode not in {"chunks", "rag"}:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message=f"Recall mode '{request.mode}' is not available in this checkpoint.",
                details={"mode": request.mode},
            )
        if request.top_k is not None and request.top_k > self._settings.recall_max_top_k:
            raise SofiasMemoryError(
                code=ErrorCode.INVALID_REQUEST,
                status_code=HTTPStatus.BAD_REQUEST,
                message="Recall top_k exceeds the configured maximum.",
                details={"top_k": request.top_k},
            )

    async def _resolve_datasets(self, slugs: list[str]) -> list[RecallDatasetSnapshot]:
        async with self._unit_of_work_factory() as uow:
            datasets: list[RecallDatasetSnapshot] = []
            for slug in slugs:
                dataset = await uow.datasets.get_by_slug(slug)
                if dataset is None or dataset.status != DatasetStatus.ACTIVE:
                    raise SofiasMemoryError(
                        code=ErrorCode.INVALID_REQUEST,
                        status_code=HTTPStatus.NOT_FOUND,
                        message="Dataset does not exist.",
                        details={"dataset": slug},
                    )
                datasets.append(RecallDatasetSnapshot(id=dataset.id, slug=dataset.slug))
            return datasets

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

    async def _vector_search(
        self,
        datasets: list[RecallDatasetSnapshot],
        query_embedding: list[float],
        request: RecallRequest,
    ) -> list[RetrievedChunk]:
        async with self._unit_of_work_factory() as uow:
            return await uow.chunks.vector_search(
                dataset_ids=[dataset.id for dataset in datasets],
                query_embedding=query_embedding,
                limit=self._settings.recall_vector_top_k,
                filters=request.filters,
            )

    async def _lexical_search(
        self,
        datasets: list[RecallDatasetSnapshot],
        request: RecallRequest,
    ) -> list[RetrievedChunk]:
        async with self._unit_of_work_factory() as uow:
            return await uow.chunks.lexical_search(
                dataset_ids=[dataset.id for dataset in datasets],
                query_text=request.query,
                limit=self._settings.recall_lexical_top_k,
                filters=request.filters,
            )

    async def _answer(self, request: RecallRequest, hits: Sequence[RecallChunkHit]) -> str | None:
        if request.mode == "chunks" or request.only_context:
            return None
        if not hits:
            return NO_EVIDENCE_ANSWER
        try:
            return await self._rag_answer_client.answer(request.query, rag_context(hits))
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("RAG generation is unavailable.") from exc

    async def _persist_query_audit(
        self,
        request: RecallRequest,
        *,
        datasets: list[RecallDatasetSnapshot],
        answer: str | None,
        hits: Sequence[RecallChunkHit],
        timings_ms: dict[str, int],
    ) -> UUID:
        query_id = uuid4()
        query = Query(
            id=query_id,
            query_text=request.query if self._settings.store_query_content else None,
            dataset_ids=[dataset.id for dataset in datasets],
            mode=request.mode,
            answer=answer if self._settings.store_query_content else None,
            references=audit_references(hits),
            timings=timings_ms,
            model=self._settings.llm_model if request.mode == "rag" and hits and answer else None,
        )
        async with self._unit_of_work_factory() as uow:
            await uow.queries.add(query)
            await uow.commit()
        return query_id

    def _effective_top_k(self, request: RecallRequest) -> int:
        return request.top_k if request.top_k is not None else self._settings.recall_default_top_k


def reciprocal_rank_fusion(
    vector_results: Sequence[RetrievedChunk],
    lexical_results: Sequence[RetrievedChunk],
    *,
    rrf_k: int,
) -> list[RecallChunkHit]:
    """Fuse ranked retrieval channels without comparing their raw score scales."""

    by_chunk_id: dict[UUID, tuple[RetrievedChunk, int | None, int | None]] = {}
    for rank, result in enumerate(vector_results, start=1):
        existing = by_chunk_id.get(result.chunk_id)
        by_chunk_id[result.chunk_id] = (
            result,
            rank,
            existing[2] if existing is not None else None,
        )
    for rank, result in enumerate(lexical_results, start=1):
        existing = by_chunk_id.get(result.chunk_id)
        by_chunk_id[result.chunk_id] = (
            result if existing is None else existing[0],
            existing[1] if existing is not None else None,
            rank,
        )

    hits = [
        RecallChunkHit(
            chunk_id=result.chunk_id,
            dataset_id=result.dataset_id,
            source_id=result.source_id,
            source_name=result.source_name,
            source_url=result.source_url,
            document_id=result.document_id,
            ordinal=result.ordinal,
            text=result.text,
            start_char=result.start_char,
            end_char=result.end_char,
            vector_rank=vector_rank,
            lexical_rank=lexical_rank,
            score=sum(1 / (rrf_k + rank) for rank in (vector_rank, lexical_rank) if rank),
        )
        for result, vector_rank, lexical_rank in by_chunk_id.values()
    ]
    return sorted(
        hits,
        key=lambda hit: (
            -hit.score,
            min(rank for rank in (hit.vector_rank, hit.lexical_rank) if rank is not None),
            str(hit.chunk_id),
        ),
    )


def context_item_from_hit(hit: RecallChunkHit) -> RecallContextItem:
    return RecallContextItem(
        chunk_id=hit.chunk_id,
        dataset_id=hit.dataset_id,
        source_id=hit.source_id,
        source_name=hit.source_name,
        document_id=hit.document_id,
        chunk_ordinal=hit.ordinal,
        text=hit.text,
        start_char=hit.start_char,
        end_char=hit.end_char,
        score=hit.score,
    )


def reference_from_hit(hit: RecallChunkHit) -> RecallReference:
    return RecallReference(
        source_id=hit.source_id,
        source_name=hit.source_name,
        document_id=hit.document_id,
        chunk_id=hit.chunk_id,
        chunk_ordinal=hit.ordinal,
        quote=reference_quote(hit.text),
        start_char=hit.start_char,
        end_char=hit.end_char,
        score=hit.score,
        url=hit.source_url,
    )


def reference_quote(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= REFERENCE_QUOTE_MAX_CHARS:
        return collapsed
    return f"{collapsed[: REFERENCE_QUOTE_MAX_CHARS - 1].rstrip()}…"


def rag_context(hits: Sequence[RecallChunkHit]) -> str:
    return "\n\n".join(
        f"[chunk:{hit.chunk_id}]\nsource: {hit.source_name}\ntext:\n{hit.text}" for hit in hits
    )


def audit_references(hits: Sequence[RecallChunkHit]) -> dict[str, object]:
    return {
        "items": [
            {
                "source_id": str(hit.source_id),
                "document_id": str(hit.document_id),
                "chunk_id": str(hit.chunk_id),
                "chunk_ordinal": hit.ordinal,
                "score": hit.score,
            }
            for hit in hits
        ]
    }


def elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> RecallUnitOfWork:
        return cast(RecallUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
