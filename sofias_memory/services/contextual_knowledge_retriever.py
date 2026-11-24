"""Contextual Knowledge retrieval for Contextual Recall (SM-CTX-007, handoff 15
SS 44-72/109/114-118/130-136).

A deliberately narrow boundary -- not a Recall framework. It turns *explicitly
selected* ACTIVE datasets plus the request query (and the request's single
query embedding, when it exists) into bounded, structured Knowledge references
over the existing PostgreSQL primitives:

- ``ChunkRepository.vector_search`` / ``lexical_search`` (authoritative
  eligibility: ACTIVE dataset, ACTIVE Source, active Document/Chunk of the
  dataset's active generation -- a forgotten or inactive chunk is never
  returned even if its embedding row still exists);
- ``reciprocal_rank_fusion`` *inside the Knowledge collection only* (never
  across Memory/Episode/Checkpoint/Knowledge).

It never calls ``RecallService`` or ``/api/v1/recall`` (no Query audit, no
Session admission, no graph, no RAG/LLM), never creates or infers a Dataset
(no implicit ``main``, no project-scope mapping), never embeds anything itself
and never returns chunk text. It runs inside the caller's read-only snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from uuid import UUID

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.recall import RecallFilters
from sofias_memory.services.recall import reciprocal_rank_fusion

KNOWLEDGE_CANDIDATE_FACTOR = 3
KNOWLEDGE_CANDIDATE_MAX = 150
"""Each retrieval channel reads a bounded candidate window (``limit * 3``, at
most 150) before the local rank fusion keeps ``limit``. Never unbounded."""


@dataclass(frozen=True, slots=True)
class DatasetSelection:
    """One explicitly requested dataset, validated ACTIVE in the snapshot."""

    dataset_id: UUID
    slug: str


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    """A retrieved chunk identity with its local (Knowledge-only) rank/score."""

    dataset_id: UUID
    source_id: UUID
    document_id: UUID
    chunk_id: UUID
    rank: int
    relevance: float


def dataset_unavailable_error(slug: str) -> SofiasMemoryError:
    """Explicit dataset selection that cannot be honoured: 422 INVALID_REQUEST
    with the requested slug only (no new error code, never ignored, never a
    fallback to another dataset)."""

    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        message="A requested dataset does not exist or is not active.",
        details={"field": "datasets", "dataset": slug},
    )


class ContextualKnowledgeRetriever:
    """Explicit ACTIVE datasets + query (+ shared embedding) -> bounded hits."""

    def __init__(self, *, rrf_k: int) -> None:
        self._rrf_k = rrf_k

    async def resolve_datasets(
        self, uow: PostgresUnitOfWork, slugs: Sequence[str]
    ) -> list[DatasetSelection]:
        """Every slug must exist and be ACTIVE in this snapshot (read-only;
        never creates). Request order is preserved."""

        selected: list[DatasetSelection] = []
        for slug in slugs:
            dataset = await uow.datasets.get_by_slug(slug)
            if dataset is None or dataset.status is not DatasetStatus.ACTIVE:
                raise dataset_unavailable_error(slug)
            selected.append(DatasetSelection(dataset_id=dataset.id, slug=dataset.slug))
        return selected

    async def retrieve(
        self,
        uow: PostgresUnitOfWork,
        *,
        datasets: Sequence[DatasetSelection],
        query: str,
        query_embedding: Sequence[float] | None,
        limit: int,
    ) -> list[KnowledgeHit]:
        """Vector + lexical retrieval fused locally; lexical only when the
        request has no usable query embedding (the caller signals that
        degradation). Without datasets or query nothing is searched -- never
        "recent" or arbitrary chunks."""

        if not datasets or not query or limit <= 0:
            return []
        dataset_ids = [item.dataset_id for item in datasets]
        window = min(limit * KNOWLEDGE_CANDIDATE_FACTOR, KNOWLEDGE_CANDIDATE_MAX)
        filters = RecallFilters()
        vector = (
            []
            if query_embedding is None
            else await uow.chunks.vector_search(
                dataset_ids=dataset_ids,
                query_embedding=list(query_embedding),
                limit=window,
                filters=filters,
            )
        )
        lexical = await uow.chunks.lexical_search(
            dataset_ids=dataset_ids, query_text=query, limit=window, filters=filters
        )
        # Already deterministic: score DESC, best channel rank ASC, chunk id ASC.
        ordered = reciprocal_rank_fusion(vector, lexical, rrf_k=self._rrf_k)[:limit]
        return [
            KnowledgeHit(
                dataset_id=hit.dataset_id,
                source_id=hit.source_id,
                document_id=hit.document_id,
                chunk_id=hit.chunk_id,
                rank=position,
                relevance=hit.score,
            )
            for position, hit in enumerate(ordered, start=1)
        ]


__all__ = [
    "KNOWLEDGE_CANDIDATE_FACTOR",
    "KNOWLEDGE_CANDIDATE_MAX",
    "ContextualKnowledgeRetriever",
    "DatasetSelection",
    "KnowledgeHit",
    "dataset_unavailable_error",
]
