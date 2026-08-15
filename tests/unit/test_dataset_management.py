from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.app import create_app
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, SourceKind, SourceStatus
from sofias_memory.infrastructure.postgres.models import Dataset
from sofias_memory.infrastructure.postgres.repositories.datasets import (
    DatasetRepository,
    DatasetStatsSnapshot,
)
from sofias_memory.infrastructure.postgres.repositories.sources import SourceListItem
from sofias_memory.schemas.datasets import (
    DatasetCreateRequest,
    DatasetListResult,
    DatasetRenameRequest,
    DatasetResult,
    DatasetSourcesResult,
    DatasetStatsResult,
)
from sofias_memory.services.datasets import (
    DatasetService,
    DatasetUnitOfWork,
    UnitOfWorkFactory,
    normalize_dataset_slug,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
UPDATED_AT = datetime(2026, 1, 2, tzinfo=UTC)


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
        self.sources: list[SourceListItem] = []
        self.stats = DatasetStatsSnapshot(
            sources_total=0,
            sources_active=0,
            documents_total=0,
            documents_active=0,
            chunks_total=0,
            chunks_active=0,
            entities_active_current_generation=0,
            relations_active_current_generation=0,
            summaries_total=0,
        )
        self.commits = 0


class FakeDatasetRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def add(self, dataset: Dataset) -> Dataset:
        self._store.datasets.append(dataset)
        return dataset

    async def get_by_id(self, dataset_id: UUID) -> Dataset | None:
        return next((dataset for dataset in self._store.datasets if dataset.id == dataset_id), None)

    async def get_by_slug(self, slug: str) -> Dataset | None:
        return next((dataset for dataset in self._store.datasets if dataset.slug == slug), None)

    async def get_by_name(self, name: str) -> Dataset | None:
        folded = name.casefold()
        return next(
            (dataset for dataset in self._store.datasets if dataset.name.casefold() == folded),
            None,
        )

    async def list_paginated(self, *, limit: int, offset: int) -> tuple[list[Dataset], int]:
        ordered = sorted(self._store.datasets, key=lambda dataset: (dataset.created_at, dataset.id))
        return ordered[offset : offset + limit], len(ordered)

    async def stats_for_dataset(self, dataset: Dataset) -> DatasetStatsSnapshot:
        del dataset
        return self._store.stats


class FakeSourceRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.dataset_ids_seen: list[UUID] = []

    async def list_for_dataset_paginated(
        self,
        *,
        dataset_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[SourceListItem], int]:
        self.dataset_ids_seen.append(dataset_id)
        scoped = [source for source in self._store.sources if source.dataset_id == dataset_id]
        ordered = sorted(scoped, key=lambda source: (source.created_at, source.id))
        return ordered[offset : offset + limit], len(ordered)


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.datasets = FakeDatasetRepository(store)
        self.sources = FakeSourceRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(store: FakeStore) -> DatasetService:
    def create_uow() -> DatasetUnitOfWork:
        return cast(DatasetUnitOfWork, FakeUnitOfWork(store))

    return DatasetService(unit_of_work_factory=cast(UnitOfWorkFactory, create_uow))


def dataset(
    name: str,
    slug: str,
    *,
    dataset_id: UUID | None = None,
    created_at: datetime = CREATED_AT,
    status: DatasetStatus = DatasetStatus.ACTIVE,
) -> Dataset:
    return Dataset(
        id=dataset_id or uuid4(),
        name=name,
        slug=slug,
        description=None,
        status=status,
        active_generation=0,
        created_at=created_at,
        updated_at=UPDATED_AT,
    )


def source_item(dataset_id: UUID, *, source_id: UUID | None = None) -> SourceListItem:
    return SourceListItem(
        id=source_id or uuid4(),
        dataset_id=dataset_id,
        kind=SourceKind.FILE,
        name="note.txt",
        mime_type="text/plain",
        original_uri=None,
        content_sha256="a" * 64,
        normalized_sha256="b" * 64,
        byte_size=12,
        metadata={"origin": "unit"},
        status=SourceStatus.ACTIVE,
        version=1,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


def test_dataset_schema_validation_and_slug_normalization() -> None:
    request = DatasetCreateRequest(name="  Meu Café  ", description="  docs  ", slug=" Docs v1 ")

    assert request.name == "Meu Café"
    assert request.description == "docs"
    assert request.slug == "Docs v1"
    assert normalize_dataset_slug("  Meu Café / v1  ") == "meu-cafe-v1"

    with pytest.raises(ValidationError):
        DatasetCreateRequest(name="Docs", owner_id="not-allowed")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        DatasetRenameRequest(name="Docs", slug="new")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_stats_sql_uses_authoritative_active_chunk_and_relation_predicates() -> None:
    dataset_id = uuid4()
    session = RecordingScalarSession()
    repository = DatasetRepository(cast(object, session))

    await repository.stats_for_dataset(dataset("Docs", "docs", dataset_id=dataset_id))

    rendered = [render_statement(statement) for statement in session.statements]
    chunks_active = next(
        statement
        for statement in rendered
        if "FROM chunks JOIN documents" in statement and "JOIN sources" in statement
    )
    relations_active = next(
        statement
        for statement in rendered
        if "FROM relations JOIN entities AS entities_1" in statement
        and "JOIN entities AS entities_2" in statement
    )

    assert "chunks.dataset_id" in chunks_active
    assert "chunks.generation" in chunks_active
    assert "chunks.is_active IS true" in chunks_active
    assert "documents.dataset_id" in chunks_active
    assert "documents.generation" in chunks_active
    assert "documents.is_active IS true" in chunks_active
    assert "sources.dataset_id" in chunks_active
    assert "sources.status" in chunks_active

    assert "relations.dataset_id" in relations_active
    assert "relations.generation" in relations_active
    assert "relations.is_active IS true" in relations_active
    assert "entities_1.dataset_id" in relations_active
    assert "entities_1.generation" in relations_active
    assert "entities_1.is_active IS true" in relations_active
    assert "entities_2.dataset_id" in relations_active
    assert "entities_2.generation" in relations_active
    assert "entities_2.is_active IS true" in relations_active


@pytest.mark.asyncio
async def test_create_dataset_derives_slug_rejects_main_and_conflicts() -> None:
    store = FakeStore()
    service = service_for(store)

    result = await service.create_dataset(DatasetCreateRequest(name="Meu Café"))

    assert result.name == "Meu Café"
    assert result.slug == "meu-cafe"
    assert result.status == DatasetStatus.ACTIVE
    assert result.active_generation == 0
    assert store.commits == 1

    with pytest.raises(SofiasMemoryError) as main_error:
        await service.create_dataset(DatasetCreateRequest(name="Main"))
    assert main_error.value.status_code == 400

    with pytest.raises(SofiasMemoryError) as conflict_error:
        await service.create_dataset(DatasetCreateRequest(name="Outro", slug="meu-cafe"))
    assert conflict_error.value.status_code == 409

    with pytest.raises(SofiasMemoryError) as name_conflict_error:
        await service.create_dataset(DatasetCreateRequest(name="meu café", slug="outro"))
    assert name_conflict_error.value.status_code == 409


@pytest.mark.asyncio
async def test_list_get_and_rename_dataset_are_uuid_scoped_and_slug_is_immutable() -> None:
    store = FakeStore()
    later = dataset(
        "Later",
        "later",
        dataset_id=UUID("10000000-0000-0000-0000-000000000002"),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    first = dataset(
        "First",
        "first",
        dataset_id=UUID("10000000-0000-0000-0000-000000000001"),
        created_at=CREATED_AT,
    )
    store.datasets.extend([later, first])
    service = service_for(store)

    listed = await service.list_datasets(limit=1, offset=0)
    got = await service.get_dataset(first.id)
    renamed = await service.rename_dataset(first.id, DatasetRenameRequest(name="Renamed"))

    assert [item.dataset_id for item in listed.items] == [first.id]
    assert listed.total == 2
    assert got.dataset_id == first.id
    assert renamed.name == "Renamed"
    assert renamed.slug == "first"
    assert first.updated_at > UPDATED_AT
    assert store.commits == 1

    with pytest.raises(SofiasMemoryError) as missing_error:
        await service.get_dataset(uuid4())
    assert missing_error.value.status_code == 404


@pytest.mark.asyncio
async def test_sources_are_dataset_scoped_paginated_and_do_not_expose_content() -> None:
    dataset_id = uuid4()
    other_dataset_id = uuid4()
    store = FakeStore()
    store.datasets.append(dataset("Docs", "docs", dataset_id=dataset_id))
    visible_source = source_item(dataset_id)
    store.sources.extend([source_item(other_dataset_id), visible_source])

    result = await service_for(store).list_sources(dataset_id=dataset_id, limit=50, offset=0)

    assert result.total == 1
    assert [item.source_id for item in result.items] == [visible_source.id]
    dumped_source = result.model_dump()["items"][0]
    assert "storage_uri" not in dumped_source
    assert "normalized_text" not in dumped_source
    assert "text" not in dumped_source


@pytest.mark.asyncio
async def test_stats_are_stable_postgresql_counters() -> None:
    dataset_id = uuid4()
    store = FakeStore()
    store.datasets.append(dataset("Docs", "docs", dataset_id=dataset_id))
    store.stats = DatasetStatsSnapshot(
        sources_total=3,
        sources_active=2,
        documents_total=5,
        documents_active=4,
        chunks_total=8,
        chunks_active=7,
        entities_active_current_generation=6,
        relations_active_current_generation=5,
        summaries_total=4,
    )

    result = await service_for(store).get_stats(dataset_id)

    assert result == DatasetStatsResult(
        dataset_id=dataset_id,
        active_generation=0,
        sources={"total": 3, "active": 2},
        documents={"total": 5, "active": 4},
        chunks={"total": 8, "active": 7},
        entities={"active_current_generation": 6},
        relations={"active_current_generation": 5},
        summaries={"total": 4},
    )


@pytest.mark.asyncio
async def test_dataset_route_returns_envelope_requires_api_key_and_has_no_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id = uuid4()

    class FakeDatasetService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def create_dataset(self, request: DatasetCreateRequest) -> DatasetResult:
            return dataset_result(dataset_id, request.name, request.slug or "docs")

        async def list_datasets(self, *, limit: int, offset: int) -> DatasetListResult:
            assert (limit, offset) == (2, 1)
            return DatasetListResult(
                items=[dataset_result(dataset_id, "Docs", "docs")],
                limit=limit,
                offset=offset,
                total=3,
            )

        async def get_dataset(self, dataset_id: UUID) -> DatasetResult:
            return dataset_result(dataset_id, "Docs", "docs")

        async def rename_dataset(
            self,
            dataset_id: UUID,
            request: DatasetRenameRequest,
        ) -> DatasetResult:
            return dataset_result(dataset_id, request.name, "docs")

        async def list_sources(
            self,
            *,
            dataset_id: UUID,
            limit: int,
            offset: int,
        ) -> DatasetSourcesResult:
            del dataset_id
            assert (limit, offset) == (50, 0)
            return DatasetSourcesResult(items=[], limit=limit, offset=offset, total=0)

        async def get_stats(self, dataset_id: UUID) -> DatasetStatsResult:
            return DatasetStatsResult(
                dataset_id=dataset_id,
                active_generation=0,
                sources={"total": 0, "active": 0},
                documents={"total": 0, "active": 0},
                chunks={"total": 0, "active": 0},
                entities={"active_current_generation": 0},
                relations={"active_current_generation": 0},
                summaries={"total": 0},
            )

    monkeypatch.setattr("sofias_memory.api.routes.datasets.DatasetService", FakeDatasetService)
    app = create_app(make_settings(tmp_path), enable_postgres_readiness=False, enable_neo4j=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_key = await client.get("/api/v1/datasets")
        created = await client.post(
            "/api/v1/datasets",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"name": "Docs", "slug": "docs"},
        )
        listed = await client.get(
            "/api/v1/datasets?limit=2&offset=1",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        fetched = await client.get(
            f"/api/v1/datasets/{dataset_id}",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        invalid_uuid = await client.get(
            "/api/v1/datasets/not-a-uuid",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        renamed = await client.patch(
            f"/api/v1/datasets/{dataset_id}",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
            json={"name": "Renamed"},
        )
        sources = await client.get(
            f"/api/v1/datasets/{dataset_id}/sources",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        stats = await client.get(
            f"/api/v1/datasets/{dataset_id}/stats",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )
        delete_response = await client.delete(
            f"/api/v1/datasets/{dataset_id}",
            headers={API_KEY_HEADER: EXPECTED_API_KEY},
        )

    assert missing_key.status_code == 401
    assert created.status_code == 201
    assert created.json()["data"]["dataset_id"] == str(dataset_id)
    assert listed.json()["data"]["limit"] == 2
    assert listed.json()["data"]["offset"] == 1
    assert listed.json()["data"]["total"] == 3
    assert fetched.status_code == 200
    assert invalid_uuid.status_code == 422
    assert renamed.json()["data"]["name"] == "Renamed"
    assert sources.json()["data"] == {"items": [], "limit": 50, "offset": 0, "total": 0}
    assert stats.json()["data"]["sources"] == {"total": 0, "active": 0}
    assert delete_response.status_code == 405


def dataset_result(dataset_id: UUID, name: str, slug: str) -> DatasetResult:
    return DatasetResult(
        dataset_id=dataset_id,
        name=name,
        slug=slug,
        description=None,
        status=DatasetStatus.ACTIVE,
        active_generation=0,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


class RecordingScalarSession:
    def __init__(self) -> None:
        self.statements: list[Select[tuple[int]]] = []

    async def scalar(self, statement: Select[tuple[int]]) -> int:
        self.statements.append(statement)
        return 0


def render_statement(statement: Select[tuple[int]]) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )
