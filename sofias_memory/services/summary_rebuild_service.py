"""Improve summaries stage for persisted document and dataset summaries."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, cast
from uuid import UUID, uuid5

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.config import DEFAULT_PROMPT_VERSIONS, Settings
from sofias_memory.domain import DatasetStatus, SummaryTargetType
from sofias_memory.infrastructure.postgres.models import Dataset, Document, Summary
from sofias_memory.infrastructure.postgres.repositories.chunks import ChunkSummaryRebuildSnapshot
from sofias_memory.infrastructure.postgres.repositories.documents import (
    DocumentSummaryRebuildCandidate,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.cognify import (
    DOCUMENT_SUMMARY_PROMPT_VERSION,
    DOCUMENT_SUMMARY_VERSION,
    GRAPH_EXTRACTION_PROMPT_VERSION,
    KNOWLEDGE_EXTRACTION_VERSION,
    document_summary_id,
    document_summary_metadata,
)

DATASET_SUMMARY_VERSION = "v1"
DATASET_SUMMARY_PROMPT_VERSION = DEFAULT_PROMPT_VERSIONS["dataset_summary"]


class EmbeddingClient(Protocol):
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


class DocumentSummaryClient(Protocol):
    async def summarize(self, chunk_summaries: Sequence[str]) -> str: ...


class DatasetSummaryClient(Protocol):
    async def summarize(self, document_summaries: Sequence[str]) -> str: ...


class DatasetRepositoryForSummaryRebuild(Protocol):
    async def get_by_id(self, dataset_id: UUID) -> Dataset | None: ...


class DocumentRepositoryForSummaryRebuild(Protocol):
    async def list_active_for_summary_rebuild(
        self,
        *,
        dataset_id: UUID,
        generation: int,
    ) -> list[DocumentSummaryRebuildCandidate]: ...

    async def get_active_for_summary_rebuild(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        document_id: UUID,
    ) -> Document | None: ...


class ChunkRepositoryForSummaryRebuild(Protocol):
    async def list_active_for_document_summary(
        self,
        *,
        document_id: UUID,
        generation: int,
    ) -> list[ChunkSummaryRebuildSnapshot]: ...


class SummaryRepositoryForSummaryRebuild(Protocol):
    async def add(self, summary: Summary) -> Summary: ...
    async def get_by_id(self, summary_id: UUID) -> Summary | None: ...

    async def list_active_for_target(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        target_type: SummaryTargetType,
        target_id: UUID,
        level: int,
    ) -> list[Summary]: ...

    async def deactivate_active_for_target_except(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        target_type: SummaryTargetType,
        target_id: UUID,
        level: int,
        keep_summary_id: UUID | None,
    ) -> int: ...


class SummaryRebuildUnitOfWork(Protocol):
    datasets: DatasetRepositoryForSummaryRebuild
    documents: DocumentRepositoryForSummaryRebuild
    chunks: ChunkRepositoryForSummaryRebuild
    summaries: SummaryRepositoryForSummaryRebuild

    async def __aenter__(self) -> SummaryRebuildUnitOfWork: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], SummaryRebuildUnitOfWork]


@dataclass(frozen=True)
class SummaryRebuildCounts:
    document_summaries_rebuilt: int
    dataset_summaries_rebuilt: int
    summaries_deactivated: int


@dataclass(frozen=True)
class SummaryRebuildDatasetSnapshot:
    id: UUID
    generation: int


@dataclass(frozen=True)
class DocumentSummaryPlan:
    document: DocumentSummaryRebuildCandidate
    expected_summary_id: UUID
    current_text: str | None
    chunk_summaries: tuple[str, ...]
    rebuild: bool


@dataclass(frozen=True)
class PreparedDocumentSummary:
    document_id: UUID
    summary_id: UUID
    text: str
    embedding: list[float]
    rebuilt: bool


@dataclass(frozen=True)
class PreparedDatasetSummary:
    summary_id: UUID
    text: str
    embedding: list[float]


@dataclass(frozen=True)
class PreparedSummaryRebuild:
    dataset: SummaryRebuildDatasetSnapshot
    documents: tuple[PreparedDocumentSummary, ...]
    dataset_summary: PreparedDatasetSummary | None
    deactivate_dataset_summary: bool


class SummaryRebuildService:
    """Rebuild supported persisted summaries for one active dataset."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient,
        document_summary_client: DocumentSummaryClient,
        dataset_summary_client: DatasetSummaryClient,
        session_factory: AsyncSessionFactory | None = None,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if unit_of_work_factory is None and session_factory is None:
            raise ValueError("session_factory or unit_of_work_factory is required")
        self._settings = settings
        self._embedding_client = embedding_client
        self._document_summary_client = document_summary_client
        self._dataset_summary_client = dataset_summary_client
        self._unit_of_work_factory = unit_of_work_factory or _postgres_unit_of_work_factory(
            cast(AsyncSessionFactory, session_factory)
        )

    async def rebuild_dataset(self, dataset_id: UUID, *, generation: int) -> SummaryRebuildCounts:
        """Convenience wrapper: prepare then apply in one owned transaction.
        The B5 Improve pipeline step never calls this -- it calls
        :meth:`prepare_dataset` from ``execute`` (external LLM/embedding
        calls happen there) and :func:`apply_prepared_summaries` from
        ``persist`` against the engine's own unit of work (ADR-0009 SS O),
        so the mutation and the step's ``succeeded`` transition land in one
        commit."""

        prepared = await self.prepare_dataset(dataset_id, generation=generation)
        return await self._persist_prepared(prepared)

    async def prepare_dataset(self, dataset_id: UUID, *, generation: int) -> PreparedSummaryRebuild:
        """External/computation phase only (ADR-0009 SS O ``execute``): reads
        PostgreSQL and calls the LLM/embedding providers, but writes nothing.
        The returned batch carries summary text and embeddings -- it must
        stay in a step's private staged cache, never in ``StepResult``
        (ADR-0009 SS 10 / SM-511 SS 35)."""

        dataset = SummaryRebuildDatasetSnapshot(id=dataset_id, generation=generation)
        document_plans = await self._load_document_plans(dataset)
        dataset_summary_current = await self._load_dataset_summary_current_text(dataset)
        prepared_documents = await self._prepare_document_summaries(document_plans)
        dataset_summary_inputs = _dataset_summary_inputs(document_plans, prepared_documents)
        rebuilt_document_ids = {
            prepared.document_id for prepared in prepared_documents if prepared.rebuilt
        }
        should_rebuild_dataset_summary = bool(rebuilt_document_ids) or (
            dataset_summary_current is None and bool(dataset_summary_inputs)
        )

        if should_rebuild_dataset_summary:
            prepared_dataset_summary = await self._prepare_dataset_summary(
                dataset,
                dataset_summary_inputs,
            )
            deactivate_dataset_summary = False
        else:
            prepared_dataset_summary = None
            deactivate_dataset_summary = not dataset_summary_inputs

        return PreparedSummaryRebuild(
            dataset=dataset,
            documents=prepared_documents,
            dataset_summary=prepared_dataset_summary,
            deactivate_dataset_summary=deactivate_dataset_summary,
        )

    async def _load_document_plans(
        self,
        dataset: SummaryRebuildDatasetSnapshot,
    ) -> tuple[DocumentSummaryPlan, ...]:
        async with self._unit_of_work_factory() as uow:
            documents = await uow.documents.list_active_for_summary_rebuild(
                dataset_id=dataset.id,
                generation=dataset.generation,
            )
            plans: list[DocumentSummaryPlan] = []
            for document in documents:
                expected_summary_id = document_summary_id(
                    document.id,
                    generation=document.generation,
                )
                expected_summary = await uow.summaries.get_by_id(expected_summary_id)
                complete = is_document_summary_snapshot_complete(document, expected_summary)
                if complete and expected_summary is not None:
                    plans.append(
                        DocumentSummaryPlan(
                            document=document,
                            expected_summary_id=expected_summary_id,
                            current_text=expected_summary.text,
                            chunk_summaries=(),
                            rebuild=False,
                        )
                    )
                    continue

                chunks = await uow.chunks.list_active_for_document_summary(
                    document_id=document.id,
                    generation=document.generation,
                )
                chunk_summaries = tuple(chunk_knowledge_summary(chunk) for chunk in chunks)
                if not chunk_summaries:
                    raise _incomplete_document_summaries_error(document.id)
                plans.append(
                    DocumentSummaryPlan(
                        document=document,
                        expected_summary_id=expected_summary_id,
                        current_text=None,
                        chunk_summaries=chunk_summaries,
                        rebuild=True,
                    )
                )
            return tuple(plans)

    async def _load_dataset_summary_current_text(
        self,
        dataset: SummaryRebuildDatasetSnapshot,
    ) -> str | None:
        async with self._unit_of_work_factory() as uow:
            summary = await uow.summaries.get_by_id(
                dataset_summary_id(dataset.id, generation=dataset.generation)
            )
            if is_dataset_summary_complete(dataset, summary):
                return summary.text if summary is not None else None
            return None

    async def _prepare_document_summaries(
        self,
        plans: Sequence[DocumentSummaryPlan],
    ) -> tuple[PreparedDocumentSummary, ...]:
        prepared: list[PreparedDocumentSummary] = []
        for plan in plans:
            if not plan.rebuild:
                if plan.current_text is None:
                    raise AssertionError("complete document summary plan must include text")
                prepared.append(
                    PreparedDocumentSummary(
                        document_id=plan.document.id,
                        summary_id=plan.expected_summary_id,
                        text=plan.current_text,
                        embedding=[],
                        rebuilt=False,
                    )
                )
                continue
            summary_text = await self._summarize_document(plan.chunk_summaries)
            embedding = await self._embed_summary(summary_text, subject="Document summary")
            prepared.append(
                PreparedDocumentSummary(
                    document_id=plan.document.id,
                    summary_id=plan.expected_summary_id,
                    text=summary_text,
                    embedding=embedding,
                    rebuilt=True,
                )
            )
        return tuple(prepared)

    async def _prepare_dataset_summary(
        self,
        dataset: SummaryRebuildDatasetSnapshot,
        document_summaries: Sequence[str],
    ) -> PreparedDatasetSummary:
        if not document_summaries:
            raise AssertionError("dataset summary requires at least one document summary")
        summary_text = await self._summarize_dataset(document_summaries)
        embedding = await self._embed_summary(summary_text, subject="Dataset summary")
        return PreparedDatasetSummary(
            summary_id=dataset_summary_id(dataset.id, generation=dataset.generation),
            text=summary_text,
            embedding=embedding,
        )

    async def _persist_prepared(self, prepared: PreparedSummaryRebuild) -> SummaryRebuildCounts:
        async with self._unit_of_work_factory() as uow:
            counts = await apply_prepared_summaries(uow, prepared, settings=self._settings)
            await uow.commit()
            return counts

    async def _summarize_document(self, chunk_summaries: Sequence[str]) -> str:
        try:
            return await self._document_summary_client.summarize(chunk_summaries)
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Document summary generation failed.") from exc

    async def _summarize_dataset(self, document_summaries: Sequence[str]) -> str:
        try:
            return await self._dataset_summary_client.summarize(document_summaries)
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Dataset summary generation failed.") from exc

    async def _embed_summary(self, summary_text: str, *, subject: str) -> list[float]:
        try:
            embeddings = await self._embedding_client.embed_texts([summary_text])
        except SofiasMemoryError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Embedding provider is unavailable.") from exc
        validate_summary_embedding_response(
            embeddings,
            expected_dimensions=self._settings.embedding_dimensions,
            subject=subject,
        )
        return embeddings[0]


