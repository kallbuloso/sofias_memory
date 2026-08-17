from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.dialects import postgresql

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.domain import (
    DatasetStatus,
    PipelineRunStatus,
    PipelineType,
    SourceKind,
    SourceStatus,
    SummaryTargetType,
)
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    Dataset,
    Document,
    Entity,
    EntityMention,
    PipelineRun,
    Relation,
    RelationEvidence,
    Source,
    Summary,
)
from sofias_memory.infrastructure.postgres.repositories.sources import SourceRepository
from sofias_memory.ports import ProjectionCommand
from sofias_memory.schemas.forget import ForgetRequest, ForgetResult
from sofias_memory.services.forget import (
    RESET_DOCUMENT_LANGUAGE,
    RESET_DOCUMENT_METADATA_KEY,
    RESET_DOCUMENT_METADATA_VERSION,
    RESET_DOCUMENT_TEXT_SHA256,
    RESET_DOCUMENT_TITLE,
    RESET_DOCUMENT_TOKEN_COUNT,
    ForgetService,
    ForgetUnitOfWork,
    StorageDeleteStatus,
    UnitOfWorkFactory,
    delete_source_storage,
    reset_document_for_recognify,
    source_storage_path,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
HASH = "a" * 64
VECTOR = [0.0] * 3072


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url=DATABASE_URL,
        neo4j_password=NEO4J_PASSWORD,
        llm_api_key=LLM_API_KEY,
        app_env="test",
        data_directory=tmp_path,
    )


class FakeStore:
    def __init__(self) -> None:
        self.datasets: list[Dataset] = []
        self.sources: list[Source] = []
        self.documents: list[Document] = []
        self.chunks: list[Chunk] = []
        self.entities: list[Entity] = []
        self.mentions: list[EntityMention] = []
        self.relations: list[Relation] = []
        self.evidence: list[RelationEvidence] = []
        self.summaries: list[Summary] = []
        self.runs: list[PipelineRun] = []
        self.graph_commands: list[ProjectionCommand] = []
        self.commits = 0
        self.flush_calls = 0
        self.simulate_autoflush_disabled = False
        self.flushed_source_status: dict[UUID, SourceStatus] = {}
        self.flushed_document_active: dict[UUID, bool] = {}
        self.flushed_chunk_active: dict[UUID, bool] = {}
        self.flushed_relation_active: dict[UUID, bool] = {}
        self.authoritative_relation_results: list[set[UUID]] = []
        self.authoritative_entity_results: list[set[UUID]] = []
        self.incident_entity_results: list[set[UUID]] = []

    def capture_flushed_state(self) -> None:
        """Model the database state visible to SQL with ``autoflush=False``."""

        self.flushed_source_status = {source.id: source.status for source in self.sources}
        self.flushed_document_active = {
            document.id: document.is_active for document in self.documents
        }
        self.flushed_chunk_active = {chunk.id: chunk.is_active for chunk in self.chunks}
        self.flushed_relation_active = {
            relation.id: relation.is_active for relation in self.relations
        }


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_slug(self, slug: str) -> Dataset | None:
        return next((dataset for dataset in self._store.datasets if dataset.slug == slug), None)


class FakeSourceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, source_id: UUID) -> Source | None:
        return next((source for source in self._store.sources if source.id == source_id), None)

    async def get_by_id_for_update(self, source_id: UUID) -> Source | None:
        return await self.get_by_id(source_id)


class FakeDocumentRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, document: Document) -> Document:
        self._store.documents.append(document)
        return document

    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Document]:
        return [
            document
            for document in self._store.documents
            if document.source_id == source_id
            and document.generation == generation
            and (document.is_active or not active_only)
        ]


class FakeChunkRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_for_source_generation(
        self,
        *,
        source_id: UUID,
        generation: int,
        active_only: bool = True,
    ) -> list[Chunk]:
        return sorted(
            [
                chunk
                for chunk in self._store.chunks
                if chunk.source_id == source_id
                and chunk.generation == generation
                and (chunk.is_active or not active_only)
            ],
            key=lambda item: (item.ordinal, item.id),
        )


class FakeEntityMentionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_for_chunks(self, *, chunk_ids: list[UUID]) -> list[EntityMention]:
        requested = set(chunk_ids)
        return sorted(
            [mention for mention in self._store.mentions if mention.chunk_id in requested],
            key=lambda item: item.id,
        )

    async def list_entity_ids_with_authoritative_mentions(
        self,
        *,
        dataset_id: UUID,
        entity_ids: list[UUID],
    ) -> set[UUID]:
        requested = set(entity_ids)
        result = {
            mention.entity_id
            for mention in self._store.mentions
            if mention.entity_id in requested
            and authoritative_chunk(self._store, dataset_id, mention.chunk_id)
        }
        self._store.authoritative_entity_results.append(result)
        return result


class FakeRelationEvidenceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_for_chunks(self, *, chunk_ids: list[UUID]) -> list[RelationEvidence]:
        requested = set(chunk_ids)
        return sorted(
            [item for item in self._store.evidence if item.chunk_id in requested],
            key=lambda item: (item.relation_id, item.chunk_id),
        )

    async def list_relation_ids_with_authoritative_evidence(
        self,
        *,
        dataset_id: UUID,
        relation_ids: list[UUID],
    ) -> set[UUID]:
        requested = set(relation_ids)
        result: set[UUID] = set()
        for evidence in self._store.evidence:
            if evidence.relation_id not in requested:
                continue
            relation = relation_by_id(self._store, evidence.relation_id)
            if (
                relation is not None
                and relation.is_active
                and relation.dataset_id == dataset_id
                and authoritative_chunk(self._store, dataset_id, evidence.chunk_id)
            ):
                result.add(evidence.relation_id)
        self._store.authoritative_relation_results.append(result)
        return result


class FakeEntityRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_current_by_ids(
        self,
        *,
        dataset_id: UUID,
        entity_ids: list[UUID],
    ) -> list[Entity]:
        requested = set(entity_ids)
        dataset = dataset_by_id(self._store, dataset_id)
        return sorted(
            [
                entity
                for entity in self._store.entities
                if entity.id in requested
                and entity.dataset_id == dataset_id
                and entity.generation == dataset.active_generation
                and entity.is_active
            ],
            key=lambda item: item.id,
        )


class FakeRelationRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_current_by_ids(
        self,
        *,
        dataset_id: UUID,
        relation_ids: list[UUID],
    ) -> list[Relation]:
        requested = set(relation_ids)
        dataset = dataset_by_id(self._store, dataset_id)
        return sorted(
            [
                relation
                for relation in self._store.relations
                if relation.id in requested
                and relation.dataset_id == dataset_id
                and relation.generation == dataset.active_generation
                and relation.is_active
                and authoritative_entity(self._store, dataset_id, relation.source_entity_id)
                and authoritative_entity(self._store, dataset_id, relation.target_entity_id)
            ],
            key=lambda item: item.id,
        )

    async def list_active_current_incident_entity_ids(
        self,
        *,
        dataset_id: UUID,
        entity_ids: list[UUID],
    ) -> set[UUID]:
        requested = set(entity_ids)
        dataset = dataset_by_id(self._store, dataset_id)
        result: set[UUID] = set()
        for relation in self._store.relations:
            if (
                relation.dataset_id != dataset_id
                or relation.generation != dataset.active_generation
                or not relation_is_active(self._store, relation)
            ):
                continue
            if relation.source_entity_id in requested:
                result.add(relation.source_entity_id)
            if relation.target_entity_id in requested:
                result.add(relation.target_entity_id)
        self._store.incident_entity_results.append(result)
        return result


class FakeSummaryRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def list_active_for_forget(
        self,
        *,
        dataset_id: UUID,
        generation: int,
        document_ids: list[UUID],
        entity_ids: list[UUID],
        include_dataset_summary: bool,
    ) -> list[Summary]:
        document_ids_set = set(document_ids)
        entity_ids_set = set(entity_ids)
        result: list[Summary] = []
        for summary in self._store.summaries:
            if (
                summary.dataset_id != dataset_id
                or summary.generation != generation
                or not summary.is_active
            ):
                continue
            is_document_summary = (
                summary.target_type == SummaryTargetType.DOCUMENT
                and summary.target_id in document_ids_set
            )
            is_entity_summary = (
                summary.target_type == SummaryTargetType.ENTITY
                and summary.target_id in entity_ids_set
            )
            is_dataset_summary = (
                include_dataset_summary
                and summary.target_type == SummaryTargetType.DATASET
                and summary.target_id == dataset_id
            )
            if is_document_summary or is_entity_summary or is_dataset_summary:
                result.append(summary)
        return sorted(result, key=lambda item: item.id)


class FakeGraphOutboxRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add_projection_command(self, command: ProjectionCommand) -> object:
        self._store.graph_commands.append(command)
        return object()


class FakePipelineRunRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, run: PipelineRun) -> PipelineRun:
        self._store.runs.append(run)
        return run

    async def get_by_id(self, run_id: UUID) -> PipelineRun | None:
        return next((run for run in self._store.runs if run.id == run_id), None)

    async def has_running_forget_for_source_except(
        self,
        *,
        source_id: UUID,
        excluded_run_id: UUID,
    ) -> bool:
        return any(
            run.id != excluded_run_id
            and run.source_id == source_id
            and run.pipeline_type == PipelineType.FORGET
            and run.status == PipelineRunStatus.RUNNING
            for run in self._store.runs
        )


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.datasets = FakeDatasetRepository(store)
        self.sources = FakeSourceRepository(store)
        self.documents = FakeDocumentRepository(store)
        self.chunks = FakeChunkRepository(store)
        self.entity_mentions = FakeEntityMentionRepository(store)
        self.relation_evidence = FakeRelationEvidenceRepository(store)
        self.entities = FakeEntityRepository(store)
        self.relations = FakeRelationRepository(store)
        self.summaries = FakeSummaryRepository(store)
        self.graph_outbox = FakeGraphOutboxRepository(store)
        self.pipeline_runs = FakePipelineRunRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def flush(self) -> None:
        self._store.flush_calls += 1
        self._store.capture_flushed_state()

    async def commit(self) -> None:
        self._store.commits += 1


