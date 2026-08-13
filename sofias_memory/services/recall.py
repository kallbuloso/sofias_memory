"""Chunk and RAG recall backed by PostgreSQL retrieval and query auditing."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid4

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.models import Dataset, Query
from sofias_memory.infrastructure.postgres.repositories.chunks import RetrievedChunk
from sofias_memory.infrastructure.postgres.repositories.entities import RecalledEntity
from sofias_memory.infrastructure.postgres.repositories.relation_evidence import (
    RecalledRelationEvidence,
)
from sofias_memory.infrastructure.postgres.repositories.relations import RecalledRelation
from sofias_memory.infrastructure.postgres.repositories.summaries import RecalledDocumentSummary
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.recall import (
    RecallContextItem,
    RecallEntity,
    RecallFilters,
    RecallReference,
    RecallRelation,
    RecallRequest,
    RecallResult,
)

NO_EVIDENCE_ANSWER = "No sufficient evidence was found in memory to answer this query."
REFERENCE_QUOTE_MAX_CHARS = 500


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


class RagAnswerClient(Protocol):
    async def answer(self, query: str, context: str) -> str: ...


class GraphRecallRecord(Protocol):
    @property
    def seed_chunk_id(self) -> UUID: ...

    @property
    def seed_entity_id(self) -> UUID: ...

    @property
    def neighbor_entity_id(self) -> UUID | None: ...

    @property
    def relation_id(self) -> UUID | None: ...


class GraphRecallClient(Protocol):
    async def retrieve(
        self,
        *,
        seed_chunk_ids: Sequence[UUID],
        dataset_ids: Sequence[UUID],
        limit: int,
    ) -> Sequence[GraphRecallRecord]: ...


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


class EntityRepositoryForRecall(Protocol):
    async def list_active_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        entity_ids: list[UUID],
    ) -> list[RecalledEntity]: ...


class RelationRepositoryForRecall(Protocol):
    async def list_active_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        relation_ids: list[UUID],
    ) -> list[RecalledRelation]: ...


class RelationEvidenceRepositoryForRecall(Protocol):
    async def list_active_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        relation_ids: list[UUID],
        filters: RecallFilters,
    ) -> list[RecalledRelationEvidence]: ...


class SummaryRepositoryForRecall(Protocol):
    async def list_active_document_summaries_for_recall(
        self,
        *,
        dataset_ids: list[UUID],
        document_ids: list[UUID],
    ) -> list[RecalledDocumentSummary]: ...


class RecallUnitOfWork(Protocol):
    datasets: DatasetRepositoryForRecall
    chunks: ChunkRepositoryForRecall
    queries: QueryRepositoryForRecall
    entities: EntityRepositoryForRecall
    relations: RelationRepositoryForRecall
    relation_evidence: RelationEvidenceRepositoryForRecall
    summaries: SummaryRepositoryForRecall

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


@dataclass(frozen=True)
class GraphRecallHydration:
    entities: list[RecallEntity]
    relations: list[RecallRelation]
    evidence: list[RecalledRelationEvidence]
    summaries: list[RecalledDocumentSummary]


class RecallService:
    """Run bounded PostgreSQL recall without holding sessions over API calls."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        rag_answer_client: RagAnswerClient,
        graph_recall_client: GraphRecallClient | None = None,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if session_factory is None and unit_of_work_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._rag_answer_client = rag_answer_client
        self._graph_recall_client = graph_recall_client
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

        graph_started_at = time.perf_counter()
        graph_hydration = await self._graph_hydration(request, datasets, hits)
        graph_ms = elapsed_ms(graph_started_at)

        context = [context_item_from_hit(hit) for hit in hits]
        all_references = recall_references(
            hits, graph_hydration.evidence, graph_hydration.relations
        )
        references = all_references if request.include_references else []
        generation_started_at = time.perf_counter()
        answer = await self._answer(request, hits, graph_hydration)
        generation_ms = elapsed_ms(generation_started_at)
        timings_ms = {
            "embedding": embedding_ms,
            "retrieval": retrieval_ms,
            "graph": graph_ms,
            "generation": generation_ms,
            "total": elapsed_ms(started_at),
        }
        query_id = await self._persist_query_audit(
            request,
            datasets=datasets,
            answer=answer,
            references=all_references,
            timings_ms=timings_ms,
        )
        return RecallResult(
            query_id=query_id,
            mode=request.mode,
            answer=answer,
            context=context,
            references=references,
            entities=graph_hydration.entities,
            relations=graph_hydration.relations,
            timings_ms=timings_ms,
        )

    def _validate_supported_mode(self, request: RecallRequest) -> None:
        if request.mode == "summaries":
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

    async def _graph_hydration(
        self,
        request: RecallRequest,
        datasets: list[RecallDatasetSnapshot],
        hits: Sequence[RecallChunkHit],
    ) -> GraphRecallHydration:
        if request.mode not in {"graph", "triplets", "hybrid"}:
            return GraphRecallHydration(entities=[], relations=[], evidence=[], summaries=[])
        if self._graph_recall_client is None:
            raise DependencyUnavailableError("Neo4j graph recall is unavailable.")
        if not hits:
            return GraphRecallHydration(entities=[], relations=[], evidence=[], summaries=[])

        seed_hits = list(hits[: self._settings.recall_graph_seed_top_k])
        seed_score_by_chunk_id = {hit.chunk_id: hit.score for hit in seed_hits}
        try:
            records = await self._graph_recall_client.retrieve(
                seed_chunk_ids=[hit.chunk_id for hit in seed_hits],
                dataset_ids=[dataset.id for dataset in datasets],
                limit=self._settings.recall_graph_max_nodes,
            )
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j graph recall is unavailable.") from exc

        entity_scores, relation_scores = graph_scores(records, seed_score_by_chunk_id)
        relation_ids = ordered_ids_by_score(relation_scores)[: self._effective_top_k(request)]
        entity_ids = ordered_entity_ids_for_recall(entity_scores, relation_ids, records)
        dataset_ids = [dataset.id for dataset in datasets]
        document_ids = unique_ordered(hit.document_id for hit in hits)

        async with self._unit_of_work_factory() as uow:
            entities = await uow.entities.list_active_for_recall(
                dataset_ids=dataset_ids,
                entity_ids=entity_ids,
            )
            relations = await uow.relations.list_active_for_recall(
                dataset_ids=dataset_ids,
                relation_ids=relation_ids,
            )
            evidence = await uow.relation_evidence.list_active_for_recall(
                dataset_ids=dataset_ids,
                relation_ids=relation_ids,
                filters=request.filters,
            )
            summaries = await uow.summaries.list_active_document_summaries_for_recall(
                dataset_ids=dataset_ids,
                document_ids=document_ids,
            )

        evidence_by_relation_id = first_evidence_by_relation_id(evidence)
        recalled_relations = [
            recall_relation_from_snapshot(
                relation,
                score=relation_scores.get(relation.id, 0.0),
                evidence=evidence_by_relation_id.get(relation.id),
            )
            for relation in sort_relations_by_score(relations, relation_scores)
        ]
        relation_entity_ids = {
            entity_id
            for relation in recalled_relations
            for entity_id in (relation.source_entity_id, relation.target_entity_id)
        }
        recalled_entities = [
            recall_entity_from_snapshot(
                entity,
                score=entity_scores.get(
                    entity.id, max_relation_score(entity.id, recalled_relations)
                ),
            )
            for entity in sort_entities_by_score(entities, entity_scores, relation_entity_ids)
            if entity.id in entity_scores or entity.id in relation_entity_ids
        ]
        return GraphRecallHydration(
            entities=recalled_entities,
            relations=recalled_relations,
            evidence=evidence,
            summaries=summaries,
        )

    async def _answer(
        self,
        request: RecallRequest,
        hits: Sequence[RecallChunkHit],
        graph_hydration: GraphRecallHydration,
    ) -> str | None:
        if request.mode in {"chunks", "triplets"} or request.only_context:
            return None
        if (
            request.mode == "graph"
            and not graph_hydration.entities
            and not graph_hydration.relations
        ):
            return NO_EVIDENCE_ANSWER
        if request.mode == "hybrid" and not hits:
            return NO_EVIDENCE_ANSWER
        if request.mode == "rag" and not hits:
            return NO_EVIDENCE_ANSWER
        try:
            return await self._rag_answer_client.answer(
                request.query,
                answer_context(request, hits, graph_hydration),
            )
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
        references: Sequence[RecallReference],
        timings_ms: dict[str, int],
    ) -> UUID:
        query_id = uuid4()
        query = Query(
            id=query_id,
            query_text=request.query if self._settings.store_query_content else None,
            dataset_ids=[dataset.id for dataset in datasets],
            mode=request.mode,
            answer=answer if self._settings.store_query_content else None,
            references=audit_references(references),
            timings=timings_ms,
            model=self._settings.llm_model if generated_with_llm(request, answer) else None,
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


def reference_from_evidence(
    evidence: RecalledRelationEvidence,
    *,
    score: float,
) -> RecallReference:
    return RecallReference(
        source_id=evidence.source_id,
        source_name=evidence.source_name,
        document_id=evidence.document_id,
        chunk_id=evidence.chunk_id,
        chunk_ordinal=evidence.chunk_ordinal,
        quote=reference_quote(evidence.quote),
        start_char=evidence.start_char,
        end_char=evidence.end_char,
        score=score,
        url=evidence.source_url,
    )


def recall_references(
    hits: Sequence[RecallChunkHit],
    evidence: Sequence[RecalledRelationEvidence],
    relations: Sequence[RecallRelation],
) -> list[RecallReference]:
    references_by_chunk_id = {hit.chunk_id: reference_from_hit(hit) for hit in hits}
    relation_scores = {relation.relation_id: relation.score for relation in relations}
    for item in evidence:
        if item.chunk_id not in references_by_chunk_id:
            references_by_chunk_id[item.chunk_id] = reference_from_evidence(
                item,
                score=relation_scores.get(item.relation_id, 0.0),
            )
    return list(references_by_chunk_id.values())


def reference_quote(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= REFERENCE_QUOTE_MAX_CHARS:
        return collapsed
    return f"{collapsed[: REFERENCE_QUOTE_MAX_CHARS - 1].rstrip()}…"


def rag_context(hits: Sequence[RecallChunkHit]) -> str:
    return "\n\n".join(
        f"[chunk:{hit.chunk_id}]\nsource: {hit.source_name}\ntext:\n{hit.text}" for hit in hits
    )


def graph_context(graph_hydration: GraphRecallHydration) -> str:
    sections: list[str] = []
    if graph_hydration.entities:
        sections.append(
            "[ENTITIES]\n"
            + "\n".join(
                (f"Entity: {entity.name} ({entity.entity_type})\nDescription: {entity.description}")
                for entity in graph_hydration.entities
            )
        )
    if graph_hydration.relations:
        sections.append(
            "[RELATIONS/TRIPLETS]\n"
            + "\n".join(
                (
                    f"Relation: {relation.source_entity_id} --{relation.predicate}--> "
                    f"{relation.target_entity_id}\n"
                    f"Description: {relation.description}\n"
                    f"Evidence: {relation.evidence or 'No filtered evidence available.'}"
                )
                for relation in graph_hydration.relations
            )
        )
    return "\n\n".join(sections)


def hybrid_context(
    hits: Sequence[RecallChunkHit],
    graph_hydration: GraphRecallHydration,
) -> str:
    sections = ["[CHUNKS]\n" + rag_context(hits)]
    if graph_hydration.summaries:
        sections.append(
            "[DOCUMENT SUMMARIES]\n"
            + "\n\n".join(
                f"[document:{summary.document_id}]\n{summary.text}"
                for summary in graph_hydration.summaries
            )
        )
    graph_section = graph_context(graph_hydration)
    if graph_section:
        sections.append(f"[GRAPH]\n{graph_section}")
    return "\n\n".join(section for section in sections if section.strip())


def answer_context(
    request: RecallRequest,
    hits: Sequence[RecallChunkHit],
    graph_hydration: GraphRecallHydration,
) -> str:
    if request.mode == "graph":
        return graph_context(graph_hydration)
    if request.mode == "hybrid":
        return hybrid_context(hits, graph_hydration)
    return rag_context(hits)


def audit_references(references: Sequence[RecallReference]) -> dict[str, object]:
    return {
        "items": [
            {
                "source_id": str(reference.source_id),
                "document_id": str(reference.document_id),
                "chunk_id": str(reference.chunk_id),
                "chunk_ordinal": reference.chunk_ordinal,
                "score": reference.score,
            }
            for reference in references
        ]
    }


def elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


def graph_scores(
    records: Sequence[GraphRecallRecord],
    seed_score_by_chunk_id: dict[UUID, float],
) -> tuple[dict[UUID, float], dict[UUID, float]]:
    entity_scores: dict[UUID, float] = {}
    relation_scores: dict[UUID, float] = {}
    for record in records:
        seed_score = seed_score_by_chunk_id.get(record.seed_chunk_id, 0.0)
        entity_scores[record.seed_entity_id] = max(
            entity_scores.get(record.seed_entity_id, 0.0),
            seed_score,
        )
        if record.relation_id is not None:
            relation_scores[record.relation_id] = max(
                relation_scores.get(record.relation_id, 0.0),
                seed_score,
            )
        if record.neighbor_entity_id is not None and record.relation_id is not None:
            entity_scores[record.neighbor_entity_id] = max(
                entity_scores.get(record.neighbor_entity_id, 0.0),
                relation_scores[record.relation_id],
            )
    return entity_scores, relation_scores


def ordered_ids_by_score(scores: dict[UUID, float]) -> list[UUID]:
    return [
        item_id for item_id, _ in sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
    ]


def ordered_entity_ids_for_recall(
    entity_scores: dict[UUID, float],
    relation_ids: Sequence[UUID],
    records: Sequence[GraphRecallRecord],
) -> list[UUID]:
    relation_id_set = set(relation_ids)
    ids = ordered_ids_by_score(entity_scores)
    for record in records:
        if record.relation_id in relation_id_set and record.neighbor_entity_id is not None:
            ids.append(record.neighbor_entity_id)
        if record.relation_id in relation_id_set:
            ids.append(record.seed_entity_id)
    return unique_ordered(ids)


def sort_entities_by_score(
    entities: Sequence[RecalledEntity],
    entity_scores: dict[UUID, float],
    relation_entity_ids: set[UUID],
) -> list[RecalledEntity]:
    return sorted(
        entities,
        key=lambda entity: (
            -entity_scores.get(entity.id, 0.0 if entity.id not in relation_entity_ids else 0.0),
            str(entity.id),
        ),
    )


def sort_relations_by_score(
    relations: Sequence[RecalledRelation],
    relation_scores: dict[UUID, float],
) -> list[RecalledRelation]:
    return sorted(
        relations, key=lambda relation: (-relation_scores.get(relation.id, 0.0), str(relation.id))
    )


def first_evidence_by_relation_id(
    evidence: Sequence[RecalledRelationEvidence],
) -> dict[UUID, RecalledRelationEvidence]:
    first: dict[UUID, RecalledRelationEvidence] = {}
    for item in evidence:
        first.setdefault(item.relation_id, item)
    return first


def recall_entity_from_snapshot(entity: RecalledEntity, *, score: float) -> RecallEntity:
    return RecallEntity(
        entity_id=entity.id,
        name=entity.name,
        entity_type=entity.entity_type,
        description=entity.description,
        score=score,
    )


def recall_relation_from_snapshot(
    relation: RecalledRelation,
    *,
    score: float,
    evidence: RecalledRelationEvidence | None,
) -> RecallRelation:
    return RecallRelation(
        relation_id=relation.id,
        source_entity_id=relation.source_entity_id,
        target_entity_id=relation.target_entity_id,
        predicate=relation.predicate,
        description=relation.description,
        confidence=relation.confidence,
        score=score,
        evidence=evidence.quote if evidence is not None else None,
        evidence_chunk_id=evidence.chunk_id if evidence is not None else None,
    )


def max_relation_score(entity_id: UUID, relations: Sequence[RecallRelation]) -> float:
    scores = [
        relation.score
        for relation in relations
        if relation.source_entity_id == entity_id or relation.target_entity_id == entity_id
    ]
    return max(scores, default=0.0)


def unique_ordered(items: Iterable[UUID]) -> list[UUID]:
    unique: list[UUID] = []
    seen: set[UUID] = set()
    for item in items:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def generated_with_llm(request: RecallRequest, answer: str | None) -> bool:
    return (
        answer is not None
        and answer != NO_EVIDENCE_ANSWER
        and request.mode in {"rag", "graph", "hybrid"}
        and not request.only_context
    )


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> RecallUnitOfWork:
        return cast(RecallUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work