async def apply_prepared_summaries(
    uow: SummaryRebuildUnitOfWork,
    prepared: PreparedSummaryRebuild,
    *,
    settings: Settings,
) -> SummaryRebuildCounts:
    """Impure application half (ADR-0009 SS O ``persist`` phase): applies an
    already-``prepare_dataset``-computed batch using the caller's own
    (engine-owned) unit of work. Never commits, never calls a provider."""

    dataset = await uow.datasets.get_by_id(prepared.dataset.id)
    if (
        dataset is None
        or dataset.status != DatasetStatus.ACTIVE
        or dataset.active_generation != prepared.dataset.generation
    ):
        raise _summary_rebuild_state_changed_error()

    summaries_deactivated = 0
    document_summaries_rebuilt = 0
    for document_summary in prepared.documents:
        if not document_summary.rebuilt:
            continue
        document = await uow.documents.get_active_for_summary_rebuild(
            dataset_id=prepared.dataset.id,
            generation=prepared.dataset.generation,
            document_id=document_summary.document_id,
        )
        if document is None:
            raise _summary_rebuild_state_changed_error()
        summaries_deactivated += await uow.summaries.deactivate_active_for_target_except(
            dataset_id=prepared.dataset.id,
            generation=prepared.dataset.generation,
            target_type=SummaryTargetType.DOCUMENT,
            target_id=document.id,
            level=0,
            keep_summary_id=document_summary.summary_id,
        )
        summary = await uow.summaries.get_by_id(document_summary.summary_id)
        if summary is None:
            summary = Summary(
                id=document_summary.summary_id,
                dataset_id=prepared.dataset.id,
                generation=prepared.dataset.generation,
                target_type=SummaryTargetType.DOCUMENT,
                target_id=document.id,
                level=0,
                text=document_summary.text,
                embedding=document_summary.embedding,
                is_active=True,
            )
            await uow.summaries.add(summary)
        else:
            summary.text = document_summary.text
            summary.embedding = document_summary.embedding
            summary.is_active = True
        document.metadata_ = document_summary_metadata(
            document.metadata_,
            summary_id=summary.id,
            llm_model=settings.llm_model,
            embedding_model=settings.embedding_model,
            config_fingerprint=settings.config_fingerprint(),
        )
        document_summaries_rebuilt += 1

    dataset_summaries_rebuilt = 0
    if prepared.dataset_summary is not None:
        summaries_deactivated += await uow.summaries.deactivate_active_for_target_except(
            dataset_id=prepared.dataset.id,
            generation=prepared.dataset.generation,
            target_type=SummaryTargetType.DATASET,
            target_id=prepared.dataset.id,
            level=0,
            keep_summary_id=prepared.dataset_summary.summary_id,
        )
        summary = await uow.summaries.get_by_id(prepared.dataset_summary.summary_id)
        if summary is None:
            summary = Summary(
                id=prepared.dataset_summary.summary_id,
                dataset_id=prepared.dataset.id,
                generation=prepared.dataset.generation,
                target_type=SummaryTargetType.DATASET,
                target_id=prepared.dataset.id,
                level=0,
                text=prepared.dataset_summary.text,
                embedding=prepared.dataset_summary.embedding,
                is_active=True,
            )
            await uow.summaries.add(summary)
        else:
            summary.text = prepared.dataset_summary.text
            summary.embedding = prepared.dataset_summary.embedding
            summary.is_active = True
        dataset_summaries_rebuilt = 1
    elif prepared.deactivate_dataset_summary:
        summaries_deactivated += await uow.summaries.deactivate_active_for_target_except(
            dataset_id=prepared.dataset.id,
            generation=prepared.dataset.generation,
            target_type=SummaryTargetType.DATASET,
            target_id=prepared.dataset.id,
            level=0,
            keep_summary_id=None,
        )

    return SummaryRebuildCounts(
        document_summaries_rebuilt=document_summaries_rebuilt,
        dataset_summaries_rebuilt=dataset_summaries_rebuilt,
        summaries_deactivated=summaries_deactivated,
    )