class FakeDrain:
    def __init__(self, store: FakeStore, *, fail: bool = False) -> None:
        self._store = store
        self.fail = fail
        self.calls = 0

    async def process_dataset(self, dataset_id: UUID) -> object:
        del dataset_id
        self.calls += 1
        if self.fail:
            raise DependencyUnavailableError("Projection drain failed.")
        return DrainResult(processed=len(self._store.graph_commands))


@dataclass(frozen=True)
class DrainResult:
    processed: int


def service_for(
    store: FakeStore,
    tmp_path: Path,
    *,
    drain: FakeDrain | None = None,
) -> ForgetService:
    def create_uow() -> ForgetUnitOfWork:
        return cast(ForgetUnitOfWork, FakeUnitOfWork(store))

    return ForgetService(
        make_settings(tmp_path),
        unit_of_work_factory=cast(UnitOfWorkFactory, create_uow),
        graph_projection_drain=drain or FakeDrain(store),
    )


@pytest.mark.asyncio
async def test_memory_only_forget_deactivates_memory_preserves_storage_and_finalizes_pending(
    tmp_path: Path,
) -> None:
    store, ids, storage_path = seed_forget_graph(tmp_path)
    old_document = document_by_id(store, ids.document_id)
    old_document.title = "Forgotten document title"
    old_document.language = "pt-BR"
    old_document.metadata_ = {
        "document_summary": {"summary_id": str(uuid4())},
        "parser": {"content": "derived"},
    }
    service = service_for(store, tmp_path)

    result = await service.forget_source(
        ForgetRequest(dataset="main", source_id=ids.source_id, memory_only=True)
    )

    source = source_by_id(store, ids.source_id)
    reset_documents = [
        document
        for document in store.documents
        if document.source_id == ids.source_id and document.is_active
    ]

    assert result.source_status == SourceStatus.PENDING
    assert result.memory_only is True
    assert result.documents_deactivated == 1
    assert result.chunks_deactivated == 2
    assert result.relations_deactivated == 1
    assert result.entities_deactivated == 1
    assert result.entity_mentions_unprojected == 2
    assert result.relation_evidence_unprojected == 2
    assert result.summaries_deactivated == 3
    assert result.graph_events_enqueued == 7
    assert result.graph_events_processed == 7
    assert result.storage_deleted is False
    assert source.status == SourceStatus.PENDING
    assert source.storage_uri == storage_path.as_uri()
    assert storage_path.exists()
    assert old_document.is_active is False
    assert len(reset_documents) == 1
    reset_document = reset_documents[0]
    assert reset_document.id != old_document.id
    assert reset_document.dataset_id == old_document.dataset_id
    assert reset_document.source_id == old_document.source_id
    assert reset_document.generation == old_document.generation
    assert reset_document.normalized_text == ""
    assert reset_document.text_sha256 == RESET_DOCUMENT_TEXT_SHA256
    assert reset_document.text_sha256 != old_document.text_sha256
    assert reset_document.token_count == RESET_DOCUMENT_TOKEN_COUNT
    assert reset_document.token_count != old_document.token_count
    assert reset_document.title == RESET_DOCUMENT_TITLE
    assert reset_document.title != old_document.title
    assert reset_document.language == RESET_DOCUMENT_LANGUAGE
    assert reset_document.language != old_document.language
    assert reset_document.metadata_ == {
        RESET_DOCUMENT_METADATA_KEY: {"version": RESET_DOCUMENT_METADATA_VERSION}
    }
    assert reset_document.metadata_ != old_document.metadata_
    assert all(not chunk.is_active for chunk in source_chunks(store, ids.source_id))
    assert relation_by_id(store, ids.shared_relation_id).is_active is True
    assert relation_by_id(store, ids.orphan_relation_id).is_active is False
    assert entity_by_id(store, ids.shared_entity_id).is_active is True
    assert entity_by_id(store, ids.orphan_entity_id).is_active is False
    assert all(
        not summary.is_active for summary in store.summaries if summary.dataset_id == ids.dataset_id
    )
    assert [command.operation for command in store.graph_commands] == ["delete"] * 7
    assert sorted(command.aggregate_type for command in store.graph_commands) == [
        "chunk",
        "chunk",
        "chunk_next",
        "entity",
        "entity_mention",
        "entity_mention",
        "relation",
    ]
    assert store.runs[-1].status == PipelineRunStatus.SUCCEEDED
    assert store.runs[-1].pipeline_type == PipelineType.FORGET