def dataset_summary_id(dataset_id: UUID, *, generation: int) -> UUID:
    return uuid5(dataset_id, f"dataset-summary:{generation}:{DATASET_SUMMARY_PROMPT_VERSION}")


def is_document_summary_snapshot_complete(
    document: DocumentSummaryRebuildCandidate,
    summary: Summary | None,
) -> bool:
    marker = document.metadata.get("document_summary")
    expected_id = document_summary_id(document.id, generation=document.generation)
    return (
        isinstance(marker, dict)
        and marker.get("version") == DOCUMENT_SUMMARY_VERSION
        and marker.get("prompt_version") == DOCUMENT_SUMMARY_PROMPT_VERSION
        and marker.get("summary_id") == str(expected_id)
        and summary is not None
        and summary.id == expected_id
        and summary.dataset_id == document.dataset_id
        and summary.generation == document.generation
        and summary.target_type == SummaryTargetType.DOCUMENT
        and summary.target_id == document.id
        and summary.level == 0
        and summary.is_active
    )


def is_dataset_summary_complete(
    dataset: SummaryRebuildDatasetSnapshot,
    summary: Summary | None,
) -> bool:
    expected_id = dataset_summary_id(dataset.id, generation=dataset.generation)
    return (
        summary is not None
        and summary.id == expected_id
        and summary.dataset_id == dataset.id
        and summary.generation == dataset.generation
        and summary.target_type == SummaryTargetType.DATASET
        and summary.target_id == dataset.id
        and summary.level == 0
        and summary.is_active
    )