@pytest.mark.asyncio
async def test_memory_only_forget_flushes_dirty_state_before_authoritative_orphan_queries(
    tmp_path: Path,
) -> None:
    store, ids, _storage_path = seed_forget_graph(tmp_path)
    store.simulate_autoflush_disabled = True
    store.capture_flushed_state()

    result = await service_for(store, tmp_path).forget_source(
        ForgetRequest(dataset="main", source_id=ids.source_id, memory_only=True)
    )

    assert store.flush_calls >= 2
    assert store.authoritative_relation_results == [{ids.shared_relation_id}]
    assert store.authoritative_entity_results == [{ids.shared_entity_id}]
    assert store.incident_entity_results == [{ids.shared_entity_id}]
    assert result.documents_deactivated == 1
    assert result.chunks_deactivated == 2
    assert result.summaries_deactivated == 3
    assert result.entities_deactivated == 1
    assert result.relations_deactivated == 1
    assert result.entity_mentions_unprojected == 2
    assert result.relation_evidence_unprojected == 2
    assert result.graph_events_enqueued == 7
    assert [(command.aggregate_type, command.operation) for command in store.graph_commands] == [
        ("chunk", "delete"),
        ("chunk", "delete"),
        ("chunk_next", "delete"),
        ("entity_mention", "delete"),
        ("entity_mention", "delete"),
        ("relation", "delete"),
        ("entity", "delete"),
    ]


def test_reset_document_placeholder_has_no_forgotten_content() -> None:
    original = document(uuid4(), uuid4(), uuid4())
    original.normalized_text = "Forgotten normalized content"
    original.text_sha256 = sha256(original.normalized_text.encode("utf-8")).hexdigest()
    original.token_count = 42
    original.title = "Forgotten title"
    original.language = "pt-BR"
    original.metadata_ = {"document_summary": {"summary_id": str(uuid4())}}

    reset = reset_document_for_recognify(original)

    assert reset.normalized_text == ""
    assert reset.text_sha256 == RESET_DOCUMENT_TEXT_SHA256
    assert reset.text_sha256 != original.text_sha256
    assert reset.token_count == RESET_DOCUMENT_TOKEN_COUNT
    assert reset.token_count != original.token_count
    assert reset.title == RESET_DOCUMENT_TITLE
    assert reset.language == RESET_DOCUMENT_LANGUAGE
    assert reset.metadata_ == {
        RESET_DOCUMENT_METADATA_KEY: {"version": RESET_DOCUMENT_METADATA_VERSION}
    }


@pytest.mark.asyncio
async def test_full_source_forget_deletes_storage_after_drain_and_finalizes_deleted(
    tmp_path: Path,
) -> None:
    store, ids, storage_path = seed_forget_graph(tmp_path)

    result = await service_for(store, tmp_path).forget_source(
        ForgetRequest(dataset="main", source_id=ids.source_id, memory_only=False)
    )

    source = source_by_id(store, ids.source_id)
    assert result.source_status == SourceStatus.DELETED
    assert result.storage_deleted is True
    assert source.status == SourceStatus.DELETED
    assert source.storage_uri is None
    assert not storage_path.exists()
    assert not any(
        document.is_active for document in store.documents if document.source_id == ids.source_id
    )


@pytest.mark.asyncio
async def test_memory_only_retry_on_pending_source_is_noop(tmp_path: Path) -> None:
    store, ids, _storage_path = seed_forget_graph(tmp_path)
    service = service_for(store, tmp_path)

    first = await service.forget_source(
        ForgetRequest(dataset="main", source_id=ids.source_id, memory_only=True)
    )
    event_count = len(store.graph_commands)
    active_document_ids = {
        document.id
        for document in store.documents
        if document.source_id == ids.source_id and document.is_active
    }

    second = await service.forget_source(
        ForgetRequest(dataset="main", source_id=ids.source_id, memory_only=True)
    )

    assert first.source_status == SourceStatus.PENDING
    assert second.source_status == SourceStatus.PENDING
    assert second.documents_deactivated == 0
    assert second.chunks_deactivated == 0
    assert second.graph_events_enqueued == 0
    assert len(store.graph_commands) == event_count
    assert {
        document.id
        for document in store.documents
        if document.source_id == ids.source_id and document.is_active
    } == active_document_ids