def chunk_knowledge_summary(chunk: ChunkSummaryRebuildSnapshot) -> str:
    marker = chunk.metadata.get("knowledge_extraction")
    if (
        not isinstance(marker, dict)
        or marker.get("version") != KNOWLEDGE_EXTRACTION_VERSION
        or marker.get("prompt_version") != GRAPH_EXTRACTION_PROMPT_VERSION
    ):
        raise _incomplete_document_summaries_error(chunk.document_id)
    summary = marker.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise _incomplete_document_summaries_error(chunk.document_id)
    return summary.strip()


def validate_summary_embedding_response(
    embeddings: Sequence[Sequence[float]],
    *,
    expected_dimensions: int,
    subject: str,
) -> None:
    if len(embeddings) != 1:
        raise DependencyUnavailableError(
            message=f"{subject} embedding provider returned an invalid response.",
            details={"reason": "count_mismatch"},
        )
    if len(embeddings[0]) != expected_dimensions:
        raise DependencyUnavailableError(
            message=f"{subject} embedding provider returned an invalid response.",
            details={"reason": "dimension_mismatch"},
        )


def _dataset_summary_inputs(
    document_plans: Sequence[DocumentSummaryPlan],
    prepared_documents: Sequence[PreparedDocumentSummary],
) -> tuple[str, ...]:
    prepared_by_document_id = {document.document_id: document for document in prepared_documents}
    inputs: list[str] = []
    for plan in document_plans:
        prepared = prepared_by_document_id[plan.document.id]
        inputs.append(prepared.text)
    return tuple(inputs)


def _postgres_unit_of_work_factory(session_factory: AsyncSessionFactory) -> UnitOfWorkFactory:
    def create_unit_of_work() -> SummaryRebuildUnitOfWork:
        return cast(SummaryRebuildUnitOfWork, PostgresUnitOfWork(session_factory))

    return create_unit_of_work


def _incomplete_document_summaries_error(document_id: UUID) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.BAD_REQUEST,
        message="Document chunks do not have complete knowledge extraction summaries.",
        details={"document_id": str(document_id)},
    )


def _summary_rebuild_state_changed_error() -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_REQUEST,
        status_code=HTTPStatus.CONFLICT,
        message="Summary rebuild target changed before persistence.",
    )