@pytest.mark.asyncio
async def test_reentrant_forget_skips_post_commit_work_owned_by_running_forget(
    tmp_path: Path,
) -> None:
    store, ids, _storage_path = seed_forget_graph(tmp_path)
    source = source_by_id(store, ids.source_id)
    source.status = SourceStatus.DELETING
    owner_run_id = uuid4()
    store.runs.append(
        PipelineRun(
            id=owner_run_id,
            pipeline_type=PipelineType.FORGET,
            dataset_id=ids.dataset_id,
            source_id=ids.source_id,
            status=PipelineRunStatus.RUNNING,
            idempotency_key=None,
            payload_hash=HASH,
            input={},
            progress=0.5,
            current_step="forget_source",
            attempt=1,
            worker_id=None,
            heartbeat_at=None,
            config_fingerprint=HASH,
            error_code=None,
            error_message=None,
            metrics={},
            started_at=None,
            finished_at=None,
        )
    )
    drain = FakeDrain(store)

    result = await service_for(store, tmp_path, drain=drain).forget_source(
        ForgetRequest(dataset="main", source_id=ids.source_id, memory_only=True)
    )

    assert result.source_status == SourceStatus.DELETING
    assert result.graph_events_enqueued == 0
    assert result.graph_events_processed == 0
    assert drain.calls == 0
    assert source.status == SourceStatus.DELETING
    assert store.graph_commands == []
    assert store.runs[-1].status == PipelineRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_forget_rejects_wait_false_missing_dataset_and_cross_dataset(tmp_path: Path) -> None:
    store, ids, _storage_path = seed_forget_graph(tmp_path)
    service = service_for(store, tmp_path)

    with pytest.raises(SofiasMemoryError) as wait_error:
        await service.forget_source(ForgetRequest(source_id=ids.source_id, wait=False))
    assert wait_error.value.status_code == 400

    with pytest.raises(SofiasMemoryError) as dataset_error:
        await service.forget_source(ForgetRequest(dataset="missing", source_id=ids.source_id))
    assert dataset_error.value.status_code == 404

    with pytest.raises(SofiasMemoryError) as source_error:
        await service.forget_source(ForgetRequest(dataset="other", source_id=ids.source_id))
    assert source_error.value.status_code == 404

    assert not any(dataset.slug == "main" for dataset in FakeStore().datasets)


@pytest.mark.asyncio
async def test_forget_missing_source_returns_404_without_creating_pipeline_run(
    tmp_path: Path,
) -> None:
    store, ids, _storage_path = seed_forget_graph(tmp_path)
    service = service_for(store, tmp_path)
    missing_source_id = uuid4()

    with pytest.raises(SofiasMemoryError) as source_error:
        await service.forget_source(ForgetRequest(dataset="main", source_id=missing_source_id))

    assert source_error.value.status_code == 404
    assert not any(run.source_id == missing_source_id for run in store.runs)
    assert store.runs == []


@pytest.mark.asyncio
async def test_forget_cross_dataset_source_matches_missing_source_envelope(
    tmp_path: Path,
) -> None:
    store, ids, _storage_path = seed_forget_graph(tmp_path)
    service = service_for(store, tmp_path)
    missing_source_id = uuid4()

    with pytest.raises(SofiasMemoryError) as missing_error:
        await service.forget_source(ForgetRequest(dataset="main", source_id=missing_source_id))

    with pytest.raises(SofiasMemoryError) as cross_error:
        await service.forget_source(ForgetRequest(dataset="other", source_id=ids.source_id))

    assert missing_error.value.status_code == cross_error.value.status_code == 404
    assert missing_error.value.code == cross_error.value.code
    assert missing_error.value.message == cross_error.value.message
    assert store.runs == []


@pytest.mark.asyncio
async def test_retry_after_projection_drain_failure_does_not_duplicate_events(
    tmp_path: Path,
) -> None:
    store, ids, storage_path = seed_forget_graph(tmp_path)
    failing_drain = FakeDrain(store, fail=True)

    with pytest.raises(DependencyUnavailableError):
        await service_for(store, tmp_path, drain=failing_drain).forget_source(
            ForgetRequest(dataset="main", source_id=ids.source_id)
        )

    source = source_by_id(store, ids.source_id)
    event_count = len(store.graph_commands)
    assert source.status == SourceStatus.DELETING
    assert storage_path.exists()
    assert store.runs[-1].status == PipelineRunStatus.FAILED

    result = await service_for(store, tmp_path).forget_source(
        ForgetRequest(dataset="main", source_id=ids.source_id)
    )

    assert len(store.graph_commands) == event_count
    assert result.graph_events_enqueued == 0
    assert result.graph_events_processed == event_count
    assert result.storage_deleted is True
    assert source.status == SourceStatus.DELETED
    assert not storage_path.exists()


@pytest.mark.asyncio
async def test_retry_after_unlink_success_before_finalization_clears_storage_uri(
    tmp_path: Path,
) -> None:
    store, ids, storage_path = seed_forget_graph(tmp_path)
    service = service_for(store, tmp_path)
    request = ForgetRequest(dataset="main", source_id=ids.source_id)

    run_id = await service._create_running_run(request)  # noqa: SLF001
    mutation = await service._apply_authoritative_forget(run_id, request)  # noqa: SLF001
    storage_delete = delete_source_storage(
        tmp_path,
        dataset_id=ids.dataset_id,
        source_id=ids.source_id,
        storage_uri=mutation.storage_uri,
    )

    assert storage_delete.status == StorageDeleteStatus.DELETED_NOW
    assert not storage_path.exists()
    assert source_by_id(store, ids.source_id).status == SourceStatus.DELETING
    assert source_by_id(store, ids.source_id).storage_uri == storage_path.as_uri()
    initial_run = next(run for run in store.runs if run.id == run_id)
    initial_run.status = PipelineRunStatus.FAILED

    retry = await service_for(store, tmp_path).forget_source(request)

    source = source_by_id(store, ids.source_id)
    assert retry.graph_events_enqueued == 0
    assert retry.storage_deleted is True
    assert source.status == SourceStatus.DELETED
    assert source.storage_uri is None


@pytest.mark.asyncio
async def test_source_repository_forget_lock_uses_for_update() -> None:
    class RecordingScalarSession:
        def __init__(self) -> None:
            self.statement: object | None = None

        async def scalar(self, statement: object) -> None:
            self.statement = statement
            return None

    session = RecordingScalarSession()
    await SourceRepository(cast(Any, session)).get_by_id_for_update(uuid4())

    assert session.statement is not None
    compiled = str(cast(Any, session.statement).compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in compiled


def test_forget_request_schema_and_storage_path_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = ForgetRequest(dataset="  main  ", source_id=uuid4())
    assert request.dataset == "main"

    with pytest.raises(ValidationError):
        ForgetRequest(dataset="main", source_id=uuid4(), extra=True)  # type: ignore[call-arg]

    dataset_id = uuid4()
    source_id = uuid4()
    storage_root = tmp_path / "sources"
    good_path = storage_root / str(dataset_id) / str(source_id) / "original.txt"
    good_path.parent.mkdir(parents=True)
    good_path.write_text("content", encoding="utf-8")
    assert (
        source_storage_path(
            storage_root,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=good_path.as_uri(),
        )
        == good_path.resolve()
    )

    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("secret", encoding="utf-8")
    with pytest.raises(SofiasMemoryError):
        source_storage_path(
            storage_root,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=outside_path.as_uri(),
        )

    if hasattr(Path, "symlink_to"):
        symlink_path = good_path.parent / "symlink.txt"
        try:
            symlink_path.symlink_to(outside_path)
        except OSError:
            pass
        else:
            with pytest.raises(SofiasMemoryError):
                source_storage_path(
                    storage_root,
                    dataset_id=dataset_id,
                    source_id=source_id,
                    storage_uri=symlink_path.as_uri(),
                )

    delete_result = delete_source_storage(
        storage_root,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_uri=good_path.as_uri(),
    )
    assert delete_result.status == StorageDeleteStatus.DELETED_NOW
    assert delete_result.completed is True
    assert not good_path.exists()

    already_absent = delete_source_storage(
        storage_root,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_uri=good_path.as_uri(),
    )
    assert already_absent.status == StorageDeleteStatus.ALREADY_ABSENT
    assert already_absent.completed is True

    missing_outside_path = tmp_path / "missing-outside.txt"
    with pytest.raises(SofiasMemoryError):
        delete_source_storage(
            storage_root,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=missing_outside_path.as_uri(),
        )

    error_path = storage_root / str(dataset_id) / str(source_id) / "error.txt"
    error_path.write_text("content", encoding="utf-8")

    def fail_unlink(self: Path) -> None:
        if self == error_path:
            raise PermissionError("denied")
        Path.unlink(self)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(DependencyUnavailableError):
        delete_source_storage(
            storage_root,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri=error_path.as_uri(),
        )


@pytest.mark.asyncio
async def test_forget_route_returns_envelope_and_requires_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = uuid4()
    source_id = uuid4()

    class FakeProjection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeProcessor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeBatch:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeForgetService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def forget_source(self, request: ForgetRequest) -> ForgetResult:
            return ForgetResult(
                run_id=uuid4(),
                status="succeeded",
                dataset_id=dataset_id,
                source_id=request.source_id,
                memory_only=request.memory_only,
                source_status=SourceStatus.PENDING,
                documents_deactivated=0,
                chunks_deactivated=0,
                summaries_deactivated=0,
                entities_deactivated=0,
                relations_deactivated=0,
                entity_mentions_unprojected=0,
                relation_evidence_unprojected=0,
                graph_events_enqueued=0,
                graph_events_processed=0,
                storage_deleted=False,
            )

    monkeypatch.setattr("sofias_memory.api.routes.forget.Neo4jProjection", FakeProjection)
    monkeypatch.setattr("sofias_memory.api.routes.forget.GraphOutboxProcessor", FakeProcessor)
    monkeypatch.setattr("sofias_memory.api.routes.forget.GraphOutboxBatchProcessor", FakeBatch)
    monkeypatch.setattr("sofias_memory.api.routes.forget.ForgetService", FakeForgetService)
    monkeypatch.setattr("sofias_memory.api.routes.forget.app_neo4j_resource", lambda app: object())
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_key = await client.post("/api/v1/forget", json={"source_id": str(source_id)})
        response = await client.post(
            "/api/v1/forget",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"source_id": str(source_id), "memory_only": True},
        )

    assert missing_key.status_code == 401
    assert response.status_code == 200
    assert response.json()["data"]["source_id"] == str(source_id)
    assert response.json()["data"]["memory_only"] is True


@dataclass(frozen=True)
class ForgetIds:
    dataset_id: UUID
    other_dataset_id: UUID
    source_id: UUID
    other_source_id: UUID
    document_id: UUID
    other_document_id: UUID
    chunk_a_id: UUID
    chunk_b_id: UUID
    other_chunk_id: UUID
    shared_entity_id: UUID
    orphan_entity_id: UUID
    target_entity_id: UUID
    other_entity_id: UUID
    shared_relation_id: UUID
    orphan_relation_id: UUID


def seed_forget_graph(tmp_path: Path) -> tuple[FakeStore, ForgetIds, Path]:
    store = FakeStore()
    ids = ForgetIds(*(uuid4() for _ in range(15)))
    dataset = Dataset(
        id=ids.dataset_id,
        name="main",
        slug="main",
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=0,
    )
    other_dataset = Dataset(
        id=ids.other_dataset_id,
        name="other",
        slug="other",
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=0,
    )
    store.datasets.extend([dataset, other_dataset])
    storage_path = tmp_path / str(ids.dataset_id) / str(ids.source_id) / "original.txt"
    storage_path.parent.mkdir(parents=True)
    storage_path.write_text("original", encoding="utf-8")
    store.sources.extend(
        [
            source(
                ids.source_id,
                ids.dataset_id,
                status=SourceStatus.ACTIVE,
                storage_uri=storage_path.as_uri(),
            ),
            source(ids.other_source_id, ids.dataset_id, status=SourceStatus.ACTIVE),
        ]
    )
    store.documents.extend(
        [
            document(ids.document_id, ids.dataset_id, ids.source_id),
            document(ids.other_document_id, ids.dataset_id, ids.other_source_id),
        ]
    )
    store.chunks.extend(
        [
            chunk(ids.chunk_a_id, ids.dataset_id, ids.document_id, ids.source_id, ordinal=0),
            chunk(ids.chunk_b_id, ids.dataset_id, ids.document_id, ids.source_id, ordinal=1),
            chunk(
                ids.other_chunk_id,
                ids.dataset_id,
                ids.other_document_id,
                ids.other_source_id,
                ordinal=0,
            ),
        ]
    )
    store.entities.extend(
        [
            entity(ids.shared_entity_id, ids.dataset_id, "shared"),
            entity(ids.orphan_entity_id, ids.dataset_id, "orphan"),
            entity(ids.target_entity_id, ids.dataset_id, "target"),
            entity(ids.other_entity_id, ids.other_dataset_id, "other"),
        ]
    )
    store.mentions.extend(
        [
            mention(ids.shared_entity_id, ids.chunk_a_id),
            mention(ids.orphan_entity_id, ids.chunk_b_id),
            mention(ids.shared_entity_id, ids.other_chunk_id),
        ]
    )
    store.relations.extend(
        [
            relation(
                ids.shared_relation_id,
                ids.dataset_id,
                ids.shared_entity_id,
                ids.target_entity_id,
                predicate="shared",
            ),
            relation(
                ids.orphan_relation_id,
                ids.dataset_id,
                ids.orphan_entity_id,
                ids.shared_entity_id,
                predicate="orphan",
            ),
        ]
    )
    store.evidence.extend(
        [
            RelationEvidence(
                relation_id=ids.shared_relation_id,
                chunk_id=ids.chunk_a_id,
                quote="forgotten evidence",
                confidence=0.8,
            ),
            RelationEvidence(
                relation_id=ids.shared_relation_id,
                chunk_id=ids.other_chunk_id,
                quote="remaining evidence",
                confidence=0.9,
            ),
            RelationEvidence(
                relation_id=ids.orphan_relation_id,
                chunk_id=ids.chunk_b_id,
                quote="orphan evidence",
                confidence=0.8,
            ),
        ]
    )
    store.summaries.extend(
        [
            summary(ids.dataset_id, SummaryTargetType.DOCUMENT, ids.document_id),
            summary(ids.dataset_id, SummaryTargetType.ENTITY, ids.orphan_entity_id),
            summary(ids.dataset_id, SummaryTargetType.DATASET, ids.dataset_id),
        ]
    )
    return store, ids, storage_path


def dataset_by_id(store: FakeStore, dataset_id: UUID) -> Dataset:
    return next(dataset for dataset in store.datasets if dataset.id == dataset_id)


def source_by_id(store: FakeStore, source_id: UUID) -> Source:
    return next(source for source in store.sources if source.id == source_id)


def document_by_id(store: FakeStore, document_id: UUID) -> Document:
    return next(document for document in store.documents if document.id == document_id)


def entity_by_id(store: FakeStore, entity_id: UUID) -> Entity:
    return next(entity for entity in store.entities if entity.id == entity_id)


def relation_by_id(store: FakeStore, relation_id: UUID) -> Relation:
    return next(relation for relation in store.relations if relation.id == relation_id)


def source_chunks(store: FakeStore, source_id: UUID) -> list[Chunk]:
    return [chunk for chunk in store.chunks if chunk.source_id == source_id]


def authoritative_chunk(store: FakeStore, dataset_id: UUID, chunk_id: UUID) -> bool:
    dataset = dataset_by_id(store, dataset_id)
    chunk = next((item for item in store.chunks if item.id == chunk_id), None)
    if chunk is None:
        return False
    document = next((item for item in store.documents if item.id == chunk.document_id), None)
    source = next((item for item in store.sources if item.id == chunk.source_id), None)
    return (
        chunk.dataset_id == dataset_id
        and chunk.generation == dataset.active_generation
        and chunk_is_active(store, chunk)
        and document is not None
        and document.dataset_id == dataset_id
        and document.generation == dataset.active_generation
        and document_is_active(store, document)
        and source is not None
        and source.dataset_id == dataset_id
        and source_is_active(store, source)
    )


def source_is_active(store: FakeStore, source: Source) -> bool:
    status = source.status
    if store.simulate_autoflush_disabled:
        status = store.flushed_source_status.get(source.id, status)
    return status == SourceStatus.ACTIVE


def document_is_active(store: FakeStore, document: Document) -> bool:
    is_active = document.is_active
    if store.simulate_autoflush_disabled:
        is_active = store.flushed_document_active.get(document.id, is_active)
    return is_active


def chunk_is_active(store: FakeStore, chunk: Chunk) -> bool:
    is_active = chunk.is_active
    if store.simulate_autoflush_disabled:
        is_active = store.flushed_chunk_active.get(chunk.id, is_active)
    return is_active


def relation_is_active(store: FakeStore, relation: Relation) -> bool:
    is_active = relation.is_active
    if store.simulate_autoflush_disabled:
        is_active = store.flushed_relation_active.get(relation.id, is_active)
    return is_active


def authoritative_entity(store: FakeStore, dataset_id: UUID, entity_id: UUID) -> bool:
    dataset = dataset_by_id(store, dataset_id)
    entity = next((item for item in store.entities if item.id == entity_id), None)
    return (
        entity is not None
        and entity.dataset_id == dataset_id
        and entity.generation == dataset.active_generation
        and entity.is_active
    )


def source(
    source_id: UUID,
    dataset_id: UUID,
    *,
    status: SourceStatus,
    storage_uri: str | None = None,
) -> Source:
    return Source(
        id=source_id,
        dataset_id=dataset_id,
        kind=SourceKind.TEXT,
        name="source",
        mime_type="text/plain",
        original_uri=None,
        storage_uri=storage_uri,
        content_sha256=HASH,
        normalized_sha256=HASH,
        byte_size=1,
        metadata_={},
        status=status,
        version=1,
    )


def document(document_id: UUID, dataset_id: UUID, source_id: UUID) -> Document:
    return Document(
        id=document_id,
        dataset_id=dataset_id,
        source_id=source_id,
        generation=0,
        title="document",
        language="en",
        normalized_text="content",
        text_sha256=HASH,
        token_count=1,
        metadata_={},
        is_active=True,
    )


def chunk(
    chunk_id: UUID,
    dataset_id: UUID,
    document_id: UUID,
    source_id: UUID,
    *,
    ordinal: int,
) -> Chunk:
    return Chunk(
        id=chunk_id,
        dataset_id=dataset_id,
        document_id=document_id,
        source_id=source_id,
        generation=0,
        ordinal=ordinal,
        text="chunk",
        content_sha256=HASH,
        token_count=1,
        start_char=0,
        end_char=5,
        section_path=[],
        metadata_={},
        embedding=VECTOR,
        lexical="",
        is_active=True,
    )


def entity(entity_id: UUID, dataset_id: UUID, canonical_key: str) -> Entity:
    return Entity(
        id=entity_id,
        dataset_id=dataset_id,
        generation=0,
        canonical_key=canonical_key,
        name=canonical_key,
        entity_type="concept",
        description="entity",
        aliases=[],
        properties={},
        confidence=0.9,
        importance_weight=0.5,
        embedding=None,
        is_active=True,
    )


def mention(entity_id: UUID, chunk_id: UUID) -> EntityMention:
    return EntityMention(
        id=uuid4(),
        entity_id=entity_id,
        chunk_id=chunk_id,
        surface_text="surface",
        start_char=0,
        end_char=7,
        confidence=0.9,
    )


def relation(
    relation_id: UUID,
    dataset_id: UUID,
    source_entity_id: UUID,
    target_entity_id: UUID,
    *,
    predicate: str,
) -> Relation:
    return Relation(
        id=relation_id,
        dataset_id=dataset_id,
        generation=0,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate=predicate,
        description="relation",
        properties={},
        confidence=0.8,
        importance_weight=0.5,
        embedding=None,
        is_active=True,
    )


def summary(dataset_id: UUID, target_type: SummaryTargetType, target_id: UUID) -> Summary:
    return Summary(
        id=uuid4(),
        dataset_id=dataset_id,
        generation=0,
        target_type=target_type,
        target_id=target_id,
        level=0,
        text="summary",
        embedding=VECTOR,
        is_active=True,
    )


def test_settings_secret_values_are_not_accidentally_exposed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert isinstance(settings.api_key, SecretStr)
